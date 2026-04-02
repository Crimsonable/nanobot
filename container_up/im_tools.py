import asyncio
import base64
import hashlib
import logging
import secrets
import string
import time

import requests
from aiohttp import ClientError
from Crypto.Cipher import AES
from venv import logger

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


_im_parser: "QxtIMParser | None" = None


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
    def _pkcs7_pad(text: str, block_size: int = AES.block_size) -> bytes:
        data = text.encode("utf-8")
        pad_len = block_size - (len(data) % block_size)
        return data + bytes([pad_len] * pad_len)

    @staticmethod
    def _pkcs7_unpad(data: bytes, block_size: int = AES.block_size) -> bytes:
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
    def generate_random_string(length=16):
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

    async def post_message_with_retry(self, *, payload: dict[str, object]) -> dict[str, object]:
        access_token = await asyncio.to_thread(self.get_access_token)
        if access_token is None:
            raise RuntimeError("Failed to retrieve access token for response sending")
        if not SEND_MSG_URL:
            raise RuntimeError("SEND_MSG_URL is not configured")

        last_error: Exception | None = None
        for attempt in range(1, SEND_MSG_RETRY_COUNT + 1):
            try:
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


def init_im_parser() -> QxtIMParser:
    global _im_parser
    _im_parser = QxtIMParser()
    return _im_parser


def get_im_parser() -> QxtIMParser:
    if _im_parser is None:
        raise RuntimeError("IM parser is not initialized")
    return _im_parser
