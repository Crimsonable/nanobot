import json

import httpx
import pytest

from agent_backend.config import RuntimeConfig
from bucket_runtime.opencode_server import OpenCodeServer


@pytest.mark.asyncio
async def test_resumed_session_uses_latest_effective_model(tmp_path):
    model = {"providerID": "old-provider", "id": "old-model"}
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == "/api/model/default":
            return httpx.Response(200, json={"data": model})
        assert request.url.path in {"/api/session/existing-session/model", "/api/session/existing-session/prompt"}
        return httpx.Response(204)

    client = OpenCodeServer(RuntimeConfig(frontend_id="web", bucket_id="b"), "password")
    await client.http.aclose()
    async with httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(handle)) as client.http:
        await client.submit("existing-session", "m1", "first", directory=tmp_path)
        model = {"providerID": "new-provider", "id": "family/new-model"}
        await client.submit("existing-session", "m2", "continue", directory=tmp_path)
    assert [r.method for r in calls] == ["GET", "POST", "POST", "GET", "POST", "POST"]
    assert calls[0].url.params["location[directory]"] == str(tmp_path.resolve())
    assert json.loads(calls[1].content)["model"] == {"providerID": "old-provider", "id": "old-model"}
    assert json.loads(calls[4].content)["model"] == model
    assert json.loads(calls[5].content) == {"id": "m2", "text": "continue", "delivery": "queue"}


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [None, "", "invalid", "/model", "provider/", 123])
async def test_missing_or_invalid_default_model_does_not_submit(model):
    def handle(request):
        assert request.method == "GET" and request.url.path == "/api/model/default"
        return httpx.Response(200, json={} if model is None else {"data": model})

    client = OpenCodeServer(RuntimeConfig(frontend_id="web", bucket_id="b"), "password")
    await client.http.aclose()
    async with httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(handle)) as client.http:
        with pytest.raises(RuntimeError, match="select OpenCode default model"):
            await client.submit("existing-session", "m1", "hello")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["config_transport", "config_status", "prompt_transport"])
async def test_config_failure_is_not_treated_as_ambiguous_prompt_submission(failure):
    def handle(request):
        if request.url.path == "/api/model/default":
            if failure == "config_transport":
                raise httpx.ConnectError("unavailable", request=request)
            return httpx.Response(503 if failure == "config_status" else 200,
                                  json={"data": {"providerID":"provider","id":"model"}})
        if request.url.path.endswith("/model"):
            return httpx.Response(204)
        assert failure == "prompt_transport"
        raise httpx.ReadTimeout("response lost", request=request)

    client = OpenCodeServer(RuntimeConfig(frontend_id="web", bucket_id="b"), "password")
    await client.http.aclose()
    expected = {"config_transport": RuntimeError, "config_status": RuntimeError,
                "prompt_transport": httpx.ReadTimeout}[failure]
    async with httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(handle)) as client.http:
        with pytest.raises(expected):
            await client.submit("existing-session", "m1", "hello")
