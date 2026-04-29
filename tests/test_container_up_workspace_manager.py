from __future__ import annotations

from pathlib import Path

from container_up.workspace_manager import WorkspaceManager


def test_workspace_manager_uses_frontend_scoped_paths(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")

    workspace = manager.get_or_create_workspace("feishu-main", "user-1")

    assert workspace == tmp_path / "workspaces" / "feishu-main" / "user-1"
    assert workspace.is_dir()
