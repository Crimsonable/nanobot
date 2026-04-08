from __future__ import annotations

from pathlib import Path

import pytest

from container_up import attachment_paths


def test_normalize_outbound_attachments_maps_child_path_to_host_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(attachment_paths, "CHILD_WORKSPACE_TARGET", "/app/nanobot_workspaces")
    monkeypatch.setattr(attachment_paths, "HOST_WORKSPACE_ROOT", Path("/data/nanobot_backend/workspace"))

    attachments = attachment_paths.normalize_outbound_attachments(
        "org-1",
        ["/app/nanobot_workspaces/user-a/report 1.png", "https://already.example/x.png"],
    )

    assert attachments == [
        str(Path("/data/nanobot_backend/workspace/org-1/user-a/report 1.png")),
        "https://already.example/x.png",
    ]


def test_normalize_outbound_attachments_preserves_dict_and_filename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(attachment_paths, "CHILD_WORKSPACE_TARGET", "/app/nanobot_workspaces")
    monkeypatch.setattr(attachment_paths, "HOST_WORKSPACE_ROOT", Path("/data/nanobot_backend/workspace"))

    attachments = attachment_paths.normalize_outbound_attachments(
        "org-1",
        [
            {
                "url": "/app/nanobot_workspaces/user-a/report.txt",
                "filename": "custom.txt",
            }
        ],
    )

    assert attachments == [
        {
            "url": str(Path("/data/nanobot_backend/workspace/org-1/user-a/report.txt")),
            "filename": "custom.txt",
        }
    ]
