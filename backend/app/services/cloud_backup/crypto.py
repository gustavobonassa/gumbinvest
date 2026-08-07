"""Optional passphrase encryption for cloud-bound ``.gumbinvest`` files.

The export already strips API keys, but it still carries a whole financial
history — some users won't put that on a third-party cloud in the clear. When
a passphrase is set, the bytes that leave this machine are sealed with
AES-256-GCM under a key derived by scrypt. Both primitives come from
``cryptography``/stdlib; nothing here is hand-rolled.

Layout: ``MAGIC(8) | salt(16) | nonce(12) | ciphertext+tag``. The magic both
identifies the format (so restore knows to ask for the passphrase) and is
bound into the GCM tag as associated data, so it cannot be swapped for
another header without the decryption failing.
"""
from __future__ import annotations

import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"GMBENC1\0"
ENCRYPTED_EXTENSION = ".gumbinvest.enc"

_SALT_LEN = 16
_NONCE_LEN = 12
_HEADER_LEN = len(MAGIC) + _SALT_LEN + _NONCE_LEN


class CloudBackupError(RuntimeError):
    """str(exc) is a pt-BR message safe to show the user."""


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=2**15,
        r=8,
        p=1,
        maxmem=64 * 2**20,
        dklen=32,
    )


def is_encrypted(payload: bytes) -> bool:
    return bytes(payload[: len(MAGIC)]) == MAGIC


def encrypt(payload: bytes, passphrase: str) -> bytes:
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    key = _derive_key(passphrase, salt)
    sealed = AESGCM(key).encrypt(nonce, payload, MAGIC)
    return MAGIC + salt + nonce + sealed


def decrypt(payload: bytes, passphrase: str) -> bytes:
    if not is_encrypted(payload) or len(payload) <= _HEADER_LEN:
        raise CloudBackupError("o arquivo não é um backup criptografado válido")
    if not passphrase:
        raise CloudBackupError(
            "este backup é criptografado: informe a senha de criptografia"
        )
    salt = payload[len(MAGIC) : len(MAGIC) + _SALT_LEN]
    nonce = payload[len(MAGIC) + _SALT_LEN : _HEADER_LEN]
    key = _derive_key(passphrase, salt)
    try:
        return AESGCM(key).decrypt(nonce, payload[_HEADER_LEN:], MAGIC)
    except InvalidTag as exc:
        raise CloudBackupError("senha incorreta ou arquivo corrompido") from exc
