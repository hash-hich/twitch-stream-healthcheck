"""Typer CLI entrypoint: check, monitor, report commands.

Entry point registered in pyproject.toml:
    twitch-healthcheck = "twitch_healthcheck.cli:app"
"""

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from twitch_healthcheck import __version__
from twitch_healthcheck.hls import parse_master_playlist, parse_media_playlist
from twitch_healthcheck.models import MonitorSnapshot, SegmentMeasurement
from twitch_healthcheck.monitor import StreamMonitor
from twitch_healthcheck.twitch_api import (
    StreamOfflineError,
    TwitchAPIError,
    fetch_playlist,
    get_master_playlist_url,
)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = typer.Typer(
    help="Monitor the health of a live Twitch stream.",
    no_args_is_help=True,
)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"twitch-healthcheck {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
) -> None:
    pass


# ---------------------------------------------------------------------------
# Rich helpers
# ---------------------------------------------------------------------------


def _status_style(status: str) -> str:
    return {"healthy": "green", "degraded": "yellow", "down": "red"}.get(status, "white")


def _severity_style(severity: str) -> str:
    return {"info": "cyan", "warning": "yellow", "critical": "red"}.get(severity, "white")


def _fmt_bps(bps: int | None) -> str:
    if bps is None:
        return "-"
    return f"{bps / 1_000_000:.2f} Mbps"


def _build_snapshot_panel(snap: MonitorSnapshot, title: str | None = None) -> Panel:
    """Render a MonitorSnapshot as a rich Panel with metrics and incidents."""
    style = _status_style(snap.status)
    status_text = Text(f"● {snap.status.upper()}", style=style)

    metrics = Table(show_header=False, box=None, padding=(0, 1))
    metrics.add_column(style="bold", min_width=20)
    metrics.add_column()

    metrics.add_row("Status", status_text)
    metrics.add_row("Channel", snap.channel)
    metrics.add_row("Uptime", f"{snap.uptime_seconds:.0f}s")
    metrics.add_row("Segments", f"{snap.segments_total} total, {snap.segments_failed} failed")

    if snap.segments_total > 0:
        rate = (snap.segments_total - snap.segments_failed) / snap.segments_total * 100
        metrics.add_row("Success rate", f"{rate:.1f}%")

    metrics.add_row("Median latency", f"{snap.median_latency_ms:.1f} ms")
    metrics.add_row("Effective bitrate", _fmt_bps(snap.effective_bitrate_bps))
    metrics.add_row("Timestamp", snap.timestamp_utc.strftime("%Y-%m-%d %H:%M:%S UTC"))

    if snap.recent_incidents:
        metrics.add_row("", "")
        metrics.add_row("[bold]Incidents[/bold]", f"({len(snap.recent_incidents)} shown)")
        for inc in snap.recent_incidents[-5:]:
            sev = _severity_style(inc.severity)
            ts = inc.timestamp_utc.strftime("%H:%M:%S")
            metrics.add_row(
                f"  [{sev}]{inc.severity[:4].upper()}[/{sev}]",
                f"[dim]{ts}[/dim] {inc.message[:70]}",
            )

    panel_title = title or f"[bold]{snap.channel}[/bold]"
    return Panel(metrics, title=panel_title, border_style=style)


def _exit_code(status: str) -> int:
    return {"healthy": 0, "degraded": 1, "down": 2}.get(status, 2)


# ---------------------------------------------------------------------------
# check command internals
# ---------------------------------------------------------------------------


async def _run_check(channel: str) -> int:
    console.print(f"Checking [bold]{channel}[/bold] …")

    try:
        master_url = await get_master_playlist_url(channel)
    except StreamOfflineError:
        console.print(f"[red]● Channel '{channel}' is offline or does not exist.[/red]")
        return 2
    except TwitchAPIError as exc:
        console.print(f"[red]Twitch API error: {exc}[/red]")
        return 2

    content = await fetch_playlist(master_url)
    master = parse_master_playlist(content)

    # ------------------------------------------------------------------
    # Print variant table
    # ------------------------------------------------------------------
    v_table = Table(title="Available Variants", show_lines=False)
    v_table.add_column("Quality", style="bold")
    v_table.add_column("Bandwidth", justify="right")
    v_table.add_column("Resolution")
    v_table.add_column("FPS", justify="right")

    for v in sorted(master.variants, key=lambda x: x.bandwidth, reverse=True):
        res = f"{v.resolution[0]}×{v.resolution[1]}" if v.resolution else "-"
        fps = f"{v.framerate:.0f}" if v.framerate else "-"
        v_table.add_row(v.quality, _fmt_bps(v.bandwidth), res, fps)

    console.print(v_table)

    # ------------------------------------------------------------------
    # Pick best variant and fetch media playlist
    # ------------------------------------------------------------------
    best = sorted(master.variants, key=lambda x: x.bandwidth, reverse=True)[0]
    console.print(
        f"\nSelected variant: [bold]{best.quality}[/bold] ({_fmt_bps(best.bandwidth)})"
    )

    media_content = await fetch_playlist(best.uri)
    media = parse_media_playlist(media_content, best.uri)
    segments = media.segments[:3]

    # ------------------------------------------------------------------
    # Download 3 segments
    # ------------------------------------------------------------------
    m_table = Table(title=f"Segment Measurements ({len(segments)} samples)")
    m_table.add_column("Seq", justify="right")
    m_table.add_column("Status", justify="center")
    m_table.add_column("HTTP", justify="right")
    m_table.add_column("Download", justify="right")
    m_table.add_column("Bitrate", justify="right")

    measurements: list[SegmentMeasurement] = []

    async with httpx.AsyncClient() as client:
        for seg in segments:
            timestamp = datetime.now(tz=UTC)
            t0 = time.monotonic()
            try:
                resp = await client.get(seg.uri, timeout=10.0)
                body = await resp.aread()
                elapsed_ms = (time.monotonic() - t0) * 1000.0

                if resp.status_code == 200:
                    bytes_dl = len(body)
                    elapsed_s = elapsed_ms / 1000.0
                    bitrate = int(bytes_dl * 8 / elapsed_s) if elapsed_s > 0 else None
                    m = SegmentMeasurement(
                        segment=seg,
                        success=True,
                        http_status=200,
                        download_time_ms=elapsed_ms,
                        bytes_downloaded=bytes_dl,
                        effective_bitrate_bps=bitrate,
                        timestamp_utc=timestamp,
                    )
                    m_table.add_row(
                        str(seg.sequence),
                        Text("✓ OK", style="green"),
                        "200",
                        f"{elapsed_ms:.1f} ms",
                        _fmt_bps(bitrate),
                    )
                else:
                    m = SegmentMeasurement(
                        segment=seg,
                        success=False,
                        http_status=resp.status_code,
                        download_time_ms=elapsed_ms,
                        bytes_downloaded=len(body),
                        error=f"HTTP {resp.status_code}",
                        timestamp_utc=timestamp,
                    )
                    m_table.add_row(
                        str(seg.sequence),
                        Text(f"✗ {resp.status_code}", style="red"),
                        str(resp.status_code),
                        f"{elapsed_ms:.1f} ms",
                        "-",
                    )

            except httpx.RequestError as exc:
                elapsed_ms = (time.monotonic() - t0) * 1000.0
                m = SegmentMeasurement(
                    segment=seg,
                    success=False,
                    http_status=None,
                    download_time_ms=elapsed_ms,
                    bytes_downloaded=0,
                    error=str(exc),
                    timestamp_utc=timestamp,
                )
                m_table.add_row(
                    str(seg.sequence),
                    Text("✗ ERR", style="red"),
                    "-",
                    f"{elapsed_ms:.1f} ms",
                    "-",
                )

            measurements.append(m)

    console.print(m_table)

    # ------------------------------------------------------------------
    # Overall status
    # ------------------------------------------------------------------
    failed = sum(1 for m in measurements if not m.success)
    if failed == len(measurements):
        status = "down"
    elif failed > 0:
        status = "degraded"
    else:
        status = "healthy"

    style = _status_style(status)
    console.print(f"\nOverall: [{style}]● {status.upper()}[/{style}]")

    return _exit_code(status)


# ---------------------------------------------------------------------------
# monitor command internals
# ---------------------------------------------------------------------------


async def _run_monitor(
    channel: str,
    duration: int,
    quality: str,
    output: str,
) -> int:
    monitor = StreamMonitor(channel, quality=quality)
    task = asyncio.create_task(monitor.start())

    # Yield once so the task can initialize and clear its stop_event before
    # we potentially call stop() immediately (e.g. when duration=0).
    await asyncio.sleep(0)

    deadline = time.monotonic() + max(duration, 0)

    with Live(console=console, refresh_per_second=1) as live:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            await asyncio.sleep(min(1.0, max(0.0, remaining)))
            live.update(_build_snapshot_panel(monitor.snapshot()))

    await monitor.stop()
    await task

    snap = monitor.snapshot()
    console.print(
        _build_snapshot_panel(snap, title=f"[bold]Final snapshot — {snap.channel}[/bold]")
    )

    if output:
        Path(output).write_text(snap.model_dump_json(indent=2))
        console.print(f"Report written to [bold]{output}[/bold]")

    return _exit_code(snap.status)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def check(
    channel: str = typer.Argument(..., help="Twitch channel name."),
) -> None:
    """Quick one-shot health check: variants, 3 segment downloads, overall status.

    Exit codes: 0 = healthy, 1 = degraded, 2 = offline / critical.
    """
    try:
        code = asyncio.run(_run_check(channel))
    except Exception as exc:
        console.print(f"[red]Unexpected error: {exc}[/red]")
        raise typer.Exit(2) from None
    raise typer.Exit(code)


@app.command()
def monitor(
    channel: str = typer.Argument(..., help="Twitch channel name."),
    duration: int = typer.Option(60, "--duration", "-d", help="Monitoring duration in seconds."),
    quality: str = typer.Option("best", "--quality", "-q", help="Quality label or 'best'/'worst'."),
    output: str = typer.Option("", "--output", "-o", help="Path to write final JSON report."),
) -> None:
    """Continuously monitor a channel and display a live metrics panel.

    Exit codes: 0 = healthy, 1 = degraded, 2 = down.
    """
    try:
        code = asyncio.run(_run_monitor(channel, duration, quality, output))
    except StreamOfflineError:
        console.print(f"[red]● Channel '{channel}' is offline.[/red]")
        raise typer.Exit(2) from None
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(2) from None
    raise typer.Exit(code)


@app.command()
def report(
    path: str = typer.Argument(..., help="Path to a JSON report written by 'monitor --output'."),
) -> None:
    """Pretty-print a saved JSON monitoring report."""
    p = Path(path)
    if not p.exists():
        console.print(f"[red]File not found: {path}[/red]")
        raise typer.Exit(1)

    try:
        snap = MonitorSnapshot.model_validate_json(p.read_text())
    except Exception as exc:
        console.print(f"[red]Failed to parse report: {exc}[/red]")
        raise typer.Exit(1) from None

    console.print(
        _build_snapshot_panel(snap, title=f"[bold]Report — {snap.channel}[/bold]")
    )

    if snap.recent_incidents:
        inc_table = Table(title="All Incidents in Report", show_lines=True)
        inc_table.add_column("Time (UTC)")
        inc_table.add_column("Type")
        inc_table.add_column("Severity")
        inc_table.add_column("Message")

        for inc in snap.recent_incidents:
            sev_style = _severity_style(inc.severity)
            inc_table.add_row(
                inc.timestamp_utc.strftime("%H:%M:%S"),
                inc.type,
                Text(inc.severity, style=sev_style),
                inc.message[:80],
            )
        console.print(inc_table)
