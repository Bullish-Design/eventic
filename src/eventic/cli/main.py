"""The ``eventic`` CLI: argument parsing and truthful exit codes."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from typing import Any

from eventic import __version__
from eventic.cli import commands
from eventic.cli.loader import load_app

URL_HELP = "database URL (sqlite:// or postgresql://); defaults to $EVENTIC_URL"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eventic",
        description="a versioned document store with transactional change notification",
    )
    parser.add_argument("--version", action="version", version=f"eventic {__version__}")
    parser.add_argument("--app", required=True, help="module:attr naming an App")
    parser.add_argument("--url", default=os.environ.get("EVENTIC_URL"), help=URL_HELP)
    sub = parser.add_subparsers(dest="command", required=True)

    schema = sub.add_parser("schema", help="schema operations")
    schema_sub = schema.add_subparsers(dest="action", required=True)
    schema_sub.add_parser("upgrade", help="run migrations")
    schema_sub.add_parser("check", help="fingerprint drift check; exits 3 on drift")

    heads = sub.add_parser("heads", help="head projection operations")
    heads_sub = heads.add_subparsers(dest="action", required=True)
    rebuild = heads_sub.add_parser("rebuild", help="rebuild heads from the log")
    rebuild.add_argument("--stream")
    rebuild.add_argument("--chunk", type=int, default=1000)

    verify = sub.add_parser("verify", help="verify log digests and heads")
    verify.add_argument("--stream")
    verify.add_argument("--chunk", type=int, default=1000)

    worker = sub.add_parser("worker", help="drain an outbox queue")
    worker.add_argument("--queue", default="default")
    worker.add_argument("--once", action="store_true", help="drain one batch and exit")

    intents = sub.add_parser("intents", help="inspect and operate on delivery intents")
    intents_sub = intents.add_subparsers(dest="action", required=True)
    intents_list = intents_sub.add_parser("list")
    intents_list.add_argument("--status", choices=["pending", "leased", "dead"])
    intents_list.add_argument("--limit", type=int, help="page size")
    intents_list.add_argument(
        "--cursor", help="opaque page cursor from a previous list"
    )
    redrive = intents_sub.add_parser("redrive")
    redrive.add_argument("--subscription", required=True)

    sub.add_parser("inspect", help="print the resolved app and store capabilities")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.url:
        print("error: --url is required (or set $EVENTIC_URL)", file=sys.stderr)
        return commands.EXIT_USAGE
    try:
        app = load_app(args.app)
    except Exception as exc:  # noqa: BLE001
        return commands.handle_error(exc)
    try:
        return _dispatch(args, app)
    except Exception as exc:  # noqa: BLE001
        return commands.handle_error(exc)


def _dispatch(args: argparse.Namespace, app: Any) -> int:
    handlers: dict[str, Callable[..., int]] = {
        "schema.upgrade": commands.schema_upgrade,
        "schema.check": commands.schema_check,
        "heads.rebuild": commands.heads_rebuild,
        "verify": commands.verify,
        "worker": commands.worker,
        "intents.list": commands.intents_list,
        "intents.redrive": commands.intents_redrive,
        "inspect": commands.inspect_app,
    }
    key = (
        f"{args.command}.{args.action}"
        if args.command in ("schema", "heads", "intents")
        else args.command
    )
    handler = handlers[key]
    if key == "heads.rebuild":
        return handler(app, args.url, stream=args.stream, chunk=args.chunk)
    if key == "verify":
        return handler(app, args.url, stream=args.stream, chunk=args.chunk)
    if key == "worker":
        return handler(app, args.url, queue=args.queue, once=args.once)
    if key == "intents.list":
        return handler(
            app, args.url, status=args.status, limit=args.limit, cursor=args.cursor
        )
    if key == "intents.redrive":
        return handler(app, args.url, subscription=args.subscription)
    return handler(app, args.url)


if __name__ == "__main__":
    sys.exit(main())
