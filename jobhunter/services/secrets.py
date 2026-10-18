"""Secret storage.

Preferred backend: the operating-system credential store via ``keyring``
(macOS Keychain, Windows Credential Locker, Secret Service on Linux).

Fallback backend: a file inside the private data directory. With
``JOBHUNTER_SECRETS_PASSPHRASE`` set, the file is encrypted with a key derived
from that passphrase (PBKDF2 + Fernet). Without it, the file is only protected
by owner-only filesystem permissions, which is NOT encryption, and the
dashboard says so.

Threat model for the file backend: it protects tokens against casual copying
of the data directory or backups when a passphrase is set, and against other
local users via permissions. It does not protect against an attacker who runs
code as the same user, or who can read the worker's environment.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from jobhunter.config import Paths, write_private_file

SERVICE_NAME = "jobhunteros"
ENV_BACKEND = "JOBHUNTER_SECRETS_BACKEND"
ENV_PASSPHRASE = "JOBHUNTER_SECRETS_PASSPHRASE"


class SecretStore:
    def __init__(self, paths: Paths, backend: str | None = None):
        self.paths = paths
        self.backend_name = (backend or os.environ.get(ENV_BACKEND) or "auto").lower()
        self._keyring = None
        self._file_cache: dict[str, str] | None = None
        if self.backend_name in {"auto", "keyring"}:
            self._keyring = _load_keyring()
            if self._keyring is None and self.backend_name == "keyring":
                raise RuntimeError("keyring backend requested but no usable OS credential store was found")

    # -- public --------------------------------------------------------------
    @property
    def backend(self) -> str:
        if self._keyring is not None:
            return "keyring"
        return "file-encrypted" if os.environ.get(ENV_PASSPHRASE) else "file-plain"

    def describe(self) -> dict:
        return {
            "backend": self.backend,
            "encrypted": self.backend in {"keyring", "file-encrypted"},
            "note": {
                "keyring": "Stored in your operating-system credential store.",
                "file-encrypted": "Stored in an encrypted file; the passphrase comes from the environment.",
                "file-plain": "Stored in a file protected only by file permissions. This is not encryption.",
            }[self.backend],
        }

    def get(self, key: str) -> str | None:
        if self._keyring is not None:
            try:
                return self._keyring.get_password(SERVICE_NAME, self._scoped(key))
            except Exception:
                return None
        return self._file_load().get(key)

    def set(self, key: str, value: str) -> None:
        if self._keyring is not None:
            self._keyring.set_password(SERVICE_NAME, self._scoped(key), value)
            return
        data = self._file_load()
        data[key] = value
        self._file_save(data)

    def delete(self, key: str) -> None:
        if self._keyring is not None:
            try:
                self._keyring.delete_password(SERVICE_NAME, self._scoped(key))
            except Exception:
                pass
            return
        data = self._file_load()
        data.pop(key, None)
        self._file_save(data)

    def list_keys(self) -> list[str]:
        if self._keyring is not None:
            index = self._file_load().get("__index__", "")
            return [k for k in index.split(",") if k]
        return [k for k in self._file_load() if k != "__index__"]

    # -- internals -----------------------------------------------------------
    def _scoped(self, key: str) -> str:
        # Keep demo and live secrets apart, and record an index so we can list keys.
        scoped = f"{'demo:' if self.paths.demo else ''}{key}"
        data = self._file_load()
        index = {k for k in data.get("__index__", "").split(",") if k}
        if key not in index:
            index.add(key)
            data["__index__"] = ",".join(sorted(index))
            self._file_save(data)
        return scoped

    def _file_load(self) -> dict[str, str]:
        f = self.paths.secrets_file
        if not f.exists():
            self._file_cache = {}
            return self._file_cache
        raw = f.read_bytes()
        try:
            self._file_cache = _decode(raw)
        except Exception:
            self._file_cache = {}
        return self._file_cache

    def _file_save(self, data: dict[str, str]) -> None:
        write_private_file(self.paths.secrets_file, _encode(data))


def _load_keyring():
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
        from keyring.backends.null import Keyring as NullKeyring
    except Exception:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:
        return None
    if isinstance(backend, FailKeyring | NullKeyring):
        return None
    name = type(backend).__module__ + "." + type(backend).__name__
    # Chainer with only fail/null backends is still unusable.
    if "chainer" in name.lower():
        try:
            if not getattr(backend, "backends", []):
                return None
        except Exception:
            return None
    # Smoke test the backend so we fail early instead of at first use.
    try:
        backend.get_password(SERVICE_NAME, "__probe__")
    except Exception:
        return None
    return backend


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _encode(data: dict[str, str]) -> bytes:
    payload = json.dumps(data).encode("utf-8")
    passphrase = os.environ.get(ENV_PASSPHRASE)
    if not passphrase:
        return b"PLAIN1\n" + payload
    from cryptography.fernet import Fernet

    salt = os.urandom(16)
    token = Fernet(_derive_key(passphrase, salt)).encrypt(payload)
    return b"ENC1\n" + base64.b64encode(salt) + b"\n" + token


def _decode(raw: bytes) -> dict[str, str]:
    if raw.startswith(b"PLAIN1\n"):
        return json.loads(raw[len(b"PLAIN1\n") :].decode("utf-8"))
    if raw.startswith(b"ENC1\n"):
        passphrase = os.environ.get(ENV_PASSPHRASE)
        if not passphrase:
            raise RuntimeError("secrets file is encrypted but JOBHUNTER_SECRETS_PASSPHRASE is not set")
        from cryptography.fernet import Fernet

        _, salt_b64, token = raw.split(b"\n", 2)
        salt = base64.b64decode(salt_b64)
        return json.loads(Fernet(_derive_key(passphrase, salt)).decrypt(token).decode("utf-8"))
    raise RuntimeError("unrecognized secrets file format")


def secrets_file_exists(paths: Paths) -> bool:
    return Path(paths.secrets_file).exists()
