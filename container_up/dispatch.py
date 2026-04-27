from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from venv import logger

from container_up.attachment_paths import normalize_outbound_attachments
from container_up.attachments import normalize_attachments
from container_up.bridge_state import get_bridge_hub
from container_up.db_store import touch_org
from container_up.frontend_config import compose_frontend_org_id, split_frontend_org_id
from container_up.im_tools import build_im_receive_event, get_im_manager, get_im_parser
from container_up.router_service import ensure_org_container


def _metadata_with_route_fallback(
    *,
    metadata: dict[str, Any],
    org_id: str,
) -> dict[str, Any]:
    updated = dict(metadata)
    frontend_id, _external_org_id = split_frontend_org_id(org_id)
    if frontend_id:
        updated.setdefault("frontend_id", frontend_id)
        reply_target = dict(updated.get("reply_target") or {})
        if reply_target:
            reply_target.setdefault("frontend_id", frontend_id)
            updated["reply_target"] = reply_target
    return updated


EventHandler = Callable[[dict[str, Any]], Any]


class DispatchParser:
    def __init__(self, *, event_key: str = "event_type") -> None:
        self.event_key = event_key
        self._handlers: dict[str, EventHandler] = {}

    def register(self, event_type: str) -> Callable[[EventHandler], EventHandler]:
        def decorator(handler: EventHandler) -> EventHandler:
            self._handlers[event_type] = handler
            return handler

        return decorator

    async def parse(self, event: dict[str, Any]) -> Any:
        event_type = str(event[self.event_key])
        handler = self._handlers[event_type]
        result = handler(event)
        if inspect.isawaitable(result):
            return await result
        return result


dispatch_parser = DispatchParser()


async def _deliver_outbound_message(
    *,
    chat_id: str,
    content: str,
    metadata: dict[str, Any] | None = None,
    attachments: list[Any] | None = None,
) -> dict[str, Any]:
    im_parser = get_im_manager().parser_for_outbound(dict(metadata or {}))
    return await im_parser.post_message_with_retry(
        payload={
            "chat_id": chat_id,
            "content": content,
            "metadata": dict(metadata or {}),
            "attachments": list(attachments or []),
        }
    )


async def forward_bridge_outbound(packet: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(packet.get("metadata") or {})
    attachments = list(packet.get("attachments") or [])
    if (
        metadata.get("_progress")
        or metadata.get("_stream_delta")
        or metadata.get("_stream_end")
    ):
        return {"ok": True, "response": None, "skipped": "non_terminal_event"}

    content = str(packet.get("content") or "")
    if not content and not attachments:
        return {"ok": True, "response": None, "skipped": "empty_content"}

    chat_id = str(packet.get("chat_id") or "")
    logger.error(
        "bridge outbound dispatch chat_id=%s metadata=%r",
        chat_id,
        metadata,
    )
    post_result = await _deliver_outbound_message(
        chat_id=chat_id,
        content=content,
        metadata=metadata,
        attachments=attachments,
    )
    return {
        "ok": True,
        "chat_id": chat_id,
        "attachments": attachments,
        "metadata": metadata,
        "response": post_result,
    }


@dispatch_parser.register("im_message_receive")
async def parse_im_message_receive(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event.get("event") or {})
    org_id = str(payload.get("org_id") or "")
    chat_id = str(payload.get("chat_id") or "")
    usr_id = str(payload.get("usr_id") or "")
    content = str(payload.get("content") or "")
    metadata = dict(payload.get("metadata") or {})
    frontend_id = str(metadata.get("frontend_id") or "").strip()
    route_org_id = compose_frontend_org_id(frontend_id, org_id)
    raw_attachments = list(payload.get("attachments") or [])
    attachments = (
        raw_attachments
        if metadata.get("attachments_materialized")
        else normalize_attachments(content, raw_attachments)
    )

    await asyncio.to_thread(
        ensure_org_container,
        route_org_id,
        frontend_id or None,
    )
    await asyncio.to_thread(touch_org, route_org_id)

    result = await get_bridge_hub().submit_message(
        org_id=route_org_id,
        chat_id=chat_id,
        usr_id=usr_id,
        content=content,
        attachments=attachments,
        metadata=metadata,
    )
    print(f"bridge processing result: {result}")
    return {
        "ok": True,
        "response": result,
    }


@dispatch_parser.register("p2p_chat_receive_msg")
async def parse_p2p_chat_receive_msg(event: dict[str, Any]) -> dict[str, Any]:
    parser = get_im_parser(str(event.get("frontend_id") or "") or None)
    if hasattr(parser, "normalize_subscribe_payload"):
        standardized = parser.normalize_subscribe_payload(event)
    else:
        payload = dict(event.get("event") or {})
        message = dict(payload.get("message") or {})
        sender_uid = str(payload.get("sender_uid") or "")
        standardized = build_im_receive_event(
            org_id=sender_uid,
            chat_id=str(message.get("chat_id") or ""),
            usr_id=sender_uid,
            content=str(message.get("content") or ""),
            attachments=[],
            metadata={
                "provider": "qxt",
                "event_type": str(event.get("event_type", "")),
                "chat_type": str(message.get("chat_type", "")),
                "message_type": str(message.get("type", "")),
                "message_id": str(message.get("message_id", "")),
                "timestamp": str(event.get("timestamp", "")),
                "source": "subscribe",
                "reply_target": {
                    "type": "qxt",
                    "to_single_uid": sender_uid,
                },
            },
        )
    prepare_event = getattr(parser, "prepare_inbound_event", None)
    if callable(prepare_event):
        standardized = await prepare_event(standardized)
    return await parse_im_message_receive(standardized)


@dispatch_parser.register("bridge_outbound_message")
async def parse_bridge_outbound_message(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event.get("event") or {})
    org_id = str(event.get("org_id") or "")
    metadata = _metadata_with_route_fallback(
        metadata=dict(payload.get("metadata") or {}),
        org_id=org_id,
    )
    return await forward_bridge_outbound(
        {
            "type": "outbound_message",
            "chat_id": str(payload.get("to") or ""),
            "content": str(payload.get("content") or ""),
            "metadata": metadata,
            "attachments": normalize_outbound_attachments(
                org_id, list(payload.get("attachments") or [])
            ),
        }
    )
