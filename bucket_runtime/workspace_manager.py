from __future__ import annotations

import shutil
from pathlib import Path

from container_up.frontend_config import safe_frontend_id


def safe_user_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value).strip(
        "-."
    )
    return cleaned[:96] or "user"


class WorkspaceManager:
    def __init__(self, workspace_root: Path, templates_root: Path) -> None:
        self.workspace_root = workspace_root
        self.templates_root = templates_root

    def workspace_path(self, frontend_id: str, user_id: str) -> Path:
        return (
            self.workspace_root
            / safe_frontend_id(frontend_id)
            / safe_user_id(user_id)
        ).resolve(strict=False)

    def ensure_workspace(
        self,
        frontend_id: str,
        user_id: str,
        template_root: Path | None = None,
    ) -> Path:
        workspace = self.workspace_path(frontend_id, user_id)
        init_flag = workspace / ".workspace_initialized"
        workspace.mkdir(parents=True, exist_ok=True)
        source_templates = template_root or self.templates_root
        if source_templates.exists() and not init_flag.exists():
            self._copy_templates(source_templates, workspace)
            init_flag.write_text("true\n", encoding="utf-8")
        return workspace

    def _copy_templates(self, src: Path, dst: Path) -> None:
        for item in src.iterdir():
            target = dst / item.name
            if target.exists():
                continue
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
