from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import mimetypes
import secrets
import string
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import ClientError
from Crypto.Cipher import AES

from container_up.attachments import (
    attachment_from_content_url,
    persist_attachment_bytes,
)
from container_up.frontend_config import compose_frontend_org_id
from container_up.http_state import get_dispatch_session
from container_up.settings import (
    ACCESS_URL,
    APP_ID,
    APP_SECRET,
    CALLBACK_TOKEN,
    CORP_ID,
    SEND_MSG_RETRY_BACKOFF,
    SEND_MSG_RETRY_COUNT,
    SEND_MSG_URL,
)


logger = logging.getLogger(__name__)
_AES_BLOCK_SIZE = 16


def build_im_receive_event(
    *,
    org_id: str,
    conversation_id: str,
    user_id: str,
    content: str,
    attachments: list[Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_type": "im_message_receive",
        "event": {
            "org_id": org_id,
            "conversation_id": conversation_id,
            "user_id": user_id,
            "content": content,
            "attachments": list(attachments or []),
            "metadata": dict(metadata or {}),
        },
    }


class QxtIMParser:
    provider = "qxt"

    def __init__(
        self,
        *,
        frontend_id: str = "default",
        app_id: str | None = None,
        app_secret: str | None = None,
        corp_id: str | None = None,
        callback_token: str | None = None,
        access_url: str | None = None,
        send_msg_url: str | None = None,
        frontend_config: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        self.frontend_id = frontend_id
        config = dict(frontend_config or {})
        configured_app_id = app_id or config.get("app_id") or config.get("APP_ID")
        configured_app_secret = (
            app_secret
            or config.get("app_secret")
            or config.get("APP_SECRET")
            or config.get("secret")
        )
        self.appid = str(configured_app_id or APP_ID).strip()
        self.appsecret = str(configured_app_secret or APP_SECRET).strip()
        self.corpid = str(
            corp_id or config.get("corp_id") or config.get("CORP_ID") or CORP_ID
        ).strip()
        self.token = str(
            callback_token
            or config.get("callback_token")
            or config.get("CALLBACK_TOKEN")
            or CALLBACK_TOKEN
        ).strip()
        self.access_url = str(
            access_url or config.get("access_url") or config.get("ACCESS_URL") or ""
        ).strip()
        self.send_msg_url = str(
            send_msg_url or config.get("send_msg_url") or config.get("SEND_MSG_URL") or ""
        ).strip()
        self.send_msg_retry_count = int(
            config.get("send_msg_retry_count")
            or config.get("SEND_MSG_RETRY_COUNT")
            or SEND_MSG_RETRY_COUNT
        )
        self.send_msg_retry_backoff = float(
            config.get("send_msg_retry_backoff")
            or config.get("SEND_MSG_RETRY_BACKOFF")
            or SEND_MSG_RETRY_BACKOFF
        )

    def _access_url(self) -> str:
        return self.access_url or ACCESS_URL

    def _send_msg_url(self) -> str:
        return self.send_msg_url or SEND_MSG_URL

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def supports_subscribe(self) -> bool:
        return True

    async def prepare_inbound_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        if str(payload.get("event_type") or "") != "im_message_receive":
            return payload

        event = dict(payload.get("event") or {})
        metadata = dict(event.get("metadata") or {})
        if str(metadata.get("provider") or "") != "qxt":
            return payload

        org_id = str(event.get("org_id") or "")
        user_id = str(event.get("user_id") or "")
        conversation_id = str(event.get("conversation_id") or "")
        content = str(event.get("content") or "")
        attachments = list(event.get("attachments") or [])

        materialized, downloaded = await self._materialize_inbound_attachments(
            org_id=org_id,
            user_id=user_id,
            conversation_id=conversation_id,
            content=content,
            metadata=metadata,
            attachments=attachments,
        )
        if materialized == attachments:
            return payload

        updated_event = dict(event)
        updated_event["attachments"] = materialized
        updated_metadata = dict(metadata)
        if downloaded:
            updated_metadata["attachments_materialized"] = True
        updated_event["metadata"] = updated_metadata
        return {
            **payload,
            "event": updated_event,
        }

    @staticmethod
    def _sha1(text: str) -> str:
        return hashlib.sha1(text.encode()).hexdigest()

    @staticmethod
    def _md5(text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()

    @staticmethod
    def _pkcs7_pad(text: str, block_size: int = _AES_BLOCK_SIZE) -> bytes:
        data = text.encode("utf-8")
        pad_len = block_size - (len(data) % block_size)
        return data + bytes([pad_len] * pad_len)

    @staticmethod
    def _pkcs7_unpad(data: bytes, block_size: int = _AES_BLOCK_SIZE) -> bytes:
        if not data:
            raise ValueError("empty decrypted payload")
        pad_len = data[-1]
        if pad_len < 1 or pad_len > block_size:
            raise ValueError(f"invalid padding length: {pad_len}")
        if data[-pad_len:] != bytes([pad_len] * pad_len):
            raise ValueError("invalid PKCS#7 padding")
        return data[:-pad_len]

    def _aes_decrypt(self, appsecret: str, text: str) -> str | None:
        try:
            iv = self._md5(appsecret)[:16].encode("utf-8")
            cipher = AES.new(appsecret.encode("utf-8"), AES.MODE_CBC, iv)
            decrypted_bytes = cipher.decrypt(base64.b64decode(text))
            return self._pkcs7_unpad(decrypted_bytes).decode("utf-8")
        except Exception as exc:
            logger.error("qxt aes decrypt failed: %s", exc)
            return None

    def _msgSignature(
        self, signature: str, timeStamp: str, nonce: str, encrypt: str
    ) -> str:
        return self._sha1("".join(sorted([signature, timeStamp, nonce, encrypt])))

    @staticmethod
    def generate_random_string(length: int = 16) -> str:
        chars = string.ascii_letters + string.digits
        return "".join(secrets.choice(chars) for _ in range(length))

    def decrypt(
        self,
        signature: str,
        timeStamp: str,
        nonce: str,
        encrypt: str,
        token: str | None = None,
        appsecret: str | None = None,
    ) -> str | None:
        verification_token = self.token if token is None else token
        secret = self.appsecret if appsecret is None else appsecret
        if (
            self._msgSignature(verification_token, timeStamp, nonce, encrypt)
            != signature
        ):
            raise ValueError("Signature verification error")
        return self._aes_decrypt(secret, encrypt)

    def encrypt(
        self,
        timeStamp: str | None = None,
        nonce: str | None = None,
        text: str = "",
        token: str | None = None,
        appsecret: str | None = None,
    ) -> dict[str, str]:
        verification_token = self.token if token is None else token
        secret = self.appsecret if appsecret is None else appsecret
        timeStamp = timeStamp or str(int(time.time()))
        nonce = nonce or self.generate_random_string()

        iv = self._md5(secret)[:16].encode("utf-8")
        cipher = AES.new(secret.encode("utf-8"), AES.MODE_CBC, iv)
        encrypted_text = base64.b64encode(cipher.encrypt(self._pkcs7_pad(text))).decode(
            "utf-8"
        )
        signature = self._msgSignature(
            verification_token, timeStamp, nonce, encrypted_text
        )
        return {
            "msgSignature": signature,
            "timeStamp": timeStamp,
            "nonce": nonce,
            "encrypt": encrypted_text,
        }

    async def get_access_token(self) -> str | None:
        access_url = self._access_url()
        if not access_url:
            logger.error("ACCESS_URL is not configured")
            return None
        if not self.corpid:
            logger.error("CORP_ID is not configured")
            return None
        if not self.appid:
            logger.error("APP_ID is not configured")
            return None

        last_error: Exception | None = None
        for attempt in range(1, self.send_msg_retry_count + 1):
            try:
                async with get_dispatch_session().get(
                    access_url,
                    params={"corpid": self.corpid, "appid": self.appid},
                ) as response:
                    response_text = await response.text()
                    if response.status >= 500:
                        raise RuntimeError(
                            f"access_token failed with {response.status}: {response_text}"
                        )
                    if response.status >= 400:
                        raise RuntimeError(
                            f"access_token rejected with {response.status}: {response_text}"
                        )
                    payload = json.loads(response_text or "{}")
                    access_token = payload.get("access_token")
                    if not access_token:
                        raise RuntimeError(
                            f"access_token missing in response: {response_text}"
                        )
                    return str(access_token)
            except (asyncio.TimeoutError, ClientError, RuntimeError, json.JSONDecodeError) as exc:
                logger.error("access_token retry attempt=%s error=%s", attempt, exc)
                last_error = exc
                if attempt >= self.send_msg_retry_count:
                    break
                await asyncio.sleep(self.send_msg_retry_backoff * attempt)
        logger.error("access_token fetch failed: %s", last_error)
        return None

    def process_subscribe_form(
        self, sub_form: Any
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        decrypted = self.decrypt(
            signature=sub_form.msgSignature,
            timeStamp=sub_form.timeStamp,
            nonce=sub_form.nonce,
            encrypt=sub_form.encrypt,
        )
        if not decrypted:
            raise ValueError("empty decrypted payload")

        try:
            payload = dict(json.loads(decrypted))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid payload") from exc
        event_type = str(payload.get("event_type") or "")
        if event_type == "check_url":
            return self.encrypt(text="success"), None

        return self.encrypt(text="success"), self.normalize_subscribe_payload(payload)

    def normalize_subscribe_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if str(payload.get("event_type") or "") != "p2p_chat_receive_msg":
            return payload

        event = dict(payload.get("event") or {})
        message = dict(event.get("message") or {})
        sender_uid = str(event.get("sender_uid") or "")
        route_org_id = compose_frontend_org_id(self.frontend_id, sender_uid)
        conversation_id = str(message.get("chat_id") or "")
        content = str(message.get("content") or "")
        return build_im_receive_event(
            org_id=route_org_id,
            conversation_id=conversation_id,
            user_id=sender_uid,
            content=content,
            attachments=[],
            metadata={
                "provider": "qxt",
                "frontend_id": self.frontend_id,
                "app_id": self.appid,
                "external_org_id": sender_uid,
                "route_org_id": route_org_id,
                "event_type": str(payload.get("event_type", "")),
                "chat_type": str(message.get("chat_type", "")),
                "message_type": str(message.get("type", "")),
                "message_id": str(message.get("message_id", "")),
                "timestamp": str(payload.get("timestamp", "")),
                "source": "subscribe",
                "reply_target": {
                    "type": "qxt",
                    "frontend_id": self.frontend_id,
                    "to_single_uid": sender_uid,
                },
            },
        )

    async def _materialize_inbound_attachments(
        self,
        *,
        org_id: str,
        user_id: str,
        conversation_id: str,
        content: str,
        metadata: dict[str, Any],
        attachments: list[Any],
    ) -> tuple[list[Any], bool]:
        normalized = list(attachments)
        auto_attachment = attachment_from_content_url(content)
        if auto_attachment and not any(
            (isinstance(item, dict) and str(item.get("url") or "").strip() == auto_attachment["url"])
            or str(item or "").strip() == auto_attachment["url"]
            for item in normalized
        ):
            normalized.append(auto_attachment)

        if not normalized:
            return normalized, False

        attachment_group = conversation_id or str(metadata.get("message_id") or "") or user_id
        local_paths: list[Any] = []
        changed = False
        for attachment in normalized:
            local_path = await self._download_inbound_attachment(
                org_id=org_id,
                user_id=user_id,
                attachment_group=attachment_group,
                attachment=attachment,
            )
            if local_path is None:
                local_paths.append(attachment)
                continue
            local_paths.append(local_path)
            changed = True

        return (local_paths if changed else normalized), changed

    async def _download_inbound_attachment(
        self,
        *,
        org_id: str,
        user_id: str,
        attachment_group: str,
        attachment: Any,
    ) -> str | None:
        filename_override: str | None = None
        if isinstance(attachment, dict):
            url = str(attachment.get("url") or "").strip()
            filename_override = str(attachment.get("filename") or "").strip() or None
        else:
            url = str(attachment or "").strip()

        if not url.startswith(("http://", "https://")):
            return None

        data, filename = await self._download_attachment_bytes(url, filename_override)
        if not data:
            return None
        return persist_attachment_bytes(
            org_id=org_id,
            user_id=user_id,
            data=data,
            filename=filename,
            provider="qxt",
            attachment_group=attachment_group,
        )

    async def _download_attachment_bytes(
        self,
        url: str,
        filename_override: str | None = None,
    ) -> tuple[bytes | None, str]:
        last_error: Exception | None = None
        for attempt in range(1, self.send_msg_retry_count + 1):
            try:
                async with get_dispatch_session().get(url) as response:
                    if response.status >= 400:
                        text = await response.text()
                        raise RuntimeError(
                            f"qxt attachment download rejected with {response.status}: {text}"
                        )
                    data = await response.read()
                    filename = filename_override or self._attachment_filename_from_response(
                        url=url,
                        headers=dict(response.headers),
                    )
                    return data, filename
            except (asyncio.TimeoutError, ClientError, RuntimeError) as exc:
                logger.error(
                    "qxt attachment download retry attempt=%s url=%s error=%s",
                    attempt,
                    url,
                    exc,
                )
                last_error = exc
                if attempt >= self.send_msg_retry_count:
                    break
                await asyncio.sleep(self.send_msg_retry_backoff * attempt)
        logger.error("qxt attachment download failed url=%s error=%s", url, last_error)
        return None, filename_override or self._guess_filename(url)

    @staticmethod
    def _attachment_filename_from_response(url: str, headers: dict[str, str]) -> str:
        content_disposition = str(
            headers.get("Content-Disposition") or headers.get("content-disposition") or ""
        )
        for part in content_disposition.split(";"):
            key, _, value = part.strip().partition("=")
            if key.lower() != "filename":
                continue
            filename = value.strip().strip('"')
            if filename:
                return filename
        return QxtIMParser._guess_filename(url)

    def _media_upload_url(self) -> str:
        parsed = urlparse(self._send_msg_url())
        if not parsed.scheme or not parsed.netloc:
            raise RuntimeError("SEND_MSG_URL is not configured")
        return f"{parsed.scheme}://{parsed.netloc}/v2/media/upload"

    @staticmethod
    def _guess_filename(ref: str, fallback: str = "attachment.bin") -> str:
        parsed = urlparse(ref)
        name = Path(parsed.path or ref).name
        return name or fallback

    @staticmethod
    def _guess_upload_type(filename: str, content_type: str | None = None) -> str:
        suffix = Path(filename).suffix.lower()
        mime = content_type or mimetypes.guess_type(filename)[0] or ""
        if mime.startswith("audio/"):
            return "audio"
        if suffix in {".jpg", ".jpeg", ".png"} or mime in {
            "image/jpeg",
            "image/png",
        }:
            return "image"
        return "file"

    async def _post_json_with_retry(
        self,
        *,
        url: str,
        params: dict[str, str],
        json_payload: dict[str, Any],
        log_label: str,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.send_msg_retry_count + 1):
            try:
                async with get_dispatch_session().post(
                    url,
                    params=params,
                    json=json_payload,
                ) as response:
                    response_text = await response.text()
                    logger.info(
                        "%s response attempt=%s status=%s body_len=%s",
                        log_label,
                        attempt,
                        response.status,
                        len(response_text),
                    )
                    if response.status >= 500:
                        raise RuntimeError(
                            f"{log_label} failed with {response.status}: {response_text}"
                        )
                    if response.status >= 400:
                        raise RuntimeError(
                            f"{log_label} rejected with {response.status}: {response_text}"
                        )
                    return {
                        "status": response.status,
                        "body": response_text,
                    }
            except (asyncio.TimeoutError, ClientError, RuntimeError) as exc:
                logger.error("%s retry attempt=%s error=%s", log_label, attempt, exc)
                last_error = exc
                if attempt >= self.send_msg_retry_count:
                    break
                await asyncio.sleep(self.send_msg_retry_backoff * attempt)
        raise RuntimeError(
            f"{log_label} failed after retries: {last_error}"
        ) from last_error

    def _attachment_ref(self, attachment: Any) -> tuple[str, str | None]:
        filename_override: str | None = None
        if isinstance(attachment, dict):
            ref = str(attachment.get("url") or "").strip()
            filename_override = str(attachment.get("filename") or "").strip() or None
        else:
            ref = str(attachment or "").strip()

        if not ref:
            raise RuntimeError("empty attachment")
        return ref, filename_override

    async def _attachment_upload_source(
        self, attachment: Any
    ) -> tuple[bytes, str, str | None]:
        ref, filename_override = self._attachment_ref(attachment)
        parsed = urlparse(ref)
        if parsed.scheme in {"http", "https"}:
            raise RuntimeError(f"attachment must be a local file path: {ref}")
        path = Path(ref).expanduser()

        if not path.is_file():
            raise RuntimeError(f"attachment file not found: {ref}")

        payload = await asyncio.to_thread(path.read_bytes)
        filename = filename_override or path.name or self._guess_filename(ref)
        content_type = mimetypes.guess_type(filename)[0]
        return payload, filename, content_type

    async def _upload_attachment_with_retry(
        self, attachment: Any, access_token: str
    ) -> dict[str, str]:
        media_bytes, filename, content_type = await self._attachment_upload_source(
            attachment
        )
        upload_type = self._guess_upload_type(filename, content_type)

        last_error: Exception | None = None
        for attempt in range(1, self.send_msg_retry_count + 1):
            try:
                form = aiohttp.FormData()
                form.add_field("type", upload_type)
                form.add_field(
                    "media",
                    media_bytes,
                    filename=filename,
                    content_type=content_type or "application/octet-stream",
                )
                async with get_dispatch_session().post(
                    self._media_upload_url(),
                    params={"access_token": access_token},
                    data=form,
                ) as response:
                    response_text = await response.text()
                    logger.info(
                        "media_upload response attempt=%s status=%s body_len=%s",
                        attempt,
                        response.status,
                        len(response_text),
                    )
                    if response.status >= 500:
                        raise RuntimeError(
                            f"media_upload failed with {response.status}: {response_text}"
                        )
                    if response.status >= 400:
                        raise RuntimeError(
                            f"media_upload rejected with {response.status}: {response_text}"
                        )
                    payload = await response.json(content_type=None)
                    media_id = payload.get("media_id") or payload.get("mediaId")
                    if not media_id:
                        raise RuntimeError(
                            f"media_upload missing media_id: {response_text}"
                        )
                    return {
                        "media_id": str(media_id),
                        "upload_type": upload_type,
                        "filename": filename,
                    }
            except (asyncio.TimeoutError, ClientError, RuntimeError) as exc:
                logger.error("media_upload retry attempt=%s error=%s", attempt, exc)
                last_error = exc
                if attempt >= self.send_msg_retry_count:
                    break
                await asyncio.sleep(self.send_msg_retry_backoff * attempt)
        raise RuntimeError(
            f"media_upload failed after retries: {last_error}"
        ) from last_error

    async def _send_message_with_retry(
        self,
        *,
        send_meta: dict[str, Any],
        access_token: str,
        message_type: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._post_json_with_retry(
            url=self._send_msg_url(),
            params={"access_token": access_token},
            json_payload=send_meta | {"type": message_type, "message": message},
            log_label="send_message",
        )

    async def send_attachments_with_retry(
        self,
        send_meta: dict[str, Any],
        attachments: list[Any],
        access_token: str,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for attachment in attachments:
            uploaded = await self._upload_attachment_with_retry(attachment, access_token)
            response = await self._send_message_with_retry(
                send_meta=send_meta,
                access_token=access_token,
                message_type="file",
                message={"media_id": uploaded["media_id"]},
            )
            results.append(
                {
                    "attachment": attachment,
                    "media_id": uploaded["media_id"],
                    "response": response,
                }
            )
        return results

    def _normalize_outbound_payload(self, payload: dict[str, object]) -> dict[str, object]:
        if payload.get("to_single_uid") is not None:
            return payload

        metadata = dict(payload.get("metadata") or {})
        reply_target = dict(metadata.get("reply_target") or {})
        to_single_uid = str(
            reply_target.get("to_single_uid") or metadata.get("to_single_uid") or ""
        ).strip()
        if not to_single_uid:
            raise RuntimeError("missing qxt recipient")

        return {
            "to_single_uid": to_single_uid,
            "type": "text",
            "message": {"content": str(payload.get("content") or "")},
            "attachments": list(payload.get("attachments") or []),
        }

    async def post_message_with_retry(
        self, *, payload: dict[str, object]
    ) -> dict[str, object]:
        normalized = self._normalize_outbound_payload(payload)
        access_token_result = self.get_access_token()
        access_token = (
            await access_token_result
            if inspect.isawaitable(access_token_result)
            else access_token_result
        )
        if access_token is None:
            raise RuntimeError("Failed to retrieve access token for response sending")
        if not self._send_msg_url():
            raise RuntimeError("SEND_MSG_URL is not configured")

        send_meta = {
            key: value
            for key, value in normalized.items()
            if key not in {"message", "attachments", "type"}
        }
        message_type = str(normalized.get("type") or "")
        message_payload = normalized.get("message")
        attachments = list(normalized.get("attachments") or [])

        result: dict[str, object] = {"message": None, "attachments": []}
        if (
            isinstance(message_payload, dict)
            and message_type
            and (
                message_type != "text"
                or str(message_payload.get("content") or "").strip()
            )
        ):
            result["message"] = await self._send_message_with_retry(
                send_meta=send_meta,
                access_token=access_token,
                message_type=message_type,
                message=message_payload,
            )

        if attachments:
            result["attachments"] = await self.send_attachments_with_retry(
                send_meta=send_meta,
                attachments=attachments,
                access_token=access_token,
            )
        return result
