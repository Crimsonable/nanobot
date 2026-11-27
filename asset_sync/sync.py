from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from pathlib import Path

from agent_backend.common import frontend_segment
from asset_sync.config import Assets
from asset_sync.mcp import update_servers
from asset_sync.skills import sync_skill
from asset_sync.source import Source

Log = Callable[[str], None]


def checked_path(path: Path, root: Path) -> Path:
    path, root = path.absolute(), root.absolute()
    if not path.is_relative_to(root) or path.resolve() != path:
        raise ValueError(f"sync path must stay within {root} without symbolic links")
    return path


@contextmanager
def frontend_lock(root: Path):
    path = checked_path(root / ".asset-sync.lock", root)
    with path.open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                if not stream.read(1):
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError(f"{root.name} already has an active asset sync") from None
        yield


def _sync_mcp(frontend_id: str, assets: Assets, path: Path, source: Source,
              dry_run: bool, log: Log) -> bool:
    desired = {}
    ok = True
    for spec in assets.mcp:
        try:
            desired[spec.name] = source.mcp_server(spec)
        except Exception as exc:
            ok = False
            log(f"{frontend_id} mcp {spec.name}: error: {exc}")
    if desired:
        try:
            changed = update_servers(path, desired, dry_run=dry_run)
        except Exception as exc:
            log(f"{frontend_id} mcp: error: {exc}")
            return False
        for name in desired:
            status = "unchanged"
            if name in changed:
                status = "would update" if dry_run else "updated"
            log(f"{frontend_id} mcp {name}: {status}")
    return ok


def sync_frontend(frontend_id: str, assets: Assets, common_root: Path, source: Source,
                  *, config_path: Path | None = None, dry_run: bool = False,
                  log: Log = print) -> bool:
    root = checked_path(Path(common_root) / frontend_segment(frontend_id), common_root)
    config_path = checked_path(config_path or root / "config/opencode-server.json", root / "config")
    skills = checked_path(root / "app/skills", root / "app")
    ok = True
    with nullcontext() if dry_run else frontend_lock(root):
        for spec in assets.skills:
            try:
                target = checked_path(skills / spec.target_name, skills)
                changed = sync_skill(spec, target, source, dry_run=dry_run)
                status = "unchanged"
                if changed:
                    status = "would update" if dry_run else "updated"
                log(f"{frontend_id} skill {spec.target_name}: {status}")
            except Exception as exc:
                ok = False
                log(f"{frontend_id} skill {spec.target_name}: error: {exc}")
        mcp_ok = _sync_mcp(frontend_id, assets, config_path, source, dry_run, log)
    return ok and mcp_ok
