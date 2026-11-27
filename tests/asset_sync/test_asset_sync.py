import fnmatch
import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from agent_backend.config import FrontendsConfig
from asset_sync.__main__ import main
from asset_sync.hashing import file_sha256
from asset_sync.mcp import update_servers
from asset_sync.skills import file_manifest
from asset_sync.sync import frontend_lock


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def sdk(monkeypatch):
    state = SimpleNamespace(
        files={"SKILL.md": b"# demo\r\n", "scripts/run.sh": b"echo hi\n"},
        downloads=[], manifests=[], details=[], versions=[], calls=[], corrupt=False,
        endpoint="http://mcp.invalid/mcp/", fail=False, auth_fields={"auth": {"type": "none"}},
        pages={None: {"items": [
            {"version_id": "uuid-old", "version_name": "v1", "publish_status": "published",
             "created_at": "2026-01-01T00:00:00Z"},
            {"version_id": "uuid-draft", "version_name": "v3", "publish_status": "draft",
             "created_at": "2026-03-01T00:00:00Z"},
        ], "next_page_token": "next"},
            "next": {"items": [
                {"version_id": "uuid-new", "version_name": "v2", "publish_status": "published",
                 "created_at": "2026-02-01T00:00:00Z"},
            ], "next_page_token": None}},
        before_mcp=None, skill_list=[{"path": "skills/demo", "repository_id": 42}],
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            if state.fail:
                raise OSError("catalog unavailable")

        def json(self):
            return {"data": {"mcp_config": {
                "endpoint": state.endpoint, "transport": "streamable_http",
                **state.auth_fields, "capabilities": {"tools": [{"name": "ocr"}]},
            }}}

    class Session:
        def get(self, url, **kwargs):
            state.calls.append((url, kwargs))
            if state.before_mcp:
                state.before_mcp()
            return Response()

        def close(self):
            pass

    class AssetClient:
        def __init__(self, **kwargs):
            state.calls.append(("asset-client", kwargs))
            self.session = Session()

        def list_apps(self, **kwargs):
            assert kwargs["kind"] == "mcp"
            return {"items": [{"id": 49, "name": "ocr"}]}

        def list_asset_versions(self, asset_id, **kwargs):
            state.versions.append((asset_id, kwargs))
            return state.pages[kwargs["page_token"]]

        def list_version_files(self, asset_id, version):
            state.manifests.append((asset_id, version))
            return {"items": [{"path": name, "sha256": hashlib.sha256(content).hexdigest().upper()}
                              for name, content in state.files.items()]}

    class PublicClient:
        def __init__(self, **kwargs):
            state.calls.append(("public-client", kwargs))
            self.session = Session()

        def list_skills(self, **kwargs):
            state.details.append(kwargs)
            return state.skill_list

    def snapshot_download(**kwargs):
        state.downloads.append(kwargs)
        if state.fail:
            raise OSError("download unavailable")
        target = Path(kwargs["local_dir"])
        for name in state.files:
            if not any(fnmatch.fnmatchcase(name, pattern) for pattern in kwargs["allow_patterns"]):
                continue
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"bad" if state.corrupt else state.files[name])
        return str(target)

    module = ModuleType("zhicheng")
    module.ZhichengAssetClient = AssetClient
    module.ZhichengPublicClient = PublicClient
    constants = ModuleType("zhicheng.constants")
    constants.DEFAULT_ZHICHENG_DOMAIN = "http://sdk.invalid"
    snapshots = ModuleType("zhicheng.snapshot_download")
    snapshots.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "zhicheng", module)
    monkeypatch.setitem(sys.modules, "zhicheng.constants", constants)
    monkeypatch.setitem(sys.modules, "zhicheng.snapshot_download", snapshots)
    monkeypatch.setenv("ZHICHENG_TOKEN", "fixture-token")
    monkeypatch.setenv("ZHICHENG_DOMAIN", "http://sdk.invalid")
    return state


@pytest.fixture
def setup(tmp_path):
    common = tmp_path / "common"
    config = common / "web-a/config/custom.json"
    write_json(config, {"model": "keep-model", "mcp": {"servers": {
        "keep": {"type": "remote", "url": "http://keep.invalid"}}}})
    registry = common / "frontends.json"
    write_json(registry, {"frontends": [
        {"frontend_id": "web-a", "provider": "web", "asset_sync": True,
         "bucket": {"common_root": str(common), "opencode": {"config_file": str(config)}}},
        {"frontend_id": "web-b", "provider": "web", "asset_sync": False, "bucket": {}},
    ]})
    return SimpleNamespace(
        args=["--frontends", str(registry)],
        common=common, config=config, registry=registry,
        skill=common / "web-a/app/skills/demo",
    )



@pytest.mark.parametrize("auth_fields", [
    {"auth": {"type": "none"}},
    {"auth": {"type": "bearer", "header": "Authorization"}},
    {"auth": {"type": "unsupported"}},
    {"auth": None},
    {},
], ids=["none", "bearer", "unknown", "null", "missing"])
def test_entrypoint_latest_version_hash_before_download_and_mcp_mtime(setup, sdk, auth_fields):
    sdk.auth_fields = auth_fields
    existing = json.loads(setup.config.read_text())
    existing["mcp"]["servers"]["ocr"] = {
        "type": "remote", "url": sdk.endpoint, "oauth": {"clientId": "old-client"},
        "headers": {"Authorization": "Bearer old-fixture-token"},
    }
    write_json(setup.config, existing)
    assert main(setup.args) == 0
    assert sdk.details == [{"page_token": None}]
    assert sdk.manifests == [("42", "v2")]
    download = sdk.downloads[0]
    assert download["repo_type"] == "skill"
    assert download["revision"] == "v2"
    assert download["allow_patterns"] == ["SKILL.md", "scripts/run.sh"]
    assert (setup.skill / "SKILL.md").read_bytes() == sdk.files["SKILL.md"]
    assert not (setup.common / "web-b").exists()
    assert not (setup.config.parent / "opencode-server.json").exists()
    data = json.loads(setup.config.read_text())
    assert data["model"] == "keep-model"
    assert set(data["mcp"]["servers"]) == {"keep", "ocr"}
    assert data["mcp"]["servers"]["ocr"] == {
        "type": "remote", "url": sdk.endpoint, "oauth": False}
    url, request = next(call for call in sdk.calls if "/catalog/" in call[0])
    assert url == "http://sdk.invalid/asset/api/v1/catalog/apps/49"
    assert request["headers"]["Authorization"] == "Bearer fixture-token"

    times = setup.config.stat().st_mtime_ns, (setup.skill / "SKILL.md").stat().st_mtime_ns
    assert main(setup.args) == 0
    assert len(sdk.downloads) == 1
    assert times == (setup.config.stat().st_mtime_ns, (setup.skill / "SKILL.md").stat().st_mtime_ns)


def test_only_changed_files_download_and_deleted_remote_files_removed(setup, sdk):
    assert main(setup.args) == 0
    sdk.files["SKILL.md"] = b"# changed\n"
    sdk.files["references/x.bin"] = b"\x00\xff"
    assert main(setup.args) == 0
    assert sdk.downloads[-1]["allow_patterns"] == ["SKILL.md", "references/x.bin"]
    del sdk.files["scripts/run.sh"]
    assert main(setup.args) == 0
    assert len(sdk.downloads) == 2
    assert not (setup.skill / "scripts/run.sh").exists()


def test_entrypoint_syncs_all_manifest_files_without_name_filters(setup, sdk):
    bytecode = "scripts/__pycache__/tool.cpython-313.pyc"
    sdk.files.update({
        ".DS_Store": b"\x00\xffdesktop",
        ".git/HEAD": b"ref: refs/heads/main\n",
        bytecode: b"\xf3\r\n\x00bytecode",
        "scripts/cached.pyo": b"\x00\xffoptimized",
        "standalone.pyc": b"\x00\xffstandalone",
    })
    assert main(setup.args) == 0
    assert set(sdk.downloads[0]["allow_patterns"]) == set(sdk.files)
    assert {path.relative_to(setup.skill).as_posix(): path.read_bytes()
            for path in setup.skill.rglob("*") if path.is_file()} == sdk.files

    mtimes = {name: (setup.skill / name).stat().st_mtime_ns for name in sdk.files}
    assert main(setup.args) == 0
    assert len(sdk.downloads) == 1
    assert mtimes == {name: (setup.skill / name).stat().st_mtime_ns for name in sdk.files}

    sdk.files[bytecode] = b"\x00\xffupdated bytecode"
    del sdk.files[".DS_Store"]
    (setup.skill / "scripts/__pycache__/local.pyc").write_bytes(b"local cache")
    assert main(setup.args) == 0
    assert len(sdk.downloads) == 2
    assert sdk.downloads[-1]["allow_patterns"] == [bytecode]
    assert {path.relative_to(setup.skill).as_posix(): path.read_bytes()
            for path in setup.skill.rglob("*") if path.is_file()} == sdk.files


def test_download_mismatch_and_failure_preserve_installed_skill(setup, sdk):
    assert main(setup.args) == 0
    previous = (setup.skill / "SKILL.md").read_bytes()
    sdk.files["SKILL.md"] = b"# new\n"
    sdk.corrupt = True
    assert main(setup.args) == 1
    assert (setup.skill / "SKILL.md").read_bytes() == previous
    sdk.fail = True
    assert main(setup.args) == 1
    assert (setup.skill / "SKILL.md").read_bytes() == previous
    sdk.corrupt = sdk.fail = False
    assert main(setup.args) == 0
    assert (setup.skill / "SKILL.md").read_bytes() == b"# new\n"


def test_dry_run_only_reads_file_manifest_and_config(setup, sdk):
    before = setup.config.read_bytes(), setup.config.stat().st_mtime_ns
    assert main(setup.args + ["--dry-run"]) == 0
    assert sdk.manifests
    assert not sdk.downloads
    assert not setup.skill.exists()
    assert not (setup.common / "web-a/.asset-sync.lock").exists()
    assert before == (setup.config.read_bytes(), setup.config.stat().st_mtime_ns)


def test_check_and_disabled_frontends_make_no_sdk_calls(setup, sdk):
    assert main(setup.args + ["--check-config"]) == 0
    assert sdk.calls == []
    assert main(setup.args + ["--frontend", "web-b"]) == 2
    assert main(setup.args + ["--frontend", "unknown"]) == 2
    data = json.loads(setup.registry.read_text())
    data["frontends"][0]["asset_sync"] = False
    write_json(setup.registry, data)
    assert main(setup.args) == 0
    assert sdk.calls == []


def test_new_published_version_is_selected_next_run(setup, sdk):
    assert main(setup.args) == 0
    sdk.pages[None]["items"][1]["publish_status"] = "published"
    sdk.files["SKILL.md"] = b"# new release"
    assert main(setup.args) == 0
    assert sdk.manifests[-1] == ("42", "v3")
    assert sdk.downloads[-1]["revision"] == "v3"


def test_failed_frontend_does_not_stop_others(setup, sdk):
    data = json.loads(setup.registry.read_text())
    data["frontends"].insert(0, {"frontend_id": "aaa", "provider": "web",
                                "asset_sync": True, "bucket": {}})
    write_json(setup.registry, data)
    assert main(setup.args) == 1
    assert (setup.skill / "SKILL.md").exists()


def test_config_edit_during_download_is_preserved(setup, sdk):
    def edit():
        data = json.loads(setup.config.read_text())
        data["model"] = "edited-while-fetching"
        write_json(setup.config, data)
    sdk.before_mcp = edit
    assert main(setup.args) == 0
    assert json.loads(setup.config.read_text())["model"] == "edited-while-fetching"


def test_concurrent_sync_fails_without_removing_staging(setup, sdk):
    with frontend_lock(setup.common / "web-a"):
        assert main(setup.args) == 1
    assert not sdk.manifests
    assert main(setup.args) == 0


def test_interrupted_skill_is_restored_before_network_failure(setup, sdk):
    assert main(setup.args) == 0
    previous = setup.skill.with_name(".demo.previous")
    setup.skill.rename(previous)
    sdk.files["SKILL.md"] = b"changed"
    sdk.fail = True
    assert main(setup.args) == 1
    assert (setup.skill / "SKILL.md").read_bytes() == b"# demo\r\n"
    assert not previous.exists()


def test_hashes_original_bytes(tmp_path):
    path = tmp_path / "raw"
    payload = b"\xef\xbb\xbfA\r\n\x00\xff"
    path.write_bytes(payload)
    assert file_sha256(path) == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../x", "a\\x", "C:/x", "./x", ""])
def test_unsafe_remote_paths_fail(name):
    with pytest.raises(ValueError):
        file_manifest([{"path": name, "sha256": "0" * 64}])


@pytest.mark.parametrize("payload", [
    {"items": [{"path": "SKILL.md"}]},
    {"items": [{"path": "SKILL.md", "sha256": "no"}]},
    {"items": [{"path": "SKILL.md", "sha256": "0" * 64}], "next_page_token": "more"},
    [{"path": "SKILL.md", "sha256": "0" * 64}, {"path": "SKILL.md", "sha256": "1" * 64}],
    [{"path": "folder/file", "sha256": "0" * 64}],
])
def test_invalid_manifests_fail(payload):
    with pytest.raises(ValueError):
        file_manifest(payload)


def test_switch_is_strict_bool():
    with pytest.raises(ValueError):
        FrontendsConfig.model_validate({"frontends": [{
            "frontend_id": "a", "provider": "web", "bucket": {}, "asset_sync": "true"}]})


def test_mcp_key_order_has_no_effect(tmp_path):
    path = tmp_path / "server.json"
    write_json(path, {"mcp": {"servers": {"x": {"type": "remote", "url": "http://x"}}}})
    before = path.stat().st_mtime_ns
    assert update_servers(path, {"x": {"url": "http://x", "type": "remote"}}) == set()
    assert path.stat().st_mtime_ns == before


def test_all_enabled_frontends_receive_discovered_assets(setup, sdk):
    data = json.loads(setup.registry.read_text())
    data["frontends"][1]["asset_sync"] = True
    data["frontends"][1]["bucket"]["common_root"] = str(setup.common)
    write_json(setup.registry, data)
    second_config = setup.common / "web-b/config/opencode-server.json"
    write_json(second_config, {"model": "other-model"})
    assert main(setup.args) == 0
    assert (setup.common / "web-b/app/skills/demo/SKILL.md").read_bytes() == sdk.files["SKILL.md"]
    assert json.loads(second_config.read_text())["model"] == "other-model"
    assert "ocr" in json.loads(second_config.read_text())["mcp"]["servers"]
    assert len(sdk.details) == 1


def test_discovery_rejects_colliding_skill_names(setup, sdk):
    sdk.skill_list.append({"path": "other/demo", "repository_id": 43})
    assert main(setup.args) == 1
    assert not sdk.downloads
    assert not setup.skill.exists()


def test_no_published_version_does_not_download(setup, sdk):
    sdk.pages = {None: {"items": [], "next_page_token": None}}
    assert main(setup.args) == 1
    assert not sdk.downloads
    assert "ocr" in json.loads(setup.config.read_text())["mcp"]["servers"]


def test_module_entrypoint_with_sdk_mocks(setup, sdk, monkeypatch):
    import runpy

    monkeypatch.delitem(sys.modules, "asset_sync.__main__", raising=False)
    monkeypatch.setattr(sys, "argv", ["asset_sync", *setup.args])
    with pytest.raises(SystemExit) as result:
        runpy.run_module("asset_sync", run_name="__main__")
    assert result.value.code == 0
    assert sdk.manifests == [("42", "v2")]
    assert setup.skill.is_dir()


def test_snapshot_patterns_match_literal_file_names(setup, sdk):
    sdk.files["references/[template].md"] = b"template"
    assert main(setup.args) == 0
    assert (setup.skill / "references/[template].md").read_bytes() == b"template"
    assert "references/[[]template].md" in sdk.downloads[0]["allow_patterns"]


def test_cronjob_uses_current_cli_arguments():
    import yaml

    from asset_sync.__main__ import _parser

    root = Path(__file__).resolve().parents[2]
    documents = list(yaml.safe_load_all((root / "k8s/backend_nfs/asset-sync.yaml").read_text(encoding="utf-8")))
    assert [document["kind"] for document in documents] == ["CronJob"]
    pod = documents[0]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    args = _parser().parse_args(pod["containers"][0]["args"])
    assert args.frontends == Path("/mnt/common/frontends.json")
