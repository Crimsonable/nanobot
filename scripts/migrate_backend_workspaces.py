"""Offline, explicit workspace/binding migration. Dry-run unless --apply is given."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from agent_backend.journal import Journal
from agent_backend.models import Identity, uid


def migrate(manifest: list[dict], source_root: Path, workspace_root: Path,
            journal: Journal, apply: bool = False) -> list[dict]:
    source_root, workspace_root = source_root.resolve(strict=True), workspace_root.resolve()
    planned = []
    identities = set()
    for row in manifest:
        identity = Identity(frontend_id=row["frontend_id"], user_id=row["user_id"])
        if identity.key in identities:
            raise ValueError("duplicate identity in migration manifest")
        identities.add(identity.key)
        source = Path(row["source_workspace"]).resolve(strict=True)
        if not source.is_relative_to(source_root) or source == source_root or not source.is_dir():
            raise ValueError("source workspace is outside the declared source root")
        if workspace_root.is_relative_to(source) or source.is_relative_to(workspace_root):
            raise ValueError("source and destination trees must not overlap")
        if any(path.is_symlink() for path in source.rglob("*")):
            raise ValueError("source workspace contains symlinks; review them before migration")
        target = workspace_root / identity.key
        if target.exists() or journal.get("binding", identity.key):
            raise ValueError("destination workspace/binding already exists; refusing overwrite")
        planned.append({"source": str(source), "target": str(target), "bucket_id": row["bucket_id"],
                        "identity": identity.model_dump(), "user_key": identity.key})
    if apply:
        workspace_root.mkdir(parents=True, exist_ok=True)
        for row in planned:
            temporary = workspace_root / (".migration-" + uid())
            # No source deletion. A failed copy leaves a clearly named staging directory for review.
            shutil.copytree(row["source"], temporary)
            temporary.rename(row["target"])
            journal.put("binding", row["user_key"], {k: row[k] for k in ["bucket_id", "identity", "user_key"]})
    return planned


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--control-db", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db = Journal(args.control_db)
    try:
        result = migrate(json.loads(args.manifest.read_text(encoding="utf-8")), args.source_root,
                         args.workspace_root, db, args.apply)
        print(json.dumps({"applied": args.apply, "workspaces": result}, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
