"""Run just the coordinator, for anyone who wants it somewhere else.

    python3 -m vlcsync.server --port 8790 --token secret

Clients behave exactly the same whether the coordinator runs here or inside a
``vlcsync host``.
"""

from __future__ import annotations

import argparse
import secrets
import sys
import time

from .coordinator import Coordinator


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m vlcsync.server",
        description="Run a SnailSync coordinator on its own.")
    parser.add_argument("--host", default="0.0.0.0",
                        help="address to bind (default: all interfaces)")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--token", default="",
                        help="shared secret required of every client")
    args = parser.parse_args(argv)

    token = args.token or secrets.token_urlsafe(9)
    server = Coordinator(host=args.host, port=args.port, token=token).start()
    print("coordinator listening on %s:%d" % (args.host, server.port))
    print("token: %s" % token)
    print("friends join with:  vlcsync join <your-ip>:%d --name X --token %s"
          % (server.port, token))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())