"""Twitch GraphQL + HLS playlist URL resolution.

Flow:
    1. POST to the Twitch GQL endpoint to obtain a signed playback access token.
    2. Build the Usher URL with that token to get the HLS master playlist URL.
    3. Optionally fetch the raw playlist text.
"""

from urllib.parse import quote, urlencode

import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GQL_ENDPOINT = "https://gql.twitch.tv/gql"

# Public Client-ID used by the Twitch web player itself.
# See: https://github.com/streamlink/streamlink and twitch-dl for prior art.
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

GQL_HEADERS = {
    "Client-ID": TWITCH_CLIENT_ID,
    "Content-Type": "application/json",
}

PLAYBACK_ACCESS_TOKEN_QUERY = """
{
  streamPlaybackAccessToken(
    channelName: "%(channel)s",
    params: {
      platform: "web",
      playerBackend: "mediaplayer",
      playerType: "site"
    }
  ) {
    value
    signature
  }
}
"""

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class StreamOfflineError(Exception):
    """Raised when the requested channel is not currently live."""


class TwitchAPIError(Exception):
    """Raised when the Twitch GQL API returns an unexpected response."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_usher_url(channel: str, token: str, signature: str) -> str:
    """Build the Usher HLS master playlist URL from a signed token.

    Args:
        channel: Twitch channel name (lowercase).
        token: JSON token string returned by the GQL access token query.
        signature: Signature returned alongside the token.

    Returns:
        Full URL to the HLS master playlist on usher.ttvnw.net.
    """
    params = urlencode({
        "sig": signature,
        "token": token,
        "allow_source": "true",
        "fast_bread": "true",
    })
    return f"https://usher.ttvnw.net/api/channel/hls/{quote(channel)}.m3u8?{params}"


async def _fetch_access_token(
    client: httpx.AsyncClient,
    channel: str,
) -> tuple[str, str] | None:
    """Call the GQL endpoint to obtain a playback access token.

    Args:
        client: An active httpx.AsyncClient.
        channel: Twitch channel name.

    Returns:
        A (token, signature) tuple, or None if the channel is offline / not found.

    Raises:
        TwitchAPIError: On non-200 HTTP responses or unexpected GQL error payloads.
    """
    query = PLAYBACK_ACCESS_TOKEN_QUERY % {"channel": channel}

    try:
        response = await client.post(
            GQL_ENDPOINT,
            json={"query": query},
            headers=GQL_HEADERS,
            timeout=10.0,
        )
    except httpx.RequestError as exc:
        raise TwitchAPIError(f"Network error contacting Twitch GQL: {exc}") from exc

    if response.status_code != 200:
        raise TwitchAPIError(
            f"Twitch GQL responded with HTTP {response.status_code}: {response.text[:200]}"
        )

    body = response.json()

    if "errors" in body:
        messages = [e.get("message", "unknown") for e in body["errors"]]
        raise TwitchAPIError(f"Twitch GQL returned errors: {'; '.join(messages)}")

    token_data = body.get("data", {}).get("streamPlaybackAccessToken")
    if token_data is None:
        # Channel offline or does not exist — GQL returns null for the field.
        return None

    token: str = token_data["value"]
    signature: str = token_data["signature"]
    return token, signature


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def is_channel_live(channel: str) -> bool:
    """Return True if *channel* is currently broadcasting a live stream.

    Args:
        channel: Twitch channel name (case-insensitive).

    Returns:
        True if the channel is live, False if offline or nonexistent.

    Raises:
        TwitchAPIError: On network errors or unexpected API responses.
    """
    async with httpx.AsyncClient() as client:
        result = await _fetch_access_token(client, channel.lower())
    return result is not None


async def get_master_playlist_url(channel: str) -> str:
    """Return the HLS master playlist URL for a live channel.

    Args:
        channel: Twitch channel name (case-insensitive).

    Returns:
        The HTTPS URL to the HLS master playlist on usher.ttvnw.net.

    Raises:
        StreamOfflineError: If the channel is not currently live.
        TwitchAPIError: On network errors or unexpected API responses.
    """
    async with httpx.AsyncClient() as client:
        result = await _fetch_access_token(client, channel.lower())

    if result is None:
        raise StreamOfflineError(f"Channel '{channel}' is not live or does not exist.")

    token, signature = result
    return _build_usher_url(channel.lower(), token, signature)


async def fetch_playlist(url: str) -> str:
    """Fetch the raw text content of an HLS playlist URL.

    Args:
        url: Full HTTPS URL to a master or media playlist.

    Returns:
        The raw M3U8 playlist text.

    Raises:
        TwitchAPIError: On HTTP errors or network failures.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=10.0)
    except httpx.RequestError as exc:
        raise TwitchAPIError(f"Network error fetching playlist: {exc}") from exc

    if response.status_code != 200:
        raise TwitchAPIError(
            f"Playlist request failed with HTTP {response.status_code}: {url}"
        )

    return response.text
