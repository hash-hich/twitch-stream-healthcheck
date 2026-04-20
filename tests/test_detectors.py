"""Tests for anomaly detectors in detectors.py.

All tests use synthetic SegmentMeasurement lists — no real HTTP traffic.
"""

from datetime import datetime, timedelta, timezone

import pytest

from twitch_healthcheck.detectors import (
    DetectionConfig,
    detect_all,
    detect_bitrate_drops,
    detect_gaps,
    detect_http_errors,
    detect_stalls,
)
from twitch_healthcheck.models import (
    MediaPlaylist,
    Segment,
    SegmentMeasurement,
    Variant,
)

UTC = timezone.utc
T0 = datetime(2024, 4, 19, 10, 0, 0, tzinfo=UTC)   # fixed reference time


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _seg(sequence: int) -> Segment:
    return Segment(
        uri=f"https://cdn.example.com/{sequence}.ts",
        duration=2.0,
        sequence=sequence,
    )


def _ok(
    sequence: int,
    timestamp: datetime = T0,
    bitrate_bps: int = 6_000_000,
    download_ms: float = 80.0,
) -> SegmentMeasurement:
    """Build a successful SegmentMeasurement."""
    return SegmentMeasurement(
        segment=_seg(sequence),
        success=True,
        http_status=200,
        download_time_ms=download_ms,
        bytes_downloaded=int(bitrate_bps * (download_ms / 1000) / 8),
        effective_bitrate_bps=bitrate_bps,
        timestamp_utc=timestamp,
    )


def _fail(
    sequence: int,
    timestamp: datetime = T0,
    http_status: int | None = 503,
    error: str = "Service Unavailable",
) -> SegmentMeasurement:
    """Build a failed SegmentMeasurement."""
    return SegmentMeasurement(
        segment=_seg(sequence),
        success=False,
        http_status=http_status,
        download_time_ms=5.0,
        bytes_downloaded=0,
        error=error,
        timestamp_utc=timestamp,
    )


def _playlist(target_duration: float = 2.0, start_seq: int = 100) -> MediaPlaylist:
    return MediaPlaylist(
        target_duration=target_duration,
        media_sequence=start_seq,
        segments=[_seg(start_seq + i) for i in range(3)],
    )


def _variant(bandwidth: int = 6_000_000) -> Variant:
    return Variant(
        quality="1080p60",
        bandwidth=bandwidth,
        resolution=(1920, 1080),
        framerate=60.0,
        uri="https://cdn.example.com/1080p60/index.m3u8",
    )


def _at(offset_seconds: float) -> datetime:
    """Return T0 + offset_seconds as a UTC datetime."""
    return T0 + timedelta(seconds=offset_seconds)


# ---------------------------------------------------------------------------
# detect_http_errors
# ---------------------------------------------------------------------------


class TestDetectHttpErrors:
    def test_all_successful_no_incidents(self) -> None:
        ms = [_ok(100), _ok(101), _ok(102)]
        assert detect_http_errors(ms) == []

    def test_404_produces_warning(self) -> None:
        ms = [_fail(100, http_status=404, error="Not Found")]
        incidents = detect_http_errors(ms)
        assert len(incidents) == 1
        assert incidents[0].type == "http_error"
        assert incidents[0].severity == "warning"
        assert "404" in incidents[0].message

    def test_503_produces_critical(self) -> None:
        ms = [_fail(100, http_status=503, error="Service Unavailable")]
        incidents = detect_http_errors(ms)
        assert len(incidents) == 1
        assert incidents[0].severity == "critical"

    def test_500_produces_critical(self) -> None:
        incidents = detect_http_errors([_fail(100, http_status=500)])
        assert incidents[0].severity == "critical"

    def test_connection_error_none_status_is_critical(self) -> None:
        ms = [_fail(100, http_status=None, error="Connection refused")]
        incidents = detect_http_errors(ms)
        assert len(incidents) == 1
        assert incidents[0].severity == "critical"
        assert "connection error" in incidents[0].message.lower()

    def test_multiple_failures_one_incident_each(self) -> None:
        ms = [_fail(100, http_status=404), _ok(101), _fail(102, http_status=503)]
        incidents = detect_http_errors(ms)
        assert len(incidents) == 2

    def test_incident_details_populated(self) -> None:
        ms = [_fail(100, http_status=404, error="Not Found")]
        inc = detect_http_errors(ms)[0]
        assert inc.details["http_status"] == 404
        assert inc.details["error"] == "Not Found"
        assert "100.ts" in inc.details["uri"]

    def test_empty_measurements(self) -> None:
        assert detect_http_errors([]) == []


# ---------------------------------------------------------------------------
# detect_stalls
# ---------------------------------------------------------------------------


class TestDetectStalls:
    def test_normal_timing_no_incidents(self) -> None:
        # Segments 2 seconds apart with target_duration=2 → gap=2s, threshold=4s → ok
        ms = [
            _ok(100, timestamp=_at(0)),
            _ok(101, timestamp=_at(2)),
            _ok(102, timestamp=_at(4)),
        ]
        assert detect_stalls(ms, _playlist()) == []

    def test_gap_just_below_threshold_no_incident(self) -> None:
        # threshold = 2 * 2.0 = 4.0 s; gap = 3.9 s → no stall
        ms = [_ok(100, timestamp=_at(0)), _ok(101, timestamp=_at(3.9))]
        assert detect_stalls(ms, _playlist(target_duration=2.0)) == []

    def test_gap_exceeds_threshold_produces_stall(self) -> None:
        # threshold = 2 * 2.0 = 4.0 s; gap = 5 s → stall
        ms = [_ok(100, timestamp=_at(0)), _ok(101, timestamp=_at(5))]
        incidents = detect_stalls(ms, _playlist(target_duration=2.0))
        assert len(incidents) == 1
        assert incidents[0].type == "stall"
        assert incidents[0].severity == "critical"

    def test_stall_details_correct(self) -> None:
        ms = [_ok(100, timestamp=_at(0)), _ok(101, timestamp=_at(6))]
        inc = detect_stalls(ms, _playlist(target_duration=2.0))[0]
        assert inc.details["gap_seconds"] == pytest.approx(6.0, abs=0.01)
        assert inc.details["threshold_seconds"] == pytest.approx(4.0)
        assert inc.details["previous_sequence"] == 100
        assert inc.details["current_sequence"] == 101

    def test_failed_measurements_skipped(self) -> None:
        # Successful 100 at t=0, failed at t=3 (ignored), successful 102 at t=3.5
        # Gap between successful: 3.5s < 4.0s threshold → no stall
        ms = [
            _ok(100, timestamp=_at(0)),
            _fail(101, timestamp=_at(3)),
            _ok(102, timestamp=_at(3.5)),
        ]
        assert detect_stalls(ms, _playlist()) == []

    def test_multiple_stalls_detected(self) -> None:
        ms = [
            _ok(100, timestamp=_at(0)),
            _ok(101, timestamp=_at(10)),   # stall: 10s > 4s
            _ok(102, timestamp=_at(25)),   # stall: 15s > 4s
        ]
        assert len(detect_stalls(ms, _playlist())) == 2

    def test_custom_stall_factor(self) -> None:
        # With factor=3, threshold = 3 * 2.0 = 6.0 s; gap = 5s → no stall
        cfg = DetectionConfig(stall_factor=3.0)
        ms = [_ok(100, timestamp=_at(0)), _ok(101, timestamp=_at(5))]
        assert detect_stalls(ms, _playlist(), config=cfg) == []

    def test_single_measurement_no_incidents(self) -> None:
        assert detect_stalls([_ok(100)], _playlist()) == []

    def test_empty_measurements(self) -> None:
        assert detect_stalls([], _playlist()) == []


# ---------------------------------------------------------------------------
# detect_bitrate_drops
# ---------------------------------------------------------------------------


class TestDetectBitrateDrops:
    def test_all_healthy_bitrate_no_incidents(self) -> None:
        ms = [_ok(100, bitrate_bps=6_000_000), _ok(101, bitrate_bps=5_500_000)]
        assert detect_bitrate_drops(ms, _variant()) == []

    def test_one_low_segment_below_threshold_no_incident(self) -> None:
        # Default consecutive = 3; only 1 low segment → no incident
        ms = [
            _ok(100, bitrate_bps=6_000_000),
            _ok(101, bitrate_bps=2_000_000),   # low
            _ok(102, bitrate_bps=6_000_000),
        ]
        assert detect_bitrate_drops(ms, _variant()) == []

    def test_two_consecutive_low_no_incident(self) -> None:
        ms = [_ok(i, bitrate_bps=2_000_000) for i in range(2)]
        assert detect_bitrate_drops(ms, _variant()) == []

    def test_three_consecutive_low_produces_incident(self) -> None:
        ms = [_ok(i, bitrate_bps=2_000_000) for i in range(3)]
        incidents = detect_bitrate_drops(ms, _variant(bandwidth=6_000_000))
        assert len(incidents) == 1
        assert incidents[0].type == "bitrate_drop"
        assert incidents[0].severity == "warning"

    def test_run_longer_than_n_emits_only_one_incident(self) -> None:
        # 6 consecutive low-bitrate segments → still only 1 incident
        ms = [_ok(i, bitrate_bps=2_000_000) for i in range(6)]
        assert len(detect_bitrate_drops(ms, _variant())) == 1

    def test_run_resets_after_good_segment(self) -> None:
        # 3 low → 1 good → 3 low → 2 incidents total
        ms = (
            [_ok(i, bitrate_bps=2_000_000) for i in range(3)]
            + [_ok(3, bitrate_bps=6_000_000)]
            + [_ok(i + 4, bitrate_bps=2_000_000) for i in range(3)]
        )
        assert len(detect_bitrate_drops(ms, _variant())) == 2

    def test_incident_details_populated(self) -> None:
        ms = [_ok(i, bitrate_bps=2_000_000) for i in range(3)]
        inc = detect_bitrate_drops(ms, _variant(bandwidth=6_000_000))[0]
        assert inc.details["effective_bitrate_bps"] == 2_000_000
        assert inc.details["expected_bitrate_bps"] == 6_000_000
        assert inc.details["ratio"] == pytest.approx(2_000_000 / 6_000_000, rel=1e-3)
        assert inc.details["consecutive_count"] == 3

    def test_failed_measurements_not_counted(self) -> None:
        # 2 low + 1 fail + 1 low = only 3 low if fail is transparent, but fail is skipped
        # so: 2 low, skip fail, 1 more low → run of 3 effective → incident
        ms = [
            _ok(0, bitrate_bps=2_000_000),
            _ok(1, bitrate_bps=2_000_000),
            _fail(2),
            _ok(3, bitrate_bps=2_000_000),
        ]
        incidents = detect_bitrate_drops(ms, _variant())
        assert len(incidents) == 1

    def test_custom_threshold_and_consecutive(self) -> None:
        cfg = DetectionConfig(bitrate_drop_threshold=0.8, bitrate_drop_consecutive=2)
        # 5_000_000 bps is 83% of 6_000_000 → above 80% threshold → no incident
        ms = [_ok(i, bitrate_bps=5_000_000) for i in range(5)]
        assert detect_bitrate_drops(ms, _variant(), config=cfg) == []

        # 4_000_000 bps is 67% of 6_000_000 → below 80% threshold, 2 consecutive → incident
        ms2 = [_ok(i, bitrate_bps=4_000_000) for i in range(2)]
        assert len(detect_bitrate_drops(ms2, _variant(), config=cfg)) == 1

    def test_empty_measurements(self) -> None:
        assert detect_bitrate_drops([], _variant()) == []


# ---------------------------------------------------------------------------
# detect_gaps
# ---------------------------------------------------------------------------


class TestDetectGaps:
    def test_sequential_no_gaps(self) -> None:
        ms = [_ok(100), _ok(101), _ok(102)]
        assert detect_gaps(ms) == []

    def test_jump_of_two_produces_gap(self) -> None:
        ms = [_ok(100), _ok(102)]   # sequence 101 is missing
        incidents = detect_gaps(ms)
        assert len(incidents) == 1
        assert incidents[0].type == "gap"
        assert incidents[0].severity == "warning"

    def test_gap_details_correct(self) -> None:
        ms = [_ok(100), _ok(103)]   # 2 segments missing
        inc = detect_gaps(ms)[0]
        assert inc.details["from_sequence"] == 100
        assert inc.details["to_sequence"] == 103
        assert inc.details["missing_count"] == 2
        assert inc.details["jump"] == 3

    def test_multiple_gaps(self) -> None:
        # gap between 100→103 and 104→107
        ms = [_ok(100), _ok(103), _ok(104), _ok(107)]
        incidents = detect_gaps(ms)
        assert len(incidents) == 2

    def test_failed_measurements_included_in_sequence_check(self) -> None:
        # A failed segment still has a sequence number; a gap between a fail and success is real
        ms = [_ok(100), _fail(103)]   # 101, 102 missing
        incidents = detect_gaps(ms)
        assert len(incidents) == 1
        assert incidents[0].details["missing_count"] == 2

    def test_custom_max_jump(self) -> None:
        # Allow jumps up to 3 (e.g. playlist configured to skip thumbnails)
        cfg = DetectionConfig(gap_max_sequence_jump=3)
        ms = [_ok(100), _ok(103)]
        assert detect_gaps(ms, config=cfg) == []

        ms2 = [_ok(100), _ok(105)]   # jump of 5 > 3 → gap
        assert len(detect_gaps(ms2, config=cfg)) == 1

    def test_single_measurement_no_incidents(self) -> None:
        assert detect_gaps([_ok(100)]) == []

    def test_empty_measurements(self) -> None:
        assert detect_gaps([]) == []


# ---------------------------------------------------------------------------
# detect_all
# ---------------------------------------------------------------------------


class TestDetectAll:
    def test_returns_empty_for_healthy_stream(self) -> None:
        ms = [
            _ok(100, timestamp=_at(0)),
            _ok(101, timestamp=_at(2)),
            _ok(102, timestamp=_at(4)),
        ]
        assert detect_all(ms, _variant(), _playlist()) == []

    def test_combines_all_detector_results(self) -> None:
        # http_error: seg 101 fails with 503
        # stall: 10s between successful segs 100 and 103 (101 is failed, 102 never arrived)
        # gap: sequence jumps 100 → 101 → 103, so 101→103 is a jump of 2
        ms = [
            _ok(100, timestamp=_at(0)),
            _fail(101, timestamp=_at(1), http_status=503),
            _ok(103, timestamp=_at(10), bitrate_bps=2_000_000),
        ]
        incidents = detect_all(ms, _variant(), _playlist())
        types = {i.type for i in incidents}
        assert "http_error" in types
        assert "stall" in types
        assert "gap" in types

    def test_ordering_http_errors_first(self) -> None:
        ms = [
            _ok(100, timestamp=_at(0)),
            _fail(101, timestamp=_at(1), http_status=404),
            _ok(103, timestamp=_at(15)),   # stall + gap
        ]
        incidents = detect_all(ms, _variant(), _playlist())
        assert incidents[0].type == "http_error"

    def test_custom_config_propagated_to_all_detectors(self) -> None:
        cfg = DetectionConfig(
            stall_factor=10.0,           # very lenient — no stall at 5s gap
            bitrate_drop_consecutive=1,  # very strict — 1 low segment is enough
        )
        ms = [
            _ok(100, timestamp=_at(0), bitrate_bps=2_000_000),
            _ok(101, timestamp=_at(5)),
        ]
        incidents = detect_all(ms, _variant(), _playlist(), config=cfg)
        types = {i.type for i in incidents}
        assert "bitrate_drop" in types
        assert "stall" not in types
