import pytest

from container_up.frontend_config import FrontendConfig
from container_up import router_service


def test_build_child_volumes_mounts_nanobot_source(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared_config = tmp_path / "config.json"
    shared_config.write_text("{}", encoding="utf-8")
    nanobot_source = tmp_path / "nanobot"
    nanobot_source.mkdir()

    monkeypatch.setattr(router_service, "CHILD_WORKSPACE_TARGET", "/app/nanobot_workspaces")
    monkeypatch.setattr(router_service, "CHILD_SHARED_CONFIG_TARGET", "/app/nanobot_workspaces/config.json")
    monkeypatch.setattr(router_service, "CHILD_NANOBOT_SOURCE_TARGET", "/app/nanobot")
    monkeypatch.setattr(router_service, "HOST_SHARED_CONFIG", shared_config)
    monkeypatch.setattr(router_service, "HOST_NANOBOT_SOURCE", nanobot_source)

    volumes = router_service.build_child_volumes(workspace)

    assert volumes[str(workspace)] == {"bind": "/app/nanobot_workspaces", "mode": "rw"}
    assert volumes[str(shared_config)] == {
        "bind": "/app/nanobot_workspaces/config.json",
        "mode": "ro",
    }
    assert volumes[str(nanobot_source)] == {"bind": "/app/nanobot", "mode": "ro"}


def test_build_child_volumes_rejects_missing_nanobot_source(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared_config = tmp_path / "config.json"
    shared_config.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(router_service, "HOST_SHARED_CONFIG", shared_config)
    monkeypatch.setattr(router_service, "HOST_NANOBOT_SOURCE", tmp_path / "missing-nanobot")

    with pytest.raises(RuntimeError, match="nanobot source missing"):
        router_service.build_child_volumes(workspace)


def test_build_child_volumes_mounts_frontend_skills_and_templates(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared_config = tmp_path / "config.json"
    shared_config.write_text("{}", encoding="utf-8")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    template_dir = tmp_path / "templates"
    template_dir.mkdir()

    monkeypatch.setattr(router_service, "CHILD_WORKSPACE_TARGET", "/app/nanobot_workspaces")
    monkeypatch.setattr(router_service, "CHILD_SHARED_CONFIG_TARGET", "/app/nanobot_workspaces/config.json")
    monkeypatch.setattr(router_service, "HOST_SHARED_CONFIG", shared_config)
    monkeypatch.setattr(router_service, "HOST_NANOBOT_SOURCE", None)

    frontend = FrontendConfig(
        id="feishu-bot-a",
        raw={"provider": "feishu"},
        builtin_skills_dir=skills_dir,
        template_dir=template_dir,
    )
    volumes = router_service.build_child_volumes(workspace, frontend)

    assert volumes[str(skills_dir)] == {
        "bind": "/app/frontend_mounts/feishu-bot-a/skills",
        "mode": "ro",
    }
    assert volumes[str(template_dir)] == {
        "bind": "/app/frontend_mounts/feishu-bot-a/templates",
        "mode": "ro",
    }
