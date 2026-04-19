"""Pydantic v2 models shared across all modules."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class Variant(BaseModel):
    """A stream variant from an HLS master playlist."""

    quality: str
    bandwidth: int = Field(..., description="Bits per second", gt=0)
    resolution: tuple[int, int] | None = None
    framerate: float | None = None
    uri: str

    @model_validator(mode="after")
    def resolution_dimensions_positive(self) -> "Variant":
        if self.resolution is not None:
            w, h = self.resolution
            if w <= 0 or h <= 0:
                raise ValueError("resolution dimensions must be positive")
        return self


class Segment(BaseModel):
    """A single HLS segment."""

    uri: str
    duration: float = Field(..., gt=0)
    sequence: int = Field(..., ge=0)
    program_date_time: datetime | None = None

    @field_validator("program_date_time")
    @classmethod
    def must_be_utc(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("program_date_time must be timezone-aware")
        return v


class MasterPlaylist(BaseModel):
    """An HLS master playlist."""

    variants: list[Variant]


class MediaPlaylist(BaseModel):
    """An HLS media playlist."""

    target_duration: float = Field(..., gt=0)
    media_sequence: int = Field(..., ge=0)
    segments: list[Segment]
    ended: bool = False


class SegmentMeasurement(BaseModel):
    """The result of downloading and timing one HLS segment."""

    segment: Segment
    success: bool
    http_status: int | None = None
    download_time_ms: float = Field(..., ge=0)
    bytes_downloaded: int = Field(..., ge=0)
    effective_bitrate_bps: int | None = None
    error: str | None = None
    timestamp_utc: datetime

    @field_validator("timestamp_utc")
    @classmethod
    def must_be_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp_utc must be timezone-aware")
        return v

    @model_validator(mode="after")
    def error_set_on_failure(self) -> "SegmentMeasurement":
        if not self.success and self.error is None:
            raise ValueError("error must be set when success is False")
        return self


IncidentType = Literal["stall", "bitrate_drop", "gap", "http_error"]
Severity = Literal["info", "warning", "critical"]
HealthStatus = Literal["healthy", "degraded", "down"]


class Incident(BaseModel):
    """A detected stream anomaly."""

    timestamp_utc: datetime
    type: IncidentType
    severity: Severity
    message: str
    details: dict  # type: ignore[type-arg]

    @field_validator("timestamp_utc")
    @classmethod
    def must_be_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp_utc must be timezone-aware")
        return v


class MonitorSnapshot(BaseModel):
    """Current monitor state, exposed to the dashboard via WebSocket."""

    channel: str
    status: HealthStatus
    uptime_seconds: float = Field(..., ge=0)
    segments_total: int = Field(..., ge=0)
    segments_failed: int = Field(..., ge=0)
    median_latency_ms: float = Field(..., ge=0)
    effective_bitrate_bps: int | None = None
    recent_incidents: list[Incident] = Field(default_factory=list, max_length=20)
    timestamp_utc: datetime

    @field_validator("timestamp_utc")
    @classmethod
    def must_be_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp_utc must be timezone-aware")
        return v

    @model_validator(mode="after")
    def failed_not_exceed_total(self) -> "MonitorSnapshot":
        if self.segments_failed > self.segments_total:
            raise ValueError("segments_failed cannot exceed segments_total")
        return self
