from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from container_up.attachments import persist_attachment_bytes
from container_up.frontend_config import compose_frontend_org_id
from container_up.settings import FEISHU_APP_ID, FEISHU_APP_SECRET


logger = logging.getLogger(__name__)
DispatchCallback = Callable[[dict[str, Any]], Awaitable[None]]


class _EventLoopProxy:
    def __getattr__(self, name: str) -> Any:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        return getattr(loop, name)


def _extract_interactive_content(content: dict[str, Any] | str) -> list[str]:
    """Recursively extract text and links from interactive card content."""
    parts: list[str] = []

    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return [content] if content.strip() else []

    if not isinstance(content, dict):
        return parts

    title = content.get("title")
    if isinstance(title, dict):
        title_content = str(title.get("content") or title.get("text") or "")
        if title_content:
            parts.append(f"title: {title_content}")
    elif isinstance(title, str) and title:
        parts.append(f"title: {title}")

    elements = content.get("elements")
    if isinstance(elements, list):
        for element in elements:
            parts.extend(_extract_element_content(element))

    card = content.get("card")
    if isinstance(card, dict):
        parts.extend(_extract_interactive_content(card))

    header = content.get("header")
    if isinstance(header, dict):
        header_title = header.get("title")
        if isinstance(header_title, dict):
            header_text = str(
                header_title.get("content") or header_title.get("text") or ""
            )
            if header_text:
                parts.append(f"title: {header_text}")

    return parts


def _extract_element_content(element: Any) -> list[str]:
    """Extract content from a single Feishu card element."""
    parts: list[str] = []
    if not isinstance(element, dict):
        return parts

    tag = str(element.get("tag") or "")
    if tag in {"markdown", "lark_md"}:
        content = str(element.get("content") or "")
        if content:
            parts.append(content)
    elif tag == "div":
        text = element.get("text")
        if isinstance(text, dict):
            text_content = str(text.get("content") or text.get("text") or "")
            if text_content:
                parts.append(text_content)
        elif isinstance(text, str) and text:
            parts.append(text)
        fields = element.get("fields")
        if isinstance(fields, list):
            for field in fields:
                if not isinstance(field, dict):
                    continue
                field_text = field.get("text")
                if isinstance(field_text, dict):
                    content = str(field_text.get("content") or "")
                    if content:
                        parts.append(content)
    elif tag == "a":
        href = str(element.get("href") or "")
        text = str(element.get("text") or "")
        if href:
            parts.append(f"link: {href}")
        if text:
            parts.append(text)
    elif tag == "button":
        text = element.get("text")
        if isinstance(text, dict):
            content = str(text.get("content") or "")
            if content:
                parts.append(content)
        multi_url = element.get("multi_url")
        url = str(element.get("url") or "")
        if not url and isinstance(multi_url, dict):
            url = str(multi_url.get("url") or "")
        if url:
            parts.append(f"link: {url}")
    elif tag == "img":
        alt = element.get("alt")
        parts.append(
            str(alt.get("content") or "[image]") if isinstance(alt, dict) else "[image]"
        )
    elif tag == "note":
        nested = element.get("elements")
        if isinstance(nested, list):
            for child in nested:
                parts.extend(_extract_element_content(child))
    elif tag == "column_set":
        columns = element.get("columns")
        if isinstance(columns, list):
            for column in columns:
                if not isinstance(column, dict):
                    continue
                nested = column.get("elements")
                if isinstance(nested, list):
                    for child in nested:
                        parts.extend(_extract_element_content(child))
    elif tag == "plain_text":
        content = str(element.get("content") or "")
        if content:
            parts.append(content)
    else:
        nested = element.get("elements")
        if isinstance(nested, list):
            for child in nested:
                parts.extend(_extract_element_content(child))

    return parts


def _extract_share_card_content(content_json: dict[str, Any], message_type: str) -> str:
    parts: list[str] = []
    if message_type == "share_chat":
        parts.append(f"[shared chat: {content_json.get('chat_id', '')}]")
    elif message_type == "share_user":
        parts.append(f"[shared user: {content_json.get('user_id', '')}]")
    elif message_type == "interactive":
        parts.extend(_extract_interactive_content(content_json))
    elif message_type == "share_calendar_event":
        parts.append(f"[shared calendar event: {content_json.get('event_key', '')}]")
    elif message_type == "system":
        parts.append("[system message]")
    elif message_type == "merge_forward":
        parts.append("[merged forward messages]")

    return "\n".join(parts) if parts else f"[{message_type}]"


def _extract_post_content(content_json: dict[str, Any]) -> tuple[str, list[str]]:
    def _parse_block(block: dict[str, Any]) -> tuple[str | None, list[str]]:
        if not isinstance(block, dict) or not isinstance(block.get("content"), list):
            return None, []

        texts: list[str] = []
        images: list[str] = []
        title = str(block.get("title") or "").strip()
        if title:
            texts.append(title)

        for row in block["content"]:
            if not isinstance(row, list):
                continue
            for element in row:
                if not isinstance(element, dict):
                    continue
                tag = str(element.get("tag") or "")
                if tag in {"text", "a"}:
                    text = str(element.get("text") or "").strip()
                    if text:
                        texts.append(text)
                elif tag == "at":
                    user_name = str(element.get("user_name") or "user").strip()
                    texts.append(f"@{user_name}")
                elif tag == "code_block":
                    code_text = str(element.get("text") or "")
                    language = str(element.get("language") or "")
                    texts.append(f"\n```{language}\n{code_text}\n```\n")
                elif tag == "img":
                    image_key = str(element.get("image_key") or "").strip()
                    if image_key:
                        images.append(image_key)

        text = " ".join(part for part in texts if part).strip()
        return text or None, images

    root = content_json
    if isinstance(root.get("post"), dict):
        root = root["post"]
    if not isinstance(root, dict):
        return "", []

    if "content" in root:
        text, images = _parse_block(root)
        if text or images:
            return text or "", images

    for locale_key in ("zh_cn", "en_us", "ja_jp"):
        block = root.get(locale_key)
        if isinstance(block, dict):
            text, images = _parse_block(block)
            if text or images:
                return text or "", images

    for block in root.values():
        if isinstance(block, dict):
            text, images = _parse_block(block)
            if text or images:
                return text or "", images

    return "", []


class FeishuIMParser:
    provider = "feishu"

    _IMAGE_SUFFIXES = {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".webp",
        ".ico",
        ".tiff",
        ".heic",
    }
    _AUDIO_SUFFIXES = {".opus"}
    _VIDEO_SUFFIXES = {".mp4", ".mov", ".avi"}
    _DELIVERY_TARGET_SEPARATOR = ":::"

    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )
    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
    _CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)
    _MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
    _MD_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__")
    _MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
    _MD_STRIKE_RE = re.compile(r"~~(.+?)~~")
    _COMPLEX_MD_RE = re.compile(
        r"```" r"|^\|.+\|.*\n\s*\|[-:\s|]+\|" r"|^#{1,6}\s+",
        re.MULTILINE,
    )
    _SIMPLE_MD_RE = re.compile(
        r"\*\*.+?\*\*" r"|__.+?__" r"|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)" r"|~~.+?~~",
        re.DOTALL,
    )
    _MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
    _LIST_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)
    _OLIST_RE = re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE)
    _TEXT_MAX_LEN = 200
    _POST_MAX_LEN = 2000

    def __init__(
        self,
        *,
        frontend_id: str = "default",
        app_id: str | None = None,
        app_secret: str | None = None,
        frontend_config: dict[str, Any] | None = None,
        dispatch_event: DispatchCallback | None = None,
        main_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.frontend_id = frontend_id
        config = dict(frontend_config or {})
        configured_app_id = (
            app_id
            or config.get("app_id")
            or config.get("APP_ID")
            or config.get("FEISHU_APP_ID")
        )
        configured_app_secret = (
            app_secret
            or config.get("app_secret")
            or config.get("APP_SECRET")
            or config.get("FEISHU_APP_SECRET")
            or config.get("secret")
        )
        self.app_id = str(configured_app_id or FEISHU_APP_ID).strip()
        self.app_secret = str(configured_app_secret or FEISHU_APP_SECRET).strip()
        self._dispatch_event = dispatch_event
        self._main_loop = main_loop
        self._thread: threading.Thread | None = None
        self._started = False
        self._api_client_local = threading.local()
        self._ws_client: Any | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        if self._started:
            return
        if not self.app_id or not self.app_secret:
            raise RuntimeError("feishu app credentials are not configured")
        if self._dispatch_event is None or self._main_loop is None:
            raise RuntimeError("feishu parser requires dispatch callback and main loop")

        self._started = True
        self._thread = threading.Thread(
            target=self._run_ws_client,
            name=f"container-up-feishu-ws-{self.frontend_id}",
            daemon=True,
        )
        self._thread.start()

    """def stop(self) -> None:
        self._started = False"""

    def stop(self) -> None:
        self._started = False

        ws_loop = self._ws_loop
        ws_client = self._ws_client
        self._ws_client = None

        if ws_loop is not None and not ws_loop.is_closed():

            async def _disconnect_client() -> None:
                if ws_client is None:
                    return
                disconnect = getattr(ws_client, "_disconnect", None)
                if disconnect is None:
                    return
                try:
                    await disconnect()
                except Exception:
                    logger.exception("failed to disconnect feishu ws client")

            try:
                future = asyncio.run_coroutine_threadsafe(_disconnect_client(), ws_loop)
                try:
                    future.result(timeout=3)
                except Exception:
                    logger.exception("failed while waiting feishu ws disconnect")
            except Exception:
                logger.exception("failed to schedule feishu ws disconnect")

            try:
                ws_loop.call_soon_threadsafe(ws_loop.stop)
            except Exception:
                logger.exception("failed to stop feishu ws loop")

        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)

        self._ws_loop = None

    def supports_subscribe(self) -> bool:
        return False

    async def post_message_with_retry(
        self, *, payload: dict[str, object]
    ) -> dict[str, object]:
        metadata = dict(payload.get("metadata") or {})
        reply_target = self._resolve_reply_target(
            chat_id=str(payload.get("chat_id") or ""),
            metadata=metadata,
        )
        if not reply_target:
            raise RuntimeError("missing feishu reply target")

        content = str(payload.get("content") or "")
        attachments = list(payload.get("attachments") or [])
        result: dict[str, object] = {"message": None, "attachments": []}
        first_send = True

        if content.strip():
            message_results: list[dict[str, Any]] = []
            for message_type, body in self._render_outbound_content(content):
                target = (
                    reply_target
                    if first_send
                    else self._without_reply_target(reply_target)
                )
                message_results.append(
                    await asyncio.to_thread(
                        self._send_or_reply_message_sync,
                        target,
                        message_type,
                        body,
                    )
                )
                first_send = False
            result["message"] = (
                message_results[0] if len(message_results) == 1 else message_results
            )

        for attachment in attachments:
            target = (
                reply_target if first_send else self._without_reply_target(reply_target)
            )
            result["attachments"].append(
                await asyncio.to_thread(self._send_attachment_sync, target, attachment)
            )
            first_send = False
        return result

    @classmethod
    def _resolve_reply_target(
        cls,
        *,
        chat_id: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        reply_target = dict(metadata.get("reply_target") or {})
        if str(reply_target.get("type") or "") == "feishu":
            return reply_target

        sender_id, separator, conversation_id = chat_id.partition(
            cls._DELIVERY_TARGET_SEPARATOR
        )
        receive_id = str(metadata.get("chat_id") or conversation_id or "").strip()
        sender_id = str(metadata.get("sender_id") or sender_id or "").strip()
        chat_type = str(metadata.get("chat_type") or "").strip()
        message_id = str(metadata.get("message_id") or "").strip()
        thread_id = str(metadata.get("thread_id") or "").strip()

        if not separator:
            receive_id = receive_id or chat_id
        if not receive_id and not sender_id:
            return {}

        if chat_type == "group":
            receive_id_type = "chat_id"
            target_receive_id = receive_id
        else:
            receive_id_type = "open_id"
            target_receive_id = sender_id or receive_id

        if not target_receive_id:
            return {}

        target = {
            "type": "feishu",
            "receive_id_type": receive_id_type,
            "receive_id": target_receive_id,
            "message_id": message_id,
            "thread_id": thread_id,
            "reply_in_thread": bool(thread_id),
        }
        frontend_id = str(metadata.get("frontend_id") or "").strip()
        if frontend_id:
            target["frontend_id"] = frontend_id
        return target

    """def _run_ws_client(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            from lark_oapi.core.enum import LogLevel
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
            import lark_oapi.ws.client as ws_client_module

            ws_client_module.loop = loop

            event_handler = (
                EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(self._on_message_receive)
                .build()
            )
            self._ws_client = ws_client_module.Client(
                self.app_id,
                self.app_secret,
                event_handler=event_handler,
                log_level=LogLevel.INFO,
            )
            self._ws_client.start()
        except Exception:
            logger.exception("feishu ws listener exited unexpectedly")"""

    def _run_ws_client(self) -> None:
        loop = asyncio.new_event_loop()
        self._ws_loop = loop
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(self._run_ws_client_async())
        except Exception:
            logger.exception("feishu ws listener exited unexpectedly")
        finally:
            try:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                logger.exception("failed to cleanup feishu ws loop")
            finally:
                self._ws_client = None
                asyncio.set_event_loop(None)
                loop.close()
                self._ws_loop = None

    async def _run_ws_client_async(self) -> None:
        from lark_oapi.core.enum import LogLevel
        from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
        import lark_oapi.ws.client as ws_client_module

        if not isinstance(getattr(ws_client_module, "loop", None), _EventLoopProxy):
            ws_client_module.loop = _EventLoopProxy()

        event_handler = (
            EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_receive)
            .build()
        )

        self._ws_client = ws_client_module.Client(
            self.app_id,
            self.app_secret,
            event_handler=event_handler,
            log_level=LogLevel.INFO,
        )

        connect = getattr(self._ws_client, "_connect", None)
        if connect is None:
            raise RuntimeError("lark ws client has no _connect method")

        await connect()

        while self._started:
            await asyncio.sleep(1)

    """def _schedule_dispatch(self, data: Any) -> None:
        if self._dispatch_event is None:
            return
        asyncio.create_task(self._normalize_and_dispatch(data))"""

    async def _normalize_and_dispatch(self, data: Any) -> None:
        if self._dispatch_event is None:
            return
        try:
            payload = await asyncio.to_thread(self._build_dispatch_event, data)
        except Exception:
            logger.exception("failed to normalize feishu event")
            return
        await self._dispatch_event(payload)

    """def _on_message_receive(self, data: Any) -> None:
        if not self._started or self._main_loop is None:
            return
        self._main_loop.call_soon_threadsafe(self._schedule_dispatch, data)"""

    def _on_message_receive(self, data: Any) -> None:
        if not self._started or self._main_loop is None or self._dispatch_event is None:
            return

        future = asyncio.run_coroutine_threadsafe(
            self._normalize_and_dispatch(data),
            self._main_loop,
        )

        def _log_future_result(fut: Any) -> None:
            try:
                fut.result()
            except Exception:
                logger.exception("failed to dispatch feishu event")

        future.add_done_callback(_log_future_result)

    def _build_dispatch_event(self, data: Any) -> dict[str, Any]:
        event = getattr(data, "event", None)
        if event is None:
            raise RuntimeError("missing feishu event body")

        sender = getattr(event, "sender", None)
        message = getattr(event, "message", None)
        sender_id = getattr(sender, "sender_id", None)
        if message is None or sender is None or sender_id is None:
            raise RuntimeError("incomplete feishu event")

        external_org_id = str(
            getattr(sender, "tenant_key", "")
            or getattr(data.header, "tenant_key", "")
            or ""
        ).strip()
        org_id = compose_frontend_org_id(self.frontend_id, external_org_id)
        user_id = str(
            getattr(sender_id, "open_id", "")
            or getattr(sender_id, "user_id", "")
            or getattr(sender_id, "union_id", "")
            or ""
        ).strip()
        if not org_id or not user_id:
            raise RuntimeError("missing feishu org_id or user_id")

        chat_type = str(getattr(message, "chat_type", "") or "")
        chat_id = str(getattr(message, "chat_id", "") or "")
        thread_id = str(getattr(message, "thread_id", "") or "")
        conversation_id = thread_id or chat_id
        message_id = str(getattr(message, "message_id", "") or "")
        mentions = []
        for mention in list(getattr(message, "mentions", None) or []):
            mention_id = getattr(mention, "id", None)
            mentions.append(
                {
                    "key": str(getattr(mention, "key", "") or ""),
                    "name": str(getattr(mention, "name", "") or ""),
                    "open_id": str(getattr(mention_id, "open_id", "") or ""),
                    "user_id": str(getattr(mention_id, "user_id", "") or ""),
                    "union_id": str(getattr(mention_id, "union_id", "") or ""),
                }
            )

        attachments, content = self._extract_inbound_content(
            org_id=org_id,
            user_id=user_id,
            attachment_group=conversation_id or message_id or user_id,
            message=message,
        )
        content = self._resolve_mentions(content, mentions)

        receive_id_type = "chat_id" if chat_type == "group" else "open_id"
        receive_id = chat_id if receive_id_type == "chat_id" else user_id

        return {
            "event_type": "im_message_receive",
            "event": {
                "org_id": org_id,
                "conversation_id": conversation_id,
                "user_id": user_id,
                "content": content,
                "attachments": attachments,
                "metadata": {
                    "provider": "feishu",
                    "frontend_id": self.frontend_id,
                    "app_id": self.app_id,
                    "external_org_id": external_org_id,
                    "route_org_id": org_id,
                    "chat_id": chat_id,
                    "chat_type": chat_type,
                    "thread_id": thread_id,
                    "message_type": str(getattr(message, "message_type", "") or ""),
                    "message_id": message_id,
                    "root_id": str(getattr(message, "root_id", "") or ""),
                    "parent_id": str(getattr(message, "parent_id", "") or ""),
                    "timestamp": str(getattr(message, "create_time", "") or ""),
                    "mentions": mentions,
                    "reply_target": {
                        "type": "feishu",
                        "frontend_id": self.frontend_id,
                        "receive_id_type": receive_id_type,
                        "receive_id": receive_id,
                        "message_id": message_id,
                        "thread_id": thread_id,
                        "reply_in_thread": bool(thread_id),
                    },
                },
            },
        }

    def _extract_inbound_content(
        self,
        *,
        org_id: str,
        user_id: str,
        attachment_group: str,
        message: Any,
    ) -> tuple[list[str], str]:
        message_type = str(getattr(message, "message_type", "") or "")
        raw_content = str(getattr(message, "content", "") or "")
        message_id = str(getattr(message, "message_id", "") or "")
        try:
            content_json = json.loads(raw_content) if raw_content else {}
        except json.JSONDecodeError:
            content_json = {}

        attachments: list[str] = []
        content = ""
        if message_type == "text":
            content = str(content_json.get("text") or "")
        elif message_type == "post":
            content, image_keys = _extract_post_content(content_json)
            for image_key in image_keys:
                local_path = self._download_resource_to_local(
                    org_id=org_id,
                    user_id=user_id,
                    attachment_group=attachment_group,
                    message_id=message_id,
                    file_key=image_key,
                    resource_type="image",
                    fallback_filename=f"{image_key[:16]}.jpg",
                )
                if local_path:
                    attachments.append(local_path)
        elif message_type == "image":
            local_path = self._download_resource_to_local(
                org_id=org_id,
                user_id=user_id,
                attachment_group=attachment_group,
                message_id=message_id,
                file_key=str(content_json.get("image_key") or ""),
                resource_type="image",
                fallback_filename="image.jpg",
            )
            if local_path:
                attachments.append(local_path)
        elif message_type in {"file", "audio", "media"}:
            fallback_name = str(content_json.get("file_name") or f"{message_type}.bin")
            local_path = self._download_resource_to_local(
                org_id=org_id,
                user_id=user_id,
                attachment_group=attachment_group,
                message_id=message_id,
                file_key=str(content_json.get("file_key") or ""),
                resource_type=message_type,
                fallback_filename=fallback_name,
            )
            if local_path:
                attachments.append(local_path)
        elif message_type in {
            "share_chat",
            "share_user",
            "interactive",
            "share_calendar_event",
            "system",
            "merge_forward",
        }:
            content = _extract_share_card_content(content_json, message_type)
        else:
            content = raw_content

        return attachments, content

    def _download_resource_to_local(
        self,
        *,
        org_id: str,
        user_id: str,
        attachment_group: str,
        message_id: str,
        file_key: str,
        resource_type: str,
        fallback_filename: str,
    ) -> str | None:
        if not message_id or not file_key:
            return None
        data, filename = self._download_message_resource_sync(
            message_id=message_id,
            file_key=file_key,
            resource_type=resource_type,
        )
        if not data:
            return None
        return persist_attachment_bytes(
            org_id=org_id,
            user_id=user_id,
            data=data,
            filename=filename or fallback_filename,
            provider="feishu",
            attachment_group=attachment_group,
        )

    def _download_message_resource_sync(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str,
    ) -> tuple[bytes | None, str | None]:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        client = self._get_api_client()
        request_type = "image" if resource_type == "image" else "file"
        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(request_type)
            .build()
        )
        response = client.im.v1.message_resource.get(request)
        if not response.success():
            logger.error(
                "feishu resource download failed code=%s msg=%s message_id=%s file_key=%s",
                response.code,
                response.msg,
                message_id,
                file_key,
            )
            return None, None

        file_data = (
            response.file.read() if hasattr(response.file, "read") else response.file
        )
        return bytes(file_data or b""), response.file_name

    """def _get_api_client(self) -> Any:
        if self._api_client is not None:
            return self._api_client

        from lark_oapi.client import Client

        self._api_client = (
            Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()
        )
        return self._api_client"""

    def _get_api_client(self) -> Any:
        client = getattr(self._api_client_local, "client", None)
        if client is not None:
            return client

        from lark_oapi.client import Client

        client = (
            Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()
        )
        self._api_client_local.client = client
        return client

    @classmethod
    def _strip_md_formatting(cls, text: str) -> str:
        text = cls._MD_BOLD_RE.sub(r"\1", text)
        text = cls._MD_BOLD_UNDERSCORE_RE.sub(r"\1", text)
        text = cls._MD_ITALIC_RE.sub(r"\1", text)
        text = cls._MD_STRIKE_RE.sub(r"\1", text)
        return text

    @classmethod
    def _parse_md_table(cls, table_text: str) -> dict[str, Any] | None:
        lines = [
            line.strip() for line in table_text.strip().split("\n") if line.strip()
        ]
        if len(lines) < 3:
            return None

        def split(line: str) -> list[str]:
            return [cell.strip() for cell in line.strip("|").split("|")]

        headers = [cls._strip_md_formatting(header) for header in split(lines[0])]
        rows = [
            [cls._strip_md_formatting(cell) for cell in split(line)]
            for line in lines[2:]
        ]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": [
                {
                    "tag": "column",
                    "name": f"c{i}",
                    "display_name": header,
                    "width": "auto",
                }
                for i, header in enumerate(headers)
            ],
            "rows": [
                {f"c{i}": row[i] if i < len(row) else "" for i in range(len(headers))}
                for row in rows
            ],
        }

    def _build_card_elements(self, content: str) -> list[dict[str, Any]]:
        elements: list[dict[str, Any]] = []
        last_end = 0
        for match in self._TABLE_RE.finditer(content):
            before = content[last_end : match.start()]
            if before.strip():
                elements.extend(self._split_headings(before))
            elements.append(
                self._parse_md_table(match.group(1))
                or {"tag": "markdown", "content": match.group(1)}
            )
            last_end = match.end()
        remaining = content[last_end:]
        if remaining.strip():
            elements.extend(self._split_headings(remaining))
        return elements or [{"tag": "markdown", "content": content}]

    @staticmethod
    def _split_elements_by_table_limit(
        elements: list[dict[str, Any]], max_tables: int = 1
    ) -> list[list[dict[str, Any]]]:
        if not elements:
            return [[]]
        groups: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        table_count = 0
        for element in elements:
            if element.get("tag") == "table":
                if table_count >= max_tables:
                    if current:
                        groups.append(current)
                    current = []
                    table_count = 0
                current.append(element)
                table_count += 1
            else:
                current.append(element)
        if current:
            groups.append(current)
        return groups or [[]]

    def _split_headings(self, content: str) -> list[dict[str, Any]]:
        protected = content
        code_blocks: list[str] = []
        for match in self._CODE_BLOCK_RE.finditer(content):
            code_blocks.append(match.group(1))
            protected = protected.replace(
                match.group(1), f"\x00CODE{len(code_blocks) - 1}\x00", 1
            )

        elements: list[dict[str, Any]] = []
        last_end = 0
        for match in self._HEADING_RE.finditer(protected):
            before = protected[last_end : match.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            text = self._strip_md_formatting(match.group(2).strip())
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{text}**" if text else "",
                    },
                }
            )
            last_end = match.end()

        remaining = protected[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})

        for i, code_block in enumerate(code_blocks):
            for element in elements:
                if element.get("tag") == "markdown":
                    element["content"] = str(element["content"]).replace(
                        f"\x00CODE{i}\x00", code_block
                    )

        return elements or [{"tag": "markdown", "content": content}]

    @classmethod
    def _detect_msg_format(cls, content: str) -> str:
        stripped = content.strip()
        if cls._COMPLEX_MD_RE.search(stripped):
            return "interactive"
        if len(stripped) > cls._POST_MAX_LEN:
            return "interactive"
        if cls._SIMPLE_MD_RE.search(stripped):
            return "interactive"
        if cls._LIST_RE.search(stripped) or cls._OLIST_RE.search(stripped):
            return "interactive"
        if cls._MD_LINK_RE.search(stripped):
            return "post"
        if len(stripped) <= cls._TEXT_MAX_LEN:
            return "text"
        return "post"

    @classmethod
    def _markdown_to_post(cls, content: str) -> str:
        paragraphs: list[list[dict[str, str]]] = []
        for line in content.strip().split("\n"):
            elements: list[dict[str, str]] = []
            last_end = 0
            for match in cls._MD_LINK_RE.finditer(line):
                before = line[last_end : match.start()]
                if before:
                    elements.append({"tag": "text", "text": before})
                elements.append(
                    {"tag": "a", "text": match.group(1), "href": match.group(2)}
                )
                last_end = match.end()

            remaining = line[last_end:]
            if remaining:
                elements.append({"tag": "text", "text": remaining})
            if not elements:
                elements.append({"tag": "text", "text": ""})
            paragraphs.append(elements)

        return json.dumps({"zh_cn": {"content": paragraphs}}, ensure_ascii=False)

    def _render_outbound_content(self, content: str) -> list[tuple[str, str]]:
        fmt = self._detect_msg_format(content)
        if fmt == "text":
            return [("text", json.dumps({"text": content.strip()}, ensure_ascii=False))]
        if fmt == "post":
            return [("post", self._markdown_to_post(content))]

        messages: list[tuple[str, str]] = []
        elements = self._build_card_elements(content)
        for chunk in self._split_elements_by_table_limit(elements):
            card = {"config": {"wide_screen_mode": True}, "elements": chunk}
            messages.append(("interactive", json.dumps(card, ensure_ascii=False)))
        return messages

    @staticmethod
    def _without_reply_target(reply_target: dict[str, Any]) -> dict[str, Any]:
        target = dict(reply_target)
        target.pop("message_id", None)
        target["reply_in_thread"] = False
        return target

    @staticmethod
    def _resolve_mentions(content: str, mentions: list[dict[str, str]]) -> str:
        if not content or not mentions:
            return content
        for mention in mentions:
            key = mention.get("key") or ""
            if not key or key not in content:
                continue
            name = mention.get("name") or key
            open_id = mention.get("open_id") or ""
            user_id = mention.get("user_id") or ""
            if open_id and user_id:
                replacement = f"@{name} ({open_id}, user id: {user_id})"
            elif open_id:
                replacement = f"@{name} ({open_id})"
            else:
                replacement = f"@{name}"
            content = content.replace(key, replacement)
        return content

    def _build_text_request_body(self, *, message_type: str, content: str) -> str:
        if message_type == "text":
            return content
        return content

    def _send_or_reply_message_sync(
        self,
        reply_target: dict[str, Any],
        message_type: str,
        content: str,
    ) -> dict[str, Any]:
        if reply_target.get("message_id"):
            return self._reply_message_sync(reply_target, message_type, content)
        return self._send_message_sync(reply_target, message_type, content)

    def _reply_message_sync(
        self,
        reply_target: dict[str, Any],
        message_type: str,
        content: str,
    ) -> dict[str, Any]:
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        client = self._get_api_client()
        request = (
            ReplyMessageRequest.builder()
            .message_id(str(reply_target.get("message_id") or ""))
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(
                    self._build_text_request_body(
                        message_type=message_type, content=content
                    )
                )
                .msg_type(message_type)
                .reply_in_thread(bool(reply_target.get("reply_in_thread")))
                .uuid(uuid4().hex)
                .build()
            )
            .build()
        )
        response = client.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError(f"feishu reply failed: {response.code} {response.msg}")
        return {
            "code": response.code,
            "msg": response.msg,
            "message_id": getattr(getattr(response, "data", None), "message_id", None),
        }

    def _send_message_sync(
        self,
        reply_target: dict[str, Any],
        message_type: str,
        content: str,
    ) -> dict[str, Any]:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        client = self._get_api_client()
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(str(reply_target.get("receive_id_type") or "open_id"))
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(str(reply_target.get("receive_id") or ""))
                .msg_type(message_type)
                .content(
                    self._build_text_request_body(
                        message_type=message_type, content=content
                    )
                )
                .uuid(uuid4().hex)
                .build()
            )
            .build()
        )
        response = client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(f"feishu send failed: {response.code} {response.msg}")
        return {
            "code": response.code,
            "msg": response.msg,
            "message_id": getattr(getattr(response, "data", None), "message_id", None),
        }

    def _send_attachment_sync(
        self,
        reply_target: dict[str, Any],
        attachment: Any,
    ) -> dict[str, Any]:
        from lark_oapi.api.im.v1 import (
            CreateFileRequest,
            CreateFileRequestBody,
            CreateImageRequest,
            CreateImageRequestBody,
        )

        path = self._attachment_path(attachment)
        suffix = path.suffix.lower()
        is_image = suffix in self._IMAGE_SUFFIXES or str(
            mimetypes.guess_type(path.name)[0] or ""
        ).startswith("image/")
        client = self._get_api_client()

        with path.open("rb") as f:
            if is_image:
                upload_response = client.im.v1.image.create(
                    CreateImageRequest.builder()
                    .request_body(
                        CreateImageRequestBody.builder()
                        .image_type("message")
                        .image(f)
                        .build()
                    )
                    .build()
                )
                if not upload_response.success():
                    raise RuntimeError(
                        f"feishu image upload failed: {upload_response.code} {upload_response.msg}"
                    )
                content = json.dumps(
                    {"image_key": upload_response.data.image_key}, ensure_ascii=False
                )
                send_type = "image"
            else:
                upload_response = client.im.v1.file.create(
                    CreateFileRequest.builder()
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type(self._feishu_file_type(path))
                        .file_name(path.name)
                        .file(f)
                        .build()
                    )
                    .build()
                )
                if not upload_response.success():
                    raise RuntimeError(
                        f"feishu file upload failed: {upload_response.code} {upload_response.msg}"
                    )
                content = json.dumps(
                    {"file_key": upload_response.data.file_key}, ensure_ascii=False
                )
                if suffix in self._AUDIO_SUFFIXES:
                    send_type = "audio"
                elif suffix in self._VIDEO_SUFFIXES:
                    send_type = "video"
                else:
                    send_type = "file"

        if reply_target.get("message_id"):
            response = self._reply_message_sync(reply_target, send_type, content)
        else:
            response = self._send_message_sync(reply_target, send_type, content)
        return {"attachment": str(path), "response": response}

    @staticmethod
    def _attachment_path(attachment: Any) -> Path:
        ref = ""
        if isinstance(attachment, dict):
            ref = str(attachment.get("url") or "").strip()
        else:
            ref = str(attachment or "").strip()
        if not ref:
            raise RuntimeError("empty attachment")

        path = Path(ref).expanduser()
        if not path.is_absolute():
            raise RuntimeError(f"attachment must be an absolute local path: {ref}")
        if not path.is_file():
            raise RuntimeError(f"attachment file not found: {ref}")
        return path

    @staticmethod
    def _feishu_file_type(path: Path) -> str:
        suffix = path.suffix.lower()
        return {
            ".opus": "opus",
            ".mp4": "mp4",
            ".pdf": "pdf",
            ".doc": "doc",
            ".docx": "doc",
            ".xls": "xls",
            ".xlsx": "xls",
            ".ppt": "ppt",
            ".pptx": "ppt",
        }.get(suffix, "stream")
