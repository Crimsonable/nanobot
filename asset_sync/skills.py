from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from asset_sync.config import SkillSpec
from asset_sync.hashing import file_sha256
from asset_sync.source import Source


def file_manifest(payload: object) -> dict[str, str]:
    """Validate list_version_files() before using its paths or checksums."""
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not items:
        raise ValueError("skill file list must be a nonempty list")
    if isinstance(payload, dict) and payload.get("next_page_token"):
        raise ValueError("incomplete skill file list")
    if isinstance(payload, dict) and payload.get("total", len(items)) != len(items):
        raise ValueError("incomplete skill file list")
    files = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("skill file entry must be an object")
        name, checksum = item.get("path"), item.get("sha256")
        if not isinstance(name, str) or not name:
            raise ValueError("skill file path is required")
        path = PurePosixPath(name)
        if (not path.parts or path.is_absolute() or path.as_posix() != name or ".." in path.parts
                or any(c in name for c in "\\:\x00") or name in files):
            raise ValueError(f"unsafe or duplicate skill file path: {name}")
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
            raise ValueError(f"missing or invalid sha256 for {name}")
        files[name] = checksum.lower()
    if "SKILL.md" not in files:
        raise ValueError("skill file list must include root SKILL.md")
    for name in files:
        if any(parent.as_posix() in files for parent in PurePosixPath(name).parents):
            raise ValueError(f"conflicting skill file path: {name}")
    return files


def local_files(root: Path) -> dict[str, str]:
    files = {}
    if not root.exists():
        return files
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"skill target must be a directory without symbolic links: {root}")
    for directory, subdirs, names in os.walk(root):
        for name in subdirs + names:
            if (Path(directory) / name).is_symlink():
                raise ValueError(f"skill contains a symbolic link: {name}")
        for name in names:
            path = Path(directory) / name
            files[path.relative_to(root).as_posix()] = file_sha256(path)
    return files


def recover_skill(target: Path) -> None:
    previous = target.with_name(f".{target.name}.previous")
    if previous.is_symlink():
        raise ValueError(f"invalid previous skill directory: {previous}")
    if previous.exists():
        if target.exists():
            shutil.rmtree(previous)
        else:
            os.replace(previous, target)


def sync_skill(spec: SkillSpec, target: Path, source: Source, *, dry_run: bool = False) -> bool:
    if not dry_run:
        recover_skill(target)
    version, payload = source.skill_files(spec)
    expected = file_manifest(payload)
    current = local_files(target)
    if current == expected:
        return False
    if dry_run:
        return True

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{target.name}-", dir=target.parent) as temporary:
        staged = Path(temporary) / "skill"
        staged.mkdir()
        changed = []
        for name, checksum in expected.items():
            destination = staged / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if current.get(name) == checksum:
                shutil.copy2(target / name, destination)
            else:
                changed.append(name)
        if changed:
            source.download_skill_files(spec, version, changed, staged)
        for name, checksum in expected.items():
            destination = staged / name
            if destination.resolve() != destination.absolute():
                raise ValueError(f"downloaded skill file is a symbolic link: {name}")
            if file_sha256(destination) != checksum:
                raise ValueError(f"downloaded skill file sha256 mismatch: {name}")
            destination.chmod(0o755 if destination.suffix in {".sh", ".bash"} else 0o644)
        if local_files(staged) != expected:
            raise ValueError("downloaded skill tree differs from file manifest")
        for directory, _, _ in os.walk(staged):
            Path(directory).chmod(0o755)

        previous = target.with_name(f".{target.name}.previous")
        if target.exists():
            os.replace(target, previous)
        try:
            os.replace(staged, target)
        except OSError:
            if previous.exists():
                os.replace(previous, target)
            raise
        if previous.exists():
            shutil.rmtree(previous)
    return True
