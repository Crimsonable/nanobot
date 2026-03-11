"""CLI entrypoint for the standalone FastAPI bridge service."""

from __future__ import annotations

import argparse

import uvicorn

from bridge_service.app import create_app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone bridge service for nanobot bridge channel")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--token", default="")
    parser.add_argument("--log-level", default="info")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    uvicorn.run(
        create_app(token=args.token or None),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
