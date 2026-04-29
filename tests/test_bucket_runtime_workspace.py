from __future__ import annotations

from bucket_runtime.port_allocator import PortAllocator
from bucket_runtime.workspace_manager import WorkspaceManager


def test_workspace_manager_initializes_from_templates(tmp_path) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "AGENTS.md").write_text("template", encoding="utf-8")

    manager = WorkspaceManager()
    workspace = manager.ensure_workspace(
        tmp_path / "workspaces" / "user-1",
        template_root=templates,
    )

    assert workspace == tmp_path / "workspaces" / "user-1"
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == "template"
    assert (workspace / ".workspace_initialized").is_file()


def test_port_allocator_reuses_released_ports() -> None:
    allocator = PortAllocator(20000, 20002)
    port = allocator.allocate("user-1")
    allocator.release("user-1")

    assert allocator.allocate("user-2") == port
