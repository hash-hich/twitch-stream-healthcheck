"""Tests for Pydantic models in twitch_healthcheck.models."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from twitch_healthcheck.models import (
    HealthStatus,
    Incident,
    MasterPlaylist,
    MediaPlaylist,
    MonitorSnapshot,
    Segment,
    SegmentMeasurement,
    Variant,
)

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_segment(sequence: int = 0) -> Segment:
    return Segment(uri="https://cdn.example.com/seg0.ts", duration=2.0, sequence=sequence)


def make_variant(**overrides: object) -> Variant:
    defaults = dict(
        quality="1080p60",
        bandwidth=6_000_000,
        resolution=(1920, 1080),
        framerate=60.0,
        uri="https://cdn.example.com/1080p60/index.m3u8",
    )
    defaults.update(overrides)
    return Variant(**defaults)  # type: ignore[arg-type]


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# Variant
# ---------------------------------------------------------------------------

class TestVariant:
    def test_valid(self) -> None:
        v = make_variant()
        assert v.quality == "1080p60"
        assert v.bandwidth == 6_000_000
        assert v.resolution == (1920, 1080)
        assert v.framerate == 60.0

    def test_no_resolution_or_framerate(self) -> None:
        v = make_variant(resolution=None, framerate=None)
        assert v.resolution is None
        assert v.framerate is None

    def test_bandwidth_must_be_positive(self) -> None:
        with pytest.raises(ValidationError, match="greater than 0"):
            make_variant(bandwidth=0)

    def test_bandwidth_negative(self) -> None:
        with pytest.raises(ValidationError):
            make_variant(bandwidth=-1)

    def test_resolution_dimensions_must_be_positive(self) -> None:
        with pytest.raises(ValidationError, match="resolution dimensions must be positive"):
            make_variant(resolution=(0, 1080))


# ---------------------------------------------------------------------------
# Segment
# ---------------------------------------------------------------------------

class TestSegment:
    def test_valid(self) -> None:
        s = make_segment()
        assert s.duration == 2.0
        assert s.sequence == 0

    def test_duration_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            Segment(uri="x", duration=0.0, sequence=0)

    def test_sequence_cannot_be_negative(self) -> None:
        with pytest.raises(ValidationError):
            Segment(uri="x", duration=2.0, sequence=-1)

    def test_program_date_time_aware(self) -> None:
        s = Segment(uri="x", duration=2.0, sequence=0, program_date_time=now_utc())
        assert s.program_date_time is not None
        assert s.program_date_time.tzinfo is not None

    def test_program_date_time_naive_rejected(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            Segment(uri="x", duration=2.0, sequence=0, program_date_time=datetime(2024, 1, 1))


# ---------------------------------------------------------------------------
# MasterPlaylist
# ---------------------------------------------------------------------------

class TestMasterPlaylist:
    def test_valid(self) -> None:
        pl = MasterPlaylist(variants=[make_variant(), make_variant(quality="720p30", bandwidth=3_000_000)])
        assert len(pl.variants) == 2

    def test_empty_variants(self) -> None:
        pl = MasterPlaylist(variants=[])
        assert pl.variants == []


# ---------------------------------------------------------------------------
# MediaPlaylist
# ---------------------------------------------------------------------------

class TestMediaPlaylist:
    def test_valid(self) -> None:
        pl = MediaPlaylist(
            target_duration=2.0,
            media_sequence=100,
            segments=[make_segment(i) for i in range(3)],
            ended=False,
        )
        assert len(pl.segments) == 3
        assert not pl.ended

    def test_ended_default_false(self) -> None:
        pl = MediaPlaylist(target_duration=2.0, media_sequence=0, segments=[])
        assert pl.ended is False

    def test_target_duration_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            MediaPlaylist(target_duration=0.0, media_sequence=0, segments=[])


# ---------------------------------------------------------------------------
# SegmentMeasurement
# ---------------------------------------------------------------------------

class TestSegmentMeasurement:
    def test_successful_measurement(self) -> None:
        m = SegmentMeasurement(
            segment=make_segment(),
            success=True,
            http_status=200,
            download_time_ms=120.5,
            bytes_downloaded=204_800,
            effective_bitrate_bps=6_000_000,
            timestamp_utc=now_utc(),
        )
        assert m.success is True
        assert m.error is None

    def test_failed_measurement_requires_error(self) -> None:
        with pytest.raises(ValidationError, match="error must be set"):
            SegmentMeasurement(
                segment=make_segment(),
                success=False,
                http_status=503,
                download_time_ms=0.0,
                bytes_downloaded=0,
                timestamp_utc=now_utc(),
            )

    def test_failed_measurement_with_error(self) -> None:
        m = SegmentMeasurement(
            segment=make_segment(),
            success=False,
            http_status=503,
            download_time_ms=0.0,
            bytes_downloaded=0,
            error="Service Unavailable",
            timestamp_utc=now_utc(),
        )
        assert m.error == "Service Unavailable"

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            SegmentMeasurement(
                segment=make_segment(),
                success=True,
                download_time_ms=100.0,
                bytes_downloaded=1024,
                timestamp_utc=datetime(2024, 1, 1),
            )


# ---------------------------------------------------------------------------
# Incident
# ---------------------------------------------------------------------------

class TestIncident:
    def test_valid(self) -> None:
        inc = Incident(
            timestamp_utc=now_utc(),
            type="stall",
            severity="critical",
            message="Stream stalled for 4s",
            details={"stall_duration_ms": 4000},
        )
        assert inc.type == "stall"
        assert inc.severity == "critical"

    def test_invalid_type(self) -> None:
        with pytest.raises(ValidationError):
            Incident(
                timestamp_utc=now_utc(),
                type="explosion",  # type: ignore[arg-type]
                severity="info",
                message="x",
                details={},
            )

    def test_invalid_severity(self) -> None:
        with pytest.raises(ValidationError):
            Incident(
                timestamp_utc=now_utc(),
                type="gap",
                severity="catastrophic",  # type: ignore[arg-type]
                message="x",
                details={},
            )


# ---------------------------------------------------------------------------
# MonitorSnapshot
# ---------------------------------------------------------------------------

class TestMonitorSnapshot:
    def _make(self, **overrides: object) -> MonitorSnapshot:
        defaults: dict[str, object] = dict(
            channel="kaicenat",
            status="healthy",
            uptime_seconds=300.0,
            segments_total=150,
            segments_failed=2,
            median_latency_ms=95.0,
            effective_bitrate_bps=5_800_000,
            recent_incidents=[],
            timestamp_utc=now_utc(),
        )
        defaults.update(overrides)
        return MonitorSnapshot(**defaults)  # type: ignore[arg-type]

    def test_valid(self) -> None:
        s = self._make()
        assert s.channel == "kaicenat"
        assert s.status == "healthy"

    def test_all_health_statuses(self) -> None:
        for status in ("healthy", "degraded", "down"):
            s = self._make(status=status)
            assert s.status == status

    def test_failed_cannot_exceed_total(self) -> None:
        with pytest.raises(ValidationError, match="segments_failed cannot exceed segments_total"):
            self._make(segments_total=10, segments_failed=11)

    def test_recent_incidents_capped_at_20(self) -> None:
        incidents = [
            Incident(
                timestamp_utc=now_utc(),
                type="http_error",
                severity="warning",
                message=f"error {i}",
                details={},
            )
            for i in range(21)
        ]
        with pytest.raises(ValidationError):
            self._make(recent_incidents=incidents)

    def test_health_status_type(self) -> None:
        # HealthStatus is a type alias, check it resolves correctly
        assert "healthy" == HealthStatus.__args__[0]  # type: ignore[attr-defined]
