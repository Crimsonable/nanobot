import hashlib
import base64
from Crypto.Cipher import AES


def sha1(text):
    sha1_hash = hashlib.sha1(text.encode()).hexdigest()
    return sha1_hash


def md5(text):
    md5_hash = hashlib.md5(text.encode()).hexdigest()
    return md5_hash


def aes_decrypt(app_key, iv, text):
    try:
        iv = iv.encode("utf-8")
        app_key = app_key.encode("utf-8")
        text = base64.b64decode(text)
        cipher = AES.new(app_key, AES.MODE_CBC, iv)
        decrypted_text = cipher.decrypt(text).decode("utf-8").rstrip("\0")
        return decrypted_text
    except Exception as e:
        print("Error during AES decryption:", e)
        return None


def aes_encrypt(app_key, iv, text):
    try:
        iv = iv.encode("utf-8")
        app_key = app_key.encode("utf-8")
        text = text.encode("utf-8")
        cipher = AES.new(app_key, AES.MODE_CBC, iv)
        padded_text = pkcs7_padding(text, AES.block_size)
        encrypted_text = cipher.encrypt(padded_text)
        encoded_text = base64.b64encode(encrypted_text).decode("utf-8")
        return encoded_text
    except Exception as e:
        print("Error during AES encryption:", e)
        return None


def pkcs7_padding(data, block_size):
    padding_length = block_size - (len(data) % block_size)
    padding = bytes([padding_length] * padding_length)
    return data + padding


def get_current_timestamp():
    import time

    timestamp = int(time.time())
    return str(timestamp)


def sort(arr):
    sorted_arr = sorted(arr)
    sorted_string = "".join(sorted_arr)
    return sorted_string


def decrypt(app_key, token, signature, timestamp, nonce, text):
    timestamp = timestamp or get_current_timestamp()
    arr = [token, timestamp, nonce, text]
    sorted_string = sort(arr)
    sha1_string = sha1(sorted_string)
    if sha1_string != signature:
        raise Exception("Signature verification error")
    iv = md5(app_key)[:16]
    return aes_decrypt(app_key, iv, text)


def encrypt(app_key, token, timestamp, nonce, text):
    timestamp = timestamp or get_current_timestamp()
    iv = md5(app_key)[:16]
    encrypted_text = aes_encrypt(app_key, iv, text)
    arr = [token, timestamp, nonce, encrypted_text]
    sorted_string = sort(arr)
    msg_signature = sha1(sorted_string)

    result = {
        "msgSignature": msg_signature,
        "encrypt": encrypted_text,
        "timeStamp": timestamp,
        "nonce": nonce,
    }

    return result
