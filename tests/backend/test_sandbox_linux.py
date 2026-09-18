import asyncio
import shutil
import sys

import pytest

from agent_backend.config import SandboxConfig
from bucket_runtime.sandbox_executor import SandboxExecutor

pytestmark = pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"),
                                reason="requires Linux bubblewrap")


@pytest.mark.asyncio
async def test_readonly_public_clean_env_and_no_background_survivors(tmp_path, monkeypatch):
    own, public, other = (tmp_path / name for name in ["own", "public", "other"])
    for path in [own, public, other]:
        path.mkdir()
    (public / "SKILL.md").write_text("trusted")
    (other / "secret").write_text("private")
    monkeypatch.setenv("BUCKET_RPC_TOKEN", "must-not-reach-sandbox")
    executor = SandboxExecutor(SandboxConfig(), 4096)
    result = await executor.execute("read", own, [public],
        f"test -z \"$BUCKET_RPC_TOKEN\" && test ! -e '{other}' && cat '{public / 'SKILL.md'}'")
    assert result == {"output": "trusted", "exit_code": 0}
    result = await executor.execute("write", own, [public], f"printf altered > '{public / 'SKILL.md'}'")
    assert result["exit_code"] != 0
    assert (public / "SKILL.md").read_text() == "trusted"
    await executor.execute("background", own, [], "(sleep 0.5; printf escaped > escaped) >/dev/null 2>&1 &")
    await asyncio.sleep(0.7)
    assert not (own / "escaped").exists()


@pytest.mark.asyncio
async def test_timeout_and_output_limit_kill_processes(tmp_path):
    executor = SandboxExecutor(SandboxConfig(), 1024)
    with pytest.raises(TimeoutError):
        await executor.execute("timeout", tmp_path, [], "sleep 30", timeout=0.05)
    assert not any(p.returncode is None for p in executor.processes.get("timeout", []))
    with pytest.raises(RuntimeError, match="output limit"):
        await executor.execute("output", tmp_path, [], "yes abcdefghijklmnopqrstuvwxyz", timeout=3)
    assert not any(p.returncode is None for p in executor.processes.get("output", []))
