import json

import httpx
import pytest

from agent_backend.config import OpenCodeConfig
from bucket_runtime.opencode_client import OpenCodeClient


@pytest.mark.asyncio
async def test_resumed_session_uses_latest_effective_model(tmp_path):
    model = "old-provider/old-model"
    calls = []

    def handle(request):
        assert request.url.params["directory"] == str(tmp_path.resolve())
        calls.append(request)
        if request.url.path == "/config":
            return httpx.Response(200, json={"model": model})
        assert request.url.path == "/session/existing-session/prompt_async"
        return httpx.Response(204)

    client = OpenCodeClient(OpenCodeConfig(), "password")
    await client.http.aclose()
    async with httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(handle)) as client.http:
        scoped = client.for_directory(tmp_path)
        await scoped.submit("existing-session", "m1", "first")
        model = "new-provider/family/new-model"
        await scoped.submit("existing-session", "m2", "continue")
    assert [r.method for r in calls] == ["GET", "POST", "GET", "POST"]
    assert json.loads(calls[1].content)["model"] == {"providerID": "old-provider", "modelID": "old-model"}
    assert json.loads(calls[3].content) == {
        "messageID": "m2", "model": {"providerID": "new-provider", "modelID": "family/new-model"},
        "parts": [{"type": "text", "text": "continue"}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [None, "", "invalid", "/model", "provider/", 123])
async def test_missing_or_invalid_default_model_does_not_submit(model):
    def handle(request):
        assert request.method == "GET" and request.url.path == "/config"
        return httpx.Response(200, json={} if model is None else {"model": model})

    client = OpenCodeClient(OpenCodeConfig(), "password")
    await client.http.aclose()
    async with httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(handle)) as client.http:
        with pytest.raises(RuntimeError, match="default model must be configured"):
            await client.submit("existing-session", "m1", "hello")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["config_transport", "config_status", "prompt_transport"])
async def test_config_failure_is_not_treated_as_ambiguous_prompt_submission(failure):
    def handle(request):
        if request.url.path == "/config":
            if failure == "config_transport":
                raise httpx.ConnectError("unavailable", request=request)
            return httpx.Response(503 if failure == "config_status" else 200,
                                  json={"model": "provider/model"})
        assert failure == "prompt_transport"
        raise httpx.ReadTimeout("response lost", request=request)

    client = OpenCodeClient(OpenCodeConfig(), "password")
    await client.http.aclose()
    expected = {"config_transport": RuntimeError, "config_status": httpx.HTTPStatusError,
                "prompt_transport": httpx.ReadTimeout}[failure]
    async with httpx.AsyncClient(base_url="http://opencode", transport=httpx.MockTransport(handle)) as client.http:
        with pytest.raises(expected):
            await client.submit("existing-session", "m1", "hello")
