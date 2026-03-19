from container_up.crypt_tools import CrptParser


def test_crypt_parser_decrypt_encrypt_roundtrip() -> None:
    encrypt = (
        "XID13ZpoDG4hAlqScOiUdbHlgAX2P7D6OhrEOJpV9z68JTddpCbYlb0IjQpiUa3cw"
        "ukehaJYezzovhjNRudFmLJYy+AI4CZ6A27YlrDf9Pc="
    )
    msgSignature = "7ab85ccc5251904de04cd32e50146f5f68a48d46"
    timeStamp = "1491805325"
    nonce = "KkSvMbDM"
    appsecret = "wbvOzbieShGQ1aFUkQJ6DR1EGW8mnecA"
    token = ""

    test = CrptParser(appsecret=appsecret, token=token).decrypt(
        signature=msgSignature,
        timeStamp=timeStamp,
        nonce=nonce,
        encrypt=encrypt,
    )
    assert test is not None

    test_encrypt = CrptParser(appsecret=appsecret, token=token).encrypt(
        timeStamp=timeStamp,
        nonce=nonce,
        text=test,
    )
    assert test_encrypt["timeStamp"] == timeStamp
    assert test_encrypt["nonce"] == nonce

    backtest_decrypt = CrptParser(appsecret=appsecret, token=token).decrypt(
        signature=test_encrypt["signature"],
        timeStamp=test_encrypt["timeStamp"],
        nonce=test_encrypt["nonce"],
        encrypt=test_encrypt["encrypt"],
    )
    assert backtest_decrypt == test
