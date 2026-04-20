"""Tests for twitch_api.py — all HTTP calls are mocked with respx."""


import httpx
import pytest
import respx

from twitch_healthcheck.twitch_api import (
    GQL_ENDPOINT,
    TWITCH_CLIENT_ID,
    StreamOfflineError,
    TwitchAPIError,
    fetch_playlist,
    get_master_playlist_url,
    is_channel_live,
)

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

LIVE_CHANNEL = "kaicenat"
OFFLINE_CHANNEL = "nobodystreaminghere"

TOKEN = '{"channel_id":"123456789","expires":9999999999}'
SIGNATURE = "abcdef1234567890"

LIVE_GQL_RESPONSE = {
    "data": {
        "streamPlaybackAccessToken": {
            "value": TOKEN,
            "signature": SIGNATURE,
        }
    }
}

OFFLINE_GQL_RESPONSE = {
    "data": {
        "streamPlaybackAccessToken": None
    }
}

GQL_ERROR_RESPONSE = {
    "errors": [{"message": "service unavailable"}]
}

SAMPLE_PLAYLIST = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=6000000\nhttps://cdn.example.com/index.m3u8\n"


def mock_gql(response_body: dict, status_code: int = 200) -> respx.Route:
    """Register a mock for the Twitch GQL endpoint."""
    return respx.post(GQL_ENDPOINT).mock(
        return_value=httpx.Response(status_code, json=response_body)
    )


# ---------------------------------------------------------------------------
# is_channel_live
# ---------------------------------------------------------------------------


class TestIsChannelLive:
    @respx.mock
    async def test_live_channel_returns_true(self) -> None:
        mock_gql(LIVE_GQL_RESPONSE)
        result = await is_channel_live(LIVE_CHANNEL)
        assert result is True

    @respx.mock
    async def test_offline_channel_returns_false(self) -> None:
        mock_gql(OFFLINE_GQL_RESPONSE)
        result = await is_channel_live(OFFLINE_CHANNEL)
        assert result is False

    @respx.mock
    async def test_channel_name_is_lowercased(self) -> None:
        route = mock_gql(LIVE_GQL_RESPONSE)
        await is_channel_live("KaicenaT")
        # verify the request body contained the lowercased channel name
        assert "kaicenat" in route.calls[0].request.content.decode()

    @respx.mock
    async def test_gql_error_raises_twitch_api_error(self) -> None:
        mock_gql(GQL_ERROR_RESPONSE)
        with pytest.raises(TwitchAPIError, match="service unavailable"):
            await is_channel_live(LIVE_CHANNEL)

    @respx.mock
    async def test_non_200_raises_twitch_api_error(self) -> None:
        respx.post(GQL_ENDPOINT).mock(return_value=httpx.Response(500, text="oops"))
        with pytest.raises(TwitchAPIError, match="HTTP 500"):
            await is_channel_live(LIVE_CHANNEL)

    @respx.mock
    async def test_client_id_header_is_sent(self) -> None:
        route = mock_gql(LIVE_GQL_RESPONSE)
        await is_channel_live(LIVE_CHANNEL)
        assert route.calls[0].request.headers["Client-ID"] == TWITCH_CLIENT_ID


# ---------------------------------------------------------------------------
# get_master_playlist_url
# ---------------------------------------------------------------------------


class TestGetMasterPlaylistUrl:
    @respx.mock
    async def test_live_channel_returns_usher_url(self) -> None:
        mock_gql(LIVE_GQL_RESPONSE)
        url = await get_master_playlist_url(LIVE_CHANNEL)
        assert "usher.ttvnw.net" in url
        assert LIVE_CHANNEL in url

    @respx.mock
    async def test_url_contains_sig_and_token(self) -> None:
        mock_gql(LIVE_GQL_RESPONSE)
        url = await get_master_playlist_url(LIVE_CHANNEL)
        assert "sig=" in url
        assert "token=" in url

    @respx.mock
    async def test_url_contains_allow_source(self) -> None:
        mock_gql(LIVE_GQL_RESPONSE)
        url = await get_master_playlist_url(LIVE_CHANNEL)
        assert "allow_source=true" in url

    @respx.mock
    async def test_offline_channel_raises_stream_offline_error(self) -> None:
        mock_gql(OFFLINE_GQL_RESPONSE)
        with pytest.raises(StreamOfflineError, match=OFFLINE_CHANNEL):
            await get_master_playlist_url(OFFLINE_CHANNEL)

    @respx.mock
    async def test_gql_error_raises_twitch_api_error(self) -> None:
        mock_gql(GQL_ERROR_RESPONSE)
        with pytest.raises(TwitchAPIError):
            await get_master_playlist_url(LIVE_CHANNEL)

    @respx.mock
    async def test_url_is_https(self) -> None:
        mock_gql(LIVE_GQL_RESPONSE)
        url = await get_master_playlist_url(LIVE_CHANNEL)
        assert url.startswith("https://")


# ---------------------------------------------------------------------------
# fetch_playlist
# ---------------------------------------------------------------------------

PLAYLIST_URL = "https://usher.ttvnw.net/api/channel/hls/kaicenat.m3u8?sig=abc&token=xyz"


class TestFetchPlaylist:
    @respx.mock
    async def test_returns_playlist_text(self) -> None:
        respx.get(PLAYLIST_URL).mock(return_value=httpx.Response(200, text=SAMPLE_PLAYLIST))
        content = await fetch_playlist(PLAYLIST_URL)
        assert content == SAMPLE_PLAYLIST

    @respx.mock
    async def test_404_raises_twitch_api_error(self) -> None:
        respx.get(PLAYLIST_URL).mock(return_value=httpx.Response(404))
        with pytest.raises(TwitchAPIError, match="HTTP 404"):
            await fetch_playlist(PLAYLIST_URL)

    @respx.mock
    async def test_503_raises_twitch_api_error(self) -> None:
        respx.get(PLAYLIST_URL).mock(return_value=httpx.Response(503))
        with pytest.raises(TwitchAPIError, match="HTTP 503"):
            await fetch_playlist(PLAYLIST_URL)

    @respx.mock
    async def test_network_error_raises_twitch_api_error(self) -> None:
        respx.get(PLAYLIST_URL).mock(side_effect=httpx.ConnectError("connection refused"))
        with pytest.raises(TwitchAPIError, match="Network error"):
            await fetch_playlist(PLAYLIST_URL)
