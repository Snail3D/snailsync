# SnailSync

Watch the same movie in VLC together, each on your own computer, and stay in
step. **Anyone** can pause, resume, seek or open a different file — everybody
else follows within a fraction of a second. No account, no server to rent, no
uploading the movie anywhere.

Everyone needs the same file on their own disk. Nothing is streamed between
you; only the playhead position is shared.

## How it works

* The person who starts the night (`host`) also runs a tiny coordinator — a
  small HTTP service on their machine holding one number: where the movie is
  right now.
* Everyone runs a normal VLC window on their own machine. SnailSync drives it
  through VLC's built-in HTTP control interface on `127.0.0.1`, so your movie
  never leaves your computer.
* Each client measures its clock offset against the coordinator, then computes
  "where should the movie be, right now". Agreeing on that projection is what
  makes two machines stay together without either knowing when the other
  showed a frame.
* Corrections are graduated so nothing jitters. Differences below 0.12 s are
  ignored; a few hundred milliseconds is absorbed by nudging the playback rate
  by at most 3 %, which also soaks up the slow drift between two machines'
  audio clocks over a two hour film; only a real seek, a late join or a stalled
  buffer is big enough to move the playhead.

Because a *client* computes each new state and the coordinator only decides who
wins a race, anyone can drive. Two people pausing at the same instant cannot
corrupt anything: the loser redoes its work on top of the winner's state.

## How well it stays in step

Measured on two VLC 3.0.21 instances playing a five minute file
(`tools/smoke.py`):

| Event | Result |
|---|---|
| A guest joins a film already running | lands within **0.25–0.5 s** and stays there |
| The host pauses | both stop **0.00–0.15 s** apart |
| Left alone while playing | drift stays under **0.6 s**, rate held at exactly 1.000 |
| Anybody seeks | everybody within half a second of the new position |
| The host closes VLC | guests keep playing, and keep control |

Two limits here are VLC's, not this tool's. Its `seek` command accepts **whole
seconds only**, so a seek can leave two peers up to half a second apart; and it
reports the playhead a little behind where it is really showing, so the
steady-state offset is a few hundred milliseconds rather than zero. Both are
absorbed rather than chased — which is why the playback rate sits at exactly
1.000 once the group has settled instead of permanently running 2 % fast.

Frame-perfect alignment is not achievable this way, for a structural reason:
VLC will not tell anyone which frame it is showing, only roughly where the
playhead is. Peers converge on the same *projection* of the timeline, which is
what makes them agree with each other; they do not lock to one another's video.

## Requirements

* **VLC 3.x** — on macOS, the normal `VLC.app`.
* **Python 3.8+** on every machine (macOS: `python3` is already there; no
  packages to install).
* Everyone on one network: the same Wi-Fi, or the same VPN (Tailscale,
  WireGuard, …) if you are not in the same house.
* The **same movie file** on each machine. Different rips will not line up.

## Start a night

The host:

```bash
vlcsync host ~/Movies/Alien.mkv
```

It starts VLC with the movie and prints one line to send to everyone:

```bash
vlcsync join http://192.168.1.42:8790 --name Sam --token kQ3f9Zx1aB
```

They run it; their VLC opens, loads the same movie, and lands where the host is.
Any of them can now pause or seek and everyone follows.

When the host stops (`Ctrl-C`), the night does not end — the guests keep
watching and keep control among themselves. The coordinator was only holding a
number.

## Everyday controls

Just use VLC as you normally would:

| In VLC | What happens |
|---|---|
| Space / the pause button | everybody pauses at the same place |
| Click the timeline, or `Ctrl-T` | everybody jumps there |
| Space again | everybody resumes |
| New file (File ▸ Open) | everybody is asked to load it too |
| Your own volume | yours only, never shared |

If you need to step out, pause and everybody waits for you.

## When something is off

* **"cannot reach the coordinator"** — the host's machine must stay awake and
  the port (default `8790`) must be reachable. On macOS the first run may ask to
  allow incoming connections: allow it. Everyone must pass the *same* `--token`.
* **"gave up loading …"** — that machine does not have the movie at that path,
  or the file is still copying. Put the same file there and restart that peer.
* **A guest is minutes off** — usually a badly measured clock offset. It is
  re-measured at every start; run on the same Wi-Fi or VPN.
* **Audio wobble while drift is being corrected** — VLC's pitch preservation is
  off (it is on by default). `--no-seek` also avoids ever seeking on a weak
  machine, at the cost of catching up more slowly.
* **You already had a VLC open** — SnailSync starts its own VLC on its own
  control port, so an existing window is untouched.

## Options worth knowing

| Flag | Meaning |
|---|---|
| `--name Sam` | what the others see you as (default: your hostname) |
| `--token X` | shared secret for the night (the host prints one) |
| `--sync-port N` | coordinator port (host only) |
| `--vlc-port N` | pin the local VLC control port |
| `--no-seek` | never seek this player; correct with the rate only |
| `--volume N` | set this player's volume (0–256) at startup |
| `--headless` | dummy video/audio out (for testing on a screenless machine) |
| `--extra ARG` | extra VLC argument, repeatable (host) |

## If you want the coordinator somewhere else

The coordinator is just an HTTP service, and clients do not care where it runs:

```bash
python3 -m vlcsync.server --port 8790 --token kQ3f9Zx1aB
```

There is no reason to do this for a movie night, but it means nothing is
secretly tied to the host's own VLC.

## Installing it

Nothing to build:

```bash
cd SnailSync
python3 -m vlcsync host ~/Movies/Alien.mkv
```

Optional: `pip install -e .` puts a `vlcsync` command on your `PATH`.

## Tests

```bash
python3 -m pytest tests/     # timeline arithmetic and reconciliation
python3 tools/smoke.py       # two real VLC instances, real HTTP, real sync
```

The smoke test builds its own five-minute movie, starts two headless VLC
instances, and checks that a late guest lands on the host's position, that
pauses and seeks reach the other side, that drift stays bounded, that a guest's
own seek moves the host, and that a guest keeps watching when the host leaves.
It takes about half a minute and needs `ffmpeg` on the `PATH`.