"""Start the external channel gateway."""
import argparse
import logging
from pathlib import Path

from agent_backend.config import GatewaySettings, load_frontends
from agent_backend.logging_config import access_log_config


def main(argv=None):
    parser = argparse.ArgumentParser(description="Channel gateway")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    registry = load_frontends(args.config)
    settings = GatewaySettings()
    import uvicorn

    from channel_gateway.server import create_app
    uvicorn.run(create_app(registry.control_config(), settings), host=settings.host,
                port=settings.port, workers=1, log_config=access_log_config())


if __name__ == "__main__":
    main()
