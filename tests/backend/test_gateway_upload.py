import asyncio
import hashlib
from types import SimpleNamespace

import httpx
import pytest
from test_gateway import accept
from test_gateway import system as system

from agent_backend.config import StorageConfig


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["web", "qxt", "feishu"])
async def test_plugin_upload_goes_directly_to_gateway_and_is_idempotent(system, provider):
    app, cu, _ = system
    gw = app.state.service
    gw.config.frontends["web"].provider = provider
    uploaded = []
    if provider == "web":
        gw.config.frontends["web"].attachment_storage = StorageConfig(
            endpoint="http://minio",
            bucket="files",
            access_key="test",
            secret_key="test",
            public_base_url="https://files.test",
        )
        gw.artifacts.client = lambda _: SimpleNamespace(
            upload_fileobj=lambda stream, *args, **kwargs: uploaded.append(stream.read())
        )
    else:
        reference = {
            "storage": provider,
            "frontend_id": "web",
            **(
                {"media_id": "m"} if provider == "qxt" else {"file_key": "f", "upload_type": "file"}
            ),
        }

        async def upload(**kwargs):
            uploaded.append(kwargs["data"])
            return reference

        gw.channels.parsers["web"] = SimpleNamespace(upload_outbound_attachment=upload)
    await accept(system)
    native = cu.db.get("request", "r")["native"]
    headers = {"x-bucket-id": "b", "x-root-session-id": native["session_id"]}
    params = {"filename": "report.txt", "sha256": hashlib.sha256(b"report").hexdigest()}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gw"
    ) as client:
        first = await client.post(
            "/internal/attachments/web/r/a", params=params, headers=headers, content=b"report"
        )
        assert first.status_code == 200, first.text
        again = await client.post(
            "/internal/attachments/web/r/a", params=params, headers=headers, content=b"report"
        )
        assert again.json() == first.json() and uploaded == [b"report"]
        assert len(gw.artifacts.attachments("r")) == 1
        for bad in [{**headers, "x-bucket-id": "other"}, {**headers, "x-root-session-id": "other"}]:
            assert (
                await client.post(
                    "/internal/attachments/web/r/a", params=params, headers=bad, content=b"report"
                )
            ).status_code == 403
        assert (
            await client.post(
                "/internal/attachments/web/r/a",
                params={**params, "sha256": "wrong"},
                headers=headers,
                content=b"report",
            )
        ).status_code == 400
        await cu.cancel("r")
        assert (
            await client.post(
                "/internal/attachments/web/r/a", params=params, headers=headers, content=b"report"
            )
        ).status_code == 409


@pytest.mark.asyncio
async def test_cancel_during_upload_prevents_attachment_joining_reply(system):
    app, cu, _ = system
    gw = app.state.service
    await accept(system)
    from agent_backend.models import Attachment

    entered = asyncio.Event()
    leave = asyncio.Event()

    async def upload(*args):
        entered.set()
        await leave.wait()
        return Attachment(attachment_id="a", filename="a", url="https://example.test/a")

    gw.artifacts.upload = upload
    headers = {
        "x-bucket-id": "b",
        "x-root-session-id": cu.db.get("request", "r")["native"]["session_id"],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gw"
    ) as client:
        task = asyncio.create_task(
            client.post(
                "/internal/attachments/web/r/a",
                params={"filename": "a", "sha256": "x"},
                headers=headers,
                content=b"a",
            )
        )
        await entered.wait()
        await cu.cancel("r")
        leave.set()
        assert (await task).status_code == 409
        assert not gw.artifacts.attachments("r")
