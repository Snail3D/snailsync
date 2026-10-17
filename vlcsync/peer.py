"""The reconciliation engine: keeps one local VLC on the shared timeline.

One :class:`Peer` owns exactly one VLC instance and runs one loop.  That loop is
the whole client: it reads the local player, long-polls the coordinator for the
shared state, and nudges the player towards it.  The long poll doubles as the
loop's clock -- it returns the instant the state moves, so a pause costs one
round trip rather than one poll interval -- and its timeout paces the local
sampling.

Bringing a peer into line is tried in order of increasing disturbance, and the
order matters: a change to pause state is applied before any position change,
because issuing both in the same pass lets VLC apply them out of order.

1. the right input is loaded and playing -> fix the *rate*, at most ±3%
2. it is loaded but paused/playing wrongly -> pause or resume
3. it is off by more than the seek band -> seek to the shared position
4. the wrong input is loaded, or none is -> open it, positioned, and paused if
   the group is paused, so VLC buffers in silence rather than showing a wrong
   frame first

Case 4 is why a joining guest does not stutter: ``in_play`` is issued together
with ``option=start-time`` and a forced pause, so the positioning happens
*during* loading rather than just after it.

Two things are deliberately *not* treated as user intent: the playhead moving
in ways we did not ask for (that is drift, and the correction policy owns it)
and any change while we are loading or settling.  Guessing at those is how a
sync tool ends up fighting itself.
"""

import threading
import time
import urllib.parse
from typing import Callable, Optional

from . import sync
from .protocol import Conflict, ProtocolError
from .sync import PAUSED, PLAYING, STOPPED, Corrections, Sample, Timeline
from .vlc import VLCClient, VLCError


ARM_TIMEOUT = 20.0          # s   how long an input may take to load
SETTLE = 0.6                # s   ignore local status churn after our own command
READ_RETRY = 0.35           # s   extra grace before judging a loaded input
STALE_MAX = 1.5             # s   oldest reading worth extrapolating
USER_SEEK = 1.0             # s   playhead jump that must be a person


def normalize(uri: Optional[str]) -> Optional[str]:
    """Compare URIs loosely: VLC hands back percent-encoded forms."""
    if not uri:
        return None
    return urllib.parse.unquote(uri)


class Peer:
    def __init__(self, name: str, vlc: VLCClient, coord,
                 clock, control: bool = True, seeker: bool = True,
                 poll: float = 0.25, idle_poll: float = 5.0,
                 log: Optional[Callable] = None) -> None:
        self.name = name
        self.vlc = vlc
        self.coord = coord
        self.clock = clock
        self.control = control      # may I move the movie for everybody?
        self.seeker = seeker        # may I seek my own player?
        self.poll = poll
        self.idle_poll = max(poll, idle_poll)
        self.log = log or (lambda msg: None)

        self.tl = Timeline()
        self.local_item: Optional[str] = None
        self.pending: Optional[dict] = None
        self.dead = False           # our player has no such file; stop trying
        self._loaded = False        # our VLC actually holds that input
        self.corr = Corrections(clock_error=clock.rtt / 2.0)

        self._stop = threading.Event()
        self._settle_until = 0.0
        self._saw_playing = False
        self._prev: Optional[Sample] = None
        self._prev_mono = 0.0
        self._sample: Optional[Sample] = None
        self._sample_mono = 0.0
        self._local_rate = 1.0
        self._logged_rate = False
        self._moves = 0
        self._synced = False        # has this peer acted on the shared state?

    # -- public API -------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    @property
    def moves(self) -> int:
        """How many state changes this peer has made (host uses it to decide
        whether it still has an audience)."""
        return self._moves

    def run(self) -> None:
        """Loop until :meth:`stop`.  Returns early if the coordinator dies."""
        last_log = 0.0
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self.tick()
                backoff = 1.0
            except Exception as exc:                      # noqa: BLE001
                if not self._fatal(exc):
                    raise
                now = time.monotonic()
                if now - last_log > 10.0:
                    self.log("coordinator unreachable (%s); retrying" % exc)
                    last_log = now
                self._stop.wait(min(backoff, 10.0))
                backoff = min(backoff * 2.0, 10.0)

    # -- one iteration ----------------------------------------------------
    def tick(self) -> None:
        """Read local, service the shared state, nudge the local player.

        The long poll doubles as the loop's clock: it returns the instant the
        shared state moves, so a pause costs one round trip rather than one
        poll interval.  Its timeout is what paces the local sampling, so it is
        capped just under ``STALE_MAX`` -- polling any slower would leave the
        playhead reading too old to extrapolate from.
        """
        self._read_local()
        self._watch_local()
        if self.pending:
            self._check_pending()

        timeout = self.poll if self.tl.status == PLAYING else min(
            self.idle_poll, STALE_MAX * 0.8)
        state = self.coord.wait(self.tl.seq, timeout=timeout)
        if state.seq != self.tl.seq or not self._synced:
            # The first pass applies whatever is already on the coordinator:
            # adopting a state and acting on it are not the same thing, and a
            # guest that joins mid-movie has to load the file immediately.
            self.tl = state
            self._synced = True
            self._apply()
        else:
            self._regulate()

        self._read_local()
        self._watch_local()

    def settle(self, seconds: float = None) -> None:
        """Ignore local change detection for a moment (our own command)."""
        self._settle_until = time.monotonic() + (SETTLE if seconds is None
                                                 else seconds)

    # -- local readings ---------------------------------------------------
    def _read_local(self) -> None:
        try:
            self._sample = self.vlc.sample()
            self._sample_mono = time.monotonic()
        except VLCError as exc:
            self.log("VLC is not answering (%s)" % exc)
            self._sample = None

    def _local_now(self) -> Optional[float]:
        """Local playhead extrapolated to this instant, or None if unknown.

        ``position`` is quantised (measured: 1/240 of the duration, i.e. a flat
        0.25 s on VLC 3.0.21), so a raw reading is only good to a quarter of a
        second.  Carrying it forward with the playback rate is what buys
        sub-second accuracy -- but only for as long as the reading is actually
        current.  A reading taken while playing that is older than
        ``STALE_MAX`` says nothing about where the movie is now (it may have
        been paused, seeked or ended since), so it is reported as unknown
        rather than extrapolated into a wrong number.
        """
        if not self._sample:
            return None
        elapsed = time.monotonic() - self._sample_mono
        if self._sample.status == PLAYING:
            if elapsed > STALE_MAX:
                return None
            return self._sample.position + elapsed * self._local_rate
        return self._sample.position

    def _check_pending(self) -> bool:
        """Is the input we asked for loaded yet?  False while still waiting."""
        item = self.pending["item"]
        sample = self._sample
        ready = (sample and sample.status in (PLAYING, PAUSED)
                 and normalize(self.vlc.current_uri()) == normalize(item))
        if not ready:
            if time.monotonic() - self.pending["since"] > ARM_TIMEOUT:
                self.log("gave up loading %s -- is the file there on this "
                         "machine?" % item)
                self.pending = None
                self.local_item = None
                if not self.control:
                    self.dead = True
            return False
        if time.monotonic() - self.pending["since"] < READ_RETRY:
            return False
        self.pending = None
        self.settle()
        return True

    # -- reconciliation ---------------------------------------------------
    def _apply(self) -> None:
        """Make the local player match :attr:`tl` (a fresh remote state)."""
        tl = self.tl
        now = self.clock.now()
        self.log("%s: %s" % (tl.holder or "someone",
                             self._describe(tl, now)))

        if tl.item is None or tl.status == STOPPED:
            if self.local_item is not None:
                self.vlc.stop()
                self.local_item = None
            self._loaded = False
            self.settle()
            return

        self._regulate()

    def _begin_open(self, tl: Timeline, now: float) -> None:
        """Load a new input, positioned correctly, before the first frame.

        Requesting the start position together with the input (rather than
        seeking after it loads) is what makes a fresh join land exactly on
        time: the seek happens while VLC is still buffering, so no wrong
        position is ever shown.
        """
        if self.pending is not None:
            self.log("switching to %s before the last input had loaded"
                     % (tl.name or tl.item))
        target = tl.position(now)
        self.log("opening %s at %.1fs%s"
                 % (tl.name or tl.item, target,
                    "" if tl.status == PLAYING else " (paused)"))
        self.local_item = tl.item
        self._loaded = True
        self.pending = {"item": tl.item, "since": time.monotonic()}
        self.settle()
        self._saw_playing = False
        self.vlc.open(tl.item, start=target, paused=(tl.status != PLAYING))

        self._regulate()

    def _regulate(self) -> None:
        """Bring the local player into line, disturbing it as little as
        possible.

        Order matters: a wrong input, then a wrong pause state, then position.
        Doing position first would seek a player that is about to be told to
        open a different file, and would fight the pause the other side asked
        for.
        """
        if self.dead or self.pending or self._sample is None:
            return
        tl = self.tl
        if tl.item is None or tl.status == STOPPED:
            return
        if time.monotonic() < self._settle_until:
            return

        # 1. Have we run out?  VLC drops the input at the end of a file, and
        #    the shared position is then past the end.  Reopening there would
        #    spin forever, so a finished movie simply stays finished until
        #    somebody moves it.
        if (tl.length and tl.status == PLAYING
                and tl.position(self.clock.now()) >= tl.length - 0.5):
            return

        # 2. Are we even playing the right thing?  The truth is taken from the
        #    player itself: when VLC drops an input (which is what the end of a
        #    file looks like from outside) it reports no length at all, so our
        #    own bookkeeping cannot be trusted here.
        if not self._player_has_input() or normalize(
                tl.item) != normalize(self.local_item):
            self._begin_open(tl, self.clock.now())
            return

        # 3. Pause state, before position: seeking and pause/resume in the
        #    same pass makes VLC apply them out of order.
        if tl.status == PAUSED and self._sample.status == PLAYING:
            self.vlc.pause()
            self.settle()
            return
        if tl.status == PLAYING and self._sample.status != PLAYING:
            self.vlc.resume()
            self.settle()
            return

        local = self._local_now()
        if local is None:
            return

        if tl.status == PAUSED:
            # Paused, nothing is moving, so land within a quarter second.  That
            # matters more than it looks: VLC can only seek to whole seconds,
            # so two peers left half a second apart can round to *different*
            # seconds and sit there looking a full second apart.
            if abs(local - tl.anchor) > 0.25 and self.seeker:
                self._seek_to(tl.anchor)
            self._set_rate(1.0)
            return

        target = tl.position(self.clock.now())
        if target >= (tl.length or float("inf")):
            return                      # past the end; wait for the group
        drift = local - target
        if not self._saw_playing:
            # The first pass after loading also absorbs the error that
            # ``start-time`` rounding left behind, without waiting for drift
            # to grow past the seek band.
            self._saw_playing = True
            if abs(drift) > 1.0 and self.seeker:
                self.log("settling onto %.1fs (off by %+.2fs)"
                         % (target, drift))
                self._seek_to(target)
                return
        rate, seek_to = self.corr.next(drift, time.monotonic())
        if seek_to is not None and self.seeker:
            self.log("off by %.2fs -> seeking to %.1fs" % (drift, target))
            self._seek_to(target)
            return
        self._set_rate(rate, drift)

    def _seek_to(self, target: float) -> None:
        """Seek, and forget the drift history -- it described the old place."""
        self.vlc.seek(target)
        self.corr.reset()
        self.settle()

    def _player_has_input(self) -> bool:
        """Does the local VLC actually hold a loaded input right now?

        When VLC drops an input (at the end of a file, or when a file vanishes)
        it reports no length, which is the only reliable signal that our own
        bookkeeping has gone stale.
        """
        sample = self._sample
        return bool(sample and sample.status in (PLAYING, PAUSED)
                    and sample.length)

    def _set_rate(self, rate: float, drift: float = 0.0) -> None:
        if abs(rate - self._local_rate) < 1e-4:
            return
        try:
            self.vlc.set_rate(rate)
        except VLCError:
            return
        self._local_rate = rate
        # Only worth a line when it crosses the "really correcting" threshold,
        # otherwise a sync that is merely breathing floods the console.
        if abs(rate - 1.0) > 0.02 or (rate == 1.0 and self._logged_rate):
            self.log("drift %+.2fs -> rate %.3f" % (drift, rate))
        self._logged_rate = abs(rate - 1.0) > 0.02

    # -- our own changes --------------------------------------------------
    def _watch_local(self) -> None:
        """Detect a change made in the VLC window and publish it.

        Only deliberate things are published: pausing, resuming, moving the
        playhead, or starting a different file.  Everything else -- our own
        commands settling, a load in progress, the rate nudging the playhead --
        is noise, and guessing at noise is how a sync tool starts fighting
        itself.
        """
        sample, mine = self._sample, time.monotonic()
        if sample is None:
            return
        prev = self._prev
        self._prev, self._prev_mono = sample, mine
        if prev is None or mine < self._settle_until or self.pending:
            return                      # our own command, or still loading
        if mine - self._sample_mono > 1.0:
            return                      # stale reading; do not judge it

        action = self._local_action(prev, sample)
        if action is None or not self.control:
            return
        name, args = action
        if name == "open":
            uri = self.vlc.current_uri()
            if not uri:
                return
            args = {"item": uri, "anchor": sample.position,
                    "name": sample.name, "length": sample.length,
                    "status": (sample.status if sample.status in
                               (PLAYING, PAUSED) else PLAYING)}
        elif name in ("pause", "play") and self._local_drift() < 1.0:
            # Anchor the group where *this* person is looking.  Only when we
            # are roughly in sync: a peer that is far out (file still loading,
            # weak machine) may still pause everybody, but it must not drag the
            # group back to its own stale position.
            args = dict(args, anchor=sample.position)
        probe = sync.next_state(self.tl, name, self.clock.now(), **args)
        if sync.same_playback(probe, self.tl):
            return                      # would not change the shared truth
        self.log("local %s -> publishing" % name)
        self.publish(name, **args)

    def _local_drift(self) -> float:
        local = self._local_now()
        if local is None or self.tl.item is None:
            return 0.0
        return abs(local - self.tl.position(self.clock.now()))

    def _local_action(self, prev: Sample, now: Sample):
        """What did the user do in the VLC window between two readings?

        Three things count as user intent: pausing, resuming, and moving the
        playhead.  Jumpiness while an input settles is not intent, so a missing
        length is never judged, and a move is only believed when it is far
        bigger than anything the sync engine itself does -- the rate correction
        shifts the playhead by tens of milliseconds between readings, so
        anything under a second is drift, not a click on the timeline.
        """
        if not now.length:
            return None
        if (now.name and prev.name and now.name != prev.name
                and now.status != STOPPED):
            return ("open", {})
        if now.status == PAUSED and prev.status == PLAYING:
            return ("pause", {})
        if now.status == PLAYING and prev.status == PAUSED:
            return ("play", {})
        if (now.status == PLAYING and prev.status == PLAYING
                and abs(now.position - prev.position) > USER_SEEK):
            return ("seek", {"pos": now.position})
        return None

    def publish(self, action: str, **fields) -> Optional[Timeline]:
        """Share a change with everybody, and make it true locally.

        The client computes the new timeline itself, from its own clock-synced
        ``now``, so what it does to its own player and what it broadcasts are
        the same numbers -- the coordinator only decides who wins a race.

        Nothing touches the local player until the coordinator has accepted
        the change.  A rejected change (somebody else moved first) is simply
        recomputed on top of the winner's state, so the player never follows a
        state that the group does not share.
        """
        for _ in range(4):
            base = self.tl
            new = sync.next_state(base, action, self.clock.now(),
                                  holder=self.name, **fields)
            try:
                self.tl = self.coord.apply_state(base.seq, new)
            except Conflict as conflict:
                self.tl = conflict.state           # somebody else won; redo
                continue
            except ProtocolError as exc:
                self.log("could not share that (%s)" % exc)
                return None
            self._moves += 1
            self._act_local(action, self.tl)
            return self.tl
        self.log("gave up sharing that change (too much contention)")
        return None

    def _act_local(self, action: str, tl: Timeline) -> None:
        """Perform an accepted change on our own player."""
        self.settle()
        self._saw_playing = False
        if action == "open":
            self.local_item = tl.item
            self._loaded = True
            self.pending = {"item": tl.item, "since": time.monotonic()}
            self._settle_until = time.monotonic() + ARM_TIMEOUT
            self.vlc.open(tl.item, start=tl.anchor,
                          paused=(tl.status != PLAYING))
        elif action == "stop":
            self.vlc.stop()
            self.local_item = None
            self._loaded = False
            self.pending = None
        elif action == "pause":
            self.vlc.pause()
        elif action == "play":
            self.vlc.resume()
        elif action == "seek":
            self._local_rate = 1.0
            self.vlc.set_rate(1.0)
            self.vlc.seek(tl.anchor)

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _describe(tl: Timeline, now: float) -> str:
        verb = {"playing": "playing", "paused": "paused",
                "stopped": "stopped"}.get(tl.status, tl.status)
        if tl.item is None:
            return "nothing loaded"
        return "%s %s at %.1fs" % (verb, tl.name or tl.item, tl.position(now))

    def _fatal(self, exc: Exception) -> bool:
        from .protocol import ProtocolError
        return isinstance(exc, (ProtocolError, VLCError, OSError))