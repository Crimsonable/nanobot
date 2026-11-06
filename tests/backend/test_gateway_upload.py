import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_backend.config import StorageConfig
from agent_backend.models import Attachment, Envelope, Result, Submit
from channel_gateway.channel_service import ChannelService
from channel_gateway.qxt_im_tool import QxtIMParser
from tests.backend.support import publication
from tests.backend import test_gateway
from tests.backend.test_gateway import accept, answer, transfer
from tests.test_gateway_im_tools import FakeResponse, FakeSession

system = test_gateway.system


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["web", "qxt", "feishu"])
async def test_publish_routes_through_gateway_and_delivers_native_attachment(system, monkeypatch, provider):
    app, cu, bu = system
    gw = app.state.service
    gw.config.frontends["web"].provider = provider  # Provider must not be inferred from the ID.
    gw.channels = ChannelService(gw.config)
    gw.artifacts.channels = gw.channels
    uploads = []
    session = None
    if provider == "web":
        gw.config.frontends["web"].attachment_storage = StorageConfig(endpoint="http://minio", bucket="reports",
            access_key="key", secret_key="secret", public_base_url="https://objects.example/reports")
        class Storage:
            def upload_fileobj(self, stream, bucket, key, ExtraArgs):
                uploads.append((stream.read(), bucket, key))
        monkeypatch.setattr(gw.artifacts, "client", lambda storage: Storage())
        gw.channels.parsers["web"] = SimpleNamespace(post_message=AsyncMock())
    elif provider == "qxt":
        parser = QxtIMParser(frontend_id="web", frontend_config={
            "send_msg_url": "https://qxt.example/v2/message/bot_send_to_conversation"})
        monkeypatch.setattr(parser, "get_access_token", AsyncMock(return_value="access-token"))
        session = FakeSession([FakeResponse(json_data={"mediaId": "media-1"}),
                               FakeResponse(), FakeResponse()])
        monkeypatch.setattr("channel_gateway.qxt_im_tool.get_dispatch_session", lambda: session)
        gw.channels.parsers["web"] = parser
    else:
        gw.channels.parsers["web"] = SimpleNamespace(post_message=AsyncMock(),
            upload_outbound_attachment=AsyncMock(return_value={"storage": "feishu", "frontend_id": "web",
                "upload_type": "file", "file_key": "file-1"}))
    await accept(system)
    row = bu.db.get("run", "r")
    assert row["submit"]["metadata"]["provider"] == provider
    assert row["submit"]["metadata"]["frontend_id"] == "web"
    assert cu.db.get("request", "r")["submit"]["metadata"] == row["submit"]["metadata"]
    # Set the trusted destination as if received from the channel callback.
    record = gw.db.get("request", "r")
    record["metadata"]["reply_target"] = {"type": provider, "to_group_id": "group-1"}
    gw.db.put("request", "r", record)
    source = bu.workspace(Submit.model_validate(row["submit"])) / "report.txt"
    source.write_bytes(b"report bytes")
    monkeypatch.setattr(bu.artifacts, "client", lambda: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"))
    body = publication(bu, "s", "publish", [{"path": "report.txt", "filename": "报告.txt"}])
    result = await bu.publish(body)
    assert await bu.publish(body) == result
    attachment = result["attachments"][0]
    assert attachment["filename"] == "报告.txt"
    assert bu.db.get("run", "r")["artifacts"] == result["attachments"]
    # Simulate an HTTP response being lost before BU recorded the upload.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        retry = await client.post("/internal/attachments/web/r/" + attachment["attachment_id"],
            params={"filename": "报告.txt", "sha256": hashlib.sha256(b"report bytes").hexdigest(),
                    "content_type": "text/plain"}, content=b"report bytes", headers={"x-bucket-id": "b"})
    assert retry.status_code == 200 and retry.json() == attachment
    if provider == "web":
        assert len(uploads) == 1 and uploads[0][0] == b"report bytes"
        assert attachment["url"].startswith("https://objects.example/reports/")
    elif provider == "qxt":
        assert attachment["media_id"] == "media-1" and attachment["url"] == ""
        assert len(session.calls) == 1
        assert session.calls[0]["url"] == "https://qxt.example/v2/media/upload"
    else:
        assert attachment["file_key"] == "file-1" and attachment["url"] == ""
        gw.channels.parsers["web"].upload_outbound_attachment.assert_awaited_once()
    answer(bu)
    await transfer(gw, cu, bu)
    record = gw.db.get("request", "r")
    await gw.channels.deliver(record, Result.model_validate(record["result"]))
    assert record["result"]["attachments"] == result["attachments"]
    assert set(cu.db.get("request", "r")["result"]) == {"request_id", "status", "error"}
    if provider == "qxt":
        assert session.calls[-1]["json"] == {
            "to_group_id": "group-1", "type": "file", "message": {"media_id": "media-1"}}
        assert len(session.calls) == 3  # Upload, text reply, file reply; no second upload.
    else:
        sent = gw.channels.parsers["web"].post_message.call_args.kwargs["payload"]
        assert sent["attachments"] == result["attachments"]
        assert sent["metadata"]["provider"] == provider


@pytest.mark.asyncio
async def test_gateway_upload_checks_request_ownership_limits_and_failure(system):
    app, cu, bu = system
    gw = app.state.service
    gw.config.frontends["web"].provider = "qxt"
    uploader = AsyncMock(side_effect=RuntimeError("upstream unavailable"))
    gw.channels.parsers = {"web": SimpleNamespace(upload_outbound_attachment=uploader)}
    await accept(system)
    params = {"filename": "file.txt", "sha256": hashlib.sha256(b"file").hexdigest()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        async def send(bucket="b", request="r", **kwargs):
            return await client.post(f"/internal/attachments/web/{request}/att_1", params=params,
                headers={"x-bucket-id": bucket}, content=b"file", **kwargs)
        assert (await send(bucket="other")).status_code == 403
        assert (await send(request="unknown")).status_code == 403
        gw.config.limits.file_bytes = 3
        assert (await send()).status_code == 413
        gw.config.limits.file_bytes = 50
        params["sha256"] = "wrong"
        assert (await send()).status_code == 400
        uploader.assert_not_awaited()
        params["sha256"] = hashlib.sha256(b"file").hexdigest()
        assert (await send()).status_code == 502
        assert gw.db.records("artifact") == []
        uploader.side_effect = None
        uploader.return_value = {"storage": "qxt", "frontend_id": "web", "media_id": "media-1", "upload_type": "file"}
        assert (await send()).status_code == 200
        params["filename"] = "another.txt"
        assert (await send()).status_code == 400
        record = gw.db.get("request", "r")
        gw.db.put("request", "r", {**record, "cancel_requested": True})
        assert (await send()).status_code == 409


@pytest.mark.asyncio
async def test_gateway_rejects_foreign_or_unpublished_media_in_result(system):
    app, cu, bu = system
    gw = app.state.service
    gw.config.frontends["web"].provider = "qxt"
    await accept(system)
    for frontend in ("other", "web"):
        result = Result(request_id="r", status="completed", attachments=[Attachment(
            storage="qxt", frontend_id=frontend, media_id="invented")])
        with pytest.raises(ValueError):
            await gw.on_event("b", Envelope(type="runtime.result", bucket_id="b", request_id="r",
                                           payload=result.model_dump()), "web")
    assert not gw.db.get("request", "r").get("result")
