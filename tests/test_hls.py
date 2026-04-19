"""Tests for the HLS playlist parser (hls.py)."""

from pathlib import Path

import pytest

from twitch_healthcheck.hls import PlaylistParseError, parse_master_playlist, parse_media_playlist
from twitch_healthcheck.models import MasterPlaylist, MediaPlaylist

FIXTURES = Path(__file__).parent / "fixtures"

MASTER_M3U8 = (FIXTURES / "master_playlist.m3u8").read_text()
MEDIA_M3U8 = (FIXTURES / "media_playlist.m3u8").read_text()
BASE_URL = "https://video-edge.fra05.twitch.tv/v1/segment/1080p60/"


# ---------------------------------------------------------------------------
# Master playlist
# ---------------------------------------------------------------------------


class TestParseMasterPlaylist:
    def test_returns_master_playlist_model(self) -> None:
        result = parse_master_playlist(MASTER_M3U8)
        assert isinstance(result, MasterPlaylist)

    def test_four_variants(self) -> None:
        result = parse_master_playlist(MASTER_M3U8)
        assert len(result.variants) == 4

    def test_1080p60_variant(self) -> None:
        result = parse_master_playlist(MASTER_M3U8)
        v = next(v for v in result.variants if v.quality == "1080p60")
        assert v.bandwidth == 8_648_000
        assert v.resolution == (1920, 1080)
        assert v.framerate == pytest.approx(60.0)
        assert "1080p60" in v.uri

    def test_720p60_variant(self) -> None:
        result = parse_master_playlist(MASTER_M3U8)
        v = next(v for v in result.variants if v.quality == "720p60")
        assert v.bandwidth == 4_928_000
        assert v.resolution == (1280, 720)

    def test_480p_variant(self) -> None:
        result = parse_master_playlist(MASTER_M3U8)
        v = next(v for v in result.variants if v.quality == "480p")
        assert v.bandwidth == 1_427_999
        assert v.resolution == (852, 480)

    def test_160p_variant(self) -> None:
        result = parse_master_playlist(MASTER_M3U8)
        v = next(v for v in result.variants if v.quality == "160p")
        assert v.bandwidth == 288_000
        assert v.resolution == (284, 160)

    def test_all_variants_have_uri(self) -> None:
        result = parse_master_playlist(MASTER_M3U8)
        for v in result.variants:
            assert v.uri.startswith("https://")

    def test_invalid_content_raises(self) -> None:
        with pytest.raises(PlaylistParseError):
            parse_master_playlist("this is not a playlist")

    def test_media_playlist_passed_as_master_raises(self) -> None:
        with pytest.raises(PlaylistParseError, match="master playlist"):
            parse_master_playlist(MEDIA_M3U8)


# ---------------------------------------------------------------------------
# Media playlist
# ---------------------------------------------------------------------------


class TestParseMediaPlaylist:
    def test_returns_media_playlist_model(self) -> None:
        result = parse_media_playlist(MEDIA_M3U8, BASE_URL)
        assert isinstance(result, MediaPlaylist)

    def test_five_segments(self) -> None:
        result = parse_media_playlist(MEDIA_M3U8, BASE_URL)
        assert len(result.segments) == 5

    def test_target_duration(self) -> None:
        result = parse_media_playlist(MEDIA_M3U8, BASE_URL)
        assert result.target_duration == pytest.approx(2.0)

    def test_media_sequence(self) -> None:
        result = parse_media_playlist(MEDIA_M3U8, BASE_URL)
        assert result.media_sequence == 7420

    def test_not_ended(self) -> None:
        result = parse_media_playlist(MEDIA_M3U8, BASE_URL)
        assert result.ended is False

    def test_ended_playlist(self) -> None:
        content = MEDIA_M3U8 + "#EXT-X-ENDLIST\n"
        result = parse_media_playlist(content, BASE_URL)
        assert result.ended is True

    def test_segment_uris_are_absolute(self) -> None:
        result = parse_media_playlist(MEDIA_M3U8, BASE_URL)
        for seg in result.segments:
            assert seg.uri.startswith("https://"), f"Expected absolute URI, got: {seg.uri}"

    def test_segment_sequence_numbers(self) -> None:
        result = parse_media_playlist(MEDIA_M3U8, BASE_URL)
        sequences = [seg.sequence for seg in result.segments]
        assert sequences == [7420, 7421, 7422, 7423, 7424]

    def test_segment_durations(self) -> None:
        result = parse_media_playlist(MEDIA_M3U8, BASE_URL)
        for seg in result.segments:
            assert seg.duration == pytest.approx(2.0)

    def test_program_date_time_is_utc_aware(self) -> None:
        result = parse_media_playlist(MEDIA_M3U8, BASE_URL)
        for seg in result.segments:
            assert seg.program_date_time is not None
            assert seg.program_date_time.tzinfo is not None

    def test_first_segment_datetime(self) -> None:
        from datetime import datetime, timezone

        result = parse_media_playlist(MEDIA_M3U8, BASE_URL)
        first = result.segments[0]
        assert first.program_date_time == datetime(2024, 4, 19, 10, 3, 40, tzinfo=timezone.utc)

    def test_master_playlist_passed_as_media_raises(self) -> None:
        with pytest.raises(PlaylistParseError, match="media playlist"):
            parse_media_playlist(MASTER_M3U8, BASE_URL)

    def test_invalid_content_raises(self) -> None:
        with pytest.raises(PlaylistParseError):
            parse_media_playlist("not a playlist at all", BASE_URL)

    def test_missing_target_duration_raises(self) -> None:
        content = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:2.0,\nseg.ts\n"
        with pytest.raises(PlaylistParseError, match="TARGETDURATION"):
            parse_media_playlist(content, BASE_URL)


# ---------------------------------------------------------------------------
# Quality label inference
# ---------------------------------------------------------------------------


class TestInferQuality:
    """Test _infer_quality indirectly through parse_master_playlist,
    and directly for edge cases."""

    def test_audio_only_variant(self) -> None:
        content = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=160000\n"
            "https://cdn.example.com/audio/index.m3u8\n"
        )
        result = parse_master_playlist(content)
        assert result.variants[0].quality == "audio_only"

    def test_1080p_without_high_fps(self) -> None:
        content = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,FRAME-RATE=30.000\n"
            "https://cdn.example.com/1080p30/index.m3u8\n"
        )
        result = parse_master_playlist(content)
        assert result.variants[0].quality == "1080p"

    def test_360p_variant(self) -> None:
        content = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=640x360,FRAME-RATE=30.000\n"
            "https://cdn.example.com/360p/index.m3u8\n"
        )
        result = parse_master_playlist(content)
        assert result.variants[0].quality == "360p"
