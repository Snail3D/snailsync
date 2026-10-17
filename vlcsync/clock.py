"""Wall-clock alignment against the coordinator (NTP-lite).

Position error equals clock error, so a client whose clock is a second off is
a second off in the movie.  Two exchange round trips give a decent offset:

    t1 --request--> server stamps t2 --response--> client stamps t3

    offset = ((t2 - t1) + (t2 - t3)) / 2        (positive: server is ahead)

Both directions are used because the asymmetry of the paths is unknown; the
error is bounded by half the round-trip time.  A handful of samples are taken
and the offset from the lowest-RTT sample is kept -- the sample with the least
queueing is the least biased estimate, and on a home network that is usually
well under 10 ms.

This is deliberately not NTP: no drift discipline, no poll schedule.  The
movie is a couple of hours long and the peers re-derive their position from the
shared anchor every tick, so a fixed offset measured at join time is enough.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SAMPLES = 5


class ClockError(RuntimeError):
    pass


class Clock:
    """A clock whose ``now()`` approximates the coordinator's wall clock."""

    def __init__(self, offset: float = 0.0, rtt: float = 0.0,
                 token: str = "") -> None:
        self.offset = offset
        self.rtt = rtt
        self.token = token
        self._lock = threading.Lock()

    def now(self) -> float:
        return time.time() + self.offset

    def update(self, offset: float, rtt: float) -> None:
        with self._lock:
            self.offset, self.rtt = offset, rtt

    def _stamp(self, url: str, timeout: float) -> float:
        req = urllib.request.Request(
            url, headers={"X-SnailSync-Token": self.token})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return float(resp.read().decode("utf-8").strip())

    def sample(self, url: str, timeout: float = 3.0) -> float:
        """Take one round trip and fold it in.  Returns the new offset."""
        t1 = time.time()
        t2 = self._stamp(url, timeout)
        t3 = time.time()
        rtt = t3 - t1
        offset = ((t2 - t1) + (t2 - t3)) / 2.0
        with self._lock:
            if self.rtt > 0.0 and rtt > self.rtt:
                return self.offset          # keep the better sample
            self.offset, self.rtt = offset, rtt
        return offset

    def sync(self, url: str, samples: int = SAMPLES,
             timeout: float = 3.0) -> float:
        """Take several samples and keep the least-delayed one."""
        best = None
        for _ in range(max(1, samples)):
            try:
                t1 = time.time()
                t2 = self._stamp(url, timeout)
                t3 = time.time()
            except (urllib.error.URLError, OSError, ValueError):
                continue
            rtt = t3 - t1
            offset = ((t2 - t1) + (t2 - t3)) / 2.0
            if best is None or rtt < best[1]:
                best = (offset, rtt)
        if best is None:
            raise ClockError("clock sync failed: no usable reply from %s "
                             "(check the token)" % url)
        with self._lock:
            self.offset, self.rtt = best
        return self.offset

    def __repr__(self) -> str:
        return "Clock(offset=%+.4fs rtt=%.1fms)" % (self.offset, self.rtt * 1e3)