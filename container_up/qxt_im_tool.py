from __future__ import annotations

import base64
import hashlib
import json
import secrets
import string
import time
from typing import Any

from Crypto.Cipher import AES
from loguru import logger

from container_up.http_state import get_dispatch_session

_AES_BLOCK_SIZE = 16


def build_im_receive_event(
    *,
    chat_id: str,
    user_id: str,
    content: str,
    attachments: list[Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_type": "im_message_receive",
        "event": {
            "chat_id": chat_id,
            "user_id": user_id,
            "content": content,
            "attachments": list(attachments or []),
            "metadata": dict(metadata or {}),
        },
    }


class QxtIMParser:
    provider = "qxt"

    def __init__(self, *, frontend_id: str, frontend_config: dict[str, Any]) -> None:
        self.frontend_id = frontend_id
        self.appid = str(frontend_config.get("app_id", "")).strip()
        self.appsecret = str(frontend_config.get("app_secret", "")).strip()
        self.corpid = str(frontend_config.get("corp_id", "")).strip()
        self.token = str(frontend_config.get("callback_token", "")).strip()
        self.access_url = str(frontend_config.get("access_url", "")).strip()
        self.send_msg_url = str(frontend_config.get("send_msg_url", "")).strip()

    def start(self) -> None:
        for name in ("appid", "appsecret", "corpid", "token", "access_url", "send_msg_url"):
            if not getattr(self, name):
                raise ValueError(f"QXT configuration requires {name}")

    def stop(self) -> None:
        pass

    def supports_subscribe(self) -> bool:
        return True

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

    async def get_access_token(self) -> str:
        access_url = self.access_url
        if not access_url:
            raise RuntimeError("access_url is not configured")
        if not self.corpid:
            raise RuntimeError("corp_id is not configured")
        if not self.appid:
            raise RuntimeError("app_id is not configured")

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
            user_id=sender_uid,
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


    async def _post_json(
        self,
        *,
        url: str,
        params: dict[str, str],
        json_payload: dict[str, Any],
        log_label: str,
    ) -> dict[str, Any]:
        async with get_dispatch_session().post(
            url,
            params=params,
            json=json_payload,
        ) as response:
            response_text = await response.text()
            logger.info(
                "{} response status={} body_len={}",
                log_label,
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


    async def _send_message(
        self,
        *,
        send_meta: dict[str, Any],
        access_token: str,
        message_type: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._post_json(
            url=self.send_msg_url,
            params={"access_token": access_token},
            json_payload=send_meta | {"type": message_type, "message": message},
            log_label="send_message",
        )

    async def post_message(self, *, payload: dict[str, object]) -> dict[str, object]:
        target = payload["metadata"]["reply_target"]
        if target.get("type") != "qxt":
            raise ValueError("qxt reply_target is required")
        recipient = {key: target[key] for key in ("to_group_id", "to_single_uid") if target.get(key)}
        if len(recipient) != 1:
            raise ValueError("qxt requires exactly one recipient")
        if payload.get("attachments"):
            raise ValueError("channel attachments must be rendered as object URLs")
        if not self.send_msg_url:
            raise RuntimeError("send_msg_url is not configured")
        token = await self.get_access_token()
        result = await self._send_message(send_meta=recipient, access_token=token,
            message_type="text", message={"content": payload["content"]})
        return {"message": result, "attachments": []}
