"""Minimal HTTP client demo for bridge_service."""

from __future__ import annotations

import argparse
import asyncio

import httpx


async def run_client(
    url: str,
    conversation_id: str,
    user_id: str,
    tenant_id: str,
    content: str,
    token: str | None = None,
    timeout_seconds: float = 60.0,
) -> None:
    headers: dict[str, str] = {}
    if token:
        headers["X-Bridge-Token"] = token

    async with httpx.AsyncClient(timeout=timeout_seconds + 5) as client:
        response = await client.post(
            url,
            json={
                "conversation_id": conversation_id,
                "user_id": user_id,
                "tenant_id": tenant_id,
                "content": content,
                "timeout_seconds": timeout_seconds,
                "metadata": {"client": "demo-http"},
            },
            headers=headers,
        )
        response.raise_for_status()
        print(response.json())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HTTP client demo for the standalone bridge service")
    parser.add_argument("--url", default="http://127.0.0.1:8766/api/messages")
    parser.add_argument("--conversation-id", default="conv-1")
    parser.add_argument("--user-id", default="user-1")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--content", required=True)
    parser.add_argument("--token", default="")
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(
        run_client(
            url=args.url,
            conversation_id=args.conversation_id,
            user_id=args.user_id,
            tenant_id=args.tenant_id,
            content=args.content,
            token=args.token or None,
            timeout_seconds=args.timeout_seconds,
        )
    )


if __name__ == "__main__":
    main()
