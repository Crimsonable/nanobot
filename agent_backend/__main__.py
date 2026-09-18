from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from agent_backend.config import ControlConfig, RuntimeConfig, load_config


def main():
    parser = argparse.ArgumentParser(description="OpenCode channel control / bucket runtime")
    parser.add_argument("service", choices=["control", "runtime", "check"])
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.service == "control":
        import uvicorn

        from container_up.server import create_app
        config = load_config(args.config, ControlConfig)
        uvicorn.run(create_app(config), host=config.host, port=config.port, workers=1)
    elif args.service == "runtime":
        from bucket_runtime.server import serve
        asyncio.run(serve(load_config(args.config, RuntimeConfig)))
    else:
        import yaml
        raw = yaml.safe_load(args.config.read_text(encoding="utf-8"))
        schema = RuntimeConfig if "opencode" in raw else ControlConfig
        schema.model_validate(raw)
        print("Configuration schema valid (credentials and runtime capabilities not probed).")


if __name__ == "__main__":
    main()
