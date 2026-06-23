"""WeChat Work (企业微信) callback message crypto — WXBizMsgCrypt.

Implements the standard verify/decrypt/encrypt scheme:
  - AES-256-CBC, key = base64decode(EncodingAESKey + '='), IV = key[:16]
  - plaintext = random(16) + msglen(4, big-endian) + msg + receiveid
  - signature = sha1(sorted([token, timestamp, nonce, encrypt]).join())
Pure-stdlib + `cryptography`; no third-party WeChat SDK.
"""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class WeChatCryptoError(Exception):
    pass


def _pkcs7_unpad(data: bytes) -> bytes:
    pad = data[-1]
    if pad < 1 or pad > 32:
        return data
    return data[:-pad]


def _pkcs7_pad(data: bytes, block: int = 32) -> bytes:
    pad = block - (len(data) % block)
    return data + bytes([pad]) * pad


class WXBizMsgCrypt:
    def __init__(self, token: str, encoding_aes_key: str, receive_id: str) -> None:
        self.token = token
        self.receive_id = receive_id
        try:
            self.aes_key = base64.b64decode(encoding_aes_key + "=")
        except Exception as exc:  # noqa: BLE001
            raise WeChatCryptoError(f"invalid EncodingAESKey: {exc}")
        if len(self.aes_key) != 32:
            raise WeChatCryptoError("EncodingAESKey must decode to 32 bytes")
        self.iv = self.aes_key[:16]

    # ----------------------------------------------------------- signature
    def _signature(self, timestamp: str, nonce: str, encrypt: str) -> str:
        items = sorted([self.token, timestamp, nonce, encrypt])
        return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()

    def verify_signature(self, signature: str, timestamp: str, nonce: str, encrypt: str) -> bool:
        return self._signature(timestamp, nonce, encrypt) == signature

    # ----------------------------------------------------------- decrypt
    def _decrypt(self, encrypt_b64: str) -> str:
        cipher = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.iv))
        decryptor = cipher.decryptor()
        raw = decryptor.update(base64.b64decode(encrypt_b64)) + decryptor.finalize()
        raw = _pkcs7_unpad(raw)
        # raw = random(16) + msglen(4) + msg + receiveid
        msg_len = socket.ntohl(struct.unpack("I", raw[16:20])[0])
        msg = raw[20 : 20 + msg_len].decode("utf-8")
        receive_id = raw[20 + msg_len :].decode("utf-8")
        if self.receive_id and receive_id != self.receive_id:
            raise WeChatCryptoError("receive_id mismatch")
        return msg

    def decrypt_message(self, encrypt_b64: str, signature: str, timestamp: str, nonce: str) -> str:
        if not self.verify_signature(signature, timestamp, nonce, encrypt_b64):
            raise WeChatCryptoError("signature verification failed")
        return self._decrypt(encrypt_b64)

    def verify_url(self, signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """GET URL-verification handshake → return the decrypted echostr."""
        if not self.verify_signature(signature, timestamp, nonce, echostr):
            raise WeChatCryptoError("signature verification failed")
        return self._decrypt(echostr)

    # ----------------------------------------------------------- encrypt
    def _encrypt(self, plaintext: str, nonce16: bytes) -> str:
        msg = plaintext.encode("utf-8")
        data = nonce16 + struct.pack("I", socket.htonl(len(msg))) + msg + self.receive_id.encode("utf-8")
        data = _pkcs7_pad(data)
        cipher = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.iv))
        enc = cipher.encryptor()
        return base64.b64encode(enc.update(data) + enc.finalize()).decode("utf-8")

    def encrypt_message(self, reply_xml: str, nonce: str, timestamp: str, nonce16: bytes = b"0123456789abcdef") -> str:
        encrypt = self._encrypt(reply_xml, nonce16)
        signature = self._signature(timestamp, nonce, encrypt)
        return (
            f"<xml><Encrypt><![CDATA[{encrypt}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
            f"<TimeStamp>{timestamp}</TimeStamp><Nonce><![CDATA[{nonce}]]></Nonce></xml>"
        )


def parse_message_xml(xml_text: str) -> dict:
    """Parse a decrypted WeChat message XML into a dict."""
    root = ET.fromstring(xml_text)
    return {child.tag: (child.text or "") for child in root}
