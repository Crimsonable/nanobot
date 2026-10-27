"""Start CU, or validate the frontend registry without starting services."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from agent_backend.config import load_frontends
from agent_backend.logging_config import access_log_config


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Bucket control service")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check-config", action="store_true",
                        help="Validate all frontend configurations and exit without starting CU")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    frontends = load_frontends(args.config)
    config = frontends.control_config()
    if args.check_config:
        for entry in frontends.frontends:
            bucket_id = frontends.control.bucket_pool.name_prefix + "-000000000000"
            frontends.runtime_config(args.config, bucket_id, entry.frontend_id)
        print("Configuration schema valid (credentials and runtime capabilities not probed).")
        return

    import uvicorn

    from container_up.server import create_app

    config.frontend_config_path = Path("/mnt/common") / args.config.name
    uvicorn.run(create_app(config), host=config.host, port=config.port, workers=1,
                log_config=access_log_config())


if __name__ == "__main__":
    main()
