from types import SimpleNamespace

import httpx
import pytest

from agent_backend.config import ControlConfig
from agent_backend.models import Envelope
from container_up.server import Inbound, create_app


@pytest.fixture
def control(tmp_path):
    config = ControlConfig.model_validate({"state_root": str(tmp_path / "state"),
        "bucket_ids": ["b"],
        "frontends": {"web": {"provider": "web", "profile_id": "p", "channel_config": {}}},
    })
    app = create_app(config)
    app.state.service.peers["b"] = SimpleNamespace(closed=False)
    yield app
    app.state.service.db.close()


@pytest.mark.asyncio
async def test_keyless_routing_idempotency_and_result_ownership(control):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=control), base_url="http://test") as client:
        body = {"user_id": "u", "content": "hello", "request_id": "r",
                "metadata": {"frontend_id": "forged", "usr_id": "someone-else"}}
        first = await client.post("/inbound/web", json=body)
        assert first.status_code == 200
        again = await client.post("/inbound/web", json=body)
        assert again.json() == first.json()
        assert len(control.state.service.db.pending("b")) == 1
        changed = await client.post("/inbound/web", json={**body, "content": "changed"})
        assert changed.status_code == 409
        row = control.state.service.db.get("request", "r")
        assert row["submit"]["identity"] == {"frontend_id": "web", "user_id": "u"}
        with pytest.raises(PermissionError):
            await control.state.service.on_event("wrong-bucket", Envelope(type="request.completed",
                bucket_id="wrong-bucket", request_id="r", payload={"request_id": "r", "status": "completed"}))
        await control.state.service.on_event("b", Envelope(type="request.completed", bucket_id="b",
            request_id="r", payload={"request_id": "r", "status": "completed", "content": "done"}))
        response = await client.get("/requests/web/r")
        assert response.json()["content"] == "done"
        assert (await client.get("/requests/other/r")).status_code == 404


@pytest.mark.asyncio
async def test_bucket_ws_needs_registered_id_but_no_auth_key(control, monkeypatch):
    from agent_backend.wire import Peer

    async def run(self):
        return

    monkeypatch.setattr(Peer, "run", run)
    endpoint = next(r.endpoint for r in control.routes if r.path == "/internal/buckets/ws")

    class Socket:
        def __init__(self, bucket_id):
            self.headers = {"x-bucket-id": bucket_id}  # No Authorization header or secret file.
            self.accepted = False
            self.closed = None

        async def accept(self):
            self.accepted = True

        async def close(self, code=1000):
            self.closed = code

    registered = Socket("b")
    await endpoint(registered)
    assert registered.accepted
    unknown = Socket("unknown")
    await endpoint(unknown)
    assert not unknown.accepted
    assert unknown.closed == 1008


@pytest.mark.asyncio
async def test_bucket_cannot_publish_for_another_bucket_request(control):
    service = control.state.service
    await service.submit("web", Inbound(user_id="alice", content="hello", request_id="r"))
    with pytest.raises(PermissionError):
        await service.rpc("other", "attachment.begin", {"request_id": "r"}, None)
    with pytest.raises(ValueError, match="file transfer RPC"):
        await service.rpc("b", "input.fetch", {"request_id": "r", "attachment_id": "not-owned"}, None)


@pytest.mark.asyncio
async def test_qxt_input_does_not_use_unguarded_legacy_download(control):
    service = control.state.service
    service.config.frontends["web"].provider = "qxt"

    async def forbidden_legacy_helper(payload):
        raise AssertionError("Inbound references must not be downloaded")

    service.channels.parsers["web"] = SimpleNamespace(prepare_inbound_event=forbidden_legacy_helper)
    await service.dispatch_channel("web", {"event": {"usr_id": "alice", "content": "file",
        "attachments": [{"url": "https://storage.example/file.txt", "filename": "file.txt"}]}})
    assert service.db.records("request")[0]["submit"]["attachments"][0]["filename"] == "file.txt"


def test_active_channels_import_without_environment_configuration():
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if not k.startswith(
        ("BUCKET_", "SOURCE_", "CONTAINER_UP_", "MINIO_", "FEISHU_"))}
    script = "\n".join([
        "import sys",
        "import container_up.server, container_up.qxt_im_tool, container_up.feishu_im_tool, container_up.web_im_tool",
        "assert 'container_up.settings' not in sys.modules",
    ])
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_inbound_references_are_forwarded_without_local_io(control):
    service = control.state.service
    references = ["https://unreachable.invalid/photo.jpg", {"path": "/not-mounted-in-cu/report.pdf"},
                  {"path": "relative/data.csv"}]
    row = await service.submit("web", Inbound(user_id="alice", content="Analyze these", attachments=references))
    attachments = row["submit"]["attachments"]
    assert attachments[0]["url"] == references[0]
    assert attachments[1]["path"] == "/not-mounted-in-cu/report.pdf"
    assert attachments[2]["path"] == "relative/data.csv"
    assert all(a["size"] == 0 and a["sha256"] == "" for a in attachments)
    assert not hasattr(service, "artifacts")
    assert not hasattr(service, "transfers")


@pytest.mark.asyncio
async def test_control_returns_runtime_published_url_without_storage(control):
    service = control.state.service
    await service.submit("web", Inbound(user_id="alice", content="Report", request_id="r"))
    artifact = {"attachment_id": "published", "url": "https://objects.example/report.pdf", "filename": "report.pdf"}
    await service.on_event("b", Envelope(type="request.completed", bucket_id="b", request_id="r",
        payload={"request_id": "r", "status": "completed", "content": "Done", "attachments": [artifact]}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=control), base_url="http://test") as client:
        result = await client.get("/requests/web/r")
        assert result.json()["attachments"][0]["url"] == artifact["url"]


@pytest.mark.asyncio
async def test_im_delivery_sends_object_links_without_file_reupload(control):
    from agent_backend.models import Attachment, Result
    from container_up.channel_service import ChannelService
    config = control.state.service.config
    config.frontends["web"].provider = "qxt"
    channels = ChannelService(config)
    delivered = []

    async def send(*, payload):
        delivered.append(payload)

    channels.parsers["web"] = SimpleNamespace(post_message_with_retry=send)
    row = await control.state.service.submit("web", Inbound(user_id="alice", content="Report", request_id="r"))
    result = Result(request_id="r", status="completed", content="Done", attachments=[
        Attachment(filename="report.pdf", url="https://objects.example/report.pdf")])
    await channels.deliver(row, result)
    assert delivered[0]["attachments"] == []
    assert "https://objects.example/report.pdf" in delivered[0]["content"]
