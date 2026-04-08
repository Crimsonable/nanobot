from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import mimetypes
import secrets
import string
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
import requests
from aiohttp import ClientError
from Crypto.Cipher import AES

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
_im_parser: "QxtIMParser | None" = None
_AES_BLOCK_SIZE = 16


class QxtIMParser:
    def __init__(self) -> None:
        self.appid = APP_ID
        self.appsecret = APP_SECRET
        self.corpid = CORP_ID
        self.token = CALLBACK_TOKEN

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
            print("Error during AES decryption:", exc)
            return None

    def _aes_encrypt(self, appsecret: str, text: str) -> str:
        iv = self._md5(appsecret)[:16].encode("utf-8")
        cipher = AES.new(appsecret.encode("utf-8"), AES.MODE_CBC, iv)
        encrypted_text = base64.b64encode(cipher.encrypt(self._pkcs7_pad(text))).decode(
            "utf-8"
        )
        return encrypted_text

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

    def get_access_token(self) -> str | None:
        if not ACCESS_URL:
            logging.error("ACCESS_URL is not configured")
            return None
        if not self.corpid:
            logging.error("CORP_ID is not configured")
            return None
        if not self.appid:
            logging.error("APP_ID is not configured")
            return None
        try:
            resp = requests.get(
                url=ACCESS_URL,
                params={"corpid": self.corpid, "appid": self.appid},
                timeout=10,
            )
        except Exception as exc:
            logging.error("Error during access token retrieval: %s", exc)
            return None
        try:
            resp.raise_for_status()
        except Exception as exc:
            logging.error("Access token request failed: %s", exc)
            return None
        return resp.json().get("access_token")

    @staticmethod
    def _media_upload_url() -> str:
        parsed = urlparse(SEND_MSG_URL)
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
        for attempt in range(1, SEND_MSG_RETRY_COUNT + 1):
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
                if attempt >= SEND_MSG_RETRY_COUNT:
                    break
                await asyncio.sleep(SEND_MSG_RETRY_BACKOFF * attempt)
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
        for attempt in range(1, SEND_MSG_RETRY_COUNT + 1):
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
                if attempt >= SEND_MSG_RETRY_COUNT:
                    break
                await asyncio.sleep(SEND_MSG_RETRY_BACKOFF * attempt)
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
            url=SEND_MSG_URL,
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

    async def post_message_with_retry(
        self, *, payload: dict[str, object]
    ) -> dict[str, object]:
        access_token = await asyncio.to_thread(self.get_access_token)
        if access_token is None:
            raise RuntimeError("Failed to retrieve access token for response sending")
        if not SEND_MSG_URL:
            raise RuntimeError("SEND_MSG_URL is not configured")

        send_meta = {
            key: value
            for key, value in payload.items()
            if key not in {"message", "attachments", "type"}
        }
        message_type = str(payload.get("type") or "")
        message_payload = payload.get("message")
        attachments = list(payload.get("attachments") or [])

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


def init_im_parser() -> QxtIMParser:
    global _im_parser
    _im_parser = QxtIMParser()
    return _im_parser


def get_im_parser() -> QxtIMParser:
    if _im_parser is None:
        raise RuntimeError("IM parser is not initialized")
    return _im_parser
