from support import register_bucket

import httpx
import pytest

from agent_backend.config import ControlConfig
from agent_backend.models import Envelope
from agent_backend.ingress import Inbound
from container_up.server import create_app


@pytest.fixture
def control(tmp_path):
    config = ControlConfig.model_validate({"state_root": str(tmp_path / "state"),

        "frontends": {"web": {"provider": "web", "channel_config": {}}},
    })
    app = create_app(config)
    register_bucket(app.state.service)
    yield app
    app.state.service.db.close()


@pytest.mark.asyncio
async def test_keyless_routing_idempotency_and_result_ownership(control):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=control), base_url="http://test") as client:
        body = {"chat_id": "chat", "user_id": "u", "content": "hello", "request_id": "r",
                "metadata": {"frontend_id": "forged", "user_id": "someone-else"}}
        first = await client.post("/internal/submit/web", json=body)
        assert first.status_code == 200
        assert first.json()["submit"]["identity"]
        assert "instance_id" not in first.json()
        assert "conversation_id" not in first.json()
        assert first.json()["chat_id"] == "chat"
        again = await client.post("/internal/submit/web", json=body)
        assert again.json() == first.json()
        assert len(control.state.service.db.pending("b")) == 1
        changed = await client.post("/internal/submit/web", json={**body, "content": "changed"})
        assert changed.status_code == 409
        row = control.state.service.db.get("request", "r")
        assert row["submit"]["identity"] == {"frontend_id": "web", "user_id": "u"}
        assert row["submit"]["chat_id"] == "chat"
        assert "conversation_id" not in row["submit"]
        with pytest.raises(PermissionError):
            await control.state.service.on_event("wrong-bucket", Envelope(type="request.completed",
                bucket_id="wrong-bucket", request_id="r", payload={"request_id": "r", "status": "completed"}))
        await control.state.service.on_event("b", Envelope(type="request.completed", bucket_id="b",
            request_id="r", payload={"request_id": "r", "status": "completed"}))
        response = await client.get("/internal/requests/web/r")
        assert response.json()["result"] == {"request_id": "r", "status": "completed", "error": None}
        assert (await client.get("/internal/requests/other/r")).status_code == 404


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


def test_active_channels_import_without_environment_configuration():
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if not k.startswith(
        ("BUCKET_", "SOURCE_", "GATEWAY_", "MINIO_", "FEISHU_"))}
    script = "\n".join([
        "import sys",
        "import container_up.server, channel_gateway.qxt_im_tool, channel_gateway.feishu_im_tool, channel_gateway.web_im_tool",
        "assert 'container_up.settings' not in sys.modules",
    ])
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_inbound_references_are_forwarded_without_local_io(control):
    service = control.state.service
    references = ["https://unreachable.invalid/photo.jpg", {"path": "/not-mounted-in-cu/report.pdf"},
                  {"path": "relative/data.csv"}]
    row = await service.submit("web", Inbound(chat_id="chat", user_id="alice", content="Analyze these", attachments=references))
    attachments = row["submit"]["attachments"]
    assert attachments[0]["url"] == references[0]
    assert attachments[1]["path"] == "/not-mounted-in-cu/report.pdf"
    assert attachments[2]["path"] == "relative/data.csv"
    assert all(a["size"] == 0 and a["sha256"] == "" for a in attachments)
    assert not hasattr(service, "artifacts")
    assert not hasattr(service, "transfers")


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("content", "Done"), ("attachments", []), ("usage", None), ("partial", False)])
async def test_control_rejects_channel_reply_fields(control, field, value):
    service = control.state.service
    await service.submit("web", Inbound(chat_id="chat", user_id="alice", content="Report", request_id="r"))
    with pytest.raises(ValueError, match="Extra inputs"):
        await service.on_event("b", Envelope(type="request.completed", bucket_id="b", request_id="r",
            payload={"request_id": "r", "status": "completed", field: value}))
    row = service.db.get("request", "r")
    assert not row.get("result")
    assert not {"delivery", "sync", "metadata"} & row.keys()
    assert not hasattr(service, "completed")


@pytest.mark.asyncio
async def test_cu_rejects_content_events(control):
    service = control.state.service
    await service.submit("web", Inbound(user_id="alice", chat_id="chat", content="hello", request_id="r"))
    with pytest.raises(ValueError, match="unsupported bucket event"):
        await service.on_event("b", Envelope(type="opencode.event", bucket_id="b", request_id="r"))
    assert not hasattr(service, "channels")
    assert not service.db.records("opencode_event")
