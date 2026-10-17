"""Thin client for VLC's HTTP (Lua) interface, plus a launcher.

Everything here is deliberately small: VLC is the decoder, we only ever read
its status and issue the handful of commands verified to work on VLC 3.0.21
(`pl_forcepause`, `pl_forceresume`, `in_play`, `seek`, `rate`, `pl_empty`).

Verified quirks this module encodes:

* The HTTP interface **refuses to start unless a password is set**, and then
  every request needs HTTP basic auth (any username, the password as password).
* Every command answers ``200 OK`` even when it does nothing (a seek past the
  end, an ``in_play`` of a missing file).  Success has to be judged from the
  status body that comes back, never from the HTTP code.
* ``in_play`` wants a percent-encoded URI.  A bare filesystem path, or a URI
  with raw spaces, silently fails; ``file:///a%20b.mkv`` works.
* **``seek`` takes whole seconds, and silently mangles a decimal.**  The
  documented grammar is ``[+|-][H:][M:][<int>S]``; ``val=104.300`` is read as
  300 s, ``val=104.5s`` as 5 s.  There is no sub-second form at all, so seek
  positions must be rounded (the grading effect is invisible, while a mangled
  value teleports the movie).  A fractional start position *is* available, but
  only at load time, via ``in_play&option=start-time=<float>``.
* ``status.json``'s ``time`` is whole seconds, but ``position * length`` gives
  1/240-of-the-duration resolution -- measured as a flat 0.25 s on VLC 3.0.21,
  which is what the sync engine uses to find a sub-second position.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional, Sequence

from .sync import PLAYING, PAUSED, STOPPED, Sample

DEFAULT_PORT = 8080
DEFAULT_PASSWORD = "vlcsync"
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")

VLC_CANDIDATES = (
    "/Applications/VLC.app/Contents/MacOS/VLC",
    "/usr/bin/vlc",
    "/usr/local/bin/vlc",
    "/snap/bin/vlc",
)


class VLCError(RuntimeError):
    pass


def find_vlc() -> str:
    """Locate a VLC executable, preferring the macOS bundle."""
    for candidate in VLC_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("vlc")
    if found:
        return found
    raise VLCError(
        "VLC not found. Install it, or pass --vlc /path/to/vlc "
        "(looked in: %s)" % ", ".join(VLC_CANDIDATES)
    )


def to_uri(target: str) -> str:
    """Turn a local path or a URL into something ``in_play`` accepts.

    Local paths are made absolute and percent-encoded (spaces and non-ASCII
    must be encoded -- VLC rejects raw ones).  Anything already carrying a
    scheme is passed through untouched so remote URLs keep working.
    """
    if _SCHEME.match(target):
        return target
    return "file://" + urllib.parse.quote(os.path.abspath(target), safe="/")


def parse_status(status: dict) -> Sample:
    """Read a VLC status document into a :class:`Sample`.

    ``position * length`` is used when available because it is ~250x finer
    than ``time``; streams with no known length fall back to whole seconds.
    """
    length = status.get("length") or 0
    length = float(length)
    if length > 0:
        pos = float(status.get("position", 0.0)) * length
    else:
        pos = float(status.get("time", 0) or 0)
        length = 0.0
    meta = (status.get("information", {}).get("category", {}).get("meta", {})
            or {})
    return Sample(
        status=status.get("state", STOPPED),
        position=pos,
        length=length if length > 0 else None,
        rate=float(status.get("rate", 1.0) or 1.0),
        name=meta.get("filename"),
    )


def launch(targets: Sequence[str] = (), port: int = DEFAULT_PORT,
           password: str = DEFAULT_PASSWORD, exe: Optional[str] = None,
           extra: Sequence[str] = (), log: Optional[str] = None) -> subprocess.Popen:
    """Start a VLC instance with its HTTP interface enabled.

    The GUI is kept (no ``-I dummy``) so the user still gets a normal VLC
    window and can drive it with the keyboard as usual; ``--extraintf http``
    adds our control channel on top.
    """
    exe = exe or find_vlc()
    args: List[str] = [
        exe,
        "--extraintf", "http",
        "--http-host", "127.0.0.1",
        "--http-port", str(port),
        "--http-password", password,
        "--no-video-title-show",
    ]
    args.extend(extra)
    args.extend(targets)
    if log:
        handle = open(log, "ab")
    else:
        handle = subprocess.DEVNULL
    return subprocess.Popen(args, stdout=handle, stderr=handle,
                            stdin=subprocess.DEVNULL)


class VLCClient:
    """Control one VLC instance over its HTTP interface."""

    def __init__(self, port: int = DEFAULT_PORT, host: str = "127.0.0.1",
                 password: str = DEFAULT_PASSWORD, timeout: float = 2.0) -> None:
        self.host = host
        self.port = port
        self.password = password or ""
        self.timeout = timeout
        token = base64.b64encode(
            (":" + self.password).encode("utf-8")).decode("ascii")
        self._auth = "Basic " + token
        self._base = "http://%s:%d/requests" % (host, port)

    # -- plumbing ---------------------------------------------------------
    def _get(self, path: str, params: Optional[dict] = None) -> bytes:
        url = self._base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": self._auth})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise VLCError(
                    "VLC on port %d rejected our password (%s). Is another VLC "
                    "instance already using that port?"
                    % (self.port, "empty password" if not self.password
                       else "wrong password")) from exc
            raise VLCError("VLC HTTP %s: %s" % (exc.code, exc.reason)) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise VLCError(
                "cannot reach VLC on %s:%d -- is it running with the HTTP "
                "interface on that port? (%s)" % (self.host, self.port, exc))

    def _json(self, path: str, params: Optional[dict] = None) -> dict:
        raw = self._get(path, params)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise VLCError("VLC sent a non-JSON reply for %s: %r"
                           % (path, raw[:120])) from exc

    def command(self, **params) -> dict:
        """Issue a command and return the resulting status document."""
        return self._json("/status.json", params)

    # -- state ------------------------------------------------------------
    def alive(self) -> bool:
        try:
            self._get("/status.json")
            return True
        except VLCError:
            return False

    def status(self) -> dict:
        return self._json("/status.json")

    def sample(self) -> Sample:
        return parse_status(self.status())

    def current_uri(self) -> Optional[str]:
        """URI of the item VLC is currently playing, if any.

        Only needed when a *local* change has to be published, so the cost of
        walking the playlist is paid rarely.
        """
        try:
            doc = self._json("/playlist.json")
        except VLCError:
            return None
        found: List[str] = []

        def walk(node: dict) -> None:
            if node.get("current") == "current" and node.get("uri"):
                found.append(node["uri"])
            for child in node.get("children") or ():
                walk(child)

        walk(doc)
        return found[0] if found else None

    # -- control ----------------------------------------------------------
    def open(self, uri: str, start: Optional[float] = None,
             paused: bool = False) -> None:
        """Replace the current input, optionally positioned before its first
        frame.  VLC ignores a bad input silently.

        ``start-time`` accepts a fraction, unlike ``seek``, and takes effect
        while the input loads -- so a peer joining mid-film lands on the right
        position without a visible jump.
        """
        if start is not None and start > 0.5:
            self.command(command="in_play", input=uri,
                         option="start-time=%.3f" % start)
        else:
            self.command(command="in_play", input=uri)
        if paused:
            self.command(command="pl_forcepause")

    def pause(self) -> None:
        self.command(command="pl_forcepause")

    def resume(self) -> None:
        self.command(command="pl_forceresume")

    def stop(self) -> None:
        self.command(command="pl_empty")

    def seek(self, position: float) -> None:
        """Seek to an absolute position.

        Rounded to whole seconds on purpose.  VLC's ``seek`` grammar has no
        fractional form and *mangles* one (``104.300`` is read as 300 s), so
        sending a fraction teleports the movie.  The leftover half second is
        below the seek deadband and is absorbed by the rate correction.
        """
        self.command(command="seek", val=str(int(max(0.0, position) + 0.5)))

    def set_rate(self, rate: float) -> None:
        self.command(command="rate", val=("%.4f" % rate))

    def set_volume(self, volume: int) -> None:
        self.command(command="volume", val=str(int(volume)))

    def wait_for_input(self, timeout: float = 15.0,
                       interval: float = 0.2) -> Sample:
        """Block until VLC has actually got an input loaded.

        Freshly opened inputs sit at ``stopped`` with ``length 0`` for a
        moment; seeking or pausing before that is silently dropped, so every
        open path waits here first.
        """
        deadline = time.time() + timeout
        last = Sample(status=STOPPED, position=0.0, length=None, rate=1.0)
        while time.time() < deadline:
            last = self.sample()
            if last.status in (PLAYING, PAUSED) and (last.length or 0) > 0:
                return last
            time.sleep(interval)
        return last