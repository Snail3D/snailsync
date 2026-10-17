"""Pure timeline arithmetic for SnailSync.

No I/O in this module: every function is deterministic and unit-testable.
Everything that touches VLC or the network lives elsewhere.

The shared timeline is a media position anchored to wall time:

    position(now) = anchor + (now - at) * rate      while status == "playing"
    position(now) = anchor                          otherwise

``at`` is expressed on the *coordinator's* wall clock (for the coordinator
itself that is just ``time.time()``; clients convert their own clock with the
offset measured in clock.py).  Because every client measures the same
projection, nobody needs to know when anybody else's player actually emitted a
frame -- they only need to agree on where the movie should be *right now*.

Positioning policy
------------------
A client is allowed to be up to SEEK_DEADBAND seconds off.  Between deadband
and HARD_SEEK the difference is dissolved by nudging VLC's playback rate by a
few percent -- invisible, unlike a seek, and it also absorbs the slow clock
drift you get between two machines' audio devices over a two hour movie.
Beyond HARD_SEEK (a seek, a late join, a stalled buffer) only a real seek can
close the gap fast enough.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

PLAYING = "playing"
PAUSED = "paused"
STOPPED = "stopped"
STATUSES = (PLAYING, PAUSED, STOPPED)

# --- tuning ---------------------------------------------------------------
SEEK_DEADBAND = 0.12   # s   below this: do nothing at all
HARD_SEEK = 2.0        # s   above this: seek instead of nudging the rate
RATE_GAIN = 0.06       # fraction of the remaining drift corrected per second
RATE_LIMIT = 0.03      # |rate - 1| ceiling, i.e. 3% at most
RATE_EPS = 1e-4        # below this, snap the rate back to exactly 1.0
END_SLACK = 0.5        # s   how close to the end counts as "past the end"


def position(anchor: float, status: str, at: float, rate: float,
             now: float) -> float:
    """Where the movie should be at wall time ``now``."""
    if status != PLAYING:
        return anchor
    return anchor + max(0.0, now - at) * rate


def rate_for(drift: float) -> float:
    """Playback rate that bleeds ``drift`` off without an audible jump.

    ``drift`` is local minus target: positive means we are ahead and must slow
    down, so the correction is negative.
    """
    if abs(drift) <= SEEK_DEADBAND:
        return 1.0
    correction = -RATE_GAIN * drift
    correction = max(-RATE_LIMIT, min(RATE_LIMIT, correction))
    if abs(correction) < RATE_EPS:
        return 1.0
    return 1.0 + correction


def needs_seek(drift: float) -> bool:
    """True when only a real seek can close the gap."""
    return abs(drift) >= HARD_SEEK


def clamp_target(pos: float, length: Optional[float]) -> float:
    """Keep a seek target inside the file."""
    if pos < 0.0:
        return 0.0
    if length and length > 0.0 and pos > length - END_SLACK:
        return max(0.0, length - END_SLACK)
    return pos


@dataclass(frozen=True)
class Timeline:
    """Shared playback state.  Immutable; mutate with :func:`next_state`."""

    seq: int = 0
    item: Optional[str] = None          # URI of the current input
    name: Optional[str] = None          # display name (basename)
    status: str = STOPPED
    anchor: float = 0.0                 # media position at wall time `at`
    at: float = 0.0                     # wall time, coordinator clock
    rate: float = 1.0
    length: Optional[float] = None      # media duration, seconds
    holder: Optional[str] = None        # who moved it last (display only)

    # -- derived ----------------------------------------------------------
    def position(self, now: float) -> float:
        return position(self.anchor, self.status, self.at, self.rate, now)

    def ends_at(self, length: Optional[float] = None) -> Optional[float]:
        """Coordinator wall time when the item runs out, if it ever will."""
        if self.status != PLAYING:
            return None
        total = length if length is not None else self.length
        if not total or total <= 0.0 or self.rate <= 0.0:
            return None
        return self.at + max(0.0, total - self.anchor) / self.rate

    def drift(self, now: float, local_time: float) -> float:
        return local_time - self.position(now)

    # -- serialisation ----------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "item": self.item,
            "name": self.name,
            "status": self.status,
            "anchor": self.anchor,
            "at": self.at,
            "rate": self.rate,
            "length": self.length,
            "holder": self.holder,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Timeline":
        return cls(
            seq=int(data.get("seq", 0)),
            item=data.get("item") or None,
            name=data.get("name") or None,
            status=data.get("status") or STOPPED,
            anchor=float(data.get("anchor") or 0.0),
            at=float(data.get("at") or 0.0),
            rate=float(data.get("rate") or 1.0),
            length=(float(data["length"]) if data.get("length") is not None
                    else None),
            holder=data.get("holder") or None,
        )


def same_playback(a: Timeline, b: Timeline) -> bool:
    """Compare two timelines ignoring bookkeeping (seq/at/holder)."""
    return (a.item == b.item and a.status == b.status
            and abs(a.anchor - b.anchor) < 1e-6 and abs(a.rate - b.rate) < 1e-9)


def next_state(tl: Timeline, action: str, now: float, **kw) -> Timeline:
    """Apply a control ``action`` to ``tl`` as of wall time ``now``.

    The acting client computes the new anchor from its *own* (clock-synced)
    ``now``, so what it does to its own player and what everybody else is told
    to do are the same number.  The coordinator never does arithmetic.

    Recognised actions: open, play, pause, stop, seek.
    """
    holder = kw.get("holder") or tl.holder
    base = replace(tl, seq=tl.seq + 1, at=now, holder=holder)

    if action == "open":
        return replace(
            base,
            item=kw["item"],
            name=kw.get("name"),
            status=kw.get("status", PLAYING),
            anchor=float(kw.get("anchor", 0.0)),
            rate=1.0,
            length=kw.get("length"),
        )

    if action == "play":
        if tl.status == PLAYING:
            return base
        anchor = float(kw.get("anchor", tl.position(now)))
        return replace(base, status=PLAYING, anchor=anchor, rate=1.0)

    if action == "pause":
        if tl.status != PLAYING:
            return base
        anchor = float(kw.get("anchor", tl.position(now)))
        return replace(base, status=PAUSED, anchor=anchor, rate=1.0)

    if action == "stop":
        return replace(base, status=STOPPED, anchor=0.0, rate=1.0)

    if action == "seek":
        return replace(base, anchor=clamp_target(float(kw["pos"]), tl.length))

    raise ValueError("unknown action: %r" % (action,))

# --- local player readings ------------------------------------------------

@dataclass(frozen=True)
class Sample:
    """One reading of the local player."""

    status: str = STOPPED
    position: float = 0.0
    length: Optional[float] = None
    rate: float = 1.0
    name: Optional[str] = None


# --- correction policy ----------------------------------------------------

SEEK_COOLDOWN = 2.0     # s   minimum gap between two corrective seeks
SMOOTHING = 0.35        # fraction of each new drift reading that is believed
BIAS_TRACKING = 1 / 30  # how fast a standing offset is accepted as such


class Corrections:
    """Decide between nudging the rate and seeking outright.

    The measured drift is noisy: VLC reports the playhead quantised to 1/240 of
    the duration (a flat 0.25 s on VLC 3.0.21), so consecutive readings of a
    perfectly synced pair still swing by a quarter of a second either way.
    Reacting to that directly makes the rate flap and the audio wobble, so the
    reading is low-pass filtered first and the filter supplies the response.

    The filtered drift is then split in two, because the two halves want
    opposite treatment.  A *bias* that persists (the reporting lag, wherever
    ``start-time`` happened to land) is not something the playback rate can
    ever remove -- trying to would mean running 2% fast for the whole film,
    audibly wrong on music and pointless.  The *error* left over once the
    standing part is set aside is what the rate should act on.  Only a
    correction that no amount of rate can deliver -- a real seek, or falling
    far enough behind -- escalates to an actual seek, and that is judged on the
    total offset, bias included.
    """

    def __init__(self, clock_error: float = 0.0,
                 hard_seek: float = HARD_SEEK,
                 cooldown: float = SEEK_COOLDOWN,
                 smoothing: float = SMOOTHING) -> None:
        self.clock_error = max(0.0, clock_error)
        self.hard_seek = max(hard_seek, 2.5 * self.clock_error)
        self.cooldown = cooldown
        self.smoothing = smoothing
        self._last_seek = -1e9
        self._filtered: Optional[float] = None
        self._bias = 0.0

    def observe(self, drift: float) -> float:
        """Fold a raw reading in; return the drift minus its standing part.

        The standing part starts at zero and is learned slowly, so a real gap
        at join time is still corrected with the rate; only an offset that
        *persists* is eventually reclassified as bias and left alone.
        """
        if self._filtered is None or abs(drift - self._filtered) > self.hard_seek:
            # A jump this big is real: a seek or a stall, not noise.
            self._filtered = drift
        else:
            self._filtered += self.smoothing * (drift - self._filtered)
        self._bias += BIAS_TRACKING * (self._filtered - self._bias)
        return self._filtered - self._bias

    @property
    def standing_offset(self) -> float:
        """The part of the drift that the rate cannot remove."""
        return self._bias

    def reset(self) -> None:
        """Forget the history (after a seek the old drift means nothing)."""
        self._filtered = None
        self._bias = 0.0

    def next(self, drift: float, now: float) -> tuple:
        """Return ``(rate, seek_to_or_None)`` for this raw drift.

        ``drift`` is local minus target; positive means we are ahead.
        """
        error = self.observe(drift)
        total = error + (self._bias or 0.0)
        if (abs(total) >= self.hard_seek
                and (now - self._last_seek) >= self.cooldown):
            self._last_seek = now
            return 1.0, total
        if abs(error) <= max(SEEK_DEADBAND, self.clock_error):
            return 1.0, None
        return rate_for(error), None
