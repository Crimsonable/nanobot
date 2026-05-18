#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
from uuid import uuid4
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

DEFAULT_CONTAINERUP_BASE_URL = "http://192.168.48.104:30080"
DEFAULT_FRONTEND_ID = "web-main"


def getenv(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve a file reference to URL. Remote URL is passed through; "
            "local path is uploaded via container_up /internal/attachments/upload."
        )
    )
    parser.add_argument("file_ref", help="Local file path or remote http(s) URL")
    parser.add_argument(
        "--containerup-url",
        default=getenv("CONTAINERUP_BASE_URL", DEFAULT_CONTAINERUP_BASE_URL),
        help="container_up base URL (default: CONTAINERUP_BASE_URL or http://127.0.0.1:8080)",
    )
    parser.add_argument(
        "--frontend-id",
        default=getenv("CONTAINERUP_FRONTEND_ID", DEFAULT_FRONTEND_ID),
        help="frontend id for upload API (default: CONTAINERUP_FRONTEND_ID or web-main)",
    )
    parser.add_argument(
        "--user-id",
        default=getenv("CONTAINERUP_USER_ID", ""),
        help="optional user id for upload API (default: CONTAINERUP_USER_ID or empty)",
    )
    parser.add_argument(
        "--alt",
        default="",
        help="Optional alt text. When provided, output also includes markdown field.",
    )
    return parser.parse_args()


def is_remote_ref(ref: str) -> bool:
    parsed = urlparse(ref)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def upload_local_via_containerup(
    *,
    base_url: str,
    frontend_id: str = "",
    user_id: str = "",
    local_path: str,
) -> dict[str, object]:
    endpoint = f"{base_url.rstrip('/')}/internal/attachments/upload"
    local_file = Path(local_path)
    if not local_file.is_file():
        raise RuntimeError(f"local file not found: {local_file}")
    mime = mimetypes.guess_type(local_file.name)[0] or "application/octet-stream"
    boundary = f"----nanobot-{uuid4().hex}"

    parts: list[bytes] = []

    def _add_text_field(name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        parts.append(value.encode("utf-8"))
        parts.append(b"\r\n")

    _add_text_field("frontend_id", frontend_id or DEFAULT_FRONTEND_ID)
    if user_id:
        _add_text_field("user_id", user_id)

    parts.append(f"--{boundary}\r\n".encode("utf-8"))
    parts.append(
        (
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{local_file.name}"\r\n'
        ).encode("utf-8")
    )
    parts.append(f"Content-Type: {mime}\r\n\r\n".encode("utf-8"))
    parts.append(local_file.read_bytes())
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    request = Request(
        endpoint,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            data = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"container_up upload failed: HTTP {exc.code} {exc.reason}: {detail}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"container_up upload request failed: {exc}") from exc
    except TimeoutError as exc:
        raise RuntimeError("container_up upload request timed out") from exc

    try:
        parsed = json.loads(data)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"container_up returned non-JSON response: {data}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"container_up returned unexpected response: {parsed!r}")
    return parsed


def main() -> int:
    args = parse_args()
    raw_ref = args.file_ref.strip()
    if not raw_ref:
        raise SystemExit("file_ref is empty")

    result: dict[str, object]
    if is_remote_ref(raw_ref):
        raise SystemExit(
            "file_ref is already URL format; reference it directly and do not call this upload script"
        )
    else:
        local_path = Path(raw_ref).expanduser()
        if not local_path.is_absolute():
            local_path = local_path.resolve()
        if not local_path.is_file():
            raise SystemExit(f"local file not found: {local_path}")

        try:
            uploaded = upload_local_via_containerup(
                base_url=args.containerup_url,
                frontend_id=args.frontend_id,
                user_id=args.user_id,
                local_path=str(local_path),
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        url = str(uploaded.get("url") or "").strip()
        if not url:
            raise SystemExit(f"container_up response has empty url: {uploaded}")
        result = dict(uploaded)
        result["url"] = url
        result["input_ref"] = str(local_path)
        result["is_remote"] = False

    if args.alt:
        result["markdown"] = f"![{args.alt}]({result['url']})"
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
