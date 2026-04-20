"""Tests for StreamMonitor in monitor.py.

HTTP calls are mocked with respx at the httpx level — no real network traffic.
"""

import asyncio
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx
import pytest
import respx

from twitch_healthcheck.models import SegmentMeasurement, Variant
from twitch_healthcheck.monitor import StreamMonitor
from twitch_healthcheck.twitch_api import GQL_ENDPOINT, StreamOfflineError

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

CHANNEL = "kaicenat"
TOKEN = '{"channel_id":"123456789","expires":9999999999}'
SIGNATURE = "abcdef1234567890"

LIVE_GQL_RESPONSE = {
    "data": {
        "streamPlaybackAccessToken": {"value": TOKEN, "signature": SIGNATURE}
    }
}
OFFLINE_GQL_RESPONSE = {"data": {"streamPlaybackAccessToken": None}}

# The exact Usher URL our code will construct for the test token/sig.
_USHER_PARAMS = urlencode(
    {"sig": SIGNATURE, "token": TOKEN, "allow_source": "true", "fast_bread": "true"}
)
USHER_URL = f"https://usher.ttvnw.net/api/channel/hls/{CHANNEL}.m3u8?{_USHER_PARAMS}"

VARIANT_URI = "https://video-edge.fra05.twitch.tv/v1/segment/1080p60/index.m3u8"
MASTER_CONTENT = (
    "#EXTM3U\n"
    f"#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,FRAME-RATE=60.0\n"
    f"{VARIANT_URI}\n"
)

SEG_100 = "https://video-edge.fra05.twitch.tv/v1/segment/1080p60/100.ts"
SEG_101 = "https://video-edge.fra05.twitch.tv/v1/segment/1080p60/101.ts"
SEG_102 = "https://video-edge.fra05.twitch.tv/v1/segment/1080p60/102.ts"

MEDIA_CONTENT_1 = (
    "#EXTM3U\n"
    "#EXT-X-TARGETDURATION:2\n"
    "#EXT-X-MEDIA-SEQUENCE:100\n"
    f"#EXTINF:2.000,\n{SEG_100}\n"
    f"#EXTINF:2.000,\n{SEG_101}\n"
)

MEDIA_CONTENT_2 = (
    "#EXTM3U\n"
    "#EXT-X-TARGETDURATION:2\n"
    "#EXT-X-MEDIA-SEQUENCE:101\n"
    f"#EXTINF:2.000,\n{SEG_101}\n"
    f"#EXTINF:2.000,\n{SEG_102}\n"
)

SEGMENT_BYTES = b"x" * 204_800   # 200 KB — realistic Twitch segment size


def _make_monitor(**kwargs: object) -> StreamMonitor:
    """Create a StreamMonitor with test-friendly defaults."""
    defaults = dict(channel=CHANNEL, quality="best", poll_interval=0)
    defaults.update(kwargs)
    return StreamMonitor(**defaults)  # type: ignore[arg-type]


def _make_variant() -> Variant:
    return Variant(quality="1080p60", bandwidth=6_000_000, uri=VARIANT_URI)


# ---------------------------------------------------------------------------
# Variant selection
# ---------------------------------------------------------------------------


class TestPickVariant:
    def _master(self, *variants: Variant) -> object:
        from twitch_healthcheck.models import MasterPlaylist
        return MasterPlaylist(variants=list(variants))

    def _v(self, quality: str, bandwidth: int) -> Variant:
        return Variant(quality=quality, bandwidth=bandwidth, uri=f"https://cdn/{quality}/index.m3u8")

    def test_best_picks_highest_bandwidth(self) -> None:
        m = _make_monitor()
        master = self._master(
            self._v("480p", 1_500_000), self._v("1080p60", 8_000_000), self._v("720p60", 4_000_000)
        )
        assert m._pick_variant(master).quality == "1080p60"

    def test_worst_picks_lowest_bandwidth(self) -> None:
        m = _make_monitor(quality="worst")
        master = self._master(self._v("480p", 1_500_000), self._v("1080p60", 8_000_000))
        assert m._pick_variant(master).quality == "480p"

    def test_exact_quality_match(self) -> None:
        m = _make_monitor(quality="720p60")
        master = self._master(
            self._v("1080p60", 8_000_000), self._v("720p60", 4_000_000), self._v("480p", 1_500_000)
        )
        assert m._pick_variant(master).quality == "720p60"

    def test_falls_back_to_best_when_no_match(self) -> None:
        m = _make_monitor(quality="360p")
        master = self._master(self._v("1080p60", 8_000_000), self._v("480p", 1_500_000))
        # "360p" not present → fall back to highest bandwidth
        assert m._pick_variant(master).quality == "1080p60"


# ---------------------------------------------------------------------------
# _poll_once
# ---------------------------------------------------------------------------


class TestPollOnce:
    @respx.mock
    async def test_new_segments_are_downloaded(self) -> None:
        monitor = _make_monitor()
        monitor._variant = _make_variant()

        respx.get(VARIANT_URI).mock(return_value=httpx.Response(200, text=MEDIA_CONTENT_1))
        respx.get(SEG_100).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))
        respx.get(SEG_101).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))

        async with httpx.AsyncClient() as client:
            await monitor._poll_once(client)

        assert monitor._segments_total == 2
        assert monitor._segments_failed == 0
        assert len(monitor._measurements) == 2
        assert all(m.success for m in monitor._measurements)

    @respx.mock
    async def test_seen_segments_are_skipped_on_second_poll(self) -> None:
        """When the playlist slides forward, only the new segment is downloaded."""
        monitor = _make_monitor()
        monitor._variant = _make_variant()

        # First poll: segments 100 + 101
        respx.get(VARIANT_URI).mock(
            side_effect=[
                httpx.Response(200, text=MEDIA_CONTENT_1),
                httpx.Response(200, text=MEDIA_CONTENT_2),
            ]
        )
        respx.get(SEG_100).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))
        respx.get(SEG_101).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))
        respx.get(SEG_102).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))

        async with httpx.AsyncClient() as client:
            await monitor._poll_once(client)   # downloads 100, 101
            await monitor._poll_once(client)   # playlist now has 101, 102 → only 102 is new

        # 100, 101 from first poll + 102 from second poll = 3 total
        assert monitor._segments_total == 3

    @respx.mock
    async def test_segment_http_error_produces_failed_measurement(self) -> None:
        monitor = _make_monitor()
        monitor._variant = _make_variant()

        respx.get(VARIANT_URI).mock(return_value=httpx.Response(200, text=MEDIA_CONTENT_1))
        respx.get(SEG_100).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))
        respx.get(SEG_101).mock(return_value=httpx.Response(503))  # server error

        async with httpx.AsyncClient() as client:
            await monitor._poll_once(client)

        assert monitor._segments_total == 2
        assert monitor._segments_failed == 1

        failed = [m for m in monitor._measurements if not m.success]
        assert len(failed) == 1
        assert failed[0].http_status == 503
        assert "503" in (failed[0].error or "")

    @respx.mock
    async def test_segment_http_error_creates_incident(self) -> None:
        monitor = _make_monitor()
        monitor._variant = _make_variant()

        respx.get(VARIANT_URI).mock(return_value=httpx.Response(200, text=MEDIA_CONTENT_1))
        respx.get(SEG_100).mock(return_value=httpx.Response(404))
        respx.get(SEG_101).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))

        async with httpx.AsyncClient() as client:
            await monitor._poll_once(client)

        assert len(monitor._incidents) == 1
        assert monitor._incidents[0].type == "http_error"
        assert monitor._incidents[0].severity == "warning"

    @respx.mock
    async def test_segment_network_error_produces_failed_measurement(self) -> None:
        monitor = _make_monitor()
        monitor._variant = _make_variant()

        respx.get(VARIANT_URI).mock(return_value=httpx.Response(200, text=MEDIA_CONTENT_1))
        respx.get(SEG_100).mock(side_effect=httpx.ConnectError("refused"))
        respx.get(SEG_101).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))

        async with httpx.AsyncClient() as client:
            await monitor._poll_once(client)

        assert monitor._segments_failed == 1
        failed = next(m for m in monitor._measurements if not m.success)
        assert failed.http_status is None
        assert "refused" in (failed.error or "")

    @respx.mock
    async def test_successful_measurement_has_bitrate(self) -> None:
        monitor = _make_monitor()
        monitor._variant = _make_variant()

        respx.get(VARIANT_URI).mock(return_value=httpx.Response(200, text=MEDIA_CONTENT_1))
        respx.get(SEG_100).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))
        respx.get(SEG_101).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))

        async with httpx.AsyncClient() as client:
            await monitor._poll_once(client)

        for m in monitor._measurements:
            assert m.effective_bitrate_bps is not None
            assert m.effective_bitrate_bps > 0
            assert m.bytes_downloaded == len(SEGMENT_BYTES)


# ---------------------------------------------------------------------------
# snapshot()
# ---------------------------------------------------------------------------


class TestSnapshot:
    def _inject_measurement(
        self,
        monitor: StreamMonitor,
        *,
        success: bool = True,
        download_time_ms: float = 100.0,
    ) -> None:
        """Directly inject a measurement into the monitor's state."""
        from twitch_healthcheck.hls import parse_media_playlist
        media = parse_media_playlist(
            "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:0\n"
            f"#EXTINF:2.000,\n{SEG_100}\n",
            VARIANT_URI,
        )
        seg = media.segments[0]
        m = SegmentMeasurement(
            segment=seg,
            success=success,
            http_status=200 if success else 503,
            download_time_ms=download_time_ms,
            bytes_downloaded=204_800 if success else 0,
            effective_bitrate_bps=int(204_800 * 8 / (download_time_ms / 1000)) if success else None,
            error=None if success else "HTTP 503",
            timestamp_utc=datetime.now(tz=UTC),
        )
        monitor._record(m)

    def test_empty_monitor_returns_valid_snapshot(self) -> None:
        monitor = _make_monitor()
        monitor._start_time = datetime.now(tz=UTC)
        snap = monitor.snapshot()

        assert snap.channel == CHANNEL
        assert snap.status == "healthy"
        assert snap.segments_total == 0
        assert snap.segments_failed == 0
        assert snap.median_latency_ms == 0.0
        assert snap.effective_bitrate_bps is None
        assert snap.recent_incidents == []
        assert snap.timestamp_utc.tzinfo is not None

    def test_snapshot_counts_are_correct(self) -> None:
        monitor = _make_monitor()
        monitor._start_time = datetime.now(tz=UTC)

        self._inject_measurement(monitor, success=True, download_time_ms=80.0)
        self._inject_measurement(monitor, success=True, download_time_ms=120.0)
        self._inject_measurement(monitor, success=False)

        snap = monitor.snapshot()
        assert snap.segments_total == 3
        assert snap.segments_failed == 1

    def test_median_latency_computed_from_successful_measurements(self) -> None:
        monitor = _make_monitor()
        monitor._start_time = datetime.now(tz=UTC)

        self._inject_measurement(monitor, success=True, download_time_ms=80.0)
        self._inject_measurement(monitor, success=True, download_time_ms=120.0)
        self._inject_measurement(monitor, success=False, download_time_ms=5.0)  # excluded

        snap = monitor.snapshot()
        assert snap.median_latency_ms == pytest.approx(100.0)

    def test_status_down_after_consecutive_failures(self) -> None:
        monitor = _make_monitor()
        monitor._start_time = datetime.now(tz=UTC)

        for _ in range(3):
            self._inject_measurement(monitor, success=False)

        snap = monitor.snapshot()
        assert snap.status == "down"

    def test_status_degraded_above_failure_rate(self) -> None:
        monitor = _make_monitor()
        monitor._start_time = datetime.now(tz=UTC)

        # 1 success then 4 failures → 80 % failure rate > 20 % threshold
        self._inject_measurement(monitor, success=True)
        for _ in range(4):
            self._inject_measurement(monitor, success=False)

        snap = monitor.snapshot()
        assert snap.status in ("degraded", "down")

    def test_status_healthy_with_all_successes(self) -> None:
        monitor = _make_monitor()
        monitor._start_time = datetime.now(tz=UTC)

        for _ in range(5):
            self._inject_measurement(monitor, success=True)

        assert monitor.snapshot().status == "healthy"

    def test_recent_incidents_capped_at_20_in_snapshot(self) -> None:
        monitor = _make_monitor()
        monitor._start_time = datetime.now(tz=UTC)

        for _ in range(25):
            self._inject_measurement(monitor, success=False)

        snap = monitor.snapshot()
        assert len(snap.recent_incidents) <= 20


# ---------------------------------------------------------------------------
# start() / stop() integration
# ---------------------------------------------------------------------------


class TestStartStop:
    async def test_monitor_runs_and_stops_cleanly(self) -> None:
        with respx.mock(assert_all_called=False) as mock:
            mock.post(GQL_ENDPOINT).mock(
                return_value=httpx.Response(200, json=LIVE_GQL_RESPONSE)
            )
            mock.get(USHER_URL).mock(return_value=httpx.Response(200, text=MASTER_CONTENT))
            mock.get(VARIANT_URI).mock(return_value=httpx.Response(200, text=MEDIA_CONTENT_1))
            mock.get(SEG_100).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))
            mock.get(SEG_101).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))

            monitor = _make_monitor(poll_interval=0)
            task = asyncio.create_task(monitor.start())

            # Yield to event loop enough times for at least one full poll iteration.
            for _ in range(20):
                await asyncio.sleep(0)

            await monitor.stop()
            await task

        snap = monitor.snapshot()
        assert snap.channel == CHANNEL
        assert snap.segments_total >= 2
        assert snap.segments_failed == 0

    @respx.mock
    async def test_start_raises_stream_offline_error_when_channel_offline(self) -> None:
        respx.post(GQL_ENDPOINT).mock(
            return_value=httpx.Response(200, json=OFFLINE_GQL_RESPONSE)
        )

        monitor = _make_monitor()
        with pytest.raises(StreamOfflineError):
            await monitor.start()

    async def test_stop_before_start_is_harmless(self) -> None:
        monitor = _make_monitor()
        await monitor.stop()  # must not raise

    async def test_snapshot_before_start_returns_zero_uptime(self) -> None:
        monitor = _make_monitor()
        snap = monitor.snapshot()
        assert snap.uptime_seconds == 0.0
        assert snap.segments_total == 0
