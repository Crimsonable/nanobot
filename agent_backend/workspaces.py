"""Keep the original server-mounted frontend/user directory layout."""
import re
from pathlib import Path

from agent_backend.models import Identity


def workspace_path(root: Path, identity: Identity, layout: str) -> Path:
    if layout == "identity_hash":
        return root / identity.key
    frontend = re.sub(r"[^a-zA-Z0-9_.-]+", "-", identity.frontend_id).strip("-.") or "frontend"
    user = "".join(c if c.isalnum() or c in "._-" else "-" for c in identity.user_id).strip("-.")
    return root / frontend / (user[:96] or "user")
