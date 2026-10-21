"""A stand-in for the claude.ai artifact runtime, so the real dashboard page can be driven in a real browser
without a claude.ai login. One small HTTP server holds the page's documents and leases; a script injected
before the page runs defines `window.claude.use("db")` and `use("user")` with the calls the page makes
(`doc().get/set/acquire/onSnapshot`, `collection().limit().onSnapshot`, `user.isOwner`).

This is a simulation of the runtime's shape, not the runtime: a test that passes here proves the page's
own JavaScript (editing, saving, conflicts, retries), never the claude.ai service or its ownership rules."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "jobhunter" / "board" / "hunter-board.html"

SHIM = r"""
(() => {
  const base = "";  // relative: location.origin is still "null" when an init script runs
  const call = async (path, body) => {
    const r = await fetch(base + path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body || {}) });
    const out = await r.json();
    if (!r.ok) { const e = new Error(out.message || out.code || "error"); e.code = out.code || "unavailable"; throw e; }
    return out;
  };
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const docSnap = (d) => ({ exists: !!d, data: () => (d ? clone(d) : undefined) });
  const watchers = [];
  let seq = -1, lastDocs = null;
  const fire = (w, docs) => { try { w(docs); } catch (e) { console.error("fake artifact watcher", e); } };
  async function poll() {
    try {
      const r = await fetch(base + "/__state");
      const s = await r.json();
      if (s.seq !== seq) { seq = s.seq; lastDocs = s.docs; for (const w of watchers) fire(w, s.docs); }
    } catch (e) { console.debug("fake artifact poll failed: " + (e && e.message)); }
    setTimeout(poll, 100);
  }
  poll();
  // Like the runtime: a new subscription gets the current state at once, then every change.
  const subscribe = (w) => { watchers.push(w); if (lastDocs) Promise.resolve().then(() => fire(w, lastDocs)); };
  const db = {
    doc: (path) => ({
      get: async () => docSnap((await call("/__get", { path })).data),
      set: async (data) => { await call("/__set", { path, data }); },
      acquire: async (opts) => call("/__acquire", { path, holder: opts && opts.holder, ttlMs: opts && opts.ttlMs }),
      onSnapshot: (cb, err) => { subscribe((docs) => cb(docSnap(docs[path] || null))); },
    }),
    collection: (name) => ({
      limit: () => ({
        onSnapshot: (cb, err) => {
          subscribe((docs) => {
            const items = Object.entries(docs).filter(([p]) => p.startsWith(name + "/")).map(([p, d]) => ({ id: p.slice(name.length + 1), exists: true, data: () => clone(d) }));
            cb({ docs: items });
          });
        },
      }),
    }),
  };
  const user = { isOwner: async () => true };
  window.claude = { use: async (name) => (name === "db" ? db : name === "user" ? user : null) };
  window.__fakeArtifact = true;
})();
"""


def _iso_ms(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1000):03d}Z"


class FakeArtifact:
    """Documents, leases and failure switches, served over loopback."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.leases: dict[str, tuple[str, float]] = {}
        self.seq = 0
        self.fail_set = False
        self.delay_set_ms = 0
        self.calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self.port = 0

    # ---- test-side helpers
    def load(self, docs: dict[str, dict]) -> None:
        with self.lock:
            self.docs.update({k: json.loads(json.dumps(v)) for k, v in docs.items()})
            self.seq += 1

    def load_export(self, manifest: dict) -> None:
        docs = {}
        for w in manifest["writes"]:
            docs[f"{w['collection']}/{w['doc_id']}"] = json.loads(Path(w["file"]).read_text(encoding="utf-8"))
        self.load(docs)

    def doc(self, path: str) -> dict | None:
        with self.lock:
            d = self.docs.get(path)
            return json.loads(json.dumps(d)) if d is not None else None

    def write_actions_dir(self, folder: Path) -> int:
        """What `ArtifactData list collection=actions out_dir=...` leaves on disk: one bare document per file."""
        (folder / "actions").mkdir(parents=True, exist_ok=True)
        n = 0
        with self.lock:
            for path, d in self.docs.items():
                if path.startswith("actions/"):
                    (folder / "actions" / f"{path.split('/', 1)[1]}.json").write_text(json.dumps(d), encoding="utf-8")
                    n += 1
        return n

    # ---- server
    def start(self) -> str:
        store = self
        page_html = PAGE.read_text(encoding="utf-8")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _json(self, code: int, body: dict) -> None:
                data = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/__state":
                    with store.lock:
                        self._json(200, {"seq": store.seq, "docs": store.docs})
                    return
                data = page_html.encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                path = str(body.get("path") or "")
                store.calls.append((self.path, path))
                if self.path == "/__get":
                    with store.lock:
                        self._json(200, {"data": store.docs.get(path)})
                elif self.path == "/__set":
                    if store.delay_set_ms:
                        time.sleep(store.delay_set_ms / 1000)
                    if store.fail_set:
                        self._json(503, {"code": "unavailable", "message": "simulated outage"})
                        return
                    if not isinstance(body.get("data"), dict) or len(json.dumps(body["data"])) > 250 * 1024:
                        self._json(400, {"code": "invalid_argument"})
                        return
                    with store.lock:
                        store.docs[path] = body["data"]
                        store.seq += 1
                    self._json(200, {"ok": True})
                elif self.path == "/__acquire":
                    with store.lock:
                        if path not in store.docs:
                            self._json(400, {"code": "invalid_argument", "message": "no such document"})
                            return
                        holder, exp = store.leases.get(path, ("", 0.0))
                        now = time.time()
                        if holder and holder != body.get("holder") and exp > now:
                            self._json(200, {"acquired": False, "expiresAt": _iso_ms(exp)})
                            return
                        ttl = float(body.get("ttlMs") or 2000) / 1000
                        store.leases[path] = (str(body.get("holder") or ""), now + ttl)
                        self._json(200, {"acquired": True, "expiresAt": _iso_ms(now + ttl)})
                elif self.path == "/__control":
                    store.fail_set = bool(body.get("fail_set", store.fail_set))
                    store.delay_set_ms = int(body.get("delay_set_ms", store.delay_set_ms))
                    self._json(200, {"ok": True})
                else:
                    self._json(404, {"code": "not_found"})

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
