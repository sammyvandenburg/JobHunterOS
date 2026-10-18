"""Small shared helpers: time, hashing, text normalization."""

from __future__ import annotations

import hashlib
import html
import json
import re
import socket
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Any

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style|iframe|object|embed|noscript)[^>]*>.*?</\1>", re.I | re.S)
_NONWORD = re.compile(r"[^a-z0-9]+")


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_iso() -> str:
    return now().isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def days_ago(value: str | None, ref: datetime | None = None) -> float | None:
    dt = parse_iso(value)
    if dt is None:
        return None
    ref = ref or now()
    return (ref - dt).total_seconds() / 86400.0


def epoch_to_iso(value: Any) -> str | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n > 1e12:  # milliseconds
        n = n / 1000.0
    try:
        return datetime.fromtimestamp(n, UTC).replace(microsecond=0).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def strip_html(raw: str | None) -> str:
    """Convert untrusted HTML to plain text. Removes scripts, styles, and tags."""
    if not raw:
        return ""
    text = html.unescape(raw)
    # Some feeds double-escape; unescape twice but never render.
    if "&lt;" in text and "<" not in text:
        text = html.unescape(text)
    text = _SCRIPT.sub(" ", text)
    text = re.sub(r"<\s*(br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", "\n", text, flags=re.I)
    text = _TAG.sub(" ", text)
    text = text.replace("\xa0", " ")
    lines = [_WS.sub(" ", ln).strip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def slugify(text: str | None) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    t = _NONWORD.sub("-", t.lower()).strip("-")
    return t


def normalize_company(name: str | None) -> str:
    if not name:
        return ""
    t = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    words = [w for w in t.split() if w not in {"inc", "llc", "ltd", "gmbh", "bv", "b", "v", "corp", "co", "the", "ag", "sa", "plc", "limited", "incorporated"}]
    return " ".join(words).strip()


def truncate(text: str | None, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown-host"


def iso_in(seconds: float) -> str:
    return (now() + timedelta(seconds=seconds)).isoformat()
