import base64
import hashlib
import secrets
import string
import time

from Crypto.Cipher import AES

class CryptoParser:
    def __init__(
        self,
        *,
        appid: str = "",
        appsecret: str = "",
        corpid: str = "",
        token: str = "",
    ) -> None:
        self.appid = appid
        self.appsecret = appsecret
        self.corpid = corpid
        self.token = token

    @staticmethod
    def _sha1(text: str) -> str:
        return hashlib.sha1(text.encode()).hexdigest()

    @staticmethod
    def _md5(text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()

    def _aes_decrypt(self, appsecret: str, text: str) -> str | None:
        try:
            iv = self._md5(appsecret)[:16].encode("utf-8")
            cipher = AES.new(appsecret.encode("utf-8"), AES.MODE_CBC, iv)
            decrypted_text = cipher.decrypt(base64.b64decode(text)).decode("utf-8")
            return decrypted_text.rstrip("\0")
        except Exception as exc:
            print("Error during AES decryption:", exc)
            return None
        
    def _aes_encrypt(self, appsecret: str, text: str) -> str:
        iv = self._md5(appsecret)[:16].encode("utf-8")
        cipher = AES.new(appsecret.encode("utf-8"), AES.MODE_CBC, iv)
        padded_text = text + (16 - len(text) % 16) * "\0"
        encrypted_text = base64.b64encode(cipher.encrypt(padded_text.encode("utf-8"))).decode("utf-8")
        return encrypted_text

    def _msgSignature(
        self, signature: str, timeStamp: str, nonce: str, encrypt: str
    ) -> str:
        return self._sha1("".join(sorted([signature, timeStamp, nonce, encrypt])))
    
    @staticmethod
    def generate_random_string(length=16):
        chars = string.ascii_letters + string.digits
        return ''.join(secrets.choice(chars) for _ in range(length))

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
        if self._msgSignature(verification_token, timeStamp, nonce, encrypt) != signature:
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
        nonce=nonce or self.generate_random_string()
        
        iv = self._md5(secret)[:16].encode("utf-8")
        cipher = AES.new(secret.encode("utf-8"), AES.MODE_CBC, iv)
        padded_text = text + (16 - len(text) % 16) * "\0"
        encrypted_text = base64.b64encode(cipher.encrypt(padded_text.encode("utf-8"))).decode("utf-8")
        signature = self._msgSignature(verification_token, timeStamp, nonce, encrypted_text)
        return {
            "msgSignature": signature,
            "timeStamp": timeStamp,
            "nonce": nonce,
            "encrypt": encrypted_text,
        }
