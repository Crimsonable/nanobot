import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agent_backend.config import load_frontends
from bucket_runtime.__main__ import main as runtime_main
from container_up.__main__ import main as control_main
from container_up.bucket_manager import BucketManager

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config/opencode/frontends.json"


@pytest.fixture
def registry(tmp_path):
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    other = json.loads(json.dumps(raw["frontends"][0]))
    other["frontend_id"] = "web-other"
    raw["frontends"].append(other)
    path = tmp_path / "frontends.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_cu_entrypoint_starts_only_control_with_all_frontends(registry, monkeypatch):
    import bucket_runtime.server
    import container_up.server
    import uvicorn

    def unexpected(*args, **kwargs):
        pytest.fail("CU must not start a runtime process")

    captured = {}
    app = object()

    def create_app(config):
        captured["config"] = config
        return app

    monkeypatch.setattr(bucket_runtime.server, "serve", unexpected)
    monkeypatch.setattr(container_up.server, "create_app", create_app)
    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: captured.update(app=application, **kwargs))
    control_main(["--config", str(registry)])
    assert captured["app"] is app
    assert set(captured["config"].frontends) == {"web-yk", "web-other"}
    assert captured["config"].frontend_config_path == Path("/mnt/common/frontends.json")
    assert captured["port"] == captured["config"].port


def test_bucket_entrypoint_uses_scheduler_identity(registry, monkeypatch):
    import bucket_runtime.server
    import container_up.server

    captured = []

    async def serve(config):
        captured.append(config)

    def unexpected(*args, **kwargs):
        pytest.fail("A bucket must not start CU")

    monkeypatch.setattr(bucket_runtime.server, "serve", serve)
    monkeypatch.setattr(container_up.server, "create_app", unexpected)
    runtime_main(["--config", str(registry), "--bucket-id", "opencode-bucket-0123456789ab",
                  "--frontend-id", "web-other"])
    assert len(captured) == 1
    config = captured[0]
    assert config.bucket_id == "opencode-bucket-0123456789ab"
    assert config.frontend_id == "web-other"
    assert config.opencode.config_file == Path("/mnt/common/web-other/config/opencode-server.json")
    with pytest.raises(SystemExit):
        runtime_main(["--config", str(registry), "--bucket-id", "opencode-bucket-0123456789ab"])
    assert len(captured) == 1


def test_check_command_validates_every_frontend_and_exits(registry):
    command = [sys.executable, "-m", "container_up", "--check-config", "--config", str(registry)]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "Configuration schema valid" in result.stdout
    # A bad runtime path in the second frontend must also be rejected by the check.
    raw = json.loads(registry.read_text(encoding="utf-8"))
    raw["frontends"][1]["runtime"]["opencode"] = {
        "config_file": "/mnt/common/web-yk/config/opencode-server.json"}
    registry.write_text(json.dumps(raw), encoding="utf-8")
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "must belong to this frontend" in result.stderr


def test_container_commands_use_component_entrypoints():
    config = load_frontends(EXAMPLE).control_config()
    workload = BucketManager(config).deployment("opencode-bucket-0123456789ab", "web-yk")
    runtime_args = workload["spec"]["template"]["spec"]["containers"][0]["args"]
    documents = yaml.safe_load_all((ROOT / "k8s/opencode/backend.yaml").read_text(encoding="utf-8"))
    control = next(doc for doc in documents if doc["kind"] == "Deployment")
    control_args = control["spec"]["template"]["spec"]["containers"][0]["args"]
    for component, args in [("container_up", control_args), ("bucket_runtime", runtime_args)]:
        docker = (ROOT / ("Dockerfile." + component)).read_text(encoding="utf-8")
        command = json.loads(next(line[4:] for line in docker.splitlines() if line.startswith("CMD ")))
        assert command == ["python", "-m", component, "--config", "/mnt/common/frontends.json"]
        assert args[:len(command)] == command
    assert runtime_args[-4:] == ["--frontend-id", "web-yk", "--bucket-id", "opencode-bucket-0123456789ab"]
