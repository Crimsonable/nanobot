from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
from pathlib import Path

from agent_backend.common import scoped_public_path
from agent_backend.config import RuntimeConfig
from bucket_runtime.opencode_client import OpenCodeClient


class Supervisor:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.password = secrets.token_urlsafe(32)
        self.rpc_token = secrets.token_urlsafe(32)
        self.client = OpenCodeClient(config.opencode, self.password)
        self.process: asyncio.subprocess.Process | None = None

    def server_config(self) -> dict:
        raw = json.loads(self.config.opencode.config_file.read_text(encoding="utf-8"))
        skills = raw.get("skills") or {}
        if "paths" in skills:
            skills["paths"] = [str(scoped_public_path(Path(path), self.config.common_root,
                self.config.frontend_id)) for path in skills["paths"]]
        return raw

    async def start(self) -> None:
        config = self.config
        oc = config.opencode
        oc.context_root.mkdir(parents=True, exist_ok=True)
        if not oc.binary.is_file() or not os.access(oc.binary, os.X_OK):
            raise RuntimeError(f"OpenCode executable missing or not executable: {oc.binary}")
        if not oc.plugin_file.is_file():
            raise RuntimeError(f"mounted runtime plugin missing: {oc.plugin_file}")
        raw = self.server_config()
        plugin = oc.plugin_file.resolve().as_uri()
        plugins = raw.setdefault("plugin", [])
        if plugin not in plugins:
            plugins.append(plugin)
        home = config.state_root / "opencode-home"
        home.mkdir(parents=True, exist_ok=True)
        # v1.18.31 checks the global config's package lock even for a bundled local plugin.
        # Seed bundled plugin dependencies; other configured plugins use OpenCode loading.
        global_config = home / "config" / "opencode"
        global_config.mkdir(parents=True, exist_ok=True)
        for name in ["package.json", "package-lock.json"]:
            shutil.copyfile(oc.dependency_dir / name, global_config / name)
        modules = global_config / "node_modules"
        if not (oc.dependency_dir / "node_modules/@opencode-ai/plugin/package.json").is_file():
            raise RuntimeError("offline plugin dependencies missing")
        target = (oc.dependency_dir / "node_modules").resolve()
        if (modules.exists() or modules.is_symlink()) and modules.resolve() != target:
            raise RuntimeError("plugin dependency directory does not match configured source")
        if not modules.exists():
            modules.symlink_to(target, target_is_directory=True)
        env = {key: os.environ[key] for key in ["PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"]
               if key in os.environ}
        env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
                    "XDG_DATA_HOME": str(home / "data"), "XDG_CACHE_HOME": str(home / "cache"),
                    "XDG_STATE_HOME": str(home / "state"), "OPENCODE_CONFIG_CONTENT": json.dumps(raw),
                    "OPENCODE_SERVER_PASSWORD": self.password, "OPENCODE_SERVER_USERNAME": "opencode",
                    "BUCKET_RPC_URL": f"http://127.0.0.1:{config.rpc_port}",
                    "BUCKET_RPC_TOKEN": self.rpc_token})
        self.process = await asyncio.create_subprocess_exec(str(oc.binary), "serve", "--hostname",
            "127.0.0.1", "--port", str(oc.port), cwd=oc.context_root, env=env,
            stdin=asyncio.subprocess.DEVNULL)
        for _ in range(60):
            if self.process.returncode is not None:
                raise RuntimeError("OpenCode exited during startup")
            try:
                if await self.client.health():
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)
        await self.stop()
        raise RuntimeError("OpenCode startup timed out")

    async def stop(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 10)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        await self.client.close()
