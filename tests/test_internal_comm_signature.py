import hashlib
import secrets
import string
import time


def generate_nonce(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def current_timestamp() -> str:
    return str(int(time.time()))


def build_signature(nonce: str, timestamp: str, app_secret: str) -> str:
    sign_str = f"{nonce}{timestamp}{app_secret}"
    return hashlib.sha256(sign_str.encode("utf-8")).hexdigest()


def build_auth_headers(app_secret: str, nonce: str | None = None, timestamp: str | None = None) -> dict[str, str]:
    actual_nonce = nonce or generate_nonce()
    actual_timestamp = timestamp or current_timestamp()
    signature = build_signature(actual_nonce, actual_timestamp, app_secret)
    return {
        "nonce": actual_nonce,
        "timestamp": actual_timestamp,
        "signature": signature,
        "signver": "v2",
    }


def test_build_signature_matches_given_example() -> None:
    nonce = "8YC3yNIXDxnB0QyP"
    timestamp = "1695022840"
    app_secret = "QcUMda05kKGRApJlNpDFzxYW6U0eSVlS"

    assert build_signature(nonce, timestamp, app_secret) == (
        "12e3d4819b7bed2d29fcd3c5744de26d4857008c8d4f0c32bd25e872c1312367"
    )


def test_build_auth_headers_uses_same_nonce_and_timestamp() -> None:
    nonce = "8YC3yNIXDxnB0QyP"
    timestamp = "1695022840"
    app_secret = "QcUMda05kKGRApJlNpDFzxYW6U0eSVlS"

    headers = build_auth_headers(app_secret, nonce=nonce, timestamp=timestamp)

    assert headers == {
        "nonce": "8YC3yNIXDxnB0QyP",
        "timestamp": "1695022840",
        "signature": "12e3d4819b7bed2d29fcd3c5744de26d4857008c8d4f0c32bd25e872c1312367",
        "signver": "v2",
    }


def test_generate_nonce_returns_16_characters() -> None:
    nonce = generate_nonce()

    assert len(nonce) == 16
    assert nonce.isalnum()

