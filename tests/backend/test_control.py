from types import SimpleNamespace

import httpx
import pytest

from agent_backend.config import ControlConfig
from agent_backend.models import Envelope
from container_up.server import Inbound, create_app
from scripts.audit_backend_env import audit


@pytest.fixture
def control(tmp_path):
    token = tmp_path / "token"
    token.write_text("test-credential-123456789")
    config = ControlConfig.model_validate({"state_root": str(tmp_path / "state"),
        "bucket_credentials": {"b": str(token)},
        "frontends": {"web": {"provider": "web", "profile_id": "p", "credential_file": str(token),
                              "api_key_file": str(token)}},
        "storage": {"endpoint": "http://storage", "bucket": "files", "access_key_file": str(token),
                    "secret_key_file": str(token)}})
    app = create_app(config)
    app.state.service.peers["b"] = SimpleNamespace(closed=False)
    yield app
    app.state.service.db.close()


@pytest.mark.asyncio
async def test_authenticated_routing_idempotency_and_result_ownership(control):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=control), base_url="http://test") as client:
        body = {"user_id": "u", "content": "hello", "request_id": "r",
                "metadata": {"frontend_id": "forged", "usr_id": "someone-else"}}
        assert (await client.post("/inbound/web", json=body)).status_code == 401
        client.headers["Authorization"] = "Bearer test-credential-123456789"
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
        assert (await client.get("/requests/other/r")).status_code == 401


@pytest.mark.asyncio
async def test_bucket_cannot_publish_for_another_bucket_request(control):
    service = control.state.service
    await service.submit("web", Inbound(user_id="alice", content="hello", request_id="r"))
    with pytest.raises(PermissionError):
        await service.rpc("other", "attachment.begin", {"request_id": "r"}, None)
    with pytest.raises(PermissionError):
        await service.rpc("b", "input.fetch", {"request_id": "r", "attachment_id": "not-owned"}, None)


@pytest.mark.asyncio
async def test_qxt_input_does_not_use_unguarded_legacy_download(control):
    service = control.state.service
    service.config.frontends["web"].provider = "qxt"

    async def forbidden_legacy_helper(payload):
        raise AssertionError("URL downloads must go through ArtifactService")

    service.channels.parsers["web"] = SimpleNamespace(prepare_inbound_event=forbidden_legacy_helper)
    await service.dispatch_channel("web", {"event": {"usr_id": "alice", "content": "file",
        "attachments": [{"url": "https://storage.example/file.txt", "filename": "file.txt"}]}})
    assert service.db.records("request")[0]["submit"]["attachments"][0]["filename"] == "file.txt"


@pytest.mark.asyncio
async def test_attachment_download_address_guard():
    from container_up.artifact_service import GuardedResolver
    with pytest.raises(ValueError):
        await GuardedResolver([]).resolve("127.0.0.1", 80)
    allowed = await GuardedResolver(["127.0.0.1/32"]).resolve("127.0.0.1", 80)
    assert allowed[0]["host"] == "127.0.0.1"


def test_env_audit_never_prints_values():
    result = audit("MINIO_SECRET_KEY=top-secret\nSOURCE_ROOT=/old\nNEW_UNKNOWN=secret\nMINIO_SECRET_KEY=other")
    assert "top-secret" not in str(result)
    assert any("duplicate" in r["action"] for r in result)
    assert any("unmapped" in r["action"] for r in result)


def test_active_channels_do_not_import_legacy_settings_or_nanobot():
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if not k.startswith(
        ("BUCKET_", "SOURCE_", "NANOBOT_", "CONTAINER_UP_", "MINIO_", "FEISHU_"))}
    script = "\n".join([
        "import sys",
        "import container_up.server, container_up.qxt_im_tool, container_up.feishu_im_tool, container_up.web_im_tool",
        "assert 'container_up.settings' not in sys.modules",
        "assert not any(n == 'nanobot' or n.startswith('nanobot.') for n in sys.modules)",
    ])
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
