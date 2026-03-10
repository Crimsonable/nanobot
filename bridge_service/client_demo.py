"""Minimal client demo for the standalone bridge service."""

from __future__ import annotations

import argparse
import asyncio

import websockets

from bridge_service.protocol import decode_packet, encode_packet, make_request_id


async def run_client(
    url: str,
    conversation_id: str,
    user_id: str,
    tenant_id: str,
    content: str,
    token: str | None = None,
) -> None:
    request_id = make_request_id()
    async with websockets.connect(url, proxy=None) as ws:
        if token:
            await ws.send(encode_packet({"type": "auth", "token": token}))
            print(await ws.recv())

        await ws.send(
            encode_packet(
                {
                    "type": "message",
                    "request_id": request_id,
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                    "content": content,
                    "attachments": [],
                    "metadata": {"client": "demo"},
                }
            )
        )

        async for raw in ws:
            packet = decode_packet(raw)
            if packet.get("request_id") not in {"", request_id} and packet.get("type") != "ack":
                continue
            print(packet)
            if packet.get("type") in {"final", "error", "cancelled"}:
                break


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Client demo for the standalone bridge service")
    parser.add_argument("--url", default="ws://127.0.0.1:8765")
    parser.add_argument("--conversation-id", default="conv-1")
    parser.add_argument("--user-id", default="user-1")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--content", required=True)
    parser.add_argument("--token", default="")
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
        )
    )


if __name__ == "__main__":
    main()
