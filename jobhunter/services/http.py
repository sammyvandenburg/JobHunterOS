"""Safe outbound HTTP for source adapters and link checks.

Protections: HTTPS-only (HTTP allowed only for explicitly whitelisted API
hosts), DNS resolution checked against private/loopback/link-local ranges on
every hop, bounded redirects validated hop by hop, response size caps,
timeouts, bounded retries with backoff, and a per-host politeness delay.
Nothing here ever follows a redirect to a private address.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from jobhunter import __version__
from jobhunter.config import HTTP_TIMEOUT_SECONDS, MAX_FETCH_BYTES

USER_AGENT = f"JobHunterOS/{__version__} (+private job-search assistant; read-only)"
MAX_REDIRECTS = 5
BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata", "instance-data"}
_HOST_LAST_REQUEST: dict[str, float] = {}
_HOST_LOCK = threading.Lock()


class UnsafeURL(ValueError):
    pass


def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or (addr.version == 6 and addr.ipv4_mapped is not None and _is_private_ip(str(addr.ipv4_mapped)))
    )


def validate_url(url: str, allow_http: bool = False) -> str:
    """Raise UnsafeURL unless the URL is a public https URL. Returns the URL."""
    if not isinstance(url, str) or len(url) > 2048:
        raise UnsafeURL("URL missing or too long")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("https", "http"):
        raise UnsafeURL(f"unsupported URL scheme: {parsed.scheme or 'none'}")
    if parsed.scheme == "http" and not allow_http:
        raise UnsafeURL("only https URLs are allowed")
    host = (parsed.hostname or "").lower().strip(".")
    if not host or host in BLOCKED_HOSTS or host.endswith(".localhost") or host.endswith(".local"):
        raise UnsafeURL("host is not allowed")
    if parsed.username or parsed.password:
        raise UnsafeURL("credentials in URLs are not allowed")
    if parsed.port not in (None, 80, 443):
        raise UnsafeURL("non-standard ports are not allowed")
    # Literal IPs must be public; names must resolve to public addresses only.
    try:
        ipaddress.ip_address(host)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False
    if is_ip_literal:
        if _is_private_ip(host):
            raise UnsafeURL("private or local IP addresses are not allowed")
        return url.strip()
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeURL(f"could not resolve host: {host}") from exc
    for info in infos:
        ip = info[4][0]
        if _is_private_ip(ip):
            raise UnsafeURL("host resolves to a private or local address")
    return url.strip()


def is_safe_url(url: str) -> bool:
    try:
        validate_url(url)
        return True
    except UnsafeURL:
        return False


@dataclass
class Response:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    content: bytes
    elapsed_ms: int

    @property
    def text(self) -> str:
        try:
            return self.content.decode("utf-8")
        except UnicodeDecodeError:
            return self.content.decode("latin-1", errors="replace")

    def json(self):
        import json

        return json.loads(self.text)


class SafeClient:
    """Small wrapper over httpx with hop-by-hop redirect validation."""

    def __init__(
        self,
        timeout: float = HTTP_TIMEOUT_SECONDS,
        max_bytes: int = MAX_FETCH_BYTES,
        retries: int = 2,
        min_interval_per_host: float = 0.5,
        transport: httpx.BaseTransport | None = None,
    ):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.last_response: dict = {}  # etag, last_modified, bytes, status of the last successful GET
        self.retries = retries
        self.min_interval = min_interval_per_host
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.8, */*;q=0.5"},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SafeClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _politeness(self, host: str) -> None:
        with _HOST_LOCK:
            last = _HOST_LAST_REQUEST.get(host, 0.0)
            wait = self.min_interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            _HOST_LAST_REQUEST[host] = time.monotonic()

    def get(self, url: str, params: dict | None = None, headers: dict | None = None, allow_http: bool = False) -> Response:
        current = validate_url(url, allow_http=allow_http)
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._get_follow(current, params, headers, allow_http)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt > self.retries:
                    raise FetchError(f"network error after {attempt} attempts: {exc}") from exc
                time.sleep(min(2.0 ** attempt, 8.0))
            except FetchError as exc:
                if exc.status == 304:
                    raise
                if exc.status in (429, 500, 502, 503, 504) and attempt <= self.retries:
                    if exc.retry_after and exc.retry_after > 30:
                        raise  # the source asked for a long wait: respect it on a later run, never spin here
                    time.sleep(min(exc.retry_after or (2.0 ** attempt), 8.0))
                    continue
                raise

    def _get_follow(self, url: str, params: dict | None, headers: dict | None, allow_http: bool) -> Response:
        hops = 0
        current = url
        start = time.monotonic()
        while True:
            host = urlparse(current).hostname or ""
            self._politeness(host)
            send_headers = dict(headers or {}) if hops == 0 else {k: v for k, v in (headers or {}).items() if k.lower() not in {"if-none-match", "if-modified-since"}}
            with self._client.stream("GET", current, params=params if hops == 0 else None, headers=send_headers or None) as resp:
                if resp.status_code == 304:
                    raise FetchError("not modified since the last check", status=304)
                if resp.status_code in (301, 302, 303, 307, 308):
                    hops += 1
                    if hops > MAX_REDIRECTS:
                        raise FetchError("too many redirects", status=resp.status_code)
                    location = resp.headers.get("location")
                    if not location:
                        raise FetchError("redirect without location", status=resp.status_code)
                    nxt = str(httpx.URL(current).join(location))
                    current = validate_url(nxt, allow_http=allow_http)
                    continue
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise FetchError(f"response larger than {self.max_bytes} bytes", status=resp.status_code)
                    chunks.append(chunk)
                content = b"".join(chunks)
                elapsed = int((time.monotonic() - start) * 1000)
                if resp.status_code >= 400:
                    raise FetchError(f"HTTP {resp.status_code} for {current}", status=resp.status_code, body=content[:500], retry_after=parse_retry_after(resp.headers.get("retry-after")))
                self.last_response = {"etag": resp.headers.get("etag"), "last_modified": resp.headers.get("last-modified"), "bytes": total, "status": resp.status_code, "url": current}
                return Response(
                    url=url,
                    final_url=current,
                    status=resp.status_code,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    content=content,
                    elapsed_ms=elapsed,
                )

    def head_final_url(self, url: str) -> tuple[str, int]:
        """Follow redirects safely and return (final_url, status) without reading bodies."""
        hops = 0
        current = validate_url(url)
        while True:
            host = urlparse(current).hostname or ""
            self._politeness(host)
            resp = self._client.head(current)
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                hops += 1
                if hops > MAX_REDIRECTS:
                    return current, resp.status_code
                current = validate_url(str(httpx.URL(current).join(resp.headers["location"])))
                continue
            return current, resp.status_code


def parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait, from a Retry-After header (delay-seconds or an HTTP date)."""
    if not value:
        return None
    v = value.strip()
    if v.isdigit():
        return float(v)
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(v)
        return max(0.0, (dt - __import__("datetime").datetime.now(dt.tzinfo)).total_seconds())
    except Exception:
        return None


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None, body: bytes | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.body = body
