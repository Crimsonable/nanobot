from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def catalog_server(config: object) -> dict:
    if not isinstance(config, dict):
        raise ValueError("catalog response must contain mcp_config")
    if config.get("transport") not in {"streamable_http", "streamable-http", "http", "sse"}:
        raise ValueError("unsupported MCP transport")
    endpoint = config.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
        raise ValueError("MCP endpoint must be an http(s) URL")
    # Sync policy: ignore catalog auth metadata and connect without authentication.
    return {"type": "remote", "url": endpoint, "oauth": False}


def update_servers(path: Path, desired: dict[str, dict], *, dry_run: bool = False) -> set[str]:
    original = path.read_bytes()
    config = json.loads(original)
    if not isinstance(config, dict):
        raise ValueError("OpenCode config must be an object")
    mcp = config.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        raise ValueError("mcp must be an object")
    servers = mcp.setdefault("servers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcp.servers must be an object")
    changed = {name for name, value in desired.items() if servers.get(name) != value}
    if not changed or dry_run:
        return changed
    servers.update(desired)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.chmod(path.stat().st_mode & 0o777)
        if path.read_bytes() != original:
            raise RuntimeError("OpenCode config changed during sync; retry")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return changed
