from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any
from venv import logger
from aiohttp import ClientError

from container_up.attachments import normalize_attachments
from container_up.bridge_state import get_bridge_hub
from container_up.crypt_tools import get_crypto_parser
from container_up.db_store import touch_org
from container_up.http_state import get_dispatch_session
from container_up.router_service import ensure_org_container
from container_up.settings import (
    FORWARD_TIMEOUT,
    SEND_MSG_RETRY_BACKOFF,
    SEND_MSG_RETRY_COUNT,
    SEND_MSG_URL,
)

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


async def _post_message_with_retry(
    *,
    access_token: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not SEND_MSG_URL:
        raise RuntimeError("SEND_MSG_URL is not configured")

    last_error: Exception | None = None
    for attempt in range(1, SEND_MSG_RETRY_COUNT + 1):
        try:
            logger.error(
                "dispatch send_message attempt=%s to_single_uid=%s type=%s",
                attempt,
                payload.get("to_single_uid"),
                payload.get("type"),
            )
            async with get_dispatch_session().post(
                SEND_MSG_URL,
                params={"access_token": access_token},
                json=payload,
            ) as response:
                response_text = await response.text()
                logger.error(
                    "dispatch send_message response attempt=%s status=%s body_len=%s",
                    attempt,
                    response.status,
                    len(response_text),
                )
                if response.status >= 500:
                    raise RuntimeError(
                        f"send message failed with {response.status}: {response_text}"
                    )
                if response.status >= 400:
                    raise RuntimeError(
                        f"send message rejected with {response.status}: {response_text}"
                    )
                return {
                    "status": response.status,
                    "body": response_text,
                }
        except (asyncio.TimeoutError, ClientError, RuntimeError) as exc:
            logger.error(
                "dispatch send_message retry attempt=%s error=%s",
                attempt,
                exc,
            )
            last_error = exc
            if attempt >= SEND_MSG_RETRY_COUNT:
                break
            await asyncio.sleep(SEND_MSG_RETRY_BACKOFF * attempt)

    raise RuntimeError(
        f"send message failed after retries: {last_error}"
    ) from last_error


@dispatch_parser.register("p2p_chat_receive_msg")
async def parse_p2p_chat_receive_msg(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event.get("event"))
    message = dict(payload.get("message"))
    sender_uid = str(payload.get("sender_uid"))
    conversation_id = str(message.get("chat_id"))
    content = str(message.get("content") or "")
    attachments = normalize_attachments(content, [])
    logger.error(
        "dispatch event start event_type=%s sender_uid=%s conversation_id=%s content_len=%s attachments_count=%s",
        event.get("event_type"),
        sender_uid,
        conversation_id,
        len(content),
        len(attachments),
    )

    await asyncio.to_thread(ensure_org_container, sender_uid)
    await asyncio.to_thread(touch_org, sender_uid)

    logger.error(
        "dispatch submit_message start sender_uid=%s conversation_id=%s",
        sender_uid,
        conversation_id,
    )
    result = await get_bridge_hub().submit_message(
        org_id=sender_uid,
        conversation_id=conversation_id,
        user_id=sender_uid,
        content=content,
        request_id=None,
        attachments=attachments,
        metadata={
            "event_type": str(event.get("event_type", "")),
            "chat_type": str(message.get("chat_type", "")),
            "message_type": str(message.get("type", "")),
            "message_id": str(message.get("message_id", "")),
            "timestamp": str(event.get("timestamp", "")),
            "source": "subscribe",
        },
        timeout=FORWARD_TIMEOUT,
    )
    logger.error(
        "dispatch submit_message done sender_uid=%s conversation_id=%s request_id=%s result_type=%s events_seen=%s",
        sender_uid,
        conversation_id,
        result.get("request_id"),
        (result.get("result") or {}).get("type"),
        len(result.get("events") or []),
    )
    logger.error("bridge processing result: %r", result)

    crypto_parser = get_crypto_parser()
    access_token = crypto_parser.get_access_token()
    if access_token is None:
        raise RuntimeError("Failed to retrieve access token for response encryption")

    payload = {
        "to_single_uid": sender_uid,
        "type": "text",
        "message": {"content": str(result["result"]["content"])},
    }
    logger.error(
        "dispatch outbound send start sender_uid=%s request_id=%s content_len=%s",
        sender_uid,
        result.get("request_id"),
        len(str(result["result"]["content"])),
    )

    post_result = await _post_message_with_retry(
        access_token=access_token,
        payload=payload,
    )
    logger.error(
        "dispatch outbound send done sender_uid=%s request_id=%s status=%s",
        sender_uid,
        result.get("request_id"),
        post_result.get("status"),
    )
    return {
        "ok": True,
        "response": post_result,
    }
