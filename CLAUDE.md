# CLAUDE.md

This file provides guidance to Claude Code when working on this repository.

## Project Overview

`twitch-stream-healthcheck` is a Python CLI and dashboard that monitors the
health of a live Twitch stream in real time. It answers a simple question:
"Is this stream working right now, and if not, why?"

It parses the HLS playlist, downloads and times individual segments, and
reports on stalls, bitrate drops, segment gaps, and HTTP errors through a
structured log, a JSON report, a rich-powered CLI, and a FastAPI + Chart.js
web dashboard.

## Architecture

src/twitch_healthcheck/
- cli.py          Typer CLI entrypoint (check, monitor, report commands)
- twitch_api.py   Twitch GraphQL + HLS playlist URL resolution
- hls.py          HLS master + media playlist parser
- monitor.py      Async monitoring loop: fetches and times segments
- detectors.py    Anomaly detection: stalls, bitrate drops, gaps, errors
- report.py       Output formatting: JSON, rich console
- models.py       Pydantic models shared across modules

dashboard/
- api.py          FastAPI app with WebSocket push of metrics
- static/         HTML + Tailwind + Chart.js dashboard

tests/
- fixtures/       Real m3u8 samples and mocked responses
- test_*.py       Pytest, one file per core module

## Development Conventions

- Python 3.11+, strict type hints, pydantic v2 for all data crossing module
  boundaries.
- Async-first for network I/O (httpx + asyncio). The monitor runs in a single
  event loop, not threads.
- One module = one responsibility. If a file grows past ~200 lines, split it.
- Public functions have docstrings with Args / Returns / Raises.
- Errors are explicit: raise domain exceptions (StreamOfflineError,
  PlaylistParseError, TwitchAPIError), never swallow. Log with context.
- Timestamps are always UTC, ISO 8601, timezone-aware.
- Network calls have explicit timeouts. No unbounded waits.
- HTTP clients use httpx.AsyncClient with connection pooling.

## Testing

pytest                          # Run all tests
pytest -k "hls"                 # Run only HLS parser tests
pytest -x --lf                  # Stop at first failure, rerun last failed
ruff check src/ tests/          # Lint
ruff format src/ tests/         # Format
mypy src/                       # Type check

HTTP calls in tests are mocked with respx. Fixtures are in tests/fixtures/.

## Running

# CLI: quick health check on a channel
twitch-healthcheck check kaicenat

# CLI: continuous monitoring, output to JSON
twitch-healthcheck monitor kaicenat --duration 120 --output report.json

# Dashboard: live visualization
uvicorn dashboard.api:app --reload

## Style Preferences

- Prefer explicit over clever. A senior engineer should read a module
  top-to-bottom and understand it in one pass.
- Log decisions, not just errors. "Selected variant 1080p60 (6000 kbps)"
  is useful; "monitor started" alone is not.
- Dashboard: avoid clutter. 4 key metrics visible at all times, rest on demand.
- Thresholds for degraded/critical status must be configurable, not hardcoded.

## What This Project Does NOT Do

- It does not download or persist video content. Segments are fetched, timed,
  then discarded.
- It does not decode frames. Quality checks stay at the transport layer.
- It does not support Twitch VODs (past broadcasts). Live streams only.
- It does not implement alerting (webhooks, email). Output is machine-readable,
  alerting is a downstream concern.
- It does not require Twitch authentication. Public playback tokens only.

