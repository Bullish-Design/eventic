"""CLI commands. Every operator action is a documented command with a truthful
exit code: 0 success, 1 operational failure, 2 usage/config, 3 drift."""

from __future__ import annotations

import json
import sys
from typing import Any

from eventic.app import App
from eventic.cli.loader import make_store
from eventic.errors import ConfigError, EventicError
from eventic.sql.admin import SqlAdmin
from eventic.worker import Worker

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_DRIFT = 3


def _store_and_admin(app: App, url: str) -> tuple[Any, SqlAdmin]:
    store = make_store(url)
    admin = store.admin()
    assert isinstance(admin, SqlAdmin)
    return store, admin


def schema_upgrade(app: App, url: str, out: Any = sys.stdout) -> int:
    store = make_store(url, create_tables=False)
    admin = store.admin()
    try:
        admin.migrate()
        print("schema upgraded", file=out)
    finally:
        store.close()
    return EXIT_OK


def schema_check(app: App, url: str, out: Any = sys.stdout) -> int:
    store, admin = _store_and_admin(app, url)
    try:
        report = admin.check(app)
        for stream, version, declared, stored, ok in report.streams:
            state = "ok" if ok else "DRIFT"
            print(
                f"{stream} v{version}: declared={declared[:12]} "
                f"stored={stored[:12]} {state}",
                file=out,
            )
        return EXIT_OK if not report.drift else EXIT_DRIFT
    finally:
        store.close()


def heads_rebuild(
    app: App, url: str, *, stream: str | None, chunk: int, out: Any = sys.stdout
) -> int:
    store, admin = _store_and_admin(app, url)
    try:
        report = admin.rebuild_heads(stream, chunk=chunk)
        print(
            f"rebuilt {report.rebuilt} heads, removed {report.orphans_removed} "
            f"orphans, {report.mismatches} mismatches",
            file=out,
        )
        return EXIT_FAILURE if report.mismatches else EXIT_OK
    finally:
        store.close()


def verify(
    app: App, url: str, *, stream: str | None, chunk: int, out: Any = sys.stdout
) -> int:
    store, admin = _store_and_admin(app, url)
    try:
        report = admin.verify(stream, chunk=chunk)
        print(
            f"verified {report.revisions_checked} revisions across "
            f"{', '.join(report.streams) or '(none)'}: {report.mismatches} mismatches",
            file=out,
        )
        return EXIT_FAILURE if report.mismatches else EXIT_OK
    finally:
        store.close()


def worker(app: App, url: str, *, queue: str, once: bool, out: Any = sys.stdout) -> int:
    import signal

    store = make_store(url)
    try:
        worker = Worker(app, store, queue=queue)
        if once:
            report = worker.drain_once()
            print(
                f"claimed={report.claimed} delivered={report.delivered} "
                f"retried={report.retried} dead_lettered={report.dead_lettered}",
                file=out,
            )
            return EXIT_FAILURE if report.dead_lettered > 0 else EXIT_OK

        # The library never installs signal handlers (F11): the CLI owns the
        # process, so SIGTERM/SIGINT map to a graceful stop after the current
        # drain — leases from a killed mid-batch drain stay claimed until they
        # expire, which at-least-once absorbs.
        signal.signal(signal.SIGTERM, lambda _s, _f: worker.stop())
        signal.signal(signal.SIGINT, lambda _s, _f: worker.stop())
        worker.run_forever()
        return EXIT_OK
    finally:
        store.close()


def intents_list(
    app: App,
    url: str,
    *,
    status: str | None,
    limit: int | None,
    cursor: str | None,
    out: Any = sys.stdout,
) -> int:
    store, admin = _store_and_admin(app, url)
    try:
        rows, next_cursor = admin.list_intents(
            status=status, limit=limit, cursor=cursor
        )
        for row in rows:
            print(
                f"{row['intent_id']} {row['subscription_id']} {row['queue']} "
                f"{row['status']} attempts={row['attempts']}",
                file=out,
            )
        if next_cursor:
            print(f"# next cursor: {next_cursor}", file=out)
        return EXIT_OK
    finally:
        store.close()


def intents_redrive(
    app: App, url: str, *, subscription: str, out: Any = sys.stdout
) -> int:
    store, admin = _store_and_admin(app, url)
    try:
        redriven = admin.redrive(subscription)
        print(f"redriven {redriven} intents", file=out)
        return EXIT_OK
    finally:
        store.close()


def inspect_app(app: App, url: str, out: Any = sys.stdout) -> int:
    store, _admin = _store_and_admin(app, url)
    try:
        facts = {
            "id": app.id,
            "streams": [
                {
                    "name": stream.name,
                    "schema_version": stream.schema_version,
                    "fingerprint": stream.fingerprint,
                }
                for stream in app.streams
            ],
            "meta_version": app.meta.version,
            "subscriptions": [
                {
                    "id": sub.id,
                    "stream": sub.stream.name,
                    "kinds": sorted(sub.kinds),
                    "delivery": _delivery_name(sub.delivery),
                }
                for sub in app.subscriptions
            ],
            "capabilities": {
                "outbox": store.capabilities.outbox,
                "json_paths": store.capabilities.json_paths,
                "concurrent_drainers": store.capabilities.concurrent_drainers,
                "max_batch": store.capabilities.max_batch,
            },
        }
        print(json.dumps(facts, indent=2, sort_keys=True), file=out)
        return EXIT_OK
    finally:
        store.close()


def _delivery_name(delivery: Any) -> str:
    if delivery.__class__.__name__ == "Inline":
        return "inline"
    queue = getattr(delivery, "queue", "default")
    return f"outbox:{queue}"


def handle_error(exc: BaseException, out: Any = sys.stderr) -> int:
    if isinstance(exc, ConfigError):
        print(f"config error: {exc}", file=out)
        return EXIT_USAGE
    if isinstance(exc, EventicError):
        print(f"error: {exc}", file=out)
        return EXIT_FAILURE
    print(f"error: {type(exc).__name__}: {exc}", file=out)
    return EXIT_FAILURE
