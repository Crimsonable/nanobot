"""Frontend-scoped workspace paths shared by scheduling, runtime and deployment."""
from pathlib import Path, PurePosixPath

from agent_backend.common import frontend_segment
from agent_backend.models import Identity


def workspace_volume_subpath(root: Path, frontend_id: str) -> PurePosixPath:
    """Return the frontend directory inside the data PVC (mounted at /mnt/env)."""
    target = PurePosixPath(root.as_posix())
    if not target.is_relative_to("/mnt/env/workspaces") or ".." in target.parts:
        raise ValueError("dynamic workspace_root must be under /mnt/env/workspaces without parent traversal")
    return target.relative_to("/mnt/env") / frontend_segment(frontend_id)


def workspace_path(root: Path, identity: Identity, layout: str) -> Path:
    frontend = frontend_segment(identity.frontend_id)
    if layout == "identity_hash":
        return root / frontend / identity.key
    if layout != "frontend_user":
        raise ValueError("unknown workspace layout")
    user = "".join(c if c.isalnum() or c in "._-" else "-" for c in identity.user_id).strip("-.")
    return root / frontend / (user[:96] or "user")
