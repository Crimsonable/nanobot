#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_PATH="${1:-$ROOT_DIR/workspace/config.json}"

if ! command -v python >/dev/null 2>&1; then
  echo "python is required but not found in PATH" >&2
  exit 1
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "config not found: $CONFIG_PATH" >&2
  exit 1
fi

readarray -t BRIDGE_INFO < <(
  python3 - "$CONFIG_PATH" <<'PY'
import json
import sys
from urllib.parse import urlparse

config_path = sys.argv[1]
with open(config_path, "r", encoding="utf-8") as f:
    data = json.load(f)

bridge = data.get("channels", {}).get("bridge", {})
bridge_url = bridge.get("bridgeUrl", "ws://127.0.0.1:8766")
bridge_token = bridge.get("bridgeToken", "")
parsed = urlparse(bridge_url)

host = parsed.hostname or "127.0.0.1"
port = parsed.port or 8766
health_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host

print(host)
print(port)
print(bridge_token)
print(f"http://{health_host}:{port}/healthz")
PY
)

BRIDGE_HOST="${BRIDGE_INFO[0]}"
BRIDGE_PORT="${BRIDGE_INFO[1]}"
BRIDGE_TOKEN="${BRIDGE_INFO[2]}"
HEALTH_URL="${BRIDGE_INFO[3]}"

BRIDGE_BIND_HOST="${BRIDGE_BIND_HOST:-$BRIDGE_HOST}"
BRIDGE_BIND_PORT="${BRIDGE_BIND_PORT:-$BRIDGE_PORT}"
BRIDGE_TOKEN="${BRIDGE_TOKEN_OVERRIDE:-$BRIDGE_TOKEN}"
GATEWAY_PORT="${GATEWAY_PORT:-}"

if [[ "$BRIDGE_BIND_HOST" == "0.0.0.0" || "$BRIDGE_BIND_HOST" == "::" ]]; then
  HEALTH_HOST="127.0.0.1"
else
  HEALTH_HOST="$BRIDGE_BIND_HOST"
fi
HEALTH_URL="http://${HEALTH_HOST}:${BRIDGE_BIND_PORT}/healthz"

cleanup() {
  if [[ -n "${BRIDGE_PID:-}" ]] && kill -0 "$BRIDGE_PID" >/dev/null 2>&1; then
    kill "$BRIDGE_PID" >/dev/null 2>&1 || true
    wait "$BRIDGE_PID" 2>/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

echo "Starting bridge service on ${BRIDGE_BIND_HOST}:${BRIDGE_BIND_PORT}"
export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"
if [[ -n "$BRIDGE_TOKEN" ]]; then
  python -m bridge_service.server \
    --host "$BRIDGE_BIND_HOST" \
    --port "$BRIDGE_BIND_PORT" \
    --token "$BRIDGE_TOKEN" &
else
  python -m bridge_service.server \
    --host "$BRIDGE_BIND_HOST" \
    --port "$BRIDGE_BIND_PORT" &
fi
BRIDGE_PID=$!

echo "Waiting for bridge service to become ready"
for _ in $(seq 1 30); do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
  echo "bridge service failed to become ready: $HEALTH_URL" >&2
  exit 1
fi

echo "Starting nanobot gateway with config $CONFIG_PATH"
if [[ -n "$GATEWAY_PORT" ]]; then
  nanobot gateway --config "$CONFIG_PATH" --workspace /app/workspace --port "$GATEWAY_PORT"
else
  nanobot gateway --config "$CONFIG_PATH" --workspace /app/workspace
fi
