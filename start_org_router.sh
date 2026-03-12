#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
ORG_TEMPLATE_CONFIG="${ORG_TEMPLATE_CONFIG:-/app/nanobot_workspaces/config.json}"

if ! command -v python >/dev/null 2>&1; then
  echo "python is required but not found in PATH" >&2
  exit 1
fi

if [[ ! -f "$ORG_TEMPLATE_CONFIG" ]]; then
  echo "org template config not found: $ORG_TEMPLATE_CONFIG" >&2
  exit 1
fi

export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec python -m nanobot.org_router
