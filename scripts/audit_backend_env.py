"""Report legacy environment migration without displaying secret values."""
import argparse
import json
from pathlib import Path

REMOVED_PREFIXES = ("BRIDGE_", "NANOBOT_BRIDGE_", "SOURCE_", "INSTANCE_PORT_", "CHILD_", "TIKTOKEN_")
MAPPING = {
    "BUCKET_ID": "bucket-runtime.yaml: bucket_id",
    "APP_HOST": "container-up.yaml: host",
    "APP_PORT": "container-up.yaml: port (service-specific)",
    "BUCKET_WORKSPACE_ROOT": "bucket-runtime.yaml: workspace_root",
    "BUCKET_MOUNT_ROOT": "deployment PVC mount + workspace_root",
    "BUCKET_COMMON_ROOT": "profiles.*.public_roots (read-only)",
    "FRONTENDS_CONFIG_PATH": "container-up.yaml: frontends + per-channel secret files",
    "BUCKET_MAX_INSTANCES_PER_BUCKET": "limits.active + limits.queued (new semantics)",
    "MAX_PROCESSES_PER_BUCKET": "limits.active + limits.queued (new semantics)",
    "SYNC_REPLY_TIMEOUT_SECONDS": "limits.request_seconds",
    "MINIO_ENDPOINT": "storage.endpoint",
    "MINIO_BUCKET": "storage.bucket",
    "MINIO_ACCESS_KEY": "storage.access_key_file (secret file)",
    "MINIO_SECRET_KEY": "storage.secret_key_file (secret file)",
    "MINIO_PUBLIC_BASE_URL": "storage.public_base_url",
    "IM_PROVIDER": "frontends.<id>.provider (no global fallback)",
    "FEISHU_APP_ID": "channel credential_file: app_id",
    "FEISHU_APP_SECRET": "channel credential_file: app_secret",
    "SEND_MSG_URL": "channel credential_file: send_msg_url",
    "PYTHONPATH": "removed: source is baked into the image",
}


def audit(text: str) -> list[dict[str, str]]:
    result = []
    seen = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, _ = line.removeprefix("export ").partition("=")
        name = name.strip()
        if not separator or not name.replace("_", "").isalnum():
            result.append({"name": "<invalid entry>", "action": "manual review"})
            continue
        if name in seen:
            result.append({"name": name, "action": "duplicate: resolve explicitly"})
        seen.add(name)
        action = MAPPING.get(name)
        if not action:
            action = "removed from active backend" if name.startswith(REMOVED_PREFIXES) else "unmapped: manual review"
        result.append({"name": name, "action": action})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("env_file", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = json.dumps(audit(args.env_file.read_text(encoding="utf-8")), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(result + "\n", encoding="utf-8")
    else:
        print(result)


if __name__ == "__main__":
    main()
