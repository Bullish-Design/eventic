"""Adversarial runtime probes for the Eventic 0.3 structural review.

These are intentionally outside the test suite: they document counterexamples
to claimed invariants without changing production code.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from pydantic import ValidationError, computed_field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from eventic import Delta, Interceptor, Record, Store, on_commit
from eventic.errors import RecordNotFound
from eventic.pipeline import rebuild_heads
from eventic.seams import RowStore, Window
from eventic.store.schema import HeadRow, LogRow, OutboxRow, now_utc


def heading(name: str) -> None:
    print(f"\n=== {name} ===")


def counts(store: Store) -> tuple[int, int, int]:
    with Session(store.engine) as session:
        return tuple(
            session.scalar(select(func.count()).select_from(table))
            for table in (LogRow, HeadRow, OutboxRow)
        )


def probe_deep_mutability_breaks_log_head_equivalence(root: Path) -> None:
    heading("deep mutability breaks log/head equivalence")

    class Mutable(Record, stream="review004_mutable", codec=Delta(k=20)):
        tags: list[str]
        n: int = 0

    store = Store(f"sqlite:///{root / 'mutable.db'}", create_tables=True)
    with store:
        base = Mutable(tags=["persisted"]).save()
        base.tags.append("in-memory-only")
        current = base.update(n=1)
        latest = Mutable.get(base.id)
        exact = Mutable.get(base.id, version=1)
        history = Mutable.history(base.id)
        print("returned tags:", current.tags)
        print("head/latest tags:", latest.tags)
        print("log/exact-v1 tags:", exact.tags)
        print("history-v1 tags:", history[-1].tags)
    store.engine.dispose()


def probe_unsaved_update_creates_broken_stream(root: Path) -> None:
    heading("update() on an unsaved value creates a broken delta stream")

    class Unsaved(Record, stream="review004_unsaved", codec=Delta(k=20)):
        required: str
        n: int = 0

    store = Store(f"sqlite:///{root / 'unsaved.db'}", create_tables=True)
    with store:
        written = Unsaved(required="present").update(n=1)
        print("returned version:", written.version)
        print("latest/head:", Unsaved.get(written.id).model_dump(mode="json"))
        for label, operation in (
            ("exact-v1", lambda: Unsaved.get(written.id, version=1)),
            ("history", lambda: Unsaved.history(written.id)),
        ):
            try:
                operation()
            except (RecordNotFound, ValidationError) as exc:
                print(f"{label} failure:", type(exc).__name__, str(exc).splitlines()[0])
    store.engine.dispose()


def probe_cross_stream_idempotency_is_silent_loss(root: Path) -> None:
    heading("same UUID in two streams is silently lost when state matches")

    class Alpha(Record, stream="review004_alpha"):
        value: int

    class Beta(Record, stream="review004_beta"):
        value: int

    store = Store(f"sqlite:///{root / 'streams.db'}", create_tables=True)
    rid = uuid.uuid4()
    with store:
        Alpha(id=rid, value=7).save()
        Beta(id=rid, value=7).save()
        with Session(store.engine) as session:
            streams = list(session.scalars(select(LogRow.stream)))
        print("persisted streams:", streams)
        try:
            Beta.get(rid)
        except RecordNotFound as exc:
            print("Beta.get failure:", type(exc).__name__)
    store.engine.dispose()


def probe_interceptor_return_is_not_returned(root: Path) -> None:
    heading("before_commit transform is persisted but not returned")

    class Enrich(Interceptor):
        def before_commit(self, record):
            return record.model_copy(update={"n": 4242})

    class Enriched(Record, stream="review004_enriched", interceptors=(Enrich(),)):
        n: int = 0

    store = Store(f"sqlite:///{root / 'interceptor.db'}", create_tables=True)
    with store:
        returned = Enriched(n=1).save()
        hydrated = Enriched.get(returned.id)
        print("save() returned n:", returned.n)
        print("persisted n:", hydrated.n)
    store.engine.dispose()


def probe_interceptor_output_is_not_revalidated(root: Path) -> None:
    heading("before_commit output can bypass validation and poison the log")

    class Poison(Interceptor):
        def before_commit(self, record):
            return record.model_copy(update={"n": "not-an-integer"})

    class Poisoned(Record, stream="review004_poisoned", interceptors=(Poison(),)):
        n: int

    store = Store(f"sqlite:///{root / 'poisoned.db'}", create_tables=True)
    with store:
        returned = Poisoned(n=1).save()
        with Session(store.engine) as session:
            persisted = session.scalar(
                select(LogRow.data).where(LogRow.id == returned.id)
            )
        print("persisted invalid state:", persisted)
        try:
            Poisoned.get(returned.id)
        except ValidationError as exc:
            print("hydration failure:", type(exc).__name__)
    store.engine.dispose()


def probe_computed_field_breaks_plain_pydantic_roundtrip(root: Path) -> None:
    heading("a standard Pydantic computed_field cannot round-trip")

    class Computed(Record, stream="review004_computed"):
        value: int

        @computed_field
        @property
        def doubled(self) -> int:
            return self.value * 2

    store = Store(f"sqlite:///{root / 'computed.db'}", create_tables=True)
    with store:
        record = Computed(value=2).save()
        with Session(store.engine) as session:
            persisted = session.scalar(
                select(LogRow.data).where(LogRow.id == record.id)
            )
        print("persisted state includes computed output:", persisted)
        try:
            Computed.get(record.id)
        except ValidationError as exc:
            print("hydration failure:", type(exc).__name__)
    store.engine.dispose()


def probe_inline_event_differs_from_durable_event(root: Path) -> None:
    heading("inline and durable handlers do not receive equivalent Events")

    class Symmetry(Record, stream="review004_symmetry"):
        value: int = 0

    inline_created_ts: list[object] = []
    durable_created_ts: list[object] = []

    @on_commit(Symmetry, via="inline")
    def inline_handler(event):
        inline_created_ts.append(event.record.created_ts)

    @on_commit(Symmetry, via="outbox", queue="symmetry")
    def durable_handler(event):
        durable_created_ts.append(event.record.created_ts)

    store = Store(f"sqlite:///{root / 'event-symmetry.db'}", create_tables=True)
    with store:
        Symmetry().save()
        from eventic import OutboxDispatcher

        OutboxDispatcher(store).drain()
    print("inline created_ts:", inline_created_ts)
    print("durable created_ts:", durable_created_ts)
    store.engine.dispose()


def probe_reads_ignore_current_unit_of_work(root: Path) -> None:
    heading("reads inside a UnitOfWork do not read their own writes")

    class Transactional(Record, stream="review004_transactional"):
        value: int = 0

    store = Store(f"sqlite:///{root / 'read-own-write.db'}", create_tables=True)
    with store:
        with store.unit_of_work():
            record = Transactional(value=1).save()
            try:
                Transactional.get(record.id)
            except RecordNotFound as exc:
                print("read-before-commit failure:", type(exc).__name__)
    store.engine.dispose()


def probe_cross_store_nested_uow_routes_to_wrong_database(root: Path) -> None:
    heading("nested UoW is not scoped to its Store")

    class Routed(Record, stream="review004_routed"):
        value: int

    first = Store(f"sqlite:///{root / 'first.db'}", create_tables=True)
    second = Store(f"sqlite:///{root / 'second.db'}", create_tables=True)
    with first:
        with first.unit_of_work():
            with second:
                Routed(value=9).save()
    print("first database counts:", counts(first))
    print("second database counts:", counts(second))
    first.engine.dispose()
    second.engine.dispose()


def probe_raw_delta_breaks_json_outbox(root: Path) -> None:
    heading("raw pre-validation delta breaks JSON outbox")

    class Timed(Record, stream="review004_timed"):
        at: dt.datetime

    @on_commit(Timed, kind="update", via="outbox", queue="time")
    def timed_handler(event):
        return None

    store = Store(f"sqlite:///{root / 'delta-json.db'}", create_tables=True)
    with store:
        base = Timed(at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)).save()
        try:
            base.update(at=dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc))
        except Exception as exc:
            print("update failure:", type(exc).__name__)
        print("database counts after failed update:", counts(store))
    store.engine.dispose()


def probe_subscription_order_is_reversed(root: Path) -> None:
    heading("subscription MRO order contradicts the documented order")

    class Base(Record, stream="review004_order_base"):
        value: int = 0

    class Derived(Base, stream="review004_order_derived"):
        pass

    seen: list[str] = []

    @on_commit(Base)
    def base_handler(event):
        seen.append("base")

    @on_commit(Derived)
    def derived_handler(event):
        seen.append("derived")

    store = Store(f"sqlite:///{root / 'order.db'}", create_tables=True)
    with store:
        Derived().save()
    print("observed order:", seen)
    store.engine.dispose()


def probe_duplicate_durable_subscription_aborts_write(root: Path) -> None:
    heading("one handler with two matching durable subscriptions aborts the write")

    class Duplicate(Record, stream="review004_duplicate"):
        value: int = 0

    @on_commit(Duplicate, kind="*", via="outbox", queue="all")
    @on_commit(Duplicate, kind="create", via="outbox", queue="creates")
    def duplicate_handler(event):
        return None

    store = Store(f"sqlite:///{root / 'duplicate.db'}", create_tables=True)
    with store:
        try:
            Duplicate().save()
        except Exception as exc:
            print("save failure:", type(exc).__name__)
        print("database counts:", counts(store))
    store.engine.dispose()


class WrappedSnapshot:
    requires = RowStore

    def encode(self, prev, new):
        state = new.model_dump(
            mode="json", exclude={"id", "version", "version_id", "created_ts"}
        )
        return {"payload": state}, True

    def decode(self, rows):
        return rows[-1].data["payload"]

    def window(self):
        return Window.POINT

    def iter_states(self, rows):
        for row in rows:
            yield row.data["payload"], row


def probe_rebuild_is_not_codec_agnostic_or_exact(root: Path) -> None:
    heading("head rebuild is neither codec-agnostic nor an exact rebuild")

    class Wrapped(Record, stream="review004_wrapped", codec=WrappedSnapshot()):
        value: int

    store = Store(f"sqlite:///{root / 'rebuild.db'}", create_tables=True)
    orphan_id = uuid.uuid4()
    with store:
        record = Wrapped(value=3).save()
        with Session(store.engine) as session:
            session.execute(
                update(HeadRow)
                .where(HeadRow.stream == "review004_wrapped", HeadRow.id == record.id)
                .values(state={"value": 999})
            )
            session.add(
                HeadRow(
                    stream="review004_wrapped",
                    id=orphan_id,
                    version=0,
                    version_id=uuid.uuid4(),
                    committed_at=now_utc(),
                    state={"value": -1},
                )
            )
            session.commit()
        rebuild_heads(store, stream="review004_wrapped")
        with Session(store.engine) as session:
            states = {
                row.id: row.state
                for row in session.scalars(
                    select(HeadRow).where(HeadRow.stream == "review004_wrapped")
                )
            }
        print("rebuilt real state:", states[record.id])
        print("orphan retained:", orphan_id in states)
        try:
            Wrapped.get(record.id)
        except ValidationError as exc:
            print("hydration after rebuild:", type(exc).__name__)
    store.engine.dispose()


def probe_sqlite_null_search_matches_missing_path(root: Path) -> None:
    heading("SQLite null search also matches missing JSON paths")

    class Searchable(Record, stream="review004_search"):
        name: str

    store = Store(f"sqlite:///{root / 'search.db'}", create_tables=True)
    with store:
        missing = Searchable(name="missing", meta={}).save()
        explicit = Searchable(name="explicit", meta={"flag": None}).save()
        found = Searchable.where(**{"meta.flag": None})
        print("matched names:", sorted(row.name for row in found))
        print("expected explicit id only:", explicit.id, "missing id:", missing.id)
    store.engine.dispose()


def probe_cli_drain_cannot_discover_application_declarations(root: Path) -> None:
    heading("standalone CLI drain cannot discover application declarations")

    class CliRecord(Record, stream="review004_cli"):
        value: int = 0

    @on_commit(CliRecord, via="outbox", queue="cli")
    def cli_handler(event):
        return None

    db = root / "cli.db"
    store = Store(f"sqlite:///{db}", create_tables=True)
    with store:
        CliRecord(value=1).save()
    result = subprocess.run(
        [sys.executable, "-m", "eventic.cli", "drain", "--url", f"sqlite:///{db}"],
        text=True,
        capture_output=True,
        check=False,
    )
    with Session(store.engine) as session:
        row = session.scalar(select(OutboxRow))
        retained = row is not None
        attempts = None if row is None else row.attempts
    print("CLI exit:", result.returncode)
    print("CLI stdout:", result.stdout.strip())
    print(
        "CLI stderr mentions unregistered stream:",
        "no Record class registered" in result.stderr,
    )
    print("row retained / attempts:", retained, attempts)
    store.engine.dispose()


def probe_store_reentrancy(root: Path) -> None:
    heading("the same Store is not reentrant")
    store = Store(f"sqlite:///{root / 'reentrant.db'}", create_tables=True)

    def run() -> None:
        try:
            with store:
                with store:
                    pass
        except Exception as exc:
            print("nested context failure:", type(exc).__name__, str(exc))

    contextvars.copy_context().run(run)
    store.engine.dispose()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="eventic-review-004-") as tmp:
        root = Path(tmp)
        probe_deep_mutability_breaks_log_head_equivalence(root)
        probe_unsaved_update_creates_broken_stream(root)
        probe_cross_stream_idempotency_is_silent_loss(root)
        probe_interceptor_return_is_not_returned(root)
        probe_interceptor_output_is_not_revalidated(root)
        probe_computed_field_breaks_plain_pydantic_roundtrip(root)
        probe_inline_event_differs_from_durable_event(root)
        probe_reads_ignore_current_unit_of_work(root)
        probe_cross_store_nested_uow_routes_to_wrong_database(root)
        probe_raw_delta_breaks_json_outbox(root)
        probe_subscription_order_is_reversed(root)
        probe_duplicate_durable_subscription_aborts_write(root)
        probe_rebuild_is_not_codec_agnostic_or_exact(root)
        probe_sqlite_null_search_matches_missing_path(root)
        probe_cli_drain_cannot_discover_application_declarations(root)
        probe_store_reentrancy(root)


if __name__ == "__main__":
    main()
