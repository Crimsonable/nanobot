from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
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
from loguru import logger

from container_up.http_state import get_dispatch_session

_AES_BLOCK_SIZE = 16


def build_im_receive_event(
    *,
    chat_id: str,
    usr_id: str,
    content: str,
    attachments: list[Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_type": "im_message_receive",
        "event": {
            "chat_id": chat_id,
            "usr_id": usr_id,
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
        user_info_url: str | None = None,
        frontend_config: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        self.frontend_id = frontend_id
        config = dict(frontend_config or {})
        self.attachment_url_prefix = config.get("attachment_url_prefix")
        configured_app_id = app_id or config.get("app_id")
        configured_app_secret = (
            app_secret or config.get("app_secret") or config.get("secret")
        )
        self.appid = str(configured_app_id).strip()
        self.appsecret = str(configured_app_secret).strip()
        self.corpid = str(corp_id or config.get("corp_id")).strip()
        self.token = str(callback_token or config.get("callback_token") or "").strip()
        self.access_url = str(access_url or config.get("access_url") or "").strip()
        self.send_msg_url = str(
            send_msg_url or config.get("send_msg_url") or ""
        ).strip()
        self.user_info_url = str(
            user_info_url or config.get("user_info_url") or ""
        ).strip()
        self.send_msg_retry_count = int(config.get("send_msg_retry_count") or 3)
        self.send_msg_retry_backoff = float(config.get("send_msg_retry_backoff") or 1.0)

    def _access_url(self) -> str:
        return self.access_url

    def _send_msg_url(self) -> str:
        return self.send_msg_url

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def supports_subscribe(self) -> bool:
        return True

    async def _user_info_url(self: str) -> str:
        return self.user_info_url

    async def prepare_inbound_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

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
            logger.error("qxt aes decrypt failed: {}", exc)
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
                            f"access_token missing in response: {response_text} with corpid:{self.corpid};appid:{self.appid};"
                        )
                    return str(access_token)
            except (
                asyncio.TimeoutError,
                ClientError,
                RuntimeError,
                json.JSONDecodeError,
            ) as exc:
                logger.error("access_token retry attempt={} error={}", attempt, exc)
                last_error = exc
                if attempt >= self.send_msg_retry_count:
                    break
                await asyncio.sleep(self.send_msg_retry_backoff * attempt)
        logger.error("access_token fetch failed: {}", last_error)
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
        logger.debug(
            "qxt subscribe payload parsed frontend_id={} event_type={} payload_size={}",
            self.frontend_id,
            event_type or "(empty)",
            len(decrypted),
        )
        if event_type == "check_url":
            logger.info(
                "received qxt URL verification request frontend_id={}", self.frontend_id
            )
            return self.encrypt(text="success"), None
        if event_type not in ["p2p_chat_receive_msg", "group_chat_at_bot"]:
            logger.info("ignore qxt subscribe event_type={}", event_type)
            return self.encrypt(text="success"), None

        return self.encrypt(text="success"), self.normalize_subscribe_payload(payload)

    def normalize_subscribe_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_type = str(payload.get("event_type") or "")
        if event_type not in ["p2p_chat_receive_msg", "group_chat_at_bot"]:
            return payload

        event = dict(payload.get("event") or {})
        message = dict(event.get("message") or {})
        sender_uid = str(event.get("sender_uid") or "")
        chat_id = str(message.get("chat_id") or "")
        content = str(message.get("content") or "")
        recipient = (
            {"to_group_id": chat_id}
            if event_type == "group_chat_at_bot"
            else {"to_single_uid": sender_uid}
        )
        return build_im_receive_event(
            chat_id=chat_id,
            usr_id=sender_uid,
            content=content,
            attachments=[],
            metadata={
                "provider": "qxt",
                "frontend_id": self.frontend_id,
                "event_type": str(payload.get("event_type", "")),
                "chat_type": str(message.get("chat_type", "")),
                "message_type": str(message.get("type", "")),
                "message_id": str(message.get("message_id", "")),
                "timestamp": str(payload.get("timestamp", "")),
                "source": "subscribe",
                "reply_target": {
                    "type": "qxt",
                    "frontend_id": self.frontend_id,
                    **recipient,
                },
            },
        )

    async def _download_attachment_bytes(
        self,
        url: str,
        filename_override: str | None = None,
    ) -> tuple[bytes | None, str, str | None]:
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
                    filename = (
                        filename_override
                        or self._attachment_filename_from_response(
                            url=url,
                            headers=dict(response.headers),
                        )
                    )
                    return (
                        data,
                        filename,
                        str(response.headers.get("Content-Type") or "").strip() or None,
                    )
            except (asyncio.TimeoutError, ClientError, RuntimeError) as exc:
                logger.error(
                    "qxt attachment download retry attempt={} url={} error={}",
                    attempt,
                    url,
                    exc,
                )
                last_error = exc
                if attempt >= self.send_msg_retry_count:
                    break
                await asyncio.sleep(self.send_msg_retry_backoff * attempt)
        logger.error("qxt attachment download failed url={} error={}", url, last_error)
        return None, filename_override or self._guess_filename(url), None

    @staticmethod
    def _attachment_filename_from_response(url: str, headers: dict[str, str]) -> str:
        content_disposition = str(
            headers.get("Content-Disposition")
            or headers.get("content-disposition")
            or ""
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
                        "{} response attempt={} status={} body_len={}",
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
                logger.error("{} retry attempt={} error={}", log_label, attempt, exc)
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
        if parsed.scheme not in {"http", "https"}:
            raise RuntimeError(f"attachment must be a url: {ref}")
        payload, filename, content_type = await self._download_attachment_bytes(
            ref, filename_override
        )
        if not payload:
            raise RuntimeError(f"attachment download failed: {ref}")
        if not content_type:
            content_type = mimetypes.guess_type(filename)[0]
        return payload, filename, content_type

    async def _upload_attachment_with_retry(
        self, attachment: Any, access_token: str
    ) -> dict[str, str]:
        if isinstance(attachment, dict) and attachment.get("storage") == "qxt":
            if attachment.get("frontend_id") != self.frontend_id:
                raise RuntimeError(
                    "qxt media reference belongs to a different frontend"
                )
            media_id = attachment.get("media_id")
            if not isinstance(media_id, str) or not media_id.strip():
                raise RuntimeError("qxt media reference is missing media_id")
            return {
                "media_id": media_id,
                "upload_type": str(attachment.get("upload_type") or "file"),
                "filename": str(attachment.get("filename") or "attachment.bin"),
            }
        media_bytes, filename, content_type = await self._attachment_upload_source(
            attachment
        )
        return await self._upload_media_bytes_with_retry(
            media_bytes, filename, content_type, access_token
        )

    async def upload_outbound_attachment(
        self,
        *,
        data: bytes | None = None,
        filename: str = "attachment.bin",
        content_type: str | None = None,
        attachment: Any = None,
    ) -> dict[str, str]:
        """Upload request bytes (or a URL) without reading the user's workspace."""
        token_result = self.get_access_token()
        access_token = (
            await token_result if inspect.isawaitable(token_result) else token_result
        )
        if not access_token:
            raise RuntimeError("Failed to retrieve access token for attachment upload")
        if data is None:
            data, filename, content_type = await self._attachment_upload_source(
                attachment
            )
        if not data:
            raise RuntimeError("empty attachment data")
        uploaded = await self._upload_media_bytes_with_retry(
            data, filename, content_type, access_token
        )
        return {
            **uploaded,
            "storage": "qxt",
            "frontend_id": self.frontend_id,
            "content_type": content_type or "application/octet-stream",
        }

    async def _upload_media_bytes_with_retry(
        self,
        media_bytes: bytes,
        filename: str,
        content_type: str | None,
        access_token: str,
    ) -> dict[str, str]:
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
                        "media_upload response attempt={} status={} body_len={}",
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
                logger.error("media_upload retry attempt={} error={}", attempt, exc)
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

    async def get_user_info(self, user_id: str) -> dict[str, Any]:
        user_info_url = await self._user_info_url()
        if not user_info_url:
            raise RuntimeError("USER_INFO_URL is not configured")

        access_token = await self.get_access_token()
        if access_token is None:
            raise RuntimeError("Failed to retrieve access token for response sending")
        if not self._send_msg_url():
            raise RuntimeError("SEND_MSG_URL is not configured")

        async with get_dispatch_session().get(
            user_info_url,
            params={"access_token": access_token, "userid": user_id},
        ) as response:
            response_text = await response.text()
            if response.status >= 400:
                raise RuntimeError(
                    f"user_info request rejected with {response.status}: {response_text}"
                )
            return json.loads(response_text or "{}")

    async def send_attachments_with_retry(
        self,
        send_meta: dict[str, Any],
        attachments: list[Any],
        access_token: str,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for attachment in attachments:
            uploaded = await self._upload_attachment_with_retry(
                attachment, access_token
            )
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

    def _normalize_outbound_payload(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        if payload.get("to_single_uid") is not None or payload.get("to_group_id") is not None:
            return payload

        metadata = dict(payload.get("metadata") or {})
        reply_target = dict(metadata.get("reply_target") or {})
        to_group_id = str(
            reply_target.get("to_group_id") or metadata.get("to_group_id") or ""
        ).strip()
        to_single_uid = str(
            reply_target.get("to_single_uid") or metadata.get("to_single_uid") or ""
        ).strip()
        if not to_group_id and not to_single_uid:
            raise RuntimeError("missing qxt recipient")

        recipient = (
            {"to_group_id": to_group_id}
            if to_group_id
            else {"to_single_uid": to_single_uid}
        )
        return {
            **recipient,
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
