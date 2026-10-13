"""Resolve frontend-owned public paths without lossy identity sanitization."""
import os
import re
from pathlib import Path, PurePosixPath


def frontend_segment(frontend_id: str) -> str:
    if frontend_id in {"frontend.json", "frontends.json"} or not re.fullmatch(r"[\w-][\w.-]{0,199}", frontend_id):
        raise ValueError("frontend_id must be a literal directory name (letters, digits, _, -, .)")
    return frontend_id


def frontend_common_root(common_root: Path, frontend_id: str) -> Path:
    return Path(os.path.abspath(common_root)) / frontend_segment(frontend_id)


def frontend_app_root(common_root: Path, frontend_id: str) -> Path:
    return frontend_common_root(common_root, frontend_id) / "app"


def scoped_public_path(path: Path, common_root: Path, frontend_id: str | None) -> Path:
    value = path.as_posix()
    if "{frontend_id}" in value:
        if not frontend_id:
            raise ValueError("frontend_id is required for frontend public directories")
        value = value.replace("{frontend_id}", frontend_segment(frontend_id))
    candidate = Path(value)
    if not (candidate.is_absolute() or PurePosixPath(value).is_absolute()) or any(c in value for c in "*?\n\r{}"):
        raise ValueError("public roots must be absolute literal paths")
    candidate = Path(os.path.abspath(candidate))
    common = Path(os.path.abspath(common_root))
    resolved = candidate.resolve()
    for target in [candidate, resolved]:
        if target.is_relative_to(common) or common.is_relative_to(target):
            if not frontend_id or not target.is_relative_to(frontend_app_root(common, frontend_id)):
                raise ValueError("public path must stay inside the current frontend app directory")
            # Reject aliases into sibling directories, including changed root symlinks.
            if candidate != resolved:
                raise ValueError("frontend common paths must not contain symbolic links")
    return resolved
