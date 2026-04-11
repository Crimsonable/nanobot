from __future__ import annotations

import logging
from typing import Any

from container_up.qxt_im_tool import QxtIMParser, build_im_receive_event
from container_up.settings import IM_PROVIDER


logger = logging.getLogger(__name__)
_im_parser: Any | None = None


def init_im_parser(**kwargs: Any) -> Any:
    global _im_parser
    if _im_parser is not None:
        try:
            _im_parser.stop()
        except Exception:
            logger.exception("failed to stop existing IM parser")

    if IM_PROVIDER == "feishu":
        from container_up.feishu_im_tool import FeishuIMParser

        _im_parser = FeishuIMParser(**kwargs)
    else:
        _im_parser = QxtIMParser(**kwargs)
    return _im_parser


def get_im_parser() -> Any:
    if _im_parser is None:
        raise RuntimeError("IM parser is not initialized")
    return _im_parser
