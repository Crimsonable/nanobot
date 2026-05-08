from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bucket_runtime.config import COMMON_ROOT

FRONTENDS_CONFIG_PATH = COMMON_ROOT / "frontends.json"


@dataclass(frozen=True)
class FrontendConfig:
    id: str
    raw: dict[str, Any]

    @property
    def safe_id(self) -> str:
        return safe_frontend_id(self.id)

    @property
    def common_root(self) -> Path:
        return COMMON_ROOT / self.safe_id

    @property
    def config_path(self) -> Path:
        return self.common_root / "config.json"

    @property
    def builtin_skills_dir(self) -> Path:
        return self.common_root / "skills"

    @property
    def template_dir(self) -> Path:
        return self.common_root / "templates"


def safe_frontend_id(frontend_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", frontend_id).strip("-.") or "frontend"


def load_frontend_configs() -> dict[str, FrontendConfig]:
    if not FRONTENDS_CONFIG_PATH.exists():
        return {}
    with FRONTENDS_CONFIG_PATH.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    frontends = payload.get("frontends", [])
    if not isinstance(frontends, list):
        raise RuntimeError("frontends config field 'frontends' must be a list")

    configs: dict[str, FrontendConfig] = {}
    for item in frontends:
        if not isinstance(item, dict):
            raise RuntimeError("frontends config entries must be objects")
        frontend_id = str(item.get("id") or "").strip()
        if not frontend_id:
            raise RuntimeError("frontends config entry missing id")
        if frontend_id in configs:
            raise RuntimeError(f"duplicate frontend id in frontends config: {frontend_id}")
        configs[frontend_id] = FrontendConfig(
            id=frontend_id,
            raw=dict(item),
        )
    return configs


def frontend_config_for(frontend_id: str | None) -> FrontendConfig | None:
    configs = load_frontend_configs()
    requested = str(frontend_id or "").strip()
    if requested:
        config = configs.get(requested)
        if config is None:
            raise RuntimeError(f"frontend not configured in frontends config: {requested}")
        return config
    if len(configs) == 1:
        return next(iter(configs.values()))
    if configs:
        raise RuntimeError("frontend_id is required when multiple frontends are configured")
    return None
