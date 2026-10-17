"""Client for the SnailSync coordinator.

The coordinator is a tiny shared-state service.  A peer that wants to move the
movie computes the *whole* new timeline itself -- from its own clock-synced
``now`` -- and posts it with the sequence number it believed it was replacing.
The coordinator either accepts it (and bumps the sequence) or rejects it with
the state that won, in which case the caller just redoes the work on top of
that.  Nobody has to reason about two people pausing at the same instant.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .sync import Timeline

DEFAULT_PORT = 8790


class ProtocolError(RuntimeError):
    pass


class Conflict(Exception):
    """Someone else moved the state first; carries the winning timeline."""

    def __init__(self, state: Timeline) -> None:
        super().__init__("state moved on")
        self.state = state


def base_url(target: str, port: int = DEFAULT_PORT) -> str:
    """Normalise ``host``, ``host:port``, ``host/path`` or a full URL."""
    target = target.strip().rstrip("/")
    if "://" not in target:
        target = "http://" + target
    parsed = urllib.parse.urlsplit(target)
    if parsed.port is None:
        parsed = parsed._replace(netloc="%s:%d" % (parsed.hostname, port))
    return urllib.parse.urlunsplit(parsed)


class Coordinator:
    """HTTP client for a running coordinator."""

    def __init__(self, url: str, token: str = "",
                 timeout: float = 40.0) -> None:
        self.url = base_url(url)
        self.token = token
        self.timeout = timeout

    # -- plumbing ---------------------------------------------------------
    def _request(self, path: str, params: Optional[dict] = None,
                 body: Optional[dict] = None, timeout: Optional[float] = None):
        url = self.url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"X-SnailSync-Token": self.token}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url, data=data, headers=headers,
            method="POST" if data is not None else "GET")
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            if exc.code == 409:
                try:
                    parsed = json.loads(detail)
                    raise Conflict(
                        Timeline.from_dict(parsed["timeline"])) from exc
                except (ValueError, KeyError):
                    pass
            if exc.code == 401:
                raise ProtocolError(
                    "the coordinator rejected our token -- everyone needs the "
                    "same --token value") from exc
            raise ProtocolError("coordinator said %s %s: %s"
                                % (exc.code, exc.reason, detail)) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ProtocolError("cannot reach the coordinator at %s (%s)"
                                % (self.url, exc)) from exc

    def _json(self, path, params=None, body=None, timeout=None) -> dict:
        with self._request(path, params, body, timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # -- API --------------------------------------------------------------
    def healthy(self) -> bool:
        try:
            self._json("/api/health", timeout=4.0)
            return True
        except ProtocolError:
            return False

    def clock(self) -> float:
        with self._request("/api/time", timeout=4.0) as resp:
            return float(resp.read().decode("utf-8").strip())

    def state(self) -> Timeline:
        return Timeline.from_dict(self._json("/api/state", timeout=6.0))

    def apply_state(self, base_seq: int, new: Timeline) -> Timeline:
        """Compare-and-swap the shared timeline.  Raises :class:`Conflict`."""
        doc = self._json("/api/state",
                         body={"base_seq": base_seq,
                               "timeline": new.to_dict()},
                         timeout=8.0)
        return Timeline.from_dict(doc)

    def wait(self, seq: int, timeout: float = 15.0) -> Timeline:
        """Long-poll until the state moves past ``seq``.

        Returns whatever the state is when the poll ends, so a quiet timeout is
        indistinguishable from an update as far as the caller is concerned.
        """
        doc = self._json("/api/events",
                         params={"seq": str(seq), "timeout": str(timeout)},
                         timeout=timeout + 10.0)
        return Timeline.from_dict(doc)

    def hello(self, name: str) -> dict:
        return self._json("/api/hello", params={"name": name}, timeout=6.0)

    def peers(self) -> list:
        return self._json("/api/peers", timeout=6.0)