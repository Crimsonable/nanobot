"""Execute the real V2 plugin's shell wrapper against Linux namespaces."""
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from test_adapter import active
from test_adapter import runtime as runtime

from bucket_runtime.file_lock import publication_lock

pytestmark = pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"),
                                reason="requires Linux bubblewrap")


async def wrapped(runtime, submit, command, *, timeout=3000):
    scope = runtime.execution_scope(submit)
    process = await asyncio.create_subprocess_exec("node",
        str(Path(__file__).resolve().parents[2] / "opencode_plugin/scope-test.mjs"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await process.communicate(json.dumps({"scope": scope, "tool": "shell", "hook": True,
        "args": {"command": command, "timeout": timeout, "background": True}}).encode())
    assert process.returncode == 0, err.decode()
    return json.loads(out)


async def execute(args):
    process = await asyncio.create_subprocess_exec("/bin/sh", "-c", args["command"], cwd=args["workdir"],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 10)
        return process.returncode, output.decode()
    finally:
        if process.returncode is None:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


@pytest.mark.asyncio
async def test_real_plugin_hard_timeout_kills_descendants_and_releases_lock(runtime):
    submit = active(runtime)
    workspace = runtime.workspace(submit)
    args = await wrapped(runtime, submit,
        "touch started; (sleep 2; touch escaped) & sleep 30; touch survived", timeout=1000)
    assert args["background"] is False and args["timeout"] == 3000
    code, _ = await execute(args)
    assert code == 124
    assert (workspace / "started").exists()
    async with asyncio.timeout(2):
        async with publication_lock(runtime.user_lock_path(submit)):
            pass
    await asyncio.sleep(2.1)
    assert not (workspace / "escaped").exists()
    assert not (workspace / "survived").exists()


@pytest.mark.asyncio
async def test_real_plugin_network_namespace_clean_environment_and_literal_command(runtime, monkeypatch):
    submit = active(runtime)
    runtime.permissions.defaults.sandbox.network = False
    monkeypatch.setenv("BUCKET_RPC_TOKEN", "host-secret-must-stay-out")
    host_net = os.readlink("/proc/self/ns/net")
    host_pid = os.readlink("/proc/self/ns/pid")
    command = ("test -z \"$BUCKET_RPC_TOKEN\" && readlink /proc/self/ns/net && readlink /proc/self/ns/pid && "
               "grep '^CapEff:' /proc/self/status && printf '%s' 'literal $HOME `id`'")
    args = await wrapped(runtime, submit, command)
    code, output = await execute(args)
    network, pid, caps, literal = output.splitlines()
    assert code == 0 and network != host_net and pid != host_pid
    assert int(caps.split()[1], 16) == 0
    assert literal == "literal $HOME `id`"


@pytest.mark.asyncio
@pytest.mark.parametrize("access", ["read_only", "none"])
async def test_real_plugin_restricts_workspace_mount(runtime, access):
    submit = active(runtime)
    runtime.permissions.defaults.filesystem.workspace = access
    workspace = runtime.workspace(submit)
    path = workspace / "existing"
    path.write_text("original")
    command = "cat existing; printf changed > existing" if access == "read_only" else f"test ! -e '{workspace}'"
    code, output = await execute(await wrapped(runtime, submit, command))
    assert (code != 0 and "original" in output) if access == "read_only" else code == 0
    assert path.read_text() == "original"
