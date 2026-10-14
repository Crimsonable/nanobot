from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from agent_backend.config import load_frontends


def main():
    parser = argparse.ArgumentParser(description="OpenCode channel control / bucket runtime")
    parser.add_argument("service", choices=["control", "runtime", "check"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--bucket-id", help="Scheduler-issued bucket ID; inferred for a single static bucket")
    parser.add_argument("--frontend-id", help="Frontend assigned to this bucket")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    frontend = load_frontends(args.config)
    if args.service == "control":
        import uvicorn

        from container_up.server import create_app
        config = frontend.control_config()
        config.frontend_config_path = Path("/mnt/common") / args.config.name
        uvicorn.run(create_app(config), host=config.host, port=config.port, workers=1)
    elif args.service == "runtime":
        from bucket_runtime.server import serve
        asyncio.run(serve(frontend.runtime_config(args.config, args.bucket_id, args.frontend_id)))
    else:
        frontend.control_config()
        for entry in frontend.frontends:
            for bucket_id in frontend.control.bucket_ids:
                frontend.runtime_config(args.config, bucket_id, entry.frontend_id)
            if frontend.control.bucket_pool:
                frontend.runtime_config(args.config, frontend.control.bucket_pool.name_prefix + "-000000000000", entry.frontend_id)
        print("Configuration schema valid (credentials and runtime capabilities not probed).")


if __name__ == "__main__":
    main()
