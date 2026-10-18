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

Safety rule: a missing file is an empty store, but an existing file that cannot
be decoded (wrong or missing passphrase, damaged file) makes the store
*locked*: every read and write raises ``SecretStoreLocked`` and the file is not
touched. ``reset_file_store`` moves such a file aside, never deletes it. Writes
are atomic (temp file in the same directory, fsync, rename) so a crash cannot
leave a half-written file behind.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import tempfile
import time
from pathlib import Path

from jobhunter.config import Paths

SERVICE_NAME = "jobhunteros"
ENV_BACKEND = "JOBHUNTER_SECRETS_BACKEND"
ENV_PASSPHRASE = "JOBHUNTER_SECRETS_PASSPHRASE"
INDEX_KEY = "__index__"

_RESET_HINT = (
    "Nothing was changed. Set JOBHUNTER_SECRETS_PASSPHRASE to the passphrase you used, "
    "or run `jobhunter data reset-secrets` to start over (the old file is kept)."
)
LOCKED_MESSAGE = "Hunter cannot read its secret store. The passphrase is wrong or the file is damaged. " + _RESET_HINT
NO_PASSPHRASE_MESSAGE = "Hunter cannot read its secret store. The file is encrypted, but JOBHUNTER_SECRETS_PASSPHRASE is not set. " + _RESET_HINT


class SecretStoreLocked(RuntimeError):
    """The secrets file exists but cannot be decoded. Nothing is read or written while this holds."""

    def __init__(self, message: str = LOCKED_MESSAGE):
        super().__init__(message)


class _PassphraseMissing(RuntimeError):
    """Internal: the file is encrypted and no passphrase is set."""


class SecretStore:
    def __init__(self, paths: Paths, backend: str | None = None):
        self.paths = paths
        self.backend_name = (backend or os.environ.get(ENV_BACKEND) or "auto").lower()
        self._keyring = None
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
        """Never raises. ``readable`` is False (with a plain-English ``problem``) when the store is locked."""
        backend = self.backend
        out = {
            "backend": backend,
            "encrypted": backend in {"keyring", "file-encrypted"},
            "note": {
                "keyring": "Stored in your operating-system credential store.",
                "file-encrypted": "Stored in an encrypted file; the passphrase comes from the environment.",
                "file-plain": "Stored in a file protected only by file permissions. This is not encryption.",
            }[backend],
            "readable": True,
        }
        try:
            self._file_load()
        except SecretStoreLocked as exc:
            out["readable"] = False
            out["problem"] = str(exc)
        return out

    def get(self, key: str) -> str | None:
        if self._keyring is not None:
            try:
                return self._keyring.get_password(SERVICE_NAME, self._scoped(key))
            except Exception:
                return None
        return self._file_load().get(key)

    def set(self, key: str, value: str) -> None:
        if self._keyring is not None:
            # Update the index first: if it is locked nothing changes, in the keyring either.
            self._index_update(add=key)
            self._keyring.set_password(SERVICE_NAME, self._scoped(key), value)
            return
        data = self._file_load()
        data[key] = value
        self._file_save(data)

    def delete(self, key: str) -> None:
        if self._keyring is not None:
            self._index_update(remove=key)
            with contextlib.suppress(Exception):
                self._keyring.delete_password(SERVICE_NAME, self._scoped(key))
            return
        data = self._file_load()
        if key in data:
            del data[key]
            self._file_save(data)

    def list_keys(self) -> list[str]:
        if self._keyring is not None:
            index = self._file_load().get(INDEX_KEY, "")
            return [k for k in index.split(",") if k]
        return [k for k in self._file_load() if k != INDEX_KEY]

    # -- internals -----------------------------------------------------------
    def _scoped(self, key: str) -> str:
        """Keyring entry name. Demo and live secrets are kept apart. Pure: never touches the file."""
        return f"{'demo:' if self.paths.demo else ''}{key}"

    def _index_update(self, add: str | None = None, remove: str | None = None) -> None:
        """Keyring backend: the file only records which keys exist so ``list_keys`` works. Written only on change."""
        data = self._file_load()
        index = {k for k in data.get(INDEX_KEY, "").split(",") if k}
        new = set(index)
        if add:
            new.add(add)
        if remove:
            new.discard(remove)
        if new != index:
            data[INDEX_KEY] = ",".join(sorted(new))
            self._file_save(data)

    def _file_load(self) -> dict[str, str]:
        """Decode the secrets file. Missing file: empty store. Undecodable file: ``SecretStoreLocked``, file untouched."""
        f = Path(self.paths.secrets_file)
        try:
            raw = f.read_bytes()
        except FileNotFoundError:
            return {}
        try:
            data = _decode(raw)
        except _PassphraseMissing as exc:
            raise SecretStoreLocked(NO_PASSPHRASE_MESSAGE) from exc
        except Exception as exc:
            raise SecretStoreLocked() from exc
        if not isinstance(data, dict):
            raise SecretStoreLocked()
        return data

    def _file_save(self, data: dict[str, str]) -> None:
        _atomic_private_write(Path(self.paths.secrets_file), _encode(data))


def _atomic_private_write(path: Path, data: bytes) -> None:
    """Owner-only, all-or-nothing write: temp file in the same directory, fsync, then rename over the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))  # created with mode 0o600
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    if os.name == "posix":
        with contextlib.suppress(OSError):
            dfd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)


def reset_file_store(paths: Paths, force: bool = False) -> dict:
    """Move an unreadable secrets file aside so a fresh store can start. Never deletes anything.

    A file that opens fine with the current passphrase is left alone unless ``force`` is set.
    """
    f = Path(paths.secrets_file)
    if not f.exists():
        return {"status": "nothing_to_do", "note": "There is no secret store file to reset."}
    problem = None
    try:
        SecretStore(paths, backend="file")._file_load()
    except SecretStoreLocked as exc:
        problem = str(exc)
    if problem is None and not force:
        return {"status": "readable", "path": str(f), "note": "The secret store opens fine with the current passphrase, so nothing was changed. Use --force to move it aside anyway."}
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = f.with_name(f"{f.name}.unreadable-{stamp}")
    n = 1
    while target.exists():
        n += 1
        target = f.with_name(f"{f.name}.unreadable-{stamp}-{n}")
    os.replace(f, target)
    return {
        "status": "reset",
        "kept": str(target),
        "problem": problem,
        "note": f"The old file was moved to {target.name}, not deleted. Hunter now starts with an empty secret store: connections that stored a password or token (SMTP, Gmail, Adzuna) need setting up again.",
    }


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
            raise _PassphraseMissing("secrets file is encrypted but JOBHUNTER_SECRETS_PASSPHRASE is not set")
        from cryptography.fernet import Fernet

        _, salt_b64, token = raw.split(b"\n", 2)
        salt = base64.b64decode(salt_b64)
        return json.loads(Fernet(_derive_key(passphrase, salt)).decrypt(token).decode("utf-8"))
    raise RuntimeError("unrecognized secrets file format")


def secrets_file_exists(paths: Paths) -> bool:
    return Path(paths.secrets_file).exists()
