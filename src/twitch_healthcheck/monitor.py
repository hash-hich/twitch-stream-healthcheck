"""Async monitoring loop: fetches and times HLS segments continuously.

Flow:
    1. Resolve the master playlist URL via Twitch API.
    2. Select the best matching stream variant.
    3. Poll the media playlist every target_duration seconds.
    4. For each new segment, download it to memory, time it, and record a
       SegmentMeasurement. The content is immediately discarded.
    5. Expose the current state as a MonitorSnapshot at any point.
"""

import asyncio
import contextlib
import statistics
import time
from collections import deque
from datetime import UTC, datetime

import httpx

from twitch_healthcheck.hls import parse_master_playlist, parse_media_playlist
from twitch_healthcheck.models import (
    Incident,
    MonitorSnapshot,
    Segment,
    SegmentMeasurement,
    Variant,
)
from twitch_healthcheck.twitch_api import TwitchAPIError, get_master_playlist_url

# ---------------------------------------------------------------------------
# Thresholds (all configurable at the class level if needed)
# ---------------------------------------------------------------------------

_MAX_INCIDENTS = 200
_DEGRADED_FAILURE_RATE = 0.20  # >20 % failures → degraded
_DOWN_CONSECUTIVE = 3          # 3 consecutive failures → down


class StreamMonitor:
    """Monitors a live Twitch stream by continuously polling its HLS media playlist.

    Usage::

        monitor = StreamMonitor("kaicenat")
        task = asyncio.create_task(monitor.start())
        ...
        await monitor.stop()
        snapshot = monitor.snapshot()
    """

    def __init__(
        self,
        channel: str,
        quality: str = "best",
        buffer_size: int = 60,
        poll_interval: float | None = None,
    ) -> None:
        """
        Args:
            channel: Twitch channel name (case-insensitive).
            quality: Desired quality label — "best", "worst", or e.g. "720p60".
                     Falls back to "best" when an exact match is not found.
            buffer_size: Number of recent SegmentMeasurements to keep in the
                         rolling buffer used for latency / bitrate statistics.
            poll_interval: Seconds to wait between media playlist fetches.
                           None (default) uses the playlist's #EXT-X-TARGETDURATION.
        """
        self._channel = channel.lower()
        self._quality = quality
        self._poll_interval = poll_interval

        # Rolling buffer — older measurements are evicted automatically.
        self._measurements: deque[SegmentMeasurement] = deque(maxlen=buffer_size)
        self._incidents: list[Incident] = []

        # Lifetime counters (survive buffer eviction).
        self._segments_total: int = 0
        self._segments_failed: int = 0

        # Sequence numbers we have already processed (prevents re-downloading
        # segments that remain in the playlist window across polls).
        self._seen_sequences: set[int] = set()

        self._target_duration: float = 2.0
        self._start_time: datetime | None = None
        self._variant: Variant | None = None
        self._stop_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Variant selection
    # ------------------------------------------------------------------

    def _pick_variant(self, master: object) -> Variant:
        """Select a stream variant from the master playlist.

        Args:
            master: Parsed MasterPlaylist model.

        Returns:
            The chosen Variant. Falls back to highest bandwidth when the
            requested quality label is not found.
        """
        from twitch_healthcheck.models import MasterPlaylist
        assert isinstance(master, MasterPlaylist)

        by_bandwidth = sorted(master.variants, key=lambda v: v.bandwidth, reverse=True)

        if self._quality == "best":
            return by_bandwidth[0]
        if self._quality == "worst":
            return by_bandwidth[-1]

        for v in master.variants:
            if v.quality == self._quality:
                return v

        # No exact match — fall back to highest bandwidth and log the decision.
        chosen = by_bandwidth[0]
        return chosen

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _fetch_text(self, client: httpx.AsyncClient, url: str) -> str:
        """GET a URL and return the body as text.

        Args:
            client: Active httpx.AsyncClient.
            url: Full HTTPS URL.

        Returns:
            Response body as a string.

        Raises:
            TwitchAPIError: On network errors or non-200 HTTP responses.
        """
        try:
            response = await client.get(url, timeout=10.0)
        except httpx.RequestError as exc:
            raise TwitchAPIError(f"Network error fetching {url}: {exc}") from exc

        if response.status_code != 200:
            raise TwitchAPIError(f"HTTP {response.status_code} fetching {url}")

        return response.text

    async def _download_segment(
        self,
        client: httpx.AsyncClient,
        segment: Segment,
    ) -> SegmentMeasurement:
        """Download a segment to memory, time it, and return a measurement.

        The response body is read in full (to count bytes and measure latency)
        then discarded — nothing is written to disk.

        Args:
            client: Active httpx.AsyncClient.
            segment: The segment to download.

        Returns:
            SegmentMeasurement with timing, size, and bitrate data.
        """
        timestamp = datetime.now(tz=UTC)
        t0 = time.monotonic()

        try:
            response = await client.get(segment.uri, timeout=10.0)
            content = await response.aread()
            elapsed_ms = (time.monotonic() - t0) * 1000.0

            if response.status_code != 200:
                return SegmentMeasurement(
                    segment=segment,
                    success=False,
                    http_status=response.status_code,
                    download_time_ms=elapsed_ms,
                    bytes_downloaded=len(content),
                    error=f"HTTP {response.status_code}",
                    timestamp_utc=timestamp,
                )

            bytes_dl = len(content)
            elapsed_s = elapsed_ms / 1000.0
            bitrate = int(bytes_dl * 8 / elapsed_s) if elapsed_s > 0 else None

            return SegmentMeasurement(
                segment=segment,
                success=True,
                http_status=response.status_code,
                download_time_ms=elapsed_ms,
                bytes_downloaded=bytes_dl,
                effective_bitrate_bps=bitrate,
                timestamp_utc=timestamp,
            )

        except httpx.RequestError as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            return SegmentMeasurement(
                segment=segment,
                success=False,
                http_status=None,
                download_time_ms=elapsed_ms,
                bytes_downloaded=0,
                error=str(exc),
                timestamp_utc=timestamp,
            )

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _record(self, measurement: SegmentMeasurement) -> None:
        """Append a measurement to the rolling buffer and update counters.

        Failed measurements also produce an Incident (up to _MAX_INCIDENTS).
        """
        self._measurements.append(measurement)
        self._segments_total += 1

        if not measurement.success:
            self._segments_failed += 1
            if len(self._incidents) < _MAX_INCIDENTS:
                self._incidents.append(
                    Incident(
                        timestamp_utc=measurement.timestamp_utc,
                        type="http_error",
                        severity="warning",
                        message=(
                            f"Segment {measurement.segment.sequence} failed: "
                            f"{measurement.error}"
                        ),
                        details={
                            "uri": measurement.segment.uri,
                            "http_status": measurement.http_status,
                            "error": measurement.error,
                        },
                    )
                )

    # ------------------------------------------------------------------
    # Status computation
    # ------------------------------------------------------------------

    def _compute_status(self) -> str:
        """Derive a HealthStatus string from recent measurements."""
        recent = list(self._measurements)
        if not recent:
            return "healthy"

        # Three consecutive failures at the tail → stream is down.
        tail = recent[-_DOWN_CONSECUTIVE:]
        if len(tail) == _DOWN_CONSECUTIVE and all(not m.success for m in tail):
            return "down"

        failure_rate = sum(1 for m in recent if not m.success) / len(recent)
        if failure_rate > _DEGRADED_FAILURE_RATE:
            return "degraded"

        return "healthy"

    # ------------------------------------------------------------------
    # Core polling
    # ------------------------------------------------------------------

    async def _poll_once(self, client: httpx.AsyncClient) -> None:
        """Fetch the media playlist once and download any new segments.

        Segments whose sequence number was already seen are skipped — this
        handles the sliding playlist window without re-downloading.

        Args:
            client: Active httpx.AsyncClient.
        """
        assert self._variant is not None

        content = await self._fetch_text(client, self._variant.uri)
        media = parse_media_playlist(content, self._variant.uri)
        self._target_duration = media.target_duration

        new_segments = [
            s for s in media.segments if s.sequence not in self._seen_sequences
        ]
        for segment in new_segments:
            self._seen_sequences.add(segment.sequence)
            measurement = await self._download_segment(client, segment)
            self._record(measurement)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Run the monitoring loop until stop() is called or the task is cancelled.

        Raises:
            StreamOfflineError: If the channel is not live at startup.
            TwitchAPIError: On unrecoverable API errors during initialisation.
        """
        self._start_time = datetime.now(tz=UTC)
        self._stop_event.clear()

        async with httpx.AsyncClient() as client:
            try:
                master_url = await get_master_playlist_url(self._channel)
                master_content = await self._fetch_text(client, master_url)
                master = parse_master_playlist(master_content)
                self._variant = self._pick_variant(master)

                while not self._stop_event.is_set():
                    try:
                        await self._poll_once(client)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Individual poll errors must not kill the loop.
                        # The failed measurement was already recorded in _download_segment.
                        pass

                    interval = (
                        self._poll_interval
                        if self._poll_interval is not None
                        else self._target_duration
                    )
                    # Wait for the next poll interval, but wake up immediately
                    # if stop() sets the event.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=interval
                        )

            except asyncio.CancelledError:
                pass

    async def stop(self) -> None:
        """Signal the monitoring loop to exit after the current poll completes."""
        self._stop_event.set()

    def snapshot(self) -> MonitorSnapshot:
        """Return the current monitor state as an immutable MonitorSnapshot.

        Safe to call from any coroutine at any time — reads a consistent view
        of the internal deque without blocking.

        Returns:
            A MonitorSnapshot reflecting all measurements recorded so far.
        """
        measurements = list(self._measurements)
        uptime = (
            (datetime.now(tz=UTC) - self._start_time).total_seconds()
            if self._start_time
            else 0.0
        )

        latencies = [m.download_time_ms for m in measurements if m.success]
        median_latency = statistics.median(latencies) if latencies else 0.0

        bitrates = [
            m.effective_bitrate_bps
            for m in measurements
            if m.effective_bitrate_bps is not None
        ]
        effective_bitrate = int(statistics.median(bitrates)) if bitrates else None

        return MonitorSnapshot(
            channel=self._channel,
            status=self._compute_status(),
            uptime_seconds=uptime,
            segments_total=self._segments_total,
            segments_failed=self._segments_failed,
            median_latency_ms=median_latency,
            effective_bitrate_bps=effective_bitrate,
            recent_incidents=list(self._incidents[-20:]),
            timestamp_utc=datetime.now(tz=UTC),
        )
