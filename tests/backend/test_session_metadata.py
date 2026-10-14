import json

import httpx
import pytest

from agent_backend.config import OpenCodeConfig
from bucket_runtime.opencode_client import OpenCodeClient


@pytest.mark.asyncio
async def test_create_and_reuse_metadata_preserves_identity_and_never_patches_permissions():
    context = {"schema_version": 1, "frontend_id": "web", "user_id": "alice", "identity_id": "identity",
               "chat_id": "group-1", "bucket_id": "b", "input_message_id": "m1",
               "requests": {"m1": "r1"}}
    info = {"id": "ses_generated", "directory": str(OpenCodeConfig().context_root.resolve()),
            "permission": [], "metadata": {"other_plugin": "keep"}}
    calls = []

    def handle(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "POST":
            info["metadata"].update(body["metadata"])
        if request.method == "PATCH":
            assert set(body) == {"metadata"}
            info["metadata"] = body["metadata"]
        return httpx.Response(200, json=info)

    client = OpenCodeClient(OpenCodeConfig(), "password")
    await client.http.aclose()
    async with httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(handle)) as client.http:
        assert await client.create_session([], context) == "ses_generated"
        next_context = {**context, "input_message_id": "m2", "requests": {"m2": "r2"}}
        await client.set_context("ses_generated", next_context, info)
        assert info["metadata"]["other_plugin"] == "keep"
        assert info["metadata"]["bucket_runtime"]["requests"] == {"m1": "r1", "m2": "r2"}
        with pytest.raises(PermissionError, match="ownership mismatch"):
            await client.set_context("ses_generated", {**next_context, "user_id": "bob"}, info)
        with pytest.raises(PermissionError, match="ownership mismatch"):
            await client.set_context("ses_generated", {**next_context, "chat_id": "group-2"}, info)
        with pytest.raises(KeyError):
            await client.set_context("ses_generated", next_context, {**info, "metadata": {}})
    assert len([call for call in calls if call[0] == "PATCH"]) == 1
