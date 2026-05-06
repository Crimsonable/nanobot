#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import quote

DEFAULT_ENDPOINT = "http://192.168.48.104:9000"
DEFAULT_ACCESS_KEY = "minio_admin"
DEFAULT_SECRET_KEY = "minio_password"
DEFAULT_BUCKET = "attachments"
DEFAULT_PUBLIC_BASE_URL = "http://192.168.48.104:9000/attachments"
DEFAULT_REGION = "us-east-1"
DEFAULT_KEY_PREFIX = "markdown-images"


def getenv(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload a local image to MinIO and return Markdown image link JSON."
    )
    parser.add_argument("image_path", help="Absolute or relative local image path")
    parser.add_argument("--alt", default="image", help="Alt text for markdown output")
    parser.add_argument(
        "--key-prefix",
        default=getenv("MINIO_KEY_PREFIX", DEFAULT_KEY_PREFIX),
        help="Object key prefix (default: markdown-images or MINIO_KEY_PREFIX)",
    )
    return parser.parse_args()


def build_client(endpoint: str, access_key: str, secret_key: str, region: str):
    try:
        import boto3
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "boto3 is required. Install with: python3 -m pip install boto3"
        ) from exc

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )


def object_key_for(path: Path, key_prefix: str) -> str:
    stat = path.stat()
    digest_input = f"{path.resolve(strict=False)}:{stat.st_size}:{stat.st_mtime_ns}".encode(
        "utf-8"
    )
    digest = hashlib.sha1(digest_input).hexdigest()
    suffix = path.suffix.lower()
    filename = f"{digest}{suffix}" if suffix else digest
    prefix = key_prefix.strip("/")
    return f"{prefix}/{filename}" if prefix else filename


def main() -> int:
    args = parse_args()

    image_path = Path(args.image_path).expanduser().resolve()
    if not image_path.exists() or not image_path.is_file():
        raise SystemExit(f"image file not found: {image_path}")

    endpoint = getenv("MINIO_ENDPOINT", DEFAULT_ENDPOINT)
    access_key = getenv("MINIO_ACCESS_KEY", DEFAULT_ACCESS_KEY)
    secret_key = getenv("MINIO_SECRET_KEY", DEFAULT_SECRET_KEY)
    bucket = getenv("MINIO_BUCKET", DEFAULT_BUCKET)
    public_base_url = getenv("MINIO_PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).rstrip("/")
    region = getenv("MINIO_REGION", DEFAULT_REGION)

    content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    object_key = object_key_for(image_path, args.key_prefix)

    client = build_client(endpoint, access_key, secret_key, region)
    client.upload_file(
        str(image_path),
        bucket,
        object_key,
        ExtraArgs={"ContentType": content_type},
    )

    if public_base_url:
        url = f"{public_base_url}/{quote(object_key)}"
    else:
        url = str(
            client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": object_key},
                ExpiresIn=3600,
            )
        )

    result = {
        "url": url,
        "markdown": f"![{args.alt}]({url})",
        "bucket": bucket,
        "object_key": object_key,
        "content_type": content_type,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
