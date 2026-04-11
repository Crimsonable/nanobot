from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from container_up import attachment_paths
from container_up import dispatch


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


@pytest.mark.asyncio
async def test_parse_im_message_receive_keeps_qxt_attachment_as_url_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_submit_message(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(dispatch, "ensure_org_container", lambda _org_id: None)
    monkeypatch.setattr(dispatch, "touch_org", lambda _org_id: None)
    monkeypatch.setattr(
        dispatch,
        "get_bridge_hub",
        lambda: SimpleNamespace(submit_message=fake_submit_message),
    )
    monkeypatch.setattr(
        "container_up.attachments.ATTACHMENT_URL_PREFIX",
        "https://files.example.com/",
    )

    result = await dispatch.parse_im_message_receive(
        {
            "event_type": "im_message_receive",
            "event": {
                "org_id": "org-1",
                "conversation_id": "chat-1",
                "user_id": "user-1",
                "content": "https://files.example.com/report.pdf",
                "attachments": [],
                "metadata": {"provider": "qxt"},
            },
        }
    )

    assert result == {"ok": True, "response": {"ok": True}}
    assert captured["attachments"] == [
        {
            "url": "https://files.example.com/report.pdf",
            "source": "content_url",
        }
    ]


@pytest.mark.asyncio
async def test_parse_im_message_receive_skips_content_url_rebuild_when_materialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_submit_message(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(dispatch, "ensure_org_container", lambda _org_id: None)
    monkeypatch.setattr(dispatch, "touch_org", lambda _org_id: None)
    monkeypatch.setattr(
        dispatch,
        "get_bridge_hub",
        lambda: SimpleNamespace(submit_message=fake_submit_message),
    )
    monkeypatch.setattr(
        "container_up.attachments.ATTACHMENT_URL_PREFIX",
        "https://files.example.com/",
    )

    result = await dispatch.parse_im_message_receive(
        {
            "event_type": "im_message_receive",
            "event": {
                "org_id": "org-1",
                "conversation_id": "chat-1",
                "user_id": "user-1",
                "content": "https://files.example.com/report.pdf",
                "attachments": ["/app/nanobot_workspaces/user-1/cache/attachments/qxt/conv/report.pdf"],
                "metadata": {
                    "provider": "qxt",
                    "attachments_materialized": True,
                },
            },
        }
    )

    assert result == {"ok": True, "response": {"ok": True}}
    assert captured["attachments"] == [
        "/app/nanobot_workspaces/user-1/cache/attachments/qxt/conv/report.pdf"
    ]
