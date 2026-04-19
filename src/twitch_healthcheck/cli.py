"""Typer CLI entrypoint: check, monitor, report commands."""

import typer

app = typer.Typer(help="Monitor the health of a live Twitch stream.")


@app.command()
def check(channel: str) -> None:
    """Run a one-shot health check on a Twitch channel."""
    raise NotImplementedError


@app.command()
def monitor(
    channel: str,
    duration: int = typer.Option(60, help="Monitoring duration in seconds."),
    output: str = typer.Option("", help="Path to write JSON report."),
) -> None:
    """Continuously monitor a Twitch channel and emit a structured report."""
    raise NotImplementedError


@app.command()
def report(path: str) -> None:
    """Render a previously saved JSON report in the terminal."""
    raise NotImplementedError
