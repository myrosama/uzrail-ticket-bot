"""Secrets at rest and in transit.

- At rest: Windows DPAPI (tied to the Windows user account); elsewhere a Fernet key kept in a
  file only the user can read.
- In transit from the Mini App: the PC owns an RSA-2048 key pair; the Mini App encrypts the railway
  password with RSA-OAEP/SHA-256 (WebCrypto) so Telegram only ever carries ciphertext.
"""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from . import paths

# ---------------------------------------------------------------- at rest

if os.name == "nt":
    import ctypes
    import ctypes.wintypes as wt

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _to_blob(data: bytes):
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _dpapi(data: bytes, encrypt: bool) -> bytes:
        inp, _keep = _to_blob(data)
        out = _Blob()
        fn = ctypes.windll.crypt32.CryptProtectData if encrypt else ctypes.windll.crypt32.CryptUnprotectData
        ok = fn(ctypes.byref(inp), None, None, None, None, 0x01, ctypes.byref(out))  # UI_FORBIDDEN
        if not ok:
            raise OSError("DPAPI failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)

    def _protect(raw: bytes) -> bytes:
        return _dpapi(raw, True)

    def _unprotect(raw: bytes) -> bytes:
        return _dpapi(raw, False)
else:
    from cryptography.fernet import Fernet

    def _fernet() -> Fernet:
        key_file = paths.data_dir() / ".key"
        if not key_file.exists():
            key_file.write_bytes(Fernet.generate_key())
            os.chmod(key_file, 0o600)
        return Fernet(key_file.read_bytes())

    def _protect(raw: bytes) -> bytes:
        return _fernet().encrypt(raw)

    def _unprotect(raw: bytes) -> bytes:
        return _fernet().decrypt(raw)


def protect(text: str) -> str:
    if not text:
        return ""
    return "enc:" + base64.b64encode(_protect(text.encode("utf-8"))).decode("ascii")


def unprotect(value: str) -> str:
    if not value:
        return ""
    if not value.startswith("enc:"):
        return value
    return _unprotect(base64.b64decode(value[4:])).decode("utf-8")


# ---------------------------------------------------------------- Mini App transport key

def new_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode("ascii")


def public_key_b64(private_pem: str) -> str:
    """SPKI DER, base64 - what WebCrypto importKey('spki') expects."""
    key = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None)
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(der).decode("ascii")


def decrypt_from_miniapp(private_pem: str, b64_cipher: str) -> str:
    key = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None)
    plain = key.decrypt(base64.b64decode(b64_cipher),
                        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return plain.decode("utf-8")
