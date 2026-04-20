"""FastAPI app with WebSocket push of live stream metrics.

Environment variables:
    TWITCH_CHANNEL   Channel to monitor (default: "kaicenat").

Endpoints:
    GET  /               Serves dashboard/static/index.html
    GET  /api/snapshot   Current MonitorSnapshot as JSON
    WS   /ws/metrics     Pushes a MonitorSnapshot every second
"""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from twitch_healthcheck.monitor import StreamMonitor

STATIC_DIR = Path(__file__).parent / "static"
CHANNEL = os.environ.get("TWITCH_CHANNEL", "kaicenat")

# Created at module load so snapshot() is always available, even before startup.
_monitor = StreamMonitor(CHANNEL)
_task: asyncio.Task | None = None  # type: ignore[type-arg]


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    """Start the monitor on startup, stop it on shutdown."""
    global _task
    _task = asyncio.create_task(_monitor.start())
    try:
        yield
    finally:
        await _monitor.stop()
        if _task is not None:
            try:
                await _task
            except Exception:
                # Swallow StreamOfflineError / TwitchAPIError so the app shuts
                # down cleanly even when the channel was never reachable.
                pass


app = FastAPI(title="Twitch Stream Monitor", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Serve the single-page dashboard."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/snapshot")
async def api_snapshot() -> dict:  # type: ignore[type-arg]
    """Return the current monitor state as a JSON object."""
    return _monitor.snapshot().model_dump(mode="json")


@app.websocket("/ws/metrics")
async def ws_metrics(websocket: WebSocket) -> None:
    """Push a MonitorSnapshot JSON blob every second while the client is connected."""
    await websocket.accept()
    try:
        while True:
            snap = _monitor.snapshot()
            await websocket.send_text(snap.model_dump_json())
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
