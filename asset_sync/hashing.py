from __future__ import annotations

import hashlib
from pathlib import Path


def file_sha256(path: Path) -> str:
    """Hash original bytes without decoding or newline conversion."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
