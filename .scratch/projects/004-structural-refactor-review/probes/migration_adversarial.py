"""Adversarial checks against the Alembic-created production schema."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from eventic import Delta, Record, Store, on_commit


def config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="eventic-review-004-migration-") as tmp:
        db = Path(tmp) / "migrated.db"
        url = f"sqlite:///{db}"
        cfg = config(url)
        command.upgrade(cfg, "head")
        try:
            command.check(cfg)
        except Exception as exc:
            print("alembic check:", type(exc).__name__, str(exc))

        class Durable(Record, stream="review004_migrated_outbox"):
            value: int = 0

        @on_commit(Durable, via="outbox", queue="migrated")
        def handler(event):
            return None

        store = Store(url, create_tables=False)
        with store:
            try:
                Durable(value=1).save()
            except Exception as exc:
                print("outbox insert on Alembic SQLite schema:", type(exc).__name__)

        inspector = inspect(store.engine)
        print("head indexes from Alembic:", inspector.get_indexes("eventic_head"))
        print(
            "outbox columns from Alembic:", inspector.get_columns("eventic_outbox")[:1]
        )

        class Deltic(Record, stream="review004_migration_delta", codec=Delta(k=20)):
            body: str

        with store:
            value = Deltic(body="v0").save().update(body="v1")
            print("delta version written:", value.version)
        store.engine.dispose()

        command.downgrade(cfg, "fold_properties_into_data")
        from sqlalchemy import create_engine

        engine = create_engine(url)
        with engine.connect() as connection:
            migrated_back = list(
                connection.execute(
                    text(
                        "SELECT version, data FROM records "
                        "WHERE class_type='review004_migration_delta' ORDER BY version"
                    )
                ).mappings()
            )
        print("downgraded delta payloads:")
        for row in migrated_back:
            payload = (
                json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
            )
            print(row["version"], payload)
        engine.dispose()


if __name__ == "__main__":
    main()
