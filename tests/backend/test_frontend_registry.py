import json

import pytest

from agent_backend.config import FrontendsConfig, load_frontends


@pytest.fixture
def registry(tmp_path):
    common = tmp_path / "common"
    entries = []
    for frontend in ["web-a", "web-b"]:
        own = common / frontend
        (own / "app/skills").mkdir(parents=True)
        (own / "app/templates").mkdir()
        (own / "app/templates/README.txt").write_text(frontend)
        (own / "app/templates/AGENTS.md").write_text(frontend)
        (own / "app/manual.txt").write_text(frontend)
        (own / "config").mkdir()
        (own / "config/future-secret.txt").write_text("not-a-known-config-file")
        (own / "config/opencode-server.json").write_text(
            json.dumps(
                {
                    "model": "local/" + frontend,
                    "instructions": [str(own / "app/templates/AGENTS.md")],
                    "providers": {"local": {"settings": {"apiKey": "model-secret-" + frontend}}},
                    "skills": [str(own / "app/skills")],
                }
            )
        )
        (own / "config/user-permissions.yaml").write_text(
            json.dumps(
                {
                    "defaults": {
                        "filesystem": {
                            "workspace": "read_only" if frontend == "web-a" else "read_write"
                        }
                    },
                    "users": [
                        {"user_id": "alice", "session": {"tools": {"shell": frontend == "web-b"}}}
                    ],
                }
            )
        )
        (own / "config/session-logging.json").write_text(
            json.dumps({"enabled": frontend == "web-a"})
        )
        entries.append(
            {
                "frontend_id": frontend,
                "provider": "web",
                "instance_idle_ttl_seconds": 1 if frontend == "web-a" else 0,
                "bucket": {
                    "common_root": str(common),
                    "logging_file": str(common / "{frontend_id}/config/session-logging.json"),
                },
            }
        )
    path = common / "frontends.json"
    path.write_text(json.dumps({"frontends": entries}))
    return path, load_frontends(path)


def test_cross_frontend_configuration_paths_fail_closed(registry):
    path, config = registry
    for section in ["opencode", "permissions_file"]:
        frontend = config.frontend("web-a")
        wrong = path.parent / "web-b"
        if section == "opencode":
            frontend.bucket.opencode.config_file = wrong / "opencode-server.json"
        else:
            setattr(frontend.bucket, section, wrong / "user-permissions.yaml")
        with pytest.raises(ValueError, match="must belong to this frontend"):
            frontend.bucket_config(path, "opencode-bucket-0123456789ab")


@pytest.mark.parametrize("section", ["opencode", "permissions_file", "storage"])
def test_configuration_and_content_cannot_swap_subtrees(registry, section):
    from agent_backend.config import StorageConfig

    path, config = registry
    frontend = config.frontend("web-a")
    own = path.parent / frontend.frontend_id
    if section == "opencode":
        frontend.bucket.opencode.config_file = own / "app/opencode-server.json"
    elif section == "permissions_file":
        frontend.bucket.permissions_file = own / "app/user-permissions.yaml"
    elif section == "storage":
        frontend.attachment_storage = StorageConfig(
            endpoint="http://minio:9000",
            bucket="test",
            access_key_file=own / "app/access-key",
            secret_key_file=own / "config/secret-key",
        )
    with pytest.raises(ValueError, match="must belong to this frontend"):
        frontend.bucket_config(path, "opencode-bucket-0123456789ab")


def test_config_symlink_cannot_alias_public_content(registry):
    path, config = registry
    frontend = config.frontend("web-a")
    own = path.parent / frontend.frontend_id
    link = own / "config/link"
    try:
        link.symlink_to(own / "app", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    frontend.bucket.permissions_file = link / "manual.txt"
    with pytest.raises(ValueError, match="without symbolic links"):
        frontend.bucket_config(path, "opencode-bucket-0123456789ab")


def test_registry_rejects_duplicate_ids_and_global_frontend_credentials(registry):
    path, config = registry
    raw = config.model_dump()
    assert FrontendsConfig.model_validate(raw).model_dump() == raw
    raw["frontends"][1]["frontend_id"] = raw["frontends"][0]["frontend_id"]
    with pytest.raises(ValueError, match="duplicate"):
        FrontendsConfig.model_validate(raw)
    raw["frontends"] = raw["frontends"][:1]
    raw["frontends"][0]["api_key_file"] = "/old/web-api-key"
    with pytest.raises(ValueError, match="Extra inputs"):
        FrontendsConfig.model_validate(raw)
    path.write_text(json.dumps({"frontend_id": "web-a", "provider": "web", "bucket": {}}))
    with pytest.raises(ValueError, match="frontends"):
        load_frontends(path)
