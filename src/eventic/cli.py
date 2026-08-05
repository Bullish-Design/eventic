"""``eventic`` command line — derived projections and delivery.

    eventic rebuild-heads --url URL [--stream S]
    eventic drain --url URL [--queue Q]

``rebuild-heads`` is the honesty check on CONCEPT §2.1: if the head
projection cannot be rebuilt from the log, the log is not the truth.
``drain`` runs the outbox dispatcher (inline delivery; DBOS deployments use
``DbosDispatcher`` from ``eventic.contrib`` instead).
"""

from __future__ import annotations

import argparse

from .dispatch.outbox import OutboxDispatcher
from .pipeline import rebuild_heads
from .store import Store


def _store(args) -> Store:
    return Store(args.url, create_tables=False).activate()


def _cmd_rebuild_heads(args) -> None:
    store = _store(args)
    n = rebuild_heads(store, stream=args.stream)
    print(f"rebuilt {n} head rows" + (f" for stream {args.stream}" if args.stream else ""))
    store.deactivate()


def _cmd_drain(args) -> None:
    store = _store(args)
    n = OutboxDispatcher(store).drain(queue=args.queue)
    print(f"drained {n} outbox rows" + (f" for queue {args.queue}" if args.queue else ""))
    store.deactivate()


def main() -> None:
    parser = argparse.ArgumentParser(prog="eventic")
    sub = parser.add_subparsers(dest="command", required=True)

    rb = sub.add_parser("rebuild-heads", help="rebuild eventic_head from the log")
    rb.add_argument("--url", required=True, help="SQLAlchemy database URL")
    rb.add_argument("--stream", help="only rebuild this stream")
    rb.set_defaults(func=_cmd_rebuild_heads)

    dr = sub.add_parser("drain", help="run the outbox dispatcher")
    dr.add_argument("--url", required=True, help="SQLAlchemy database URL")
    dr.add_argument("--queue", help="only drain this queue")
    dr.set_defaults(func=_cmd_drain)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
