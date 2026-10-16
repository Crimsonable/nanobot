"""Fail-closed Linux executor; the parent never evaluates user shell text."""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from agent_backend.config import SandboxConfig, UserSandbox


def contained(path: str, root: Path, *, directory: bool = False) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    # Reject symlinks at every component, not only a final symlink outside the root.
    absolute = Path(os.path.abspath(candidate))
    if not absolute.is_relative_to(root.resolve()):
        raise PermissionError("path outside user workspace")
    for part in [absolute, *absolute.parents]:
        if part == root.parent:
            break
        if part.is_symlink():
            raise PermissionError("symbolic links are not accepted by host file operations")
    resolved = absolute.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()):
        raise PermissionError("path outside user workspace")
    if directory and not resolved.is_dir():
        raise ValueError("working directory is not a directory")
    if not directory and (not resolved.is_file() or resolved.stat().st_nlink != 1):
        raise PermissionError("only regular, singly-linked files can be published")
    return resolved


class SandboxExecutor:
    def __init__(self, config: SandboxConfig, output_limit: int):
        self.config, self.output_limit = config, output_limit
        self.processes: dict[str, set[asyncio.subprocess.Process]] = {}

    def working_directory(self, workspace: Path, public: list[Path], workdir: str | None,
                          workspace_access: str) -> Path:
        if not workdir:
            return workspace if workspace_access != "none" else Path("/")
        candidate = Path(workdir)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        roots = [*public, *([workspace] if workspace_access != "none" else [])]
        root = next((root for root in roots if candidate.resolve().is_relative_to(root)), None)
        if root is None:
            raise PermissionError("working directory outside user permission scope")
        return contained(str(candidate), root, directory=True)

    def argv(self, workspace: Path, public: list[Path], cwd: Path, command: str, *,
             sandbox: UserSandbox | None = None, workspace_access: str = "read_write",
             private_files: list[Path] = ()) -> list[str]:
        sandbox = sandbox or UserSandbox()
        if not sandbox.enabled:
            return [self.config.shell, "-c", command]
        if sys.platform != "linux" or not self.config.binary.is_file():
            raise RuntimeError("Linux bubblewrap is required; unsandboxed execution is forbidden")
        args = [str(self.config.binary), "--unshare-all", "--die-with-parent", "--new-session",
                "--cap-drop", "ALL", "--clearenv", "--proc", "/proc", "--dev", "/dev",
                "--tmpfs", "/tmp", "--dir", "/home/agent"]
        if sandbox.network:
            args.append("--share-net")
            if Path("/etc/resolv.conf").exists():
                args.extend(["--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf"])
        for root in [*self.config.readonly_roots, *public]:
            if root.exists():
                args.extend(["--ro-bind", str(root), str(root)])
            elif root in public:
                raise RuntimeError("configured public directory is missing")
        if workspace_access != "none":
            args.extend(["--bind" if workspace_access == "read_write" else "--ro-bind", str(workspace), str(workspace)])
        for path in private_files:
            if any(path.resolve().is_relative_to(root.resolve()) for root in [*self.config.readonly_roots, *public]):
                args.extend(["--ro-bind", "/dev/null", str(path.resolve())])
        args.extend(["--chdir", str(cwd),
                     "--setenv", "HOME", "/home/agent", "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
                     "--setenv", "LANG", "C.UTF-8", "--setenv", "TMPDIR", "/tmp",
                     "--setenv", "PLAYWRIGHT_BROWSERS_PATH", "/opt/toolchain/browsers",
                     "--", self.config.shell, "-c", command])
        return args

    async def kill(self, request_id: str, *, drain: bool = False) -> None:
        for process in list(self.processes.get(request_id, set())):
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if drain and process.stdout is not None:
                # Once the collector stops on its size limit, drain the killed process's
                # bounded pipe buffer; Process.wait can otherwise wait forever on that pipe.
                while await process.stdout.read(65536):
                    pass
            await process.wait()

    async def execute(self, request_id: str, workspace: Path, public: list[Path],
                      command: str, workdir: str | None = None, timeout: float = 120, *,
                      sandbox: UserSandbox | None = None, workspace_access: str = "read_write",
                      private_files: list[Path] = ()) -> dict:
        sandbox = sandbox or UserSandbox()
        cwd = self.working_directory(workspace, public, workdir, workspace_access)
        process = await asyncio.create_subprocess_exec(
            *self.argv(workspace, public, cwd, command, sandbox=sandbox, workspace_access=workspace_access,
                       private_files=private_files),
            cwd=str(cwd) if not sandbox.enabled else None, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={"PATH": "/usr/bin:/bin"}, start_new_session=True, close_fds=True)
        self.processes.setdefault(request_id, set()).add(process)
        captured = bytearray()

        async def collect():
            while chunk := await process.stdout.read(65536):
                if len(captured) + len(chunk) > self.output_limit:
                    raise RuntimeError("sandbox output limit exceeded")
                captured.extend(chunk)
            await process.wait()

        try:
            await asyncio.wait_for(collect(), timeout=max(0.01, timeout))
            return {"output": captured.decode(errors="replace"), "exit_code": process.returncode}
        finally:
            await self.kill(request_id, drain=True)
            self.processes.get(request_id, set()).discard(process)

    async def probe(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        result = await self.execute("sandbox-probe", directory, [], "printf sandbox-ok", timeout=10)
        if result["exit_code"] or result["output"] != "sandbox-ok":
            raise RuntimeError("bubblewrap startup probe failed: " + result["output"][:300])
