"""Better-money Windows entry point.

Default mode launches or opens the single running instance. Internal
``--server`` mode runs the controlled Uvicorn server and must receive a
non-empty session token through the ``BETTER_MONEY_SESSION_TOKEN``
environment variable (never the command line).
"""
from __future__ import annotations

import argparse
import os
import sys

from app.launcher import launch_or_open, request_shutdown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="BetterMoney")
    parser.add_argument(
        "--server", action="store_true",
        help="internal: run the web server instead of the launcher")
    parser.add_argument(
        "--request-shutdown", action="store_true",
        help="stop the verified running instance")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8642)
    args = parser.parse_args(argv)

    if args.request_shutdown:
        return request_shutdown()
    if not args.server:
        return launch_or_open()

    if args.host != "127.0.0.1":
        print("Better-money 服务只允许绑定 127.0.0.1", file=sys.stderr)
        return 1
    token = os.environ.get("BETTER_MONEY_SESSION_TOKEN", "").strip()
    if not token:
        print(
            "BETTER_MONEY_SESSION_TOKEN is required in --server mode",
            file=sys.stderr,
        )
        return 1
    from app.server import run_server

    return run_server(args.host, args.port, token)


if __name__ == "__main__":
    raise SystemExit(main())
