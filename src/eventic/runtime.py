"""
eventic.runtime  ──  A thin façade so developers can use @Eventic.step()
without importing dbos.DBOS directly.

Usage pattern in user code
--------------------------
    from eventic.runtime import Eventic

    app = Eventic.create_app("eventic-demo", db_url="postgresql://...")

    @Eventic.transaction()
    def create_story(): ...

    if __name__ == "__main__":
        Eventic.launch()
"""

from __future__ import annotations
from typing import Any, ClassVar, Optional

from fastapi import FastAPI
from dbos import DBOS  # the only direct dbos import
from .bootstrap import init_eventic


from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


class Eventic(DBOS):  # ① inherit all decorators & queue API
    """
    Drop-in replacement for DBOS in downstream code.  We keep a private
    singleton instance so developers don’t have to juggle multiple
    DBOS/Eventic objects in different modules.
    """

    _singleton: ClassVar[Optional["Eventic"]] = None
    _engine: ClassVar[Optional[Engine]] = None

    # ---------- one-shot initialiser ----------
    @classmethod
    def init(
        cls,
        *,
        name: str,
        database_url: str,
        fastapi: Optional[FastAPI] = None,
        **extra_cfg: Any,
    ) -> "Eventic":
        if cls._singleton is None:
            cfg = {"name": name, "database_url": database_url, **extra_cfg}
            cls._singleton = cls(config=cfg, fastapi=fastapi)
            cls._engine = create_engine(database_url, pool_pre_ping=True, future=True)

            init_eventic(cls._engine)  # auto-wire RecordStores
        return cls._singleton

    # ---------- convenience helpers ----------
    @classmethod
    def instance(cls) -> "Eventic":
        if cls._singleton is None:
            raise RuntimeError("Eventic.init() has not been called")
        return cls._singleton

    @classmethod
    def create_app(
        cls,
        name: str,
        *,
        db_url: str,
        **fastapi_kwargs: Any,
    ) -> FastAPI:
        """
        One-liner for web apps:
            app = Eventic.create_app("svc-name", db_url=URL)
        """
        app = FastAPI(**fastapi_kwargs)
        cls.init(name=name, database_url=db_url, fastapi=app)
        # Passing fastapi=app means DBOS will call .launch() automatically
        # during the ASGI "startup" event.
        return app


## Optional: re-export Eventic at package level so
## `from eventic import Eventic` also works.
# import sys

# sys.modules.setdefault("eventic").Eventic = Eventic
