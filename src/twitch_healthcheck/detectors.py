"""Anomaly detection: stalls, bitrate drops, segment gaps, HTTP errors.

All detectors are pure functions — they receive measurements and context,
and return a (possibly empty) list of Incidents. No state is mutated.
"""

from pydantic import BaseModel, Field

from twitch_healthcheck.models import (
    Incident,
    MediaPlaylist,
    SegmentMeasurement,
    Variant,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class DetectionConfig(BaseModel):
    """Thresholds for all anomaly detectors.

    All values have production-sensible defaults and can be overridden per
    monitoring session.
    """

    # Stall: gap between consecutive successful downloads exceeds this multiple
    # of the playlist target duration.
    stall_factor: float = Field(
        2.0,
        gt=0,
        description="Multiplier applied to target_duration to define a stall.",
    )

    # Bitrate drop: effective bitrate below this fraction of the variant's
    # advertised bandwidth is flagged as low.
    bitrate_drop_threshold: float = Field(
        0.5,
        gt=0,
        description="Fraction of variant.bandwidth below which a segment is low-bitrate.",
    )

    # Bitrate drop: only emit an incident after this many consecutive low segments.
    bitrate_drop_consecutive: int = Field(
        3,
        ge=1,
        description="Minimum consecutive low-bitrate segments before emitting an incident.",
    )

    # Gap: sequence jump larger than this is flagged as a missing-segment gap.
    gap_max_sequence_jump: int = Field(
        1,
        ge=1,
        description="Maximum allowed sequence-number difference between consecutive measurements.",
    )


_DEFAULT_CONFIG = DetectionConfig()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc_from(m: SegmentMeasurement) -> str:
    return m.timestamp_utc.isoformat()


def _make_incident(
    measurement: SegmentMeasurement,
    incident_type: str,
    severity: str,
    message: str,
    details: dict,  # type: ignore[type-arg]
) -> Incident:
    return Incident(
        timestamp_utc=measurement.timestamp_utc,
        type=incident_type,  # type: ignore[arg-type]
        severity=severity,   # type: ignore[arg-type]
        message=message,
        details=details,
    )


# ---------------------------------------------------------------------------
# Individual detectors
# ---------------------------------------------------------------------------


def detect_http_errors(
    measurements: list[SegmentMeasurement],
    config: DetectionConfig | None = None,
) -> list[Incident]:
    """Produce one "http_error" Incident for every segment that failed.

    Severity rules:
    - 4xx response → "warning" (client-side / content issue, often transient)
    - 5xx response → "critical" (server-side failure)
    - Connection error (http_status is None) → "critical"

    Args:
        measurements: Sequence of segment download results.
        config: Detection thresholds (unused here, kept for API consistency).

    Returns:
        List of Incidents, one per failed segment.
    """
    _ = config  # reserved for future per-status-code threshold overrides
    incidents: list[Incident] = []

    for m in measurements:
        if m.success and (m.http_status is None or m.http_status < 400):
            continue

        status = m.http_status
        severity = "critical" if status is None or status >= 500 else "warning"

        incidents.append(
            _make_incident(
                m,
                incident_type="http_error",
                severity=severity,
                message=(
                    f"Segment {m.segment.sequence} failed"
                    + (f" with HTTP {status}" if status else " (connection error)")
                    + (f": {m.error}" if m.error else "")
                ),
                details={
                    "uri": m.segment.uri,
                    "http_status": status,
                    "error": m.error,
                },
            )
        )

    return incidents


def detect_stalls(
    measurements: list[SegmentMeasurement],
    playlist: MediaPlaylist,
    config: DetectionConfig | None = None,
) -> list[Incident]:
    """Produce a "stall" Incident when consecutive successful downloads are
    further apart in time than stall_factor × target_duration.

    Only successful measurements are compared — download failures are skipped
    so that a single bad segment does not mask a genuine stall.

    Args:
        measurements: Sequence of segment download results.
        playlist: The current MediaPlaylist (provides target_duration).
        config: Detection thresholds.

    Returns:
        List of stall Incidents.
    """
    cfg = config or _DEFAULT_CONFIG
    threshold_s = cfg.stall_factor * playlist.target_duration
    incidents: list[Incident] = []

    successful = [m for m in measurements if m.success]
    for prev, curr in zip(successful, successful[1:], strict=False):
        gap_s = (curr.timestamp_utc - prev.timestamp_utc).total_seconds()
        if gap_s > threshold_s:
            incidents.append(
                _make_incident(
                    curr,
                    incident_type="stall",
                    severity="critical",
                    message=(
                        f"Stall detected before segment {curr.segment.sequence}: "
                        f"{gap_s:.1f}s gap (threshold {threshold_s:.1f}s)"
                    ),
                    details={
                        "gap_seconds": round(gap_s, 3),
                        "threshold_seconds": threshold_s,
                        "previous_sequence": prev.segment.sequence,
                        "current_sequence": curr.segment.sequence,
                    },
                )
            )

    return incidents


def detect_bitrate_drops(
    measurements: list[SegmentMeasurement],
    variant: Variant,
    config: DetectionConfig | None = None,
) -> list[Incident]:
    """Produce a "bitrate_drop" Incident when effective bitrate falls below a
    fraction of the variant's advertised bandwidth for N consecutive segments.

    One incident is emitted at the start of each low-bitrate run (not once
    per segment), and the run resets when a normal-bitrate segment appears.

    Args:
        measurements: Sequence of segment download results.
        variant: The chosen stream variant (provides expected bandwidth).
        config: Detection thresholds.

    Returns:
        List of bitrate_drop Incidents.
    """
    cfg = config or _DEFAULT_CONFIG
    min_bps = cfg.bitrate_drop_threshold * variant.bandwidth
    incidents: list[Incident] = []

    consecutive = 0
    incident_emitted = False
    run_start: SegmentMeasurement | None = None

    for m in measurements:
        if not m.success or m.effective_bitrate_bps is None:
            # Don't count failed segments toward or against the run.
            continue

        if m.effective_bitrate_bps < min_bps:
            if consecutive == 0:
                run_start = m
            consecutive += 1

            if consecutive >= cfg.bitrate_drop_consecutive and not incident_emitted:
                assert run_start is not None
                ratio = m.effective_bitrate_bps / variant.bandwidth
                incidents.append(
                    _make_incident(
                        run_start,
                        incident_type="bitrate_drop",
                        severity="warning",
                        message=(
                            f"Bitrate dropped to {m.effective_bitrate_bps:,} bps "
                            f"({ratio:.0%} of expected {variant.bandwidth:,} bps) "
                            f"for {consecutive} consecutive segments"
                        ),
                        details={
                            "effective_bitrate_bps": m.effective_bitrate_bps,
                            "expected_bitrate_bps": variant.bandwidth,
                            "ratio": round(ratio, 3),
                            "consecutive_count": consecutive,
                            "threshold_fraction": cfg.bitrate_drop_threshold,
                        },
                    )
                )
                incident_emitted = True
        else:
            # Good segment — reset the run.
            consecutive = 0
            incident_emitted = False
            run_start = None

    return incidents


def detect_gaps(
    measurements: list[SegmentMeasurement],
    config: DetectionConfig | None = None,
) -> list[Incident]:
    """Produce a "gap" Incident when the segment sequence jumps by more than
    gap_max_sequence_jump between two consecutive measurements.

    A gap means the monitor missed one or more segments that appeared and
    then fell off the playlist window before being downloaded.

    Args:
        measurements: Sequence of segment download results (any success status).
        config: Detection thresholds.

    Returns:
        List of gap Incidents.
    """
    cfg = config or _DEFAULT_CONFIG
    incidents: list[Incident] = []

    for prev, curr in zip(measurements, measurements[1:], strict=False):
        jump = curr.segment.sequence - prev.segment.sequence
        if jump > cfg.gap_max_sequence_jump:
            missing = jump - 1
            incidents.append(
                _make_incident(
                    curr,
                    incident_type="gap",
                    severity="warning",
                    message=(
                        f"Sequence gap: {missing} segment(s) missing between "
                        f"{prev.segment.sequence} and {curr.segment.sequence}"
                    ),
                    details={
                        "from_sequence": prev.segment.sequence,
                        "to_sequence": curr.segment.sequence,
                        "missing_count": missing,
                        "jump": jump,
                    },
                )
            )

    return incidents


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def detect_all(
    measurements: list[SegmentMeasurement],
    variant: Variant,
    playlist: MediaPlaylist,
    config: DetectionConfig | None = None,
) -> list[Incident]:
    """Run all detectors and return the combined list of Incidents.

    Incidents are ordered: http_errors first, then stalls, bitrate drops, gaps.

    Args:
        measurements: All segment measurements for the current window.
        variant: The active stream variant (for bitrate reference).
        playlist: The current MediaPlaylist (for target_duration reference).
        config: Detection thresholds. Uses sensible defaults when None.

    Returns:
        Combined list of Incidents from all detectors.
    """
    cfg = config or _DEFAULT_CONFIG
    return (
        detect_http_errors(measurements, cfg)
        + detect_stalls(measurements, playlist, cfg)
        + detect_bitrate_drops(measurements, variant, cfg)
        + detect_gaps(measurements, cfg)
    )
