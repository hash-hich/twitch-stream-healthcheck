"""Tests for the Typer CLI (cli.py).

HTTP calls are mocked with respx. CLI is invoked via typer.testing.CliRunner
which runs synchronously; asyncio.run() inside each command creates its own
event loop, which respx intercepts at the httpx transport level.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx
import respx
from typer.testing import CliRunner

from twitch_healthcheck.cli import app
from twitch_healthcheck.models import MonitorSnapshot
from twitch_healthcheck.twitch_api import GQL_ENDPOINT

runner = CliRunner()

# ---------------------------------------------------------------------------
# Shared HTTP mock constants
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

MEDIA_CONTENT = (
    "#EXTM3U\n"
    "#EXT-X-TARGETDURATION:2\n"
    "#EXT-X-MEDIA-SEQUENCE:100\n"
    f"#EXTINF:2.000,\n{SEG_100}\n"
    f"#EXTINF:2.000,\n{SEG_101}\n"
    f"#EXTINF:2.000,\n{SEG_102}\n"
)

SEGMENT_BYTES = b"x" * 204_800


# ---------------------------------------------------------------------------
# --version
# ---------------------------------------------------------------------------


class TestVersion:
    def test_version_flag_prints_version(self) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "twitch-healthcheck" in result.output

    def test_version_contains_semver(self) -> None:
        result = runner.invoke(app, ["--version"])
        # Version string looks like "0.1.0" — three dot-separated integers
        parts = result.output.strip().split()[-1].split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)


# ---------------------------------------------------------------------------
# check command
# ---------------------------------------------------------------------------


class TestCheckCommand:
    def _mock_all(self, mock: respx.MockRouter) -> None:
        mock.post(GQL_ENDPOINT).mock(
            return_value=httpx.Response(200, json=LIVE_GQL_RESPONSE)
        )
        mock.get(USHER_URL).mock(return_value=httpx.Response(200, text=MASTER_CONTENT))
        mock.get(VARIANT_URI).mock(return_value=httpx.Response(200, text=MEDIA_CONTENT))
        mock.get(SEG_100).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))
        mock.get(SEG_101).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))
        mock.get(SEG_102).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))

    def test_live_healthy_channel_exits_zero(self) -> None:
        with respx.mock(assert_all_called=False) as mock:
            self._mock_all(mock)
            result = runner.invoke(app, ["check", CHANNEL])
        assert result.exit_code == 0

    def test_output_contains_channel_name(self) -> None:
        with respx.mock(assert_all_called=False) as mock:
            self._mock_all(mock)
            result = runner.invoke(app, ["check", CHANNEL])
        assert CHANNEL in result.output

    def test_output_contains_healthy_status(self) -> None:
        with respx.mock(assert_all_called=False) as mock:
            self._mock_all(mock)
            result = runner.invoke(app, ["check", CHANNEL])
        assert "HEALTHY" in result.output

    def test_output_lists_variant_quality(self) -> None:
        with respx.mock(assert_all_called=False) as mock:
            self._mock_all(mock)
            result = runner.invoke(app, ["check", CHANNEL])
        assert "1080p60" in result.output

    def test_output_shows_segment_measurements(self) -> None:
        with respx.mock(assert_all_called=False) as mock:
            self._mock_all(mock)
            result = runner.invoke(app, ["check", CHANNEL])
        # At least one segment sequence number should appear
        assert "100" in result.output

    def test_offline_channel_exits_two(self) -> None:
        with respx.mock() as mock:
            mock.post(GQL_ENDPOINT).mock(
                return_value=httpx.Response(200, json=OFFLINE_GQL_RESPONSE)
            )
            result = runner.invoke(app, ["check", "nobody"])
        assert result.exit_code == 2

    def test_offline_channel_output_says_offline(self) -> None:
        with respx.mock() as mock:
            mock.post(GQL_ENDPOINT).mock(
                return_value=httpx.Response(200, json=OFFLINE_GQL_RESPONSE)
            )
            result = runner.invoke(app, ["check", "nobody"])
        assert "offline" in result.output.lower()

    def test_one_failed_segment_exits_one(self) -> None:
        with respx.mock(assert_all_called=False) as mock:
            mock.post(GQL_ENDPOINT).mock(
                return_value=httpx.Response(200, json=LIVE_GQL_RESPONSE)
            )
            mock.get(USHER_URL).mock(return_value=httpx.Response(200, text=MASTER_CONTENT))
            mock.get(VARIANT_URI).mock(return_value=httpx.Response(200, text=MEDIA_CONTENT))
            mock.get(SEG_100).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))
            mock.get(SEG_101).mock(return_value=httpx.Response(503))  # one failure
            mock.get(SEG_102).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))
            result = runner.invoke(app, ["check", CHANNEL])
        assert result.exit_code == 1   # degraded

    def test_all_segments_failed_exits_two(self) -> None:
        with respx.mock(assert_all_called=False) as mock:
            mock.post(GQL_ENDPOINT).mock(
                return_value=httpx.Response(200, json=LIVE_GQL_RESPONSE)
            )
            mock.get(USHER_URL).mock(return_value=httpx.Response(200, text=MASTER_CONTENT))
            mock.get(VARIANT_URI).mock(return_value=httpx.Response(200, text=MEDIA_CONTENT))
            mock.get(SEG_100).mock(return_value=httpx.Response(503))
            mock.get(SEG_101).mock(return_value=httpx.Response(503))
            mock.get(SEG_102).mock(return_value=httpx.Response(503))
            result = runner.invoke(app, ["check", CHANNEL])
        assert result.exit_code == 2   # down


# ---------------------------------------------------------------------------
# monitor command
# ---------------------------------------------------------------------------


class TestMonitorCommand:
    def _setup_mocks(self, mock: respx.MockRouter) -> None:
        mock.post(GQL_ENDPOINT).mock(
            return_value=httpx.Response(200, json=LIVE_GQL_RESPONSE)
        )
        mock.get(USHER_URL).mock(return_value=httpx.Response(200, text=MASTER_CONTENT))
        # Media playlist may be fetched multiple times — allow it.
        mock.get(VARIANT_URI).mock(return_value=httpx.Response(200, text=MEDIA_CONTENT))
        mock.get(SEG_100).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))
        mock.get(SEG_101).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))
        mock.get(SEG_102).mock(return_value=httpx.Response(200, content=SEGMENT_BYTES))

    def test_exits_zero_for_healthy_stream(self) -> None:
        with respx.mock(assert_all_called=False) as mock:
            self._setup_mocks(mock)
            result = runner.invoke(app, ["monitor", CHANNEL, "--duration", "0"])
        assert result.exit_code == 0

    def test_output_contains_channel_name(self) -> None:
        with respx.mock(assert_all_called=False) as mock:
            self._setup_mocks(mock)
            result = runner.invoke(app, ["monitor", CHANNEL, "--duration", "0"])
        assert CHANNEL in result.output

    def test_output_file_written(self, tmp_path: Path) -> None:
        report_file = tmp_path / "report.json"
        with respx.mock(assert_all_called=False) as mock:
            self._setup_mocks(mock)
            result = runner.invoke(
                app,
                ["monitor", CHANNEL, "--duration", "0", "--output", str(report_file)],
            )
        assert result.exit_code == 0
        assert report_file.exists()
        data = json.loads(report_file.read_text())
        assert data["channel"] == CHANNEL

    def test_output_file_is_valid_monitor_snapshot(self, tmp_path: Path) -> None:
        report_file = tmp_path / "report.json"
        with respx.mock(assert_all_called=False) as mock:
            self._setup_mocks(mock)
            runner.invoke(
                app,
                ["monitor", CHANNEL, "--duration", "0", "--output", str(report_file)],
            )
        snap = MonitorSnapshot.model_validate_json(report_file.read_text())
        assert snap.channel == CHANNEL

    def test_offline_channel_exits_two(self) -> None:
        with respx.mock() as mock:
            mock.post(GQL_ENDPOINT).mock(
                return_value=httpx.Response(200, json=OFFLINE_GQL_RESPONSE)
            )
            result = runner.invoke(app, ["monitor", "nobody", "--duration", "0"])
        assert result.exit_code == 2

    def test_quality_option_accepted(self) -> None:
        with respx.mock(assert_all_called=False) as mock:
            self._setup_mocks(mock)
            result = runner.invoke(
                app, ["monitor", CHANNEL, "--duration", "0", "--quality", "1080p60"]
            )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# report command
# ---------------------------------------------------------------------------


class TestReportCommand:
    def _write_snapshot(self, path: Path) -> MonitorSnapshot:
        snap = MonitorSnapshot(
            channel=CHANNEL,
            status="healthy",
            uptime_seconds=120.0,
            segments_total=60,
            segments_failed=1,
            median_latency_ms=88.5,
            effective_bitrate_bps=5_800_000,
            recent_incidents=[],
            timestamp_utc=datetime(2024, 4, 19, 10, 0, 0, tzinfo=UTC),
        )
        path.write_text(snap.model_dump_json(indent=2))
        return snap

    def test_valid_report_exits_zero(self, tmp_path: Path) -> None:
        f = tmp_path / "report.json"
        self._write_snapshot(f)
        result = runner.invoke(app, ["report", str(f)])
        assert result.exit_code == 0

    def test_output_contains_channel_name(self, tmp_path: Path) -> None:
        f = tmp_path / "report.json"
        self._write_snapshot(f)
        result = runner.invoke(app, ["report", str(f)])
        assert CHANNEL in result.output

    def test_output_contains_status(self, tmp_path: Path) -> None:
        f = tmp_path / "report.json"
        self._write_snapshot(f)
        result = runner.invoke(app, ["report", str(f)])
        assert "HEALTHY" in result.output

    def test_output_contains_metrics(self, tmp_path: Path) -> None:
        f = tmp_path / "report.json"
        self._write_snapshot(f)
        result = runner.invoke(app, ["report", str(f)])
        assert "88" in result.output   # median latency ~88 ms

    def test_missing_file_exits_one(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["report", str(tmp_path / "nonexistent.json")])
        assert result.exit_code == 1

    def test_missing_file_output_says_not_found(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["report", str(tmp_path / "nonexistent.json")])
        assert "not found" in result.output.lower()

    def test_invalid_json_exits_one(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.json"
        f.write_text("this is not json")
        result = runner.invoke(app, ["report", str(f)])
        assert result.exit_code == 1
