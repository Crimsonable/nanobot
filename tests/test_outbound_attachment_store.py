from __future__ import annotations

from pathlib import Path

import pytest

from container_up.outbound_attachment_store import (
    MinioAttachmentStore,
    prepare_outbound_attachments,
)


@pytest.mark.asyncio
async def test_prepare_outbound_attachments_keeps_local_paths_when_minio_disabled(
    tmp_path: Path,
) -> None:
    attachment = tmp_path / "report.txt"
    attachment.write_text("hello", encoding="utf-8")

    result = await prepare_outbound_attachments(
        [str(attachment)],
        frontend_id="web-main",
        user_id="user-1",
        frontend_config={},
    )

    assert result == [str(attachment)]


@pytest.mark.asyncio
async def test_prepare_outbound_attachments_uploads_local_files_to_minio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = tmp_path / "report.txt"
    attachment.write_text("hello", encoding="utf-8")
    uploads: list[tuple[str, str, str]] = []

    def fake_upload(self: MinioAttachmentStore, path: Path, object_key: str, content_type: str) -> None:
        uploads.append((str(path), object_key, content_type))

    def fake_url(self: MinioAttachmentStore, object_key: str) -> str:
        return f"https://files.example.com/{object_key}"

    monkeypatch.setattr(MinioAttachmentStore, "_upload_file_sync", fake_upload)
    monkeypatch.setattr(MinioAttachmentStore, "_resolve_object_url_sync", fake_url)

    result = await prepare_outbound_attachments(
        [str(attachment), {"url": "https://already.example.com/demo.png"}],
        frontend_id="web-main",
        user_id="user-1",
        frontend_config={
            "attachment_storage": {
                "provider": "minio",
                "endpoint": "http://minio:9000",
                "access_key": "minio",
                "secret_key": "secret",
                "bucket": "attachments",
                "public_base_url": "https://files.example.com",
                "key_prefix": "web-outbound",
            }
        },
    )

    assert len(uploads) == 1
    assert uploads[0][0] == str(attachment)
    assert uploads[0][1].startswith("web-outbound/web-main/user-1/")
    assert uploads[0][2] == "text/plain"
    assert result[0]["url"].startswith("https://files.example.com/web-outbound/web-main/user-1/")
    assert result[0]["filename"] == "report.txt"
    assert result[0]["storage"] == "minio"
    assert result[1] == {"url": "https://already.example.com/demo.png"}
