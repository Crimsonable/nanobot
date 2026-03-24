import base64
import hashlib
import secrets
import string
import time

from Crypto.Cipher import AES
import requests
import logging


_crypto_parser: "CryptoParser | None" = None


class CryptoParser:
    def __init__(
        self,
        *,
        access_url: str,
        appid: str = "",
        appsecret: str = "",
        corpid: str = "",
        token: str = "",
    ) -> None:
        self.appid = appid
        self.appsecret = appsecret
        self.corpid = corpid
        self.token = token
        self.access_url = access_url

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
        if not self.access_url:
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
                url=self.access_url, params={"corpid": self.corpid, "appid": self.appid}
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


def init_crypto_parser(
    *,
    access_url: str = "",
    appid: str = "",
    appsecret: str = "",
    corpid: str = "",
    token: str = "",
) -> CryptoParser:
    global _crypto_parser
    _crypto_parser = CryptoParser(
        access_url=access_url,
        appid=appid,
        appsecret=appsecret,
        corpid=corpid,
        token=token,
    )
    return _crypto_parser


def get_crypto_parser() -> CryptoParser:
    if _crypto_parser is None:
        raise RuntimeError("crypto parser is not initialized")
    return _crypto_parser
