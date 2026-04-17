from __future__ import annotations

import logging
from typing import Any

from container_up.frontend_config import FrontendConfig, load_frontend_configs
from container_up.qxt_im_tool import QxtIMParser, build_im_receive_event
from container_up.settings import IM_PROVIDER


logger = logging.getLogger(__name__)
_im_manager: "IMManager | None" = None


class IMManager:
    def __init__(self, parsers: dict[str, Any]) -> None:
        self.parsers = parsers

    @property
    def frontend_ids(self) -> list[str]:
        return list(self.parsers)

    def start(self) -> None:
        for parser in self.parsers.values():
            parser.start()

    def stop(self) -> None:
        for parser in self.parsers.values():
            parser.stop()

    def parser_for_frontend(self, frontend_id: str | None = None) -> Any:
        requested = str(frontend_id or "").strip()
        if requested:
            parser = self.parsers.get(requested)
            if parser is None:
                raise RuntimeError(f"IM frontend is not initialized: {requested}")
            return parser
        if len(self.parsers) == 1:
            return next(iter(self.parsers.values()))
        parser = self.parsers.get("default")
        if parser is not None:
            return parser
        raise RuntimeError("frontend_id is required when multiple IM frontends are configured")

    def parser_for_outbound(self, metadata: dict[str, Any]) -> Any:
        reply_target = dict(metadata.get("reply_target") or {})
        return self.parser_for_frontend(
            str(reply_target.get("frontend_id") or metadata.get("frontend_id") or "").strip()
            or None
        )


def _parser_for_config(
    config: FrontendConfig,
    **kwargs: Any,
) -> Any:
    provider = (config.provider or IM_PROVIDER).strip().lower() or "qxt"
    if provider == "feishu":
        from container_up.feishu_im_tool import FeishuIMParser

        return FeishuIMParser(
            frontend_id=config.id,
            frontend_config=config.raw,
            **kwargs,
        )
    return QxtIMParser(
        frontend_id=config.id,
        frontend_config=config.raw,
        **kwargs,
    )


def init_im_parser(**kwargs: Any) -> IMManager:
    global _im_manager
    if _im_manager is not None:
        try:
            _im_manager.stop()
        except Exception:
            logger.exception("failed to stop existing IM manager")

    configs = load_frontend_configs()
    if configs:
        parsers = {
            frontend_id: _parser_for_config(config, **kwargs)
            for frontend_id, config in configs.items()
        }
    else:
        parsers = {
            "default": _parser_for_config(
                FrontendConfig(id="default", raw={"provider": IM_PROVIDER}),
                **kwargs,
            )
        }
    _im_manager = IMManager(parsers)
    return _im_manager


def get_im_manager() -> IMManager:
    if _im_manager is None:
        raise RuntimeError("IM manager is not initialized")
    return _im_manager


def get_im_parser(frontend_id: str | None = None) -> Any:
    return get_im_manager().parser_for_frontend(frontend_id)
