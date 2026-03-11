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

GATEWAY_PORT="${GATEWAY_PORT:-}"
export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"
echo "Starting nanobot gateway with config $CONFIG_PATH"
if [[ -n "$GATEWAY_PORT" ]]; then
  exec nanobot gateway --config "$CONFIG_PATH" --workspace /app/workspace --port "$GATEWAY_PORT"
else
  exec nanobot gateway --config "$CONFIG_PATH" --workspace /app/workspace
fi
