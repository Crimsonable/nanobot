from __future__ import annotations

from pathlib import Path

from container_up.frontend_config import safe_frontend_id


def safe_user_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value).strip(
        "-."
    )
    return cleaned[:96] or "user"


class WorkspaceManager:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root

    def workspace_path(self, frontend_id: str, user_id: str) -> Path:
        return (
            self.workspace_root
            / safe_frontend_id(frontend_id)
            / safe_user_id(user_id)
        ).resolve(strict=False)

    def get_or_create_workspace(self, frontend_id: str, user_id: str) -> Path:
        workspace = self.workspace_path(frontend_id, user_id)
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace
