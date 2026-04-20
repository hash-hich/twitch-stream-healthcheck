#!/usr/bin/env bash
# Run the Twitch Stream Monitor dashboard.
#
# Environment variables:
#   TWITCH_CHANNEL   Channel to monitor (default: kaicenat)
#   PORT             Port to listen on  (default: 8000)
#
# Usage:
#   TWITCH_CHANNEL=kaicenat ./scripts/run_dashboard.sh

set -euo pipefail

PORT="${PORT:-8000}"
TWITCH_CHANNEL="${TWITCH_CHANNEL:-kaicenat}"

export TWITCH_CHANNEL

echo "Starting dashboard for channel: ${TWITCH_CHANNEL} on port ${PORT}"
uvicorn dashboard.api:app --reload --port "${PORT}" --host 0.0.0.0
