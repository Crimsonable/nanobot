import json
from types import SimpleNamespace

import pytest

from agent_backend.config import RuntimeConfig
from bucket_runtime.supervisor import Supervisor


@pytest.mark.asyncio
async def test_restart_repoints_old_plugin_dependency_link_to_shared_source(tmp_path, monkeypatch):
    source = tmp_path / "source" / "opencode_plugin"
    module = source / "node_modules/@opencode-ai/plugin/package.json"
    module.parent.mkdir(parents=True)
    module.write_text("{}")
    plugin = source / "dist/index.mjs"
    plugin.parent.mkdir()
    plugin.write_text("export default {}")
    for name in ["package.json", "package-lock.json"]:
        (source / name).write_text("{}")
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\nexit 1\n")
    binary.chmod(0o555)
    config_file = tmp_path / "opencode.json"
    config_file.write_text(json.dumps({"autoupdate": False}))
    config = RuntimeConfig(bucket_id="b", state_root=tmp_path / "state",
        workspace_root=tmp_path / "users", profiles={"p": {"model": "test/test"}},
        opencode={"binary": binary, "context_root": tmp_path / "context",
            "config_file": config_file, "plugin_file": plugin, "dependency_dir": source})
    modules = config.state_root / "opencode-home/config/opencode/node_modules"
    modules.parent.mkdir(parents=True)
    try:
        modules.symlink_to(tmp_path / "old-image-plugin/node_modules", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")

    async def spawn(*args, **kwargs):
        assert args[0] == str(binary)
        assert json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])["plugin"] == [plugin.as_uri()]
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    supervisor = Supervisor(config)
    try:
        with pytest.raises(RuntimeError, match="exited during startup"):
            await supervisor.start()
        assert modules.resolve() == module.parents[2]
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_missing_mounted_binary_fails_before_launch(tmp_path):
    config = RuntimeConfig(bucket_id="b", state_root=tmp_path / "state",
        workspace_root=tmp_path / "users", profiles={"p": {"model": "test/test"}},
        opencode={"binary": tmp_path / "missing", "context_root": tmp_path / "context"})
    supervisor = Supervisor(config)
    try:
        with pytest.raises(RuntimeError, match="executable missing"):
            await supervisor.start()
    finally:
        await supervisor.stop()
