from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent_backend.config import load_frontends
from asset_sync.source import ZhichengSource
from asset_sync.sync import checked_path, sync_frontend


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asset-sync",
        description="Sync the latest Zhicheng skills and MCP connections for enabled frontends.",
    )
    parser.add_argument("--frontends", type=Path, default=Path("/mnt/common/frontends.json"))
    parser.add_argument("--endpoint", help="Platform URL, defaults to ZHICHENG_DOMAIN")
    parser.add_argument("--common-root", type=Path)
    parser.add_argument("--frontend", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check-config", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        registry = load_frontends(args.frontends)
        registered = {frontend.frontend_id: frontend for frontend in registry.frontends}
        enabled = set(registry.enabled_asset_sync_frontends())
        requested = set(args.frontend)
        if requested - registered.keys():
            raise ValueError(f"unknown frontend: {', '.join(sorted(requested - registered.keys()))}")
        if requested - enabled:
            raise ValueError(f"asset_sync is disabled for: {', '.join(sorted(requested - enabled))}")
        selected = sorted(requested or enabled)
        if not selected:
            print("no frontend has asset_sync enabled")
            return 0
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    source = None
    failures = 0
    try:
        if not args.check_config:
            source = ZhichengSource(endpoint=args.endpoint)
            assets = source.discover()
        for frontend_id in selected:
            try:
                frontend = registered[frontend_id]
                common = (args.common_root or frontend.bucket.common_root).absolute()
                root = common / frontend_id
                original_common = frontend.bucket.common_root.absolute()
                configured = Path(frontend.bucket.opencode.config_file.as_posix().replace("{frontend_id}", frontend_id))
                if configured.as_posix().startswith("/mnt/common/"):
                    configured = original_common / configured.relative_to("/mnt/common")
                relative = configured.absolute().relative_to(original_common / frontend_id / "config")
                config_path = checked_path(root / "config" / relative, root / "config")
                checked_path(root / "app/skills", root / "app")
                if args.check_config:
                    print(f"{frontend_id}: skills={root / 'app/skills'}, config={config_path}")
                    continue
                if not sync_frontend(frontend_id, assets, common, source,
                                     config_path=config_path, dry_run=args.dry_run):
                    failures += 1
            except Exception as exc:
                failures += 1
                print(f"{frontend_id}: error: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"asset discovery: error: {exc}", file=sys.stderr)
        return 1
    finally:
        if source is not None:
            source.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
