from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from container_up.settings import (
    CONTAINER_UP_LOG_COMPRESSION,
    CONTAINER_UP_LOG_DIR,
    CONTAINER_UP_LOG_LEVEL,
    CONTAINER_UP_LOG_RETENTION,
)

_CONFIGURED = False


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_dir = Path(CONTAINER_UP_LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()
    logger.add(
        sys.stderr,
        level=CONTAINER_UP_LOG_LEVEL,
        colorize=None,
        enqueue=True,
        backtrace=True,
        diagnose=False,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss} | "
            "{level:<8} | "
            "{name}:{function}:{line} | "
            "{message}"
        ),
    )
    logger.add(
        log_dir / "app.{time:YYYY-MM-DD}.log",
        level=CONTAINER_UP_LOG_LEVEL,
        rotation="00:00",
        retention=CONTAINER_UP_LOG_RETENTION,
        compression=CONTAINER_UP_LOG_COMPRESSION or None,
        enqueue=True,
        backtrace=True,
        diagnose=False,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level:<8} | "
            "{process.id}:{thread.id} | "
            "{name}:{function}:{line} | "
            "{message}"
        ),
    )
    _CONFIGURED = True
