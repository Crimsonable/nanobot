"""Start the runtime for a scheduler-assigned bucket and frontend."""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from agent_backend.config import load_frontends


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="OpenCode bucket runtime")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--bucket-id", required=True)
    parser.add_argument("--frontend-id", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    frontends = load_frontends(args.config)
    config = frontends.runtime_config(args.config, args.bucket_id, args.frontend_id)

    from bucket_runtime.server import serve

    asyncio.run(serve(config))


if __name__ == "__main__":
    main()
