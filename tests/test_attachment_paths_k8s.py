from __future__ import annotations

from pathlib import Path

from container_up import attachment_paths
from container_up import attachments


def test_frontend_user_layout_uses_frontend_scoped_workspace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(attachment_paths, "HOST_WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(attachment_paths, "CHILD_WORKSPACE_TARGET", "/mnt/nanobot/workspaces")
    monkeypatch.setattr(attachment_paths, "WORKSPACE_LAYOUT", "frontend-user")

    saved = attachments.persist_attachment_bytes(
        user_id="user-1",
        frontend_id="feishu-main",
        data=b"hello",
        filename="demo.txt",
        provider="feishu",
        attachment_group="chat-1",
    )

    saved_path = Path(saved)
    assert str(saved_path).startswith("/mnt/nanobot/workspaces/feishu-main/user-1/")
    host_file = tmp_path / "feishu-main" / "user-1" / "cache" / "attachments" / "feishu"
    assert any(item.is_file() for item in host_file.rglob("*"))


def test_normalize_outbound_attachments_frontend_user_layout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(attachment_paths, "HOST_WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(attachment_paths, "CHILD_WORKSPACE_TARGET", "/mnt/nanobot/workspaces")
    monkeypatch.setattr(attachment_paths, "WORKSPACE_LAYOUT", "frontend-user")

    result = attachment_paths.normalize_outbound_attachments(
        ["/mnt/nanobot/workspaces/feishu-main/user-1/report.txt"],
        frontend_id="feishu-main",
    )

    assert result == [str(tmp_path / "feishu-main" / "user-1" / "report.txt")]
