from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from container_up.settings import CONTAINER_UP_CONFIG_PATH

FRONTEND_ORG_SEPARATOR = "?"


@dataclass(frozen=True)
class FrontendConfig:
    id: str
    raw: dict[str, Any]
    builtin_skills_dir: Path | None = None
    template_dir: Path | None = None

    @property
    def provider(self) -> str:
        return str(self.raw.get("provider") or "").strip()

    @property
    def safe_id(self) -> str:
        return safe_frontend_id(self.id)

    @property
    def child_builtin_skills_dir(self) -> str:
        return f"/app/frontend_mounts/{self.safe_id}/skills"

    @property
    def child_template_dir(self) -> str:
        return f"/app/frontend_mounts/{self.safe_id}/templates"


def _optional_path(value: Any) -> Path | None:
    text = str(value or "").strip()
    return Path(text).expanduser() if text else None


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


def split_frontend_org_id(org_id: str) -> tuple[str | None, str]:
    text = str(org_id or "").strip()
    frontend_id, separator, external_org_id = text.partition(FRONTEND_ORG_SEPARATOR)
    if not separator or not frontend_id or not external_org_id:
        return None, text
    return frontend_id, external_org_id


def split_frontend_org_id(org_id: str) -> tuple[str | None, str]:
    text = str(org_id or "").strip()
    frontend_id, separator, external_org_id = text.partition(FRONTEND_ORG_SEPARATOR)
    if not separator or not frontend_id or not external_org_id:
        return None, text
    return frontend_id, external_org_id


def load_frontend_configs() -> dict[str, FrontendConfig]:
    if not CONTAINER_UP_CONFIG_PATH.exists():
        return {}
    with CONTAINER_UP_CONFIG_PATH.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    frontends = payload.get("frontends", [])
    if not isinstance(frontends, list):
        raise RuntimeError("container_up.json field 'frontends' must be a list")

    configs: dict[str, FrontendConfig] = {}
    for item in frontends:
        if not isinstance(item, dict):
            raise RuntimeError("container_up.json frontends entries must be objects")
        frontend_id = str(item.get("id") or "").strip()
        if not frontend_id:
            raise RuntimeError("container_up.json frontend entry missing id")
        if frontend_id in configs:
            raise RuntimeError(
                f"duplicate frontend id in container_up.json: {frontend_id}"
            )
        configs[frontend_id] = FrontendConfig(
            id=frontend_id,
            raw=dict(item),
            builtin_skills_dir=_optional_path(
                item.get("builtin_skills_dir", item.get("BUILTIN_SKILLS_DIR"))
            ),
            template_dir=_optional_path(
                item.get("template_dir", item.get("TEMPLATE_DIR"))
            ),
        )
    return configs


def frontend_config_for(frontend_id: str | None) -> FrontendConfig | None:
    configs = load_frontend_configs()
    requested = str(frontend_id or "").strip()
    if requested:
        config = configs.get(requested)
        if config is None:
            raise RuntimeError(
                f"frontend not configured in container_up.json: {requested}"
            )
        return config
    if len(configs) == 1:
        return next(iter(configs.values()))
    if configs:
        raise RuntimeError(
            "frontend_id is required when multiple frontends are configured in container_up.json"
        )
    return None
