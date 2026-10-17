"""Tests for the parts that decide what a peer does.

These run without VLC and without a network: the pure timeline arithmetic, the
correction policy, and the peer's reconciliation decisions against a fake
player.  The end-to-end behaviour against real VLC instances is checked by the
smoke script (``tools/smoke.py``), not here.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vlcsync import sync                                   # noqa: E402
from vlcsync.sync import (PAUSED, PLAYING, STOPPED, Corrections,  # noqa: E402
                          Sample, Timeline)


# --- timeline arithmetic --------------------------------------------------

def test_position_advances_only_while_playing():
    tl = Timeline(status=PLAYING, anchor=10.0, at=1000.0, rate=1.0)
    assert tl.position(1005.0) == pytest.approx(15.0)
    paused = sync.next_state(tl, "pause", 1005.0)
    assert paused.status == PAUSED
    assert paused.anchor == pytest.approx(15.0)
    assert paused.position(9999.0) == pytest.approx(15.0)


def test_position_never_runs_backwards_when_the_clock_jitters():
    tl = Timeline(status=PLAYING, anchor=10.0, at=1000.0, rate=1.0)
    assert tl.position(999.0) == 10.0


def test_pause_then_resume_keeps_the_place():
    tl = Timeline(status=PLAYING, anchor=0.0, at=0.0)
    paused = sync.next_state(tl, "pause", 30.0)
    resumed = sync.next_state(paused, "play", 90.0)
    assert resumed.status == PLAYING
    assert resumed.position(90.0) == pytest.approx(30.0)
    assert resumed.position(95.0) == pytest.approx(35.0)


def test_every_action_bumps_the_sequence():
    tl = Timeline(status=PLAYING, anchor=0.0, at=0.0, item="file:///a.mkv")
    for action, kw in (("pause", {}), ("play", {}), ("seek", {"pos": 5.0}),
                       ("stop", {}),
                       ("open", {"item": "file:///b.mkv", "anchor": 0.0})):
        nxt = sync.next_state(tl, action, 1.0, **kw)
        assert nxt.seq == tl.seq + 1, action


def test_seek_is_clamped_inside_the_movie():
    tl = Timeline(item="file:///a.mkv", status=PLAYING, anchor=0.0, at=0.0,
                  length=100.0)
    assert sync.next_state(tl, "seek", 1.0, pos=-5.0).anchor == 0.0
    assert sync.next_state(tl, "seek", 1.0, pos=500.0).anchor <= 100.0
    assert sync.next_state(tl, "seek", 1.0, pos=42.0).anchor == 42.0


def test_seek_while_paused_stays_paused():
    tl = Timeline(item="file:///a.mkv", status=PAUSED, anchor=10.0, at=5.0,
                  length=100.0)
    moved = sync.next_state(tl, "seek", 9.0, pos=70.0)
    assert moved.status == PAUSED
    assert moved.position(50.0) == pytest.approx(70.0)


def test_pausing_when_already_paused_does_not_move_the_place():
    tl = sync.next_state(Timeline(item="file:///a.mkv", status=PLAYING,
                                  anchor=0.0, at=0.0), "pause", 10.0)
    again = sync.next_state(tl, "pause", 500.0)
    assert again.anchor == pytest.approx(tl.anchor)


def test_round_trips_through_json():
    tl = Timeline(seq=7, item="file:///a%20b.mkv", name="a b.mkv",
                  status=PAUSED, anchor=12.5, at=1000.0, rate=1.02,
                  length=7200.0, holder="sam")
    again = Timeline.from_dict(tl.to_dict())
    assert again == tl


def test_end_time_accounts_for_the_playback_rate():
    tl = Timeline(status=PLAYING, item="file:///a", anchor=0.0, at=1000.0,
                  rate=1.0, length=100.0)
    assert tl.ends_at() == pytest.approx(1100.0)
    fast = Timeline(status=PLAYING, item="file:///a", anchor=0.0, at=1000.0,
                    rate=2.0, length=100.0)
    assert fast.ends_at() == pytest.approx(1050.0)


def test_stopped_or_paused_timelines_have_no_end():
    assert Timeline(status=STOPPED, length=100.0, at=0.0).ends_at() is None
    assert Timeline(status=PAUSED, item="file:///a", length=100.0,
                    at=0.0).ends_at() is None
    assert Timeline(status=PLAYING, item="file:///a", length=None,
                    at=0.0).ends_at() is None


# --- correction policy ----------------------------------------------------

def test_small_drift_is_ignored_entirely():
    rate, seek = Corrections(clock_error=0.0).next(0.05, 0.0)
    assert rate == 1.0 and seek is None


def test_moderate_drift_is_absorbed_by_the_playback_rate():
    rate, seek = Corrections(clock_error=0.0).next(0.5, 0.0)
    assert seek is None
    assert 0.95 <= rate < 1.0            # being ahead means slowing down

    behind_rate, _ = Corrections().next(-0.5, 0.0)
    assert 1.0 < behind_rate <= 1.05

def test_the_rate_correction_is_always_within_three_percent():
    corr = Corrections()
    for drift in (-5, -3, -1, 0.2, 1, 3, 5):
        rate, seek = corr.next(drift, 0.0)
        if seek is None:
            assert abs(rate - 1.0) <= 0.03 + 1e-9


def test_stopped_timeline_has_no_end():
    assert Timeline(status=STOPPED, length=100.0, at=0.0).ends_at() is None
    assert Timeline(status=PLAYING, length=None, at=0.0).ends_at() is None
    assert Timeline(status=PAUSED, item="file:///a", length=100.0,
                    at=0.0).ends_at() is None


def test_a_large_drift_causes_a_seek_not_a_rate():
    rate, seek = Corrections(clock_error=0.0).next(30.0, 1000.0)
    assert seek == pytest.approx(30.0)
    assert rate == 1.0


def test_seeks_are_rate_limited():
    corr = Corrections(clock_error=0.0)
    assert corr.next(10.0, 1000.0)[1] is not None
    assert corr.next(10.0, 1000.5)[1] is None       # too soon to seek again
    assert corr.next(10.0, 1005.0)[1] is not None

def test_clock_error_widens_the_seek_band():
    """A 300 ms clock error must not make 350 ms corrections look meaningful."""
    corr = Corrections(clock_error=0.3)
    assert corr.next(0.5, 0.0)[1] is None
    fresh = Corrections(clock_error=0.3)
    assert fresh.next(0.05, 0.0) == (1.0, None)     # inside the floor
def test_a_standing_offset_stops_driving_the_rate():
    """VLC reports the playhead a little behind where it is showing, so every
    peer sits at a small constant offset.  Chasing that would mean playing 2%
    fast for the whole film, which is audible and buys nothing."""
    corr = Corrections(clock_error=0.0)
    for _ in range(200):                      # a few minutes of steady readings
        rate, seek = corr.next(-0.45, 0.0)
    assert abs(rate - 1.0) < 1e-9
    assert seek is None
    assert corr.standing_offset == pytest.approx(-0.45, abs=0.02)


def test_a_standing_offset_still_leads_to_a_seek_when_it_is_large():
    """A bias the rate can never remove must eventually be seeked away."""
    corr = Corrections(clock_error=0.0)
    seek = None
    for _ in range(200):
        rate, seek = corr.next(-4.0, 1000.0 + _ * 0.25)
        if seek is not None:
            break
    assert seek == pytest.approx(-4.0, abs=0.3)


def test_a_fresh_gap_is_corrected_with_the_rate_not_written_off_as_bias():
    corr = Corrections(clock_error=0.0)
    rate, seek = corr.next(-0.5, 0.0)
    assert seek is None
    assert rate > 1.0                        # we are behind, so speed up
# --- local reading --------------------------------------------------------

def test_status_parsing_prefers_position_over_whole_seconds():
    from vlcsync.vlc import parse_status
    got = parse_status({
        "state": "playing", "time": 12, "position": 0.5, "length": 100,
        "rate": 1.0,
        "information": {"category": {"meta": {"filename": "a.mkv"}}},
    })
    assert got.position == pytest.approx(50.0)
    assert got.length == 100.0
    assert got.name == "a.mkv"


def test_status_parsing_falls_back_for_streams_without_a_length():
    from vlcsync.vlc import parse_status
    got = parse_status({"state": "playing", "time": 12, "position": 0.0,
                        "length": 0})
    assert got.position == pytest.approx(12.0)
    assert got.length is None


def test_local_paths_become_percent_encoded_uris():
    from vlcsync.vlc import to_uri
    uri = to_uri("/Users/me/My Movie (2019) é.mkv")
    assert uri.startswith("file:///")
    assert " " not in uri and "%20" in uri
    assert to_uri("http://example.com/a b.mkv") == "http://example.com/a b.mkv"


# --- the peer, against a fake player --------------------------------------

class FakeVLC:
    """Stands in for the VLC client, recording what it was told to do."""

    def __init__(self, item="file:///m/a.mp4", status=PLAYING, length=100.0,
                 position=0.0):
        self.item, self.status, self.length = item, status, length
        self.position = position
        self.rate = 1.0
        self.calls = []

    def sample(self):
        return Sample(status=self.status, position=self.position,
                      length=self.length, rate=self.rate, name="a.mp4")

    def current_uri(self):
        return self.item

    def open(self, uri, start=None, paused=False):
        self.calls.append(("open", uri, start, paused))
        self.item = uri
        self.position = start or 0.0
        self.status = PAUSED if paused else PLAYING

    def pause(self):
        self.calls.append(("pause",))
        self.status = PAUSED

    def resume(self):
        self.calls.append(("resume",))
        self.status = PLAYING

    def stop(self):
        self.calls.append(("stop",))
        self.item, self.status, self.position = None, STOPPED, 0.0

    def seek(self, pos):
        self.calls.append(("seek", round(pos, 3)))
        self.position = pos

    def set_rate(self, rate):
        self.calls.append(("rate", round(rate, 4)))
        self.rate = rate

    # names used by Peer
    def actions(self):
        return [c[0] for c in self.calls]


class FakeClock:
    def __init__(self):
        self.offset = 0.0
        self.rtt = 0.004

    def now(self):
        return time.time() + self.offset


class FakeCoord:
    def __init__(self):
        self.tl = Timeline()

    def wait(self, seq, timeout=1.0):
        return self.tl

    def apply_state(self, base_seq, new):
        self.tl = Timeline.from_dict(dict(new.to_dict(), seq=base_seq + 1))
        return self.tl


def make_peer():
    from vlcsync.peer import Peer
    fake = FakeVLC()
    peer = Peer("me", fake, FakeCoord(), FakeClock())
    return peer, fake


def prime(peer, fake, *, status=PLAYING, position=20.0, length=100.0,
          item="file:///m/a.mp4", name="a.mp4"):
    """Make the fake player look loaded and put a reading in the cache."""
    fake.item, fake.status, fake.position = item, status, position
    fake.length = length
    peer.local_item = item
    peer._loaded = True
    peer._sample = Sample(status=status, position=position, length=length,
                          name=name)
    peer._sample_mono = time.monotonic()
    return peer._sample


def test_a_guest_opens_at_the_right_position_and_paused():
    peer, fake = make_peer()
    peer.tl = Timeline(seq=1, item="file:///m/a.mp4", name="a.mp4",
                       status=PAUSED, anchor=42.0, at=time.time(), length=100.0)
    peer._sample = Sample(status=STOPPED, position=0.0, length=None)
    peer._sample_mono = time.monotonic()
    peer._apply()
    opens = [c for c in fake.calls if c[0] == "open"]
    assert len(opens) == 1
    _, uri, start, paused = opens[0]
    assert uri == "file:///m/a.mp4"
    assert start == pytest.approx(42.0, abs=0.5)
    assert paused is True                  # never shows a wrong frame


def test_a_playing_open_is_not_paused():
    peer, fake = make_peer()
    peer.tl = Timeline(seq=1, item="file:///m/a.mp4", status=PLAYING,
                       anchor=10.0, at=time.time(), length=100.0)
    peer._sample = Sample(status=STOPPED, position=0.0, length=None)
    peer._sample_mono = time.monotonic()
    peer._apply()
    opens = [c for c in fake.calls if c[0] == "open"]
    assert opens and opens[0][3] is False


def test_a_lost_input_is_reopened():
    """VLC drops the input at the end of a file; that must not be fatal."""
    peer, fake = make_peer()
    peer.tl = Timeline(seq=1, item="file:///m/a.mp4", status=PLAYING,
                       anchor=30.0, at=FakeClock().now(), length=100.0)
    prime(peer, fake, status=PLAYING, position=30.0)
    fake.status, fake.position = STOPPED, 0.0     # input vanished
    peer._sample = fake.sample()
    peer._regulate()
    assert any(c[0] == "open" for c in fake.calls)


def test_a_finished_movie_is_not_reopened_over_and_over():
    """Past the end of the file there is nowhere left to open, so stop."""
    peer, fake = make_peer()
    now = FakeClock().now()
    peer.tl = Timeline(seq=1, item="file:///m/a.mp4", status=PLAYING,
                       anchor=100.0, at=now - 5.0, length=100.0)
    prime(peer, fake, status=STOPPED, position=0.0)
    peer._loaded = False
    peer._regulate()
    assert not any(c[0] == "open" for c in fake.calls)


def test_a_peer_that_is_ahead_slows_down_and_one_behind_speeds_up():
    now = time.time()
    for drift, expect in ((0.5, "slow"), (-0.5, "fast")):
        peer, fake = make_peer()
        peer.tl = Timeline(seq=1, item="file:///m/a.mp4", status=PLAYING,
                           anchor=20.0 - drift, at=now, length=100.0)
        # position 20.0 against a target of 20.0 - drift means the local
        # player is `drift` ahead of (or behind) the group.
        prime(peer, fake, position=20.0)
        peer._saw_playing = True          # not the settling pass
        peer._regulate()
        rates = [c[1] for c in fake.calls if c[0] == "rate"]
        assert rates, "expected a rate correction for drift %s" % drift
        if expect == "slow":
            assert rates[-1] < 1.0
        else:
            assert rates[-1] > 1.0


def test_pausing_locally_publishes_a_pause_at_the_local_place():
    """Pressing pause in the window must read as intent, not as position."""
    peer, fake = make_peer()
    now = time.time()
    peer.tl = Timeline(seq=1, item="file:///m/a.mp4", status=PLAYING,
                       anchor=10.0, at=now, length=100.0)
    prime(peer, fake, status=PLAYING, position=10.0)
    peer._prev, peer._prev_mono = (
        Sample(status=PLAYING, position=10.0, length=100.0, name="a.mp4"),
        time.monotonic() - 1.0)
    peer._sample = Sample(status=PAUSED, position=10.0, length=100.0,
                          name="a.mp4")
    peer._sample_mono = time.monotonic()
    peer._watch_local()
    assert peer.tl.status == PAUSED
    assert peer.tl.anchor == pytest.approx(10.0, abs=0.1)
    assert not any(c[0] == "seek" for c in fake.calls)


def test_a_peer_that_is_way_out_of_sync_does_not_drag_the_group_with_it():
    """A guest whose file is still loading must not move everybody else."""
    peer, fake = make_peer()
    peer.tl = Timeline(seq=1, item="file:///m/a.mp4", status=PLAYING,
                       anchor=500.0, at=time.time(), length=1000.0)
    prime(peer, fake, position=3.0, length=1000.0)
    peer._prev = Sample(status=PLAYING, position=3.0, length=1000.0,
                        name="a.mp4")
    peer._prev_mono = time.monotonic() - 0.2
    peer._sample = Sample(status=PAUSED, position=3.0, length=1000.0,
                          name="a.mp4")
    peer._sample_mono = time.monotonic()
    peer._watch_local()
    # The pause is honoured, but it lands at the group's position (500 s),
    # not at the straggler's stale local read (3 s).
    assert peer.tl.status == PAUSED
    assert peer.tl.anchor == pytest.approx(500.0, abs=1.0)


def test_our_own_pause_is_not_bounced_back_to_the_group():
    peer, fake = make_peer()
    peer.tl = Timeline(seq=1, item="file:///m/a.mp4", status=PLAYING,
                       anchor=0.0, at=time.time(), length=100.0)
    prime(peer, fake, status=PLAYING, position=10.0)
    peer._prev = Sample(status=PLAYING, position=10.0, length=100.0,
                        name="a.mp4")
    peer._prev_mono = time.monotonic() - 1.0
    peer.settle(5.0)                       # pretend we just pressed pause
    peer._sample = Sample(status=PAUSED, position=10.0, length=100.0,
                          name="a.mp4")
    peer._sample_mono = time.monotonic()
    before = peer.tl
    peer._watch_local()
    assert peer.tl is before


def test_dragging_the_timeline_in_the_window_publishes_a_seek():
    """A big playhead jump can only be a person, and must be shared."""
    peer, fake = make_peer()
    peer.tl = Timeline(seq=1, item="file:///m/a.mp4", status=PLAYING,
                       anchor=10.0, at=FakeClock().now(), length=300.0)
    prime(peer, fake, status=PLAYING, position=10.0)
    peer._prev = Sample(status=PLAYING, position=10.0, length=300.0,
                        name="a.mp4")
    peer._prev_mono = time.monotonic() - 0.3
    peer._sample = Sample(status=PLAYING, position=140.0, length=300.0,
                          name="a.mp4")          # the user dragged it
    peer._sample_mono = time.monotonic()
    peer._watch_local()
    assert peer.tl.anchor == pytest.approx(140.0, abs=0.5)


def test_the_rate_correction_alone_does_not_look_like_a_user_seek():
    """Corrections move the playhead a little; that is not a person."""
    peer, fake = make_peer()
    peer.tl = Timeline(seq=1, item="file:///m/a.mp4", status=PLAYING,
                       anchor=10.0, at=FakeClock().now(), length=300.0)
    prime(peer, fake, status=PLAYING, position=10.0)
    peer._prev = Sample(status=PLAYING, position=10.0, length=300.0,
                        name="a.mp4")
    peer._prev_mono = time.monotonic() - 0.3
    peer._sample = Sample(status=PLAYING, position=10.3, length=300.0,
                          name="a.mp4")
    peer._sample_mono = time.monotonic()
    before = peer.tl
    peer._watch_local()
    assert peer.tl is before


def test_changing_the_file_in_the_window_publishes_an_open():
    peer, fake = make_peer()
    peer.tl = Timeline(seq=1, item="file:///m/a.mp4", status=PLAYING,
                       anchor=0.0, at=time.time(), length=100.0)
    prime(peer, fake, status=PLAYING, position=1.0, name="a.mp4")
    peer._prev = Sample(status=PLAYING, position=1.0, length=100.0,
                        name="a.mp4")
    peer._prev_mono = time.monotonic() - 1.0
    fake.item = "file:///m/b.mp4"
    peer._sample = Sample(status=PLAYING, position=0.0, length=100.0,
                          name="b.mp4")
    peer._sample_mono = time.monotonic()
    peer._watch_local()
    assert peer.tl.item == "file:///m/b.mp4"


def test_a_pending_open_is_not_reissued_every_tick():
    """Status granularity must not cause an open storm."""
    peer, fake = make_peer()
    peer.tl = Timeline(seq=1, item="file:///m/a.mp4", status=PLAYING,
                       anchor=0.0, at=time.time(), length=100.0)
    for _ in range(10):
        if peer.pending:
            peer._sample = None                # VLC has not loaded it yet
            peer._check_pending()
        else:
            peer._sample = Sample(status=STOPPED, position=0.0, length=None)
            peer._sample_mono = time.monotonic()
            peer._regulate()
    opens = [c for c in fake.calls if c[0] == "open"]
    assert len(opens) == 1


def test_a_missing_file_is_retried_without_wedging_the_peer():
    """A file that never loads must not stop the peer from reacting later."""
    peer, fake = make_peer()
    peer.tl = Timeline(seq=1, item="file:///m/gone.mp4", status=PLAYING,
                       anchor=0.0, at=time.time(), length=100.0)
    peer._sample = Sample(status=STOPPED, position=0.0, length=None)
    peer._sample_mono = time.monotonic()
    peer._apply()
    assert peer.pending is not None
    peer.pending["since"] = time.monotonic() - 100.0     # pretend it timed out
    peer._sample = None
    peer._check_pending()
    assert peer.pending is None
    # And the next pass tries again rather than giving up on the night.
    peer._settle_until = 0.0
    peer._sample = Sample(status=STOPPED, position=0.0, length=None)
    peer._sample_mono = time.monotonic()
    peer._regulate()
    assert peer.pending is not None


def test_the_rate_is_only_sent_when_it_actually_changes():
    peer, fake = make_peer()
    peer._set_rate(1.0)
    peer._set_rate(1.0)
    assert not [c for c in fake.calls if c[0] == "rate"]
    peer._set_rate(1.02)
    assert [c for c in fake.calls if c[0] == "rate"] == [("rate", 1.02)]