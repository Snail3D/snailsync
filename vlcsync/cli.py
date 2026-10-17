"""Command line entry points: ``vlcsync host`` and ``vlcsync join``.

    vlcsync host  ~/Movies/Alien.mkv          # start everything, be the host
    vlcsync join  http://192.168.1.42:8790 "Sam"
    vlcsync join  snails-mac-mini.local

The host runs the coordinator, launches the movie in its own VLC, and is a
peer like any other.  Guests copy the same number on the command line; when
the host leaves, guests carry on and keep control among themselves.
"""

from __future__ import annotations

import argparse
import os
import secrets
import signal
import socket
import sys
import threading
import time
import urllib.parse

from .clock import Clock
from .coordinator import Coordinator as CoordServer
from .peer import Peer
from .protocol import Coordinator as CoordClient, ProtocolError, base_url
from .vlc import VLCClient, VLCError, launch, to_uri

EXIT_OK = 0
EXIT_ERROR = 1


def _default_name() -> str:
    try:
        return socket.gethostname().split(".")[0][:32]
    except Exception:                                     # noqa: BLE001
        return "peer"


def _lan_address() -> str:
    """Best guess at an address a friend on the same Wi-Fi can reach."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))       # no packets sent, just routing
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def _logger(name: str):
    start = time.time()

    def log(message: str) -> None:
        print("[%7.1fs] %-10s %s" % (time.time() - start, name, message),
              flush=True)

    return log


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="vlcsync",
        description="Watch the same movie in VLC together, on your own "
                    "computers. Anyone can pause or seek; everybody follows.")
    sub = parser.add_subparsers(dest="mode", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--name", default=_default_name(),
                        help="what the others see you as (default: hostname)")
    common.add_argument("--token", default="",
                        help="shared secret; everybody must pass the same one")
    common.add_argument("--vlc-port", type=int, default=0,
                        help="port for this machine's VLC control interface "
                             "(default: a free one)")
    common.add_argument("--vlc", default=None, help="path to the VLC binary")
    common.add_argument("--volume", type=int, default=None,
                        help="set this player's volume 0-256 at startup")
    common.add_argument("--no-seek", action="store_true",
                        help="never seek this player; only correct drift "
                             "with the playback rate")

    host = sub.add_parser("host", parents=[common],
                          help="start a movie night")
    host.add_argument("movie", help="path to the movie to watch")
    host.add_argument("--sync-port", type=int, default=0,
                      help="port for the coordinator (default 8790)")
    host.add_argument("--headless", action="store_true",
                      help="use the dummy video output (for testing)")
    host.add_argument("--extra", action="append", default=[],
                      help="extra VLC argument (repeatable)")

    join = sub.add_parser("join", parents=[common],
                          help="join a movie night somebody else started")
    join.add_argument("address",
                      help="what the host told you: host:port or a URL")
    join.add_argument("--headless", action="store_true",
                      help="use the dummy video output (for testing)")
    return parser.parse_args(argv)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _vlc_password() -> str:
    return secrets.token_urlsafe(12)


def _start_vlc(port: int, password: str, media: str = None,
               exe: str = None, headless: bool = False,
               extra=(), log=None) -> VLCClient:
    """Launch VLC and return a client once its interface answers."""
    args = list(extra)
    if headless:
        args += ["--vout", "dummy", "--aout", "dummy"]
    targets = [to_uri(media)] if media else []
    proc = launch(targets=targets, port=port, password=password, exe=exe,
                  extra=args, log=log)
    client = VLCClient(port=port, password=password)
    deadline = time.time() + 20.0
    while time.time() < deadline:
        if proc.poll() is not None:
            raise VLCError("VLC exited immediately (code %s). Try running it "
                           "once by hand to see why." % proc.returncode)
        if client.alive():
            return client
        time.sleep(0.2)
    raise VLCError("VLC started but its control interface never answered on "
                   "port %d." % port)


def _heartbeat(coord, name: str, stop: threading.Event) -> None:
    """Tell the coordinator we are still here (and who to count as present)."""
    while not stop.is_set():
        try:
            coord.hello(name)
        except ProtocolError:
            pass
        stop.wait(5.0)


def _run_peer(peer: Peer, coord, name: str) -> int:
    """Run the peer loop in the foreground, reacting to Ctrl-C."""
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except ValueError:
            pass                       # not on the main thread; carry on

    beat = threading.Event()
    threading.Thread(target=_heartbeat, args=(coord, name, beat),
                     daemon=True).start()
    thread = threading.Thread(target=peer.run, name="peer", daemon=True)
    thread.start()

    # The loop also ends by itself when the coordinator goes away and there is
    # nothing left to follow.
    while not stop.is_set() and thread.is_alive():
        stop.wait(0.5)

    beat.set()
    peer.stop()
    thread.join(timeout=5.0)
    return EXIT_OK


def cmd_host(args) -> int:

    log = _logger("host")
    movie = os.path.abspath(os.path.expanduser(args.movie))
    if not os.path.exists(movie):
        print("no such file: %s" % movie, file=sys.stderr)
        return EXIT_ERROR

    sync_port = args.sync_port or 8790
    token = args.token or secrets.token_urlsafe(9)
    server = CoordServer(port=sync_port, token=token).start()
    address = "http://%s:%d" % (_lan_address(), server.port)

    vlc_port = args.vlc_port or _free_port()
    log("starting VLC ...")
    vlc_log = os.path.expanduser("~/.vlcsync-vlc.log")
    try:
        client = _start_vlc(vlc_port, _vlc_password(), media=movie,
                            exe=args.vlc, headless=args.headless,
                            extra=args.extra, log=vlc_log)
    except VLCError as exc:
        server.stop()
        print(exc, file=sys.stderr)
        return EXIT_ERROR

    if args.volume is not None:
        client.set_volume(args.volume)
    sample = client.wait_for_input(timeout=20.0)
    if not sample.length:
        log("warning: VLC has not loaded the movie yet")

    clock = Clock(token=token)
    coord = CoordClient(address, token=token, timeout=40.0)
    peer = Peer(args.name, client, coord, clock, control=True,
                seeker=not args.no_seek, log=log)
    # The host is the coordinator, so it already keeps coordinator time and
    # has nothing to measure.  Adopt whatever state is already there (a
    # restart keeps it) or publish this movie as the opening state.
    existing = coord.state()
    if existing.item is None:
        from . import sync
        uri = client.current_uri() or ("file://" + urllib.parse.quote(movie))
        peer.tl = coord.apply_state(
            existing.seq,
            sync.next_state(existing, "open", clock.now(), item=uri,
                            name=os.path.basename(movie),
                            length=sample.length, anchor=sample.position,
                            status=sample.status if sample.status in
                            ("playing", "paused") else "playing",
                            holder=args.name))
    else:
        peer.tl = existing
        log("adopting the state already on the coordinator")
    peer.local_item = peer.tl.item

    print()
    print("  " + "=" * 68)
    print("   Movie: %s" % os.path.basename(movie))
    print("   Tell each friend to run exactly this:")
    print()
    print("       vlcsync join %s --name THEIR_NAME --token %s"
          % (address, token))
    print()
    print("   Anyone can pause, seek and resume; everybody follows.")
    print("   Ctrl-C here stops your player only.")
    print()
    print("   VLC's own control port: %d (a handy place to poke at it)"
          % vlc_port)
    print("   A different night on this machine: --sync-port 8800")
    print("  " + "=" * 68)
    print()

    return _run_peer(peer, coord, args.name)


def cmd_join(args) -> int:
    log = _logger(args.name)
    address = base_url(args.address)
    coord = CoordClient(address, token=args.token, timeout=40.0)

    try:
        coord.state()
    except ProtocolError as exc:
        print("cannot reach the coordinator at %s (%s)\n"
              "is the host still running, and did they give you the right "
              "address and token?" % (address, exc), file=sys.stderr)
        return EXIT_ERROR

    clock = Clock(token=args.token)
    try:
        offset = clock.sync(coord.url + "/api/time")
    except Exception as exc:                              # noqa: BLE001
        print("clock sync failed: %s" % exc, file=sys.stderr)
        return EXIT_ERROR
    log("clock offset %+.3fs (rtt %.0f ms)"
        % (offset, clock.rtt * 1000))

    vlc_port = args.vlc_port or _free_port()
    try:
        client = _start_vlc(vlc_port, _vlc_password(), exe=args.vlc,
                            headless=args.headless,
                            log=os.path.expanduser("~/.vlcsync-vlc.log"))
    except VLCError as exc:
        print(exc, file=sys.stderr)
        return EXIT_ERROR
    if args.volume is not None:
        client.set_volume(args.volume)

    peer = Peer(args.name, client, coord, clock, control=True,
                seeker=not args.no_seek, log=log)
    peer.tl = coord.state()
    log("connected to %s" % address)
    log("%s" % Peer._describe(peer.tl, clock.now()))
    return _run_peer(peer, coord, args.name)


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        return cmd_host(args) if args.mode == "host" else cmd_join(args)
    except KeyboardInterrupt:
        return EXIT_OK
    except VLCError as exc:
        print(exc, file=sys.stderr)
        return EXIT_ERROR