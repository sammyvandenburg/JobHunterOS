"""Paths, environment, and application settings.

All private state lives under one application-data directory. Nothing in this
module reads secrets; see ``jobhunter.services.secrets`` for that.
"""

from __future__ import annotations

import os
import platform
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "jobhunteros"
POLICY_VERSION = "2026.10-1"  # matching policy version; bump when scoring rules change
DEFAULT_DASHBOARD_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = 8765

ENV_HOME = "JOBHUNTER_HOME"
ENV_DEMO = "JOBHUNTER_DEMO"
ENV_DASHBOARD_HOST = "JOBHUNTER_DASHBOARD_HOST"
ENV_DASHBOARD_PORT = "JOBHUNTER_DASHBOARD_PORT"

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_FETCH_BYTES = 8 * 1024 * 1024  # 8 MB per HTTP response
HTTP_TIMEOUT_SECONDS = 20.0
HTTP_MAX_CONCURRENCY = 4


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(p))).resolve()


def default_home() -> Path:
    """Default private state directory: ~/.jobhunteros (or $JOBHUNTER_HOME)."""
    override = os.environ.get(ENV_HOME)
    if override:
        return _expand(override)
    return Path.home() / ".jobhunteros"


def demo_from_env() -> bool:
    return os.environ.get(ENV_DEMO, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Paths:
    """Filesystem layout for one state root (live or demo)."""

    home: Path
    demo: bool = False

    @property
    def root(self) -> Path:
        return self.home / "demo" if self.demo else self.home

    @property
    def db_path(self) -> Path:
        return self.root / "jobhunter.sqlite3"

    @property
    def documents_dir(self) -> Path:
        return self.root / "documents"

    @property
    def derived_dir(self) -> Path:
        return self.root / "derived"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def handoffs_dir(self) -> Path:
        return self.root / "exports" / "interviewos"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def locks_dir(self) -> Path:
        return self.root / "locks"

    @property
    def secrets_file(self) -> Path:
        return self.root / "secrets.enc"

    @property
    def dashboard_secret_file(self) -> Path:
        return self.root / "dashboard.secret"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    def ensure(self) -> None:
        for d in (
            self.root,
            self.documents_dir,
            self.derived_dir,
            self.exports_dir,
            self.handoffs_dir,
            self.cache_dir,
            self.logs_dir,
            self.locks_dir,
            self.backups_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        _restrict_dir(self.root)


def _restrict_dir(path: Path) -> None:
    """Best-effort owner-only permissions on POSIX. This is not encryption."""
    if platform.system() == "Windows":
        return
    try:
        os.chmod(path, stat.S_IRWXU)
    except OSError:
        pass


def write_private_file(path: Path, data: bytes) -> None:
    """Write a file readable only by the owner (POSIX). Not encryption."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    if platform.system() != "Windows":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def dashboard_secret(paths: Paths) -> bytes:
    """Per-installation secret used for CSRF tokens and session signing."""
    f = paths.dashboard_secret_file
    if f.exists():
        data = f.read_bytes().strip()
        if len(data) >= 32:
            return data
    data = secrets.token_hex(32).encode()
    write_private_file(f, data)
    return data


@dataclass(frozen=True)
class AppContext:
    """Everything a command or request handler needs to find state."""

    paths: Paths

    @classmethod
    def from_env(cls, demo: bool | None = None, home: Path | None = None) -> AppContext:
        use_demo = demo_from_env() if demo is None else demo
        p = Paths(home=home or default_home(), demo=use_demo)
        p.ensure()
        return cls(paths=p)

    @property
    def demo(self) -> bool:
        return self.paths.demo


def dashboard_bind() -> tuple[str, int]:
    host = os.environ.get(ENV_DASHBOARD_HOST, DEFAULT_DASHBOARD_HOST).strip() or DEFAULT_DASHBOARD_HOST
    try:
        port = int(os.environ.get(ENV_DASHBOARD_PORT, DEFAULT_DASHBOARD_PORT))
    except ValueError:
        port = DEFAULT_DASHBOARD_PORT
    return host, port
