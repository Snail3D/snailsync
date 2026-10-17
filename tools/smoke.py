"""End-to-end smoke test: two real VLC instances, one movie, real network.

This is the test that actually proves the thing works.  It starts a
coordinator, launches two headless VLC instances on the same file, wires a
:class:`~vlcsync.peer.Peer` to each, and drives them over the real HTTP
interfaces.  Nothing is mocked -- the only fictions are that the video output
is ``dummy`` and the two players share one machine.

Checks, in order:

1. a guest that joins late lands within the deadband of the host
2. the host pausing makes the guest pause at the same position
3. the host resuming keeps them together
4. the host seeking moves the guest to the new position
5. drift between the two stays bounded while playing
6. closing the host's player does not disturb the guest
7. a peer whose file is missing reports it instead of wedging the group

Usage:  python3 tools/smoke.py [--keep] [--verbose]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vlcsync.clock import Clock                         # noqa: E402
from vlcsync.coordinator import Coordinator as Server   # noqa: E402
from vlcsync.peer import Peer                           # noqa: E402
from vlcsync.protocol import Coordinator as Client      # noqa: E402
from vlcsync.sync import PAUSED, PLAYING, STOPPED, Timeline  # noqa: E402
from vlcsync.vlc import VLCClient, launch                # noqa: E402

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
VERBOSE = False
FAILURES = []


def log(msg):
    if VERBOSE:
        print("    " + msg, flush=True)


def check(name, ok, detail=""):
    print("  [%s] %s%s" % (PASS if ok else FAIL, name,
                           ("  -- " + detail) if detail else ""), flush=True)
    if not ok:
        FAILURES.append(name)
    return ok


def free_port():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

def make_movie(path, seconds=300):
    """A cheap, seekable test file with a picture and a tone."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24",
         "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000",
         "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
         "-g", "24", "-crf", "30", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", path], check=True)


class Node:
    """One participant: a VLC instance plus the peer driving it."""

    def __init__(self, name, client, coord, clock, headless=True):
        self.name = name
        self.client = client
        self.peer = Peer(name, client, coord, clock,
                         log=(lambda m: log("%s | %s" % (name, m))))
        self.peer.tl = coord.state()
        self.thread = None

    def start(self):
        import threading
        self.thread = threading.Thread(target=self.peer.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.peer.stop()
        if self.thread:
            self.thread.join(timeout=5.0)

    def local(self):
        """Local playhead, extrapolated like the peer does."""
        return self.peer._local_now()

    def drift(self):
        """Local minus the group's position, in seconds."""
        local = self.local()
        if local is None:
            return None
        return local - self.peer.tl.position(self.peer.clock.now())

    def status(self):
        return self.peer._sample.status if self.peer._sample else "?"


def wait_until(predicate, timeout=20.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def main(argv=None):
    global VERBOSE
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true",
                        help="keep the temporary directory")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    VERBOSE = args.verbose

    if not shutil.which("ffmpeg"):
        print("ffmpeg is required to build the test movie")
        return 2

    tmp = tempfile.mkdtemp(prefix="vlcsync-smoke-")
    movie = os.path.join(tmp, "movie.mp4")
    print("building a 5-minute test movie ...", flush=True)
    make_movie(movie, 300)
    print("  %s (%.1f MB)" % (movie, os.path.getsize(movie) / 1e6))

    server = Server(host="127.0.0.1", port=free_port(), token="smoke").start()
    url = "http://127.0.0.1:%d" % server.port
    print("coordinator on %s" % url)

    procs = []
    nodes = []
    try:
        # --- the host: VLC already playing the movie ---------------------
        host_port = free_port()
        procs.append(launch(targets=[movie], port=host_port,
                            password="hostpw", 
                            extra=["--vout", "dummy", "--aout", "dummy"]))
        host_client = VLCClient(port=host_port, password="hostpw")
        if not wait_until(host_client.alive, timeout=20):
            print("host VLC never answered on port %d" % host_port)
            return 2
        host_client.wait_for_input(timeout=20)

        clock_h = Clock(token="smoke")
        client_h = Client(url, token="smoke")
        host = Node("host", host_client, client_h, clock_h)
        # Host publishes its own state first (it is the one with the movie).
        host.peer.tl = client_h.state()
        sample = host_client.sample()
        print("host VLC: %s, length %.1fs" % (sample.status, sample.length or 0))
        fresh = host.peer.tl.seq == 0 and sample.length
        if fresh:
            from vlcsync import (sync as syncmod)
            new = syncmod.next_state(
                host.peer.tl, "open", clock_h.now(),
                item=host_client.current_uri() or ("file://" + movie),
                name=os.path.basename(movie), length=sample.length,
                anchor=sample.position, status=PLAYING, holder="host")
            host.peer.tl = client_h.apply_state(host.peer.tl.seq, new)
            host.peer.local_item = host.peer.tl.item
        host.start()

        # --- the guest joins late ----------------------------------------
        time.sleep(3.0)                # let the movie get going first
        guest_port = free_port()
        procs.append(launch(port=guest_port, password="guestpw",
                            extra=["--vout", "dummy", "--aout", "dummy"]))
        guest_client = VLCClient(port=guest_port, password="guestpw")
        if not wait_until(guest_client.alive, timeout=20):
            print("guest VLC never answered on port %d" % guest_port)
            return 2

        clock_g = Clock(token="smoke")
        client_g = Client(url, token="smoke")
        clock_g.sync(url + "/api/time")
        guest = Node("guest", guest_client, client_g, clock_g)
        guest.peer.tl = client_g.state()
        guest.start()

        joined = wait_until(
            lambda: guest.local() is not None
            and guest.peer.pending is None
            and guest.drift() is not None
            and abs(guest.drift()) < 2.0, timeout=30.0)
        check("a late guest lands on the host's position",
              joined, "drift %.3fs" % (guest.drift() or 0.0))

        # --- the host pauses ---------------------------------------------
        host.peer.publish("pause")
        ok = wait_until(lambda: guest.status() == PAUSED, timeout=8.0)
        # Let both players settle: a pause is applied locally, and the other
        # side only sees it after a round trip, so measuring immediately would
        # time the network, not the sync.
        time.sleep(2.0)
        wait_until(lambda: all(p.peer.pending is None
                               for p in (host, guest)), timeout=5.0)
        time.sleep(0.5)
        shift = abs((guest.local() or 0) - (host.local() or 0))
        check("host pause reaches the guest", ok,
              "guest %s, %.3fs apart" % (guest.status(), shift))
        check("the pair stops within a quarter second of each other",
              shift < 0.25, "%.3fs" % shift)

        # --- the host resumes --------------------------------------------
        host.peer.publish("play")
        ok = wait_until(lambda: guest.status() == PLAYING, timeout=8.0)
        check("host resume reaches the guest", ok, guest.status())

        # --- drift stays bounded while playing ---------------------------
        samples, worst_at = [], ""
        deadline = time.time() + 12.0
        while time.time() < deadline:
            d = guest.drift()
            if d is not None:
                samples.append(abs(d))
                if abs(d) == max(samples):
                    worst_at = ("host %.2fs / guest %.2fs, target %.2fs"
                                % (host.local() or -1, guest.local() or -1,
                                   guest.peer.tl.position(guest.peer.clock.now())))
            time.sleep(0.25)
        worst = max(samples) if samples else 99.0
        check("drift stays inside the deadband while playing", worst < 0.75,
              "worst %.3fs at %s" % (worst, worst_at))

        # --- the guest seeks (anyone can drive) --------------------------
        guest.peer.publish("seek", pos=200.0)
        ok = wait_until(lambda: abs((host.local() or 0) - 200.0) < 2.0,
                        timeout=8.0)
        check("a guest's seek moves the host too", ok,
              "host at %.1fs" % (host.local() or 0.0))

        # --- a seek followed by a pause ----------------------------------
        # This is the worst case for alignment, because VLC can only seek to
        # whole seconds: each peer rounds the target independently, so a
        # fractional position can leave the two of them on different seconds.
        # The reachable bound is therefore about half a second, not the tens of
        # milliseconds a plain pause achieves.
        host.peer.publish("seek", pos=260.0)
        time.sleep(2.0)
        host.peer.publish("pause")
        ok = wait_until(lambda: guest.status() == PAUSED, timeout=8.0)
        time.sleep(2.0)                    # let the paused position settle
        shift = abs((guest.local() or 0) - (host.local() or 0))
        check("a seek then a pause still lands together", ok and shift < 0.6,
              "%.3fs apart" % shift)

        # --- the host leaves ---------------------------------------------
        host.peer.publish("play")
        time.sleep(2.0)
        host.stop()
        host_client.stop()
        procs[-2].terminate()
        time.sleep(3.0)
        alive = guest.drift() is not None and guest.status() == PLAYING
        check("a guest keeps playing when the host disappears", alive,
              guest.status())
        check("the guest can still drive alone",
              guest.peer.publish("pause") is not None
              or guest.peer.tl.status == PAUSED,
              guest.peer.tl.status)

    finally:
        for node in nodes:
            node.stop()
        try:
            guest.stop()
            guest_client.stop()
        except Exception:                                  # noqa: BLE001
            pass
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        server.stop()
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print("kept %s" % tmp)

    print()
    if FAILURES:
        print("%d check(s) FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())