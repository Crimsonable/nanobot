from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import sys
from pathlib import Path

from agent_backend.config import RuntimeConfig
from agent_backend.transfers import file_hash
from bucket_runtime.opencode_client import OpenCodeClient


class Supervisor:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.password = secrets.token_urlsafe(32)
        self.rpc_token = secrets.token_urlsafe(32)
        self.client = OpenCodeClient(config.opencode, self.password)
        self.process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        config = self.config
        oc = config.opencode
        if file_hash(oc.binary) != oc.sha256:
            raise RuntimeError("OpenCode binary SHA256 mismatch")
        oc.context_root.mkdir(parents=True, exist_ok=True)
        if any(oc.context_root.iterdir()):
            raise RuntimeError("OpenCode context_root must be an empty trusted directory")
        if not oc.plugin_file.is_file():
            raise RuntimeError("bundled runtime plugin missing")
        raw = json.loads(oc.config_file.read_text(encoding="utf-8"))
        # Providers/models are trusted operator configuration. Plugins and permissions are owned here.
        if set(raw) - {"provider", "model", "small_model", "enabled_providers", "disabled_providers"}:
            raise ValueError("OpenCode config may contain provider/model fields only")
        for provider in raw.get("provider", {}).values():
            if provider.get("npm") not in {None, "@ai-sdk/openai-compatible", "@ai-sdk/openai", "@ai-sdk/anthropic"}:
                raise ValueError("provider requires an unbundled SDK; offline runtime installation is forbidden")
        raw.update({"plugin": [oc.plugin_file.resolve().as_uri()], "permission": {"*": "deny"},
                    "mcp": {}, "lsp": False, "formatter": False, "autoupdate": False,
                    "share": "disabled", "snapshot": False,
                    "compaction": {"auto": False, "prune": False}})
        home = config.state_root / "opencode-home"
        home.mkdir(parents=True, exist_ok=True)
        # v1.18.31 checks the global config's package lock even for a bundled local plugin.
        # Seed the exact build-time dependencies so initialization never invokes npm download.
        global_config = home / "config" / "opencode"
        global_config.mkdir(parents=True, exist_ok=True)
        for name in ["package.json", "package-lock.json"]:
            shutil.copyfile(oc.dependency_dir / name, global_config / name)
        modules = global_config / "node_modules"
        if not (oc.dependency_dir / "node_modules/@opencode-ai/plugin/package.json").is_file():
            raise RuntimeError("offline plugin dependencies missing")
        if not modules.exists():
            modules.symlink_to((oc.dependency_dir / "node_modules").resolve(), target_is_directory=True)
        env = {key: os.environ[key] for key in ["PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"]
               if key in os.environ}
        env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
                    "XDG_DATA_HOME": str(home / "data"), "XDG_CACHE_HOME": str(home / "cache"),
                    "XDG_STATE_HOME": str(home / "state"), "OPENCODE_CONFIG_CONTENT": json.dumps(raw),
                    "OPENCODE_SERVER_PASSWORD": self.password, "OPENCODE_SERVER_USERNAME": "opencode",
                    "OPENCODE_DISABLE_PROJECT_CONFIG": "true", "OPENCODE_DISABLE_AUTOUPDATE": "true",
                    "OPENCODE_DISABLE_MODELS_FETCH": "true", "OPENCODE_DISABLE_AUTOCOMPACT": "true",
                    "OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER": "true",
                    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true", "OPENCODE_DISABLE_EXTERNAL_SKILLS": "true",
                    "OPENCODE_DISABLE_CLAUDE_CODE": "true", "OPENCODE_DISABLE_LSP_DOWNLOAD": "true",
                    "OPENCODE_DISABLE_EMBEDDED_WEB_UI": "true",
                    "BUCKET_RPC_URL": f"http://127.0.0.1:{config.rpc_port}",
                    "BUCKET_RPC_TOKEN": self.rpc_token,
                    "BUCKET_EXEC_PYTHON": sys.executable,
                    "BUCKET_EXEC_CLIENT": str(Path(__file__).with_name("exec_client.py").resolve())})
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
