from unittest.mock import AsyncMock

import httpx
import pytest
from support import register_bucket

from agent_backend.config import ControlConfig
from agent_backend.ingress import Inbound
from container_up.server import create_app


@pytest.fixture
def control(tmp_path):
    app = create_app(
        ControlConfig(state_root=tmp_path / "cu", frontends={"web": {"provider": "web"}})
    )
    register_bucket(app.state.service)
    yield app
    app.state.service.db.close()


@pytest.mark.asyncio
async def test_native_admission_idempotency_and_result_ownership(control):
    service = control.state.service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://control"
    ) as http:
        body = {
            "request_id": "r",
            "user_id": "alice",
            "chat_id": "chat",
            "content": "hello",
            "metadata": {"user_id": "forged"},
        }
        first = await http.post("/internal/submit/web", json=body)
        assert first.status_code == 200, first.text
        row = first.json()
        assert row["submit"]["identity"] == {"frontend_id": "web", "user_id": "alice"}
        assert (await http.post("/internal/submit/web", json=body)).json() == row
        native = await service.pool.client("b")
        assert len(native.inputs) == len(native.submissions) == 1
        assert (
            await http.post("/internal/submit/web", json={**body, "content": "different"})
        ).status_code == 409
        assert (await http.get("/internal/requests/foreign/r")).status_code == 404
        assert (
            await http.post(
                "/internal/requests/web/r/collected",
                json={"request_id": "other", "status": "completed"},
            )
        ).status_code == 400
        assert (
            await http.post(
                "/internal/requests/web/r/collected",
                json={"request_id": "r", "status": "completed", "content": "not control data"},
            )
        ).status_code == 422
        assert (
            await http.post(
                "/internal/requests/web/r/collected",
                json={"request_id": "r", "status": "completed"},
            )
        ).status_code == 200
        assert not service.pool.busy("b")
        assert service.db.get("request", "r")["result"] == {
            "request_id": "r",
            "status": "completed",
            "error": None,
        }


@pytest.mark.asyncio
async def test_uncertain_native_admission_reuses_durable_input_id(control):
    service = control.state.service
    native = await service.pool.client("b")
    original = native.submit

    async def lose_reply(*args):
        await original(*args)
        raise httpx.ReadTimeout("lost reply")

    native.submit = lose_reply
    row = await service.submit("web", Inbound(request_id="r", user_id="alice", content="hello"))
    assert not row.get("admitted") and len(native.inputs) == 1
    native.submit = original
    again = await service.submit("web", Inbound(request_id="r", user_id="alice", content="hello"))
    assert again["admitted"] and again["native"] == row["native"] and len(native.inputs) == 1


@pytest.mark.asyncio
async def test_cancel_is_native_and_output_stays_pinned_until_collected(control):
    service = control.state.service
    row = await service.submit("web", Inbound(request_id="r", user_id="alice", content="hello"))
    native = await service.pool.client("b")
    native.cancel_input = AsyncMock()
    await service.cancel("r")
    native.cancel_input.assert_awaited_once_with(row["native"])
    assert service.db.get("request", "r")["result"]["status"] == "cancelled"
    assert service.pool.busy("b")


@pytest.mark.asyncio
async def test_input_attachments_are_references_and_native_metadata_is_trusted(control):
    service = control.state.service
    row = await service.submit(
        "web",
        Inbound(
            request_id="r",
            user_id="alice",
            content="hi",
            attachments=[{"url": "https://example.test/image.png", "filename": "image.png"}],
            metadata={"managed": {"request_id": "fake"}},
        ),
    )
    native = await service.pool.client("b")
    message = native.history[row["native"]["session_id"]][0]
    assert "https://example.test/image.png" in message["text"]
    assert message["metadata"]["managed"]["request_id"] == "r"


def test_active_channels_import_without_environment_configuration():
    import os
    import subprocess
    import sys

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("BUCKET_", "SOURCE_", "GATEWAY_", "MINIO_", "FEISHU_"))
    }
    script = "\n".join(
        [
            "import sys",
            "import container_up.server, channel_gateway.qxt_im_tool, channel_gateway.feishu_im_tool, channel_gateway.web_im_tool",
            "assert 'container_up.settings' not in sys.modules",
        ]
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
