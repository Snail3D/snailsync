"""The coordinator: a tiny shared-state service for one movie night.

It holds exactly one :class:`~vlcsync.sync.Timeline` and a condition variable.
Peers publish a new timeline with compare-and-swap on the sequence number, so
two people pausing at the same instant cannot interleave into a corrupt state
-- one of them gets a conflict and simply reapplies the winner's state.

Clients block on ``/api/events`` (long poll), so a pause reaches everybody in
one round trip instead of one polling interval.  A peer that goes away cannot
disturb the others: nobody talks to anybody except this process.

There is deliberately no policy here.  The client that acts computes the new
timeline (it has the clock offset, so it can), and this service only decides
*who gets to be the successor*.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .sync import Timeline

MAX_BODY = 64 * 1024
MAX_EVENT_TIMEOUT = 60.0


class Conflict(Exception):
    def __init__(self, state: Timeline) -> None:
        super().__init__("state moved on")
        self.state = state


class State:
    """The shared timeline, guarded by a condition variable."""

    def __init__(self, token: str = "") -> None:
        self.token = token
        self.tl = Timeline()
        self.cond = threading.Condition()
        self.peers: dict = {}
        self.started = time.time()

    def snapshot(self) -> Timeline:
        with self.cond:
            return self.tl

    def publish(self, base_seq: int, new: Timeline) -> Timeline:
        with self.cond:
            if base_seq != self.tl.seq:
                raise Conflict(self.tl)
            self.tl = Timeline.from_dict(dict(new.to_dict(), seq=base_seq + 1))
            self.cond.notify_all()
            return self.tl

    def wait(self, seq: int, timeout: float) -> Timeline:
        timeout = max(0.0, min(timeout, MAX_EVENT_TIMEOUT))
        with self.cond:
            if self.tl.seq == seq:
                self.cond.wait(timeout)
            return self.tl

    def hello(self, name: str) -> None:
        with self.cond:
            entry = self.peers.setdefault(name, {})
            entry["first_seen"] = entry.get("first_seen", time.time())
            entry["last_seen"] = time.time()

    def roster(self) -> list:
        now = time.time()
        with self.cond:
            stale = [n for n, e in self.peers.items()
                     if now - e["last_seen"] > 40.0]
            for name in stale:
                self.peers.pop(name, None)
            return [{"name": n, "first_seen": e["first_seen"],
                     "last_seen": e["last_seen"]}
                    for n, e in sorted(self.peers.items())]

    def audience(self) -> int:
        """Peers other than the host who are still around."""
        now = time.time()
        with self.cond:
            return sum(1 for e in self.peers.values()
                       if now - e["last_seen"] <= 40.0)


class Handler(BaseHTTPRequestHandler):
    server_version = "SnailSync"
    protocol_version = "HTTP/1.1"
    state: State = None                      # set by Coordinator

    # -- helpers ----------------------------------------------------------
    def log_message(self, fmt, *args):       # keep the console quiet
        pass

    def _send(self, code: int, payload, ctype: str = "application/json") -> None:
        if not isinstance(payload, (bytes, bytearray)):
            payload = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authorised(self) -> bool:
        token = self.state.token
        if not token:
            return True
        given = self.headers.get("X-SnailSync-Token", "")
        return given == token

    def _query(self) -> dict:
        parsed = urllib.parse.urlsplit(self.path)
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(parsed.query).items()}

    def _route(self) -> str:
        return urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"

    def _read_body(self) -> Optional[dict]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return None

    # -- verbs ------------------------------------------------------------
    def do_GET(self) -> None:                 # noqa: N802
        route = self._route()
        if route in ("/", "/api/health"):
            return self._send(200, {"ok": True, "service": "snailsync",
                                    "uptime": round(time.time()
                                                    - self.state.started, 1)})
        if not self._authorised():
            return self._send(401, {"error": "bad or missing token"})
        if route == "/api/time":
            return self._send(200, ("%.6f" % time.time()).encode("utf-8"),
                              "text/plain")
        if route == "/api/state":
            return self._send(200, self.state.snapshot().to_dict())
        if route == "/api/events":
            q = self._query()
            try:
                seq = int(q.get("seq", 0))
            except ValueError:
                seq = 0
            try:
                timeout = float(q.get("timeout", 15.0))
            except ValueError:
                timeout = 15.0
            return self._send(200, self.state.wait(seq, timeout).to_dict())
        if route == "/api/hello":
            name = (self._query().get("name") or "anonymous")[:32]
            self.state.hello(name)
            return self._send(200, {"ok": True, "name": name,
                                    "peers": self.state.roster()})
        if route == "/api/peers":
            return self._send(200, self.state.roster())
        return self._send(404, {"error": "no such endpoint"})

    def do_POST(self) -> None:                # noqa: N802
        route = self._route()
        if not self._authorised():
            return self._send(401, {"error": "bad or missing token"})
        if route != "/api/state":
            return self._send(404, {"error": "no such endpoint"})
        body = self._read_body()
        if not body or "timeline" not in body:
            return self._send(400, {"error": "expected {base_seq, timeline}"})
        try:
            base_seq = int(body.get("base_seq", -1))
        except (TypeError, ValueError):
            return self._send(400, {"error": "base_seq must be an integer"})
        try:
            new = Timeline.from_dict(body["timeline"])
        except (TypeError, ValueError) as exc:
            return self._send(400, {"error": "bad timeline: %s" % exc})
        try:
            return self._send(200, self.state.publish(base_seq, new).to_dict())
        except Conflict as conflict:
            return self._send(409, {"error": "conflict",
                                    "timeline": conflict.state.to_dict()})


class Coordinator:
    """Embeddable coordinator server."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8790,
                 token: str = "") -> None:
        self.state = State(token=token)
        handler = type("BoundHandler", (Handler,), {"state": self.state})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "Coordinator":
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        kwargs={"poll_interval": 0.2},
                                        name="coordinator", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=3.0)