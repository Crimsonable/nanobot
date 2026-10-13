from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_backend.config import ControlConfig
from agent_backend.models import Attachment, Result
from container_up.channel_service import ChannelService
from container_up.server import create_app


@pytest.fixture
def config(tmp_path):
    return ControlConfig.model_validate({"state_root": str(tmp_path / "state"),
        "bucket_ids": ["b"], "frontends": {"qxt": {"provider": "qxt", "profile_id": "p",
        "channel_config": {}}}})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["links", "media"])
async def test_qxt_delivery_uses_trusted_group_target_and_configured_attachment_mode(config, mode):
    config.frontends["qxt"].channel_config = {"attachment_delivery": mode}
    channels = ChannelService(config)
    send = AsyncMock()
    channels.parsers["qxt"] = SimpleNamespace(post_message_with_retry=send)
    target = {"to_group_id": "group-1"}
    await channels.deliver({"submit": {"identity": {"frontend_id": "qxt", "user_id": "alice"}},
        "chat_id": "group-1", "metadata": {"reply_target": target}},
        Result(request_id="r", status="completed", content="report",
               attachments=[Attachment(url="https://files.example/report.pdf", filename="report.pdf")]))
    payload = send.call_args.kwargs["payload"]
    assert payload["metadata"]["reply_target"] == target
    if mode == "media":
        assert payload["attachments"][0]["url"] == "https://files.example/report.pdf"
        assert payload["content"] == "report"
    else:
        assert payload["attachments"] == []
        assert "https://files.example/report.pdf" in payload["content"]


@pytest.mark.asyncio
async def test_qxt_upload_purpose_and_keyless_reference_contract(config):
    app = create_app(config)
    upload = AsyncMock(return_value={"storage": "qxt", "frontend_id": "qxt", "media_id": "m"})
    app.state.service.channels.parsers["qxt"] = SimpleNamespace(upload_outbound_attachment=upload)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            body = {"frontend_id": "qxt", "attachment_url": "https://files.example/report.pdf"}
            reference = await client.post("/internal/attachments/upload", json=body)
            assert reference.json()["url"] == body["attachment_url"]
            upload.assert_not_awaited()
            outbound = await client.post("/internal/attachments/upload", json={**body, "purpose": "outbound"})
            assert outbound.json()["media_id"] == "m"
            upload.assert_awaited_once()
            for extra in [{"purpose": "invalid"}, {"purpose": "outbound", "attachment_url": "/local/file"}]:
                assert (await client.post("/internal/attachments/upload", json={**body, **extra})).status_code == 400
    finally:
        app.state.service.db.close()
