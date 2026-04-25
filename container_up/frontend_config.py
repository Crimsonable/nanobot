from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from container_up.settings import FRONTENDS_CONFIG_PATH

FRONTEND_ORG_SEPARATOR = "::"


@dataclass(frozen=True)
class FrontendConfig:
    id: str
    raw: dict[str, Any]
    common_root: Path | None = None
    config_path: Path | None = None
    builtin_skills_dir: Path | None = None
    template_dir: Path | None = None

    @property
    def provider(self) -> str:
        return str(self.raw.get("provider") or "").strip()

    @property
    def safe_id(self) -> str:
        return safe_frontend_id(self.id)


def _optional_path(value: Any) -> Path | None:
    text = str(value or "").strip()
    return Path(text).expanduser() if text else None


def _frontends_config_path() -> Path:
    override = str(os.getenv("FRONTENDS_CONFIG_PATH") or "").strip()
    if override:
        return Path(override).expanduser()
    legacy = str(os.getenv("CONTAINER_UP_CONFIG_PATH") or "").strip()
    if legacy:
        return Path(legacy).expanduser()
    return FRONTENDS_CONFIG_PATH


def _resolve_frontend_paths(item: dict[str, Any]) -> tuple[Path | None, Path | None, Path | None, Path | None]:
    common_root = _optional_path(item.get("common_root", item.get("COMMON_ROOT")))
    config_path = _optional_path(item.get("config_path", item.get("CONFIG_PATH")))
    builtin_skills_dir = _optional_path(
        item.get("builtin_skills_dir", item.get("BUILTIN_SKILLS_DIR"))
    )
    template_dir = _optional_path(item.get("template_dir", item.get("TEMPLATE_DIR")))

    if common_root is not None:
        config_path = config_path or (common_root / "config.json")
        builtin_skills_dir = builtin_skills_dir or (common_root / "skills")
        template_dir = template_dir or (common_root / "templates")

    return common_root, config_path, builtin_skills_dir, template_dir


def safe_frontend_id(frontend_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", frontend_id).strip("-.") or "frontend"


def compose_frontend_org_id(frontend_id: str | None, org_id: str) -> str:
    frontend = str(frontend_id or "").strip()
    base_org_id = str(org_id or "").strip()
    if not frontend or not base_org_id:
        return base_org_id
    safe_frontend = safe_frontend_id(frontend)
    prefix = f"{safe_frontend}{FRONTEND_ORG_SEPARATOR}"
    if base_org_id.startswith(prefix):
        return base_org_id
    return f"{prefix}{base_org_id}"


def split_frontend_org_id(org_id: str) -> tuple[str | None, str]:
    text = str(org_id or "").strip()
    frontend_id, separator, external_org_id = text.partition(FRONTEND_ORG_SEPARATOR)
    if not separator or not frontend_id or not external_org_id:
        return None, text
    return frontend_id, external_org_id


def load_frontend_configs() -> dict[str, FrontendConfig]:
    config_path = _frontends_config_path()
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as f:
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
            raise RuntimeError(
                f"duplicate frontend id in frontends config: {frontend_id}"
            )
        common_root, config_path, builtin_skills_dir, template_dir = _resolve_frontend_paths(
            item
        )
        configs[frontend_id] = FrontendConfig(
            id=frontend_id,
            raw=dict(item),
            common_root=common_root,
            config_path=config_path,
            builtin_skills_dir=builtin_skills_dir,
            template_dir=template_dir,
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
        raise RuntimeError(
            "frontend_id is required when multiple frontends are configured"
        )
    return None
