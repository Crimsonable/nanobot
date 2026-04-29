from __future__ import annotations

from pathlib import Path

from container_up import attachments
from container_up import attachment_paths


def test_persist_attachment_bytes_returns_absolute_local_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(attachment_paths, "HOST_WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(attachment_paths, "CHILD_WORKSPACE_TARGET", "/app/nanobot_workspaces")

    saved = attachments.persist_attachment_bytes(
        user_id="user-1",
        data=b"hello",
        filename="demo file.txt",
        provider="feishu",
        attachment_group="conv-1",
        frontend_id="feishu-main",
    )

    path = Path(saved)
    assert path.is_absolute()
    assert str(path).startswith("/app/nanobot_workspaces/feishu-main/user-1/")
    assert "feishu" in saved
    assert "cache/attachments" in saved

    host_workspace = attachment_paths.host_instance_workspace_path(
        "user-1",
        frontend_id="feishu-main",
    )
    host_file = (
        tmp_path
        / host_workspace.relative_to(tmp_path)
        / "cache"
        / "attachments"
        / "feishu"
        / attachment_paths.safe_instance_name("conv-1")
        / path.name
    )
    assert host_file.is_file()
    assert host_file.read_bytes() == b"hello"
    assert "feishu" in saved
