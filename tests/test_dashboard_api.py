"""Integration tests for the FastAPI dashboard (dashboard/api.py).

Uses httpx.AsyncClient with ASGITransport — no lifespan is triggered, which
means the StreamMonitor is created but never started. snapshot() still returns
a valid (empty) MonitorSnapshot, so all assertions about shape hold.

WebSocket endpoint testing is intentionally skipped: the /ws/metrics route is
exercised manually by the browser dashboard.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from dashboard.api import CHANNEL, app
from twitch_healthcheck.models import MonitorSnapshot


@pytest.fixture
def client():
    """An httpx AsyncClient wired to the FastAPI app via ASGI transport.

    ASGITransport does not trigger FastAPI lifespan events, so the monitor
    background task is never started. This keeps tests fast and dependency-free.
    """
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------


class TestIndex:
    async def test_returns_200(self, client: AsyncClient) -> None:
        async with client as c:
            response = await c.get("/")
        assert response.status_code == 200

    async def test_content_type_is_html(self, client: AsyncClient) -> None:
        async with client as c:
            response = await c.get("/")
        assert "text/html" in response.headers["content-type"]

    async def test_body_contains_chart_js(self, client: AsyncClient) -> None:
        async with client as c:
            response = await c.get("/")
        assert "chart.js" in response.text.lower()

    async def test_body_references_app_js(self, client: AsyncClient) -> None:
        async with client as c:
            response = await c.get("/")
        assert "app.js" in response.text

    async def test_body_contains_websocket_connection(self, client: AsyncClient) -> None:
        """app.js must reference /ws/metrics so the browser connects."""
        async with client as c:
            # Fetch app.js (served as a static file)
            response = await c.get("/static/app.js")
        assert response.status_code == 200
        assert "/ws/metrics" in response.text


# ---------------------------------------------------------------------------
# GET /api/snapshot
# ---------------------------------------------------------------------------


class TestApiSnapshot:
    async def test_returns_200(self, client: AsyncClient) -> None:
        async with client as c:
            response = await c.get("/api/snapshot")
        assert response.status_code == 200

    async def test_content_type_is_json(self, client: AsyncClient) -> None:
        async with client as c:
            response = await c.get("/api/snapshot")
        assert "application/json" in response.headers["content-type"]

    async def test_response_is_valid_monitor_snapshot(self, client: AsyncClient) -> None:
        """The JSON must deserialize into a MonitorSnapshot without errors."""
        async with client as c:
            response = await c.get("/api/snapshot")
        snap = MonitorSnapshot.model_validate(response.json())
        assert snap.channel == CHANNEL

    async def test_required_fields_present(self, client: AsyncClient) -> None:
        async with client as c:
            data = (await c.get("/api/snapshot")).json()
        required = {
            "channel",
            "status",
            "uptime_seconds",
            "segments_total",
            "segments_failed",
            "median_latency_ms",
            "recent_incidents",
            "timestamp_utc",
        }
        assert required.issubset(data.keys())

    async def test_status_is_valid_health_status(self, client: AsyncClient) -> None:
        async with client as c:
            data = (await c.get("/api/snapshot")).json()
        assert data["status"] in ("healthy", "degraded", "down")

    async def test_segments_total_is_non_negative(self, client: AsyncClient) -> None:
        async with client as c:
            data = (await c.get("/api/snapshot")).json()
        assert data["segments_total"] >= 0

    async def test_recent_incidents_is_list(self, client: AsyncClient) -> None:
        async with client as c:
            data = (await c.get("/api/snapshot")).json()
        assert isinstance(data["recent_incidents"], list)


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------


class TestStaticFiles:
    async def test_app_js_is_served(self, client: AsyncClient) -> None:
        async with client as c:
            response = await c.get("/static/app.js")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"].lower() or "text" in response.headers["content-type"].lower()

    async def test_app_js_not_empty(self, client: AsyncClient) -> None:
        async with client as c:
            response = await c.get("/static/app.js")
        assert len(response.text) > 100
