import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from hazard_expert_mcp.app import create_app
from hazard_expert_mcp.config import Settings
from hazard_expert_mcp.service import ExpertService

HEADERS = {
    "Authorization": "Bearer test-service-token",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-11-25",
}


@pytest.mark.parametrize("deployment", ["compose", "kubernetes"])
def test_deployment_environment_and_production_lifespan(monkeypatch, deployment):
    project = Path(__file__).resolve().parents[1]
    if deployment == "compose":
        compose = yaml.safe_load((project / "docker-compose.yml").read_text(encoding="utf-8"))
        assert list(compose["services"]) == ["hazard-expert-mcp"]
        env = compose["services"]["hazard-expert-mcp"]["environment"]
    else:
        manifest = project.parent / "k8s/base/hazard-expert-mcp.yaml"
        resources = list(yaml.safe_load_all(manifest.read_text(encoding="utf-8")))
        service = next(item for item in resources if item["kind"] == "Service")
        workload = next(item for item in resources if item["kind"] == "Deployment")
        pod = workload["spec"]["template"]
        container = pod["spec"]["containers"][0]
        env = {item["name"]: item["value"] for item in container["env"]}
        assert service["spec"]["selector"] == pod["metadata"]["labels"]
        assert service["spec"]["ports"][0]["targetPort"] == container["ports"][0]["name"]
        assert container["ports"][0]["containerPort"] == int(env["MCP_PORT"])
        assert service["spec"]["ports"][0]["nodePort"] == 30088
        assert (
            pod["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == "nanobot-source-pvc"
        )
        assert container["workingDir"] == "/mnt/source/hazard_expert_mcp"
        assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
        assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"
    assert {field.upper() for field in Settings.model_fields} <= set(env)
    for name, value in env.items():
        monkeypatch.setenv(name, str(value))
    settings = Settings()
    # Use the real lifespan: construct/close S3 clients and the upstream HTTP client.
    with TestClient(create_app(settings), base_url="http://localhost:8082") as client:
        assert client.get("/healthz").status_code == 200
        response = client.post(
            "/mcp",
            headers={
                **HEADERS,
                "Authorization": f"Bearer {settings.mcp_api_key.get_secret_value()}",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert response.status_code == 200
        assert len(response.json()["result"]["tools"]) == 2


def rpc(client, method, params=None, request_id=1):
    response = client.post(
        "/mcp",
        headers=HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_real_mcp_protocol_lists_exactly_two_tools_and_calls_both(settings, completed_reply):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completed_reply(json.loads(request.content)))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    storage, signer = Mock(), Mock()
    signer.generate_presigned_url.return_value = "https://files.example.com/photo.jpg?signature=123"
    service = ExpertService(settings, http, storage, signer)
    with TestClient(create_app(settings, service)) as client:
        initialized = rpc(
            client,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "integration-test", "version": "1.0"},
            },
        )
        assert initialized["serverInfo"]["name"] == "hazard-expert-mcp"
        listed = rpc(client, "tools/list")
        assert {t["name"] for t in listed["tools"]} == {
            "upload_attachment",
            "hazard_analysis_expert",
        }
        upload = rpc(
            client,
            "tools/call",
            {
                "name": "upload_attachment",
                "arguments": {
                    "user_id": "u1",
                    "filename": "photo.jpg",
                    "content_base64": "aGVsbG8=",
                },
            },
        )
        assert not upload.get("isError")
        url = upload["structuredContent"]["url"]
        assert storage.put_object.call_args.kwargs["Body"] == b"hello"
        for query in ["识别隐患", "具体如何整改"]:
            result = rpc(
                client,
                "tools/call",
                {
                    "name": "hazard_analysis_expert",
                    "arguments": {
                        "user_id": "u1",
                        "chat_id": "c1",
                        "query": query,
                        "attachments": [url],
                    },
                },
            )
            assert not result.get("isError")
            assert result["structuredContent"]["content"] == "发现临边防护缺失。"
            assert json.loads(requests[-1].content)["attachments"] == [url]
            assert result["structuredContent"]["chat_id"] == "c1"


def test_auth_health_and_request_limits(settings):
    app = create_app(settings, Mock())
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.post("/mcp", json={}).status_code == 401
        assert (
            client.post("/mcp", headers={"Authorization": "Bearer wrong"}, json={}).status_code
            == 401
        )
    settings.max_upload_bytes = 1
    with TestClient(create_app(settings, Mock())) as client:
        response = client.post(
            "/mcp", headers=HEADERS, content=b"x" * (settings.max_request_bytes + 1)
        )
        assert response.status_code == 413


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "192.168.1.20:30088"},
        {"Host": "hazard-expert-mcp.nanobot.svc.cluster.local:8082"},
        {"Origin": "https://client.example.com"},
        {"Host": "mcp.example.com", "Origin": "https://another-client.example.com"},
    ],
)
def test_arbitrary_host_and_origin_are_accepted(settings, headers):
    with TestClient(create_app(settings, Mock())) as client:
        response = client.post(
            "/mcp",
            headers={**HEADERS, **headers},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert response.status_code == 200
        assert len(response.json()["result"]["tools"]) == 2


def test_invalid_tool_input_is_error_and_never_calls_backend(settings):
    service = Mock()
    with TestClient(create_app(settings, service)) as client:
        result = rpc(
            client,
            "tools/call",
            {
                "name": "hazard_analysis_expert",
                "arguments": {
                    "user_id": "",
                    "chat_id": "c",
                    "query": "分析",
                },
            },
        )
        assert result["isError"] is True
        service.analyze.assert_not_called()
