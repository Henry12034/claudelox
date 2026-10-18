"""Crittografia richiesta dall'API del Miniserver.

Riferimento: "Communicating with the Loxone Miniserver" (Loxone, v16.0, 2025-06-03):
- RSA (PKCS1 v1.5) per inviare la chiave di sessione AES al Miniserver;
- AES-256-CBC con zero padding per cifrare i comandi di autenticazione;
- SHA1/SHA256 + HMAC per non inviare mai la password in chiaro.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import urllib.parse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SALT_MAX_USES = 10


def load_public_key(value: str):
    """La risposta di jdev/sys/getPublicKey e' una chiave pubblica X.509 (SPKI)
    racchiusa tra marcatori "CERTIFICATE" e senza a capo."""
    body = value.replace("-----BEGIN CERTIFICATE-----", "").replace("-----END CERTIFICATE-----", "")
    der = base64.b64decode("".join(body.split()))
    return serialization.load_der_public_key(der)


def _hash_fn(alg: str | None):
    return hashlib.sha256 if (alg or "").upper() == "SHA256" else hashlib.sha1


def password_hash(user: str, password: str, key_hex: str, user_salt: str, alg: str | None) -> str:
    """Hash per getjwt: HMAC(key, "user:" + UPPER(SHA(password:salt)))."""
    fn = _hash_fn(alg)
    pw_hash = fn(f"{password}:{user_salt}".encode("utf-8")).hexdigest().upper()
    return hmac.new(bytes.fromhex(key_hex), f"{user}:{pw_hash}".encode("utf-8"), fn).hexdigest()


def token_hash(token: str, key_hex: str, alg: str | None) -> str:
    """Hash del token per authwithtoken / refreshjwt / killtoken."""
    return hmac.new(bytes.fromhex(key_hex), token.encode("utf-8"), _hash_fn(alg)).hexdigest()


class Session:
    """Chiave AES di sessione negoziata con jdev/sys/keyexchange."""

    def __init__(self) -> None:
        self.key = os.urandom(32)
        self.iv = os.urandom(16)
        self.salt = secrets.token_hex(2)
        self._salt_uses = 0

    def encrypted_session_key(self, public_key) -> str:
        payload = f"{self.key.hex()}:{self.iv.hex()}".encode("ascii")
        return base64.b64encode(public_key.encrypt(payload, padding.PKCS1v15())).decode("ascii")

    def _cipher(self):
        return Cipher(algorithms.AES(self.key), modes.CBC(self.iv))

    def encrypt(self, plaintext: str) -> str:
        data = plaintext.encode("utf-8")
        data += b"\0" * ((-len(data)) % 16)
        enc = self._cipher().encryptor()
        return base64.b64encode(enc.update(data) + enc.finalize()).decode("ascii")

    def decrypt(self, b64: str) -> str:
        dec = self._cipher().decryptor()
        raw = dec.update(base64.b64decode(b64)) + dec.finalize()
        return raw.rstrip(b"\0").decode("utf-8", "replace")

    def wrap(self, cmd: str, full: bool = False) -> str:
        """Comando cifrato: jdev/sys/enc/... (fenc = anche la risposta e' cifrata)."""
        if self._salt_uses >= SALT_MAX_USES:
            new_salt = secrets.token_hex(2)
            plain = f"nextSalt/{self.salt}/{new_salt}/{cmd}"
            self.salt, self._salt_uses = new_salt, 0
        else:
            plain = f"salt/{self.salt}/{cmd}"
        self._salt_uses += 1
        # terminatore esplicito: il Miniserver legge il comando come stringa C
        cipher = urllib.parse.quote(self.encrypt(plain + "\0"), safe="")
        return f"jdev/sys/{'fenc' if full else 'enc'}/{cipher}"
