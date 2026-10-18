"""Environment checks for `jobhunter doctor`."""

from __future__ import annotations

import os
import platform
import shutil
import sys

from jobhunter.config import AppContext


def run(ctx: AppContext) -> dict:
    checks = []

    def add(name: str, ok: bool, detail: str, fix: str | None = None) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": fix})

    v = sys.version_info
    add("Python", v >= (3, 11), f"{platform.python_version()} on {platform.system()} {platform.machine()}", None if v >= (3, 11) else "Install Python 3.11 or newer.")
    add("uv", shutil.which("uv") is not None, shutil.which("uv") or "not found", "Install uv: https://docs.astral.sh/uv/getting-started/installation/")
    for mod, label in (("fastapi", "Dashboard (FastAPI)"), ("httpx", "HTTP client"), ("pypdf", "PDF reading"), ("keyring", "OS credential store"), ("cryptography", "Encryption"), ("google_auth_oauthlib", "Gmail OAuth")):
        try:
            __import__(mod)
            add(label, True, "installed")
        except ImportError:
            add(label, False, "missing", "Run `uv sync`.")
    try:
        import playwright  # noqa: F401

        add("Browser tests (Playwright)", True, "installed (optional)")
    except ImportError:
        add("Browser tests (Playwright)", True, "not installed (optional; `uv sync --extra browser`)")
    root = ctx.paths.root
    writable = os.access(root, os.W_OK)
    add("Data directory", writable, str(root) + (" (demo)" if ctx.demo else ""), None if writable else "Check permissions on the data directory.")
    try:
        from jobhunter.services.secrets import SecretStore

        st = SecretStore(ctx.paths).describe()
        add("Secret storage", True, f"{st['backend']}: {st['note']}")
    except Exception as exc:
        add("Secret storage", False, str(exc))
    claude = shutil.which("claude")
    add("Claude Code CLI", claude is not None, claude or "not found (needed only for unattended explanations)", None)
    try:
        import httpx

        r = httpx.get("https://boards-api.greenhouse.io/v1/boards/gitlab/jobs", params={"content": "false"}, timeout=10)
        add("Internet access to job APIs", r.status_code == 200, f"Greenhouse answered {r.status_code}")
    except Exception as exc:
        add("Internet access to job APIs", False, f"{type(exc).__name__}", "Check your network or proxy settings.")
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
