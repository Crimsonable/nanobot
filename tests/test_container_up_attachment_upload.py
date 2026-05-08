from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from container_up.app import UploadAttachmentRequest, post_upload_attachment
from nanobot.utils.document import _upload_local_attachment_ref


@pytest.mark.asyncio
async def test_container_up_upload_endpoint_returns_uploaded_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_frontend_config_for(frontend_id: str):
        captured["frontend_id_lookup"] = frontend_id
        return type("Frontend", (), {"id": frontend_id, "raw": {"attachment_storage": {}}})()

    def fake_upload_local_attachment(
        *,
        frontend_id: str,
        user_id: str,
        local_path: str,
        frontend_config: dict[str, object] | None = None,
    ) -> dict[str, object]:
        captured["frontend_id"] = frontend_id
        captured["user_id"] = user_id
        captured["local_path"] = local_path
        captured["frontend_config"] = frontend_config
        return {
            "url": "https://files.example.com/demo.png",
            "filename": "demo.png",
            "content_type": "image/png",
        }

    monkeypatch.setattr("container_up.app.frontend_config_for", fake_frontend_config_for)
    monkeypatch.setattr("container_up.app.upload_local_attachment", fake_upload_local_attachment)

    result = await post_upload_attachment(
        UploadAttachmentRequest(
            frontend_id="feishu-main",
            user_id="user-1",
            local_path=str(tmp_path / "demo.png"),
        )
    )

    assert result["url"] == "https://files.example.com/demo.png"
    assert captured == {
        "frontend_id_lookup": "feishu-main",
        "frontend_id": "feishu-main",
        "user_id": "user-1",
        "local_path": str(tmp_path / "demo.png"),
        "frontend_config": {"attachment_storage": {}},
    }


@pytest.mark.asyncio
async def test_container_up_upload_endpoint_maps_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "container_up.app.frontend_config_for",
        lambda frontend_id: type("Frontend", (), {"id": frontend_id, "raw": {}})(),
    )

    def fake_upload_local_attachment(**kwargs):
        raise RuntimeError("minio is not configured")

    monkeypatch.setattr("container_up.app.upload_local_attachment", fake_upload_local_attachment)

    with pytest.raises(HTTPException) as exc_info:
        await post_upload_attachment(
            UploadAttachmentRequest(
                frontend_id="feishu-main",
                user_id="user-1",
                local_path=str(tmp_path / "demo.png"),
            )
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "minio is not configured"


def test_document_upload_local_attachment_ref_calls_container_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    attachment = tmp_path / "report.pdf"
    attachment.write_text("hello", encoding="utf-8")
    captured: dict[str, object] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"url": "https://files.example.com/report.pdf"}

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            captured["client_kwargs"] = kwargs

        def __enter__(self) -> "_FakeClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def post(self, url: str, json: dict[str, object]) -> _FakeResponse:
            captured["url"] = url
            captured["json"] = json
            return _FakeResponse()

    monkeypatch.setenv(
        "CONTAINER_UP_ATTACHMENT_UPLOAD_URL",
        "http://127.0.0.1:18080/internal/attachments/upload",
    )
    monkeypatch.setattr("nanobot.utils.document.httpx.Client", _FakeClient)

    result = _upload_local_attachment_ref(
        attachment,
        metadata={"frontend_id": "feishu-main", "usr_id": "user-1"},
    )

    assert result == "https://files.example.com/report.pdf"
    assert captured["url"] == "http://127.0.0.1:18080/internal/attachments/upload"
    assert captured["json"] == {
        "frontend_id": "feishu-main",
        "user_id": "user-1",
        "local_path": str(attachment),
    }
