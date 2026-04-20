"""HLS master + media playlist parser, backed by the m3u8 library."""

from datetime import UTC

import m3u8

from twitch_healthcheck.models import MasterPlaylist, MediaPlaylist, Segment, Variant


class PlaylistParseError(Exception):
    """Raised when an M3U8 playlist cannot be parsed or has unexpected structure."""


def _infer_quality(resolution: tuple[int, int] | None, framerate: float | None) -> str:
    """Infer a human-readable quality label from resolution and frame rate.

    Args:
        resolution: (width, height) in pixels, or None for audio-only variants.
        framerate: frames per second, or None if not specified.

    Returns:
        A label such as "1080p60", "720p", "480p", or "audio_only".
    """
    if resolution is None:
        return "audio_only"

    _, height = resolution
    high_fps = framerate is not None and framerate >= 50

    match height:
        case h if h >= 1080:
            return "1080p60" if high_fps else "1080p"
        case h if h >= 720:
            return "720p60" if high_fps else "720p"
        case h if h >= 480:
            return "480p"
        case h if h >= 360:
            return "360p"
        case h if h >= 240:
            return "240p"
        case _:
            return f"{height}p"


def parse_master_playlist(content: str) -> MasterPlaylist:
    """Parse an HLS master playlist string and return a MasterPlaylist model.

    Args:
        content: Raw text of an M3U8 master playlist.

    Returns:
        A MasterPlaylist containing all stream variants.

    Raises:
        PlaylistParseError: If the content is not a valid master playlist.
    """
    try:
        parsed = m3u8.loads(content)
    except Exception as exc:
        raise PlaylistParseError(f"Failed to parse playlist: {exc}") from exc

    if not parsed.is_variant:
        raise PlaylistParseError(
            "Expected a master playlist (with #EXT-X-STREAM-INF variants) "
            "but got a media playlist."
        )

    variants: list[Variant] = []
    for pl in parsed.playlists:
        info = pl.stream_info
        bandwidth: int = info.bandwidth

        raw_res = info.resolution  # (width, height) tuple or None
        resolution: tuple[int, int] | None = (int(raw_res[0]), int(raw_res[1])) if raw_res else None
        framerate: float | None = float(info.frame_rate) if info.frame_rate is not None else None
        quality = _infer_quality(resolution, framerate)

        variants.append(
            Variant(
                quality=quality,
                bandwidth=bandwidth,
                resolution=resolution,
                framerate=framerate,
                uri=pl.uri,
            )
        )

    if not variants:
        raise PlaylistParseError("Master playlist contains no stream variants.")

    return MasterPlaylist(variants=variants)


def parse_media_playlist(content: str, base_url: str) -> MediaPlaylist:
    """Parse an HLS media playlist string and return a MediaPlaylist model.

    Relative segment URIs are resolved against base_url.

    Args:
        content: Raw text of an M3U8 media playlist.
        base_url: Base URL used to resolve relative segment URIs, e.g.
                  "https://cdn.example.com/hls/1080p60/".

    Returns:
        A MediaPlaylist with all segments and metadata.

    Raises:
        PlaylistParseError: If the content is not a valid media playlist.
    """
    try:
        parsed = m3u8.loads(content, uri=base_url)
    except Exception as exc:
        raise PlaylistParseError(f"Failed to parse playlist: {exc}") from exc

    if parsed.is_variant:
        raise PlaylistParseError(
            "Expected a media playlist (with #EXTINF segments) "
            "but got a master playlist."
        )

    target_duration: float | None = parsed.target_duration
    if target_duration is None:
        raise PlaylistParseError("Media playlist is missing #EXT-X-TARGETDURATION.")

    media_sequence: int = parsed.media_sequence or 0

    segments: list[Segment] = []
    for seg in parsed.segments:
        uri = seg.absolute_uri or seg.uri
        if not uri:
            raise PlaylistParseError("Segment is missing a URI.")

        pdt = seg.program_date_time
        if pdt is not None and pdt.tzinfo is None:
            # m3u8 sometimes returns naive datetimes — treat as UTC
            pdt = pdt.replace(tzinfo=UTC)

        segments.append(
            Segment(
                uri=uri,
                duration=float(seg.duration),
                sequence=media_sequence + len(segments),
                program_date_time=pdt,
            )
        )

    return MediaPlaylist(
        target_duration=float(target_duration),
        media_sequence=media_sequence,
        segments=segments,
        ended=parsed.is_endlist,
    )
