# twitch-stream-healthcheck

> Is this stream working right now, and if not, why?

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![CI](https://img.shields.io/github/actions/workflow/status/your-org/twitch-stream-healthcheck/ci.yml?label=CI)

**Status: Work in progress**

## Installation

```bash
# TODO
```

## Usage

```bash
# Quick health check
twitch-healthcheck check <channel>

# Continuous monitoring
twitch-healthcheck monitor <channel> --duration 120 --output report.json

# Live dashboard
uvicorn dashboard.api:app --reload
```

## How it works

<!-- TODO: HLS parsing, segment timing, anomaly detection -->

## Development

```bash
pip install -e ".[dev]"
```

## Tests

```bash
pytest
ruff check src/ tests/
mypy src/
```
