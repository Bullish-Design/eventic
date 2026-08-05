"""hair_trigger — the explicitly-invariant-relaxing scripting escape hatch.

Off by default (safety is the default); on, it re-enables the old implicit
writes — documented as "scripts only; violates I2".
"""

import pytest

from eventic.connect import _reset, connect, engine
from eventic.record import Record


class Plain(Record):
    text: str = ""


class Scratch(Record, hair_trigger=True):
    text: str = ""


@pytest.fixture()
def db(tmp_path):
    _reset()
    connect(f"sqlite:///{tmp_path / 'e.db'}")
    yield
    _reset()


def test_hair_trigger_off_by_default(db):
    assert not hasattr(Plain, "_hair_trigger")
    assert hasattr(Scratch, "_hair_trigger")
    assert Scratch._hair_trigger is True


def test_hair_trigger_construction_does_not_write(db):
    """Even with hair_trigger on, constructing is still pure (no durable v0)."""
    s = Scratch(text="hello")
    assert s.version == 0
    assert len(Scratch.history(s.id)) == 0  # no rows yet


def test_hair_trigger_auto_persists_on_mutation(db):
    s = Scratch(text="hello").save()
    assert s.version == 0
    s.text = "world"  # implicit write (violates I2 — deliberate, documented)
    assert s.version == 1
    assert Scratch.get(s.id).text == "world"
    assert len(Scratch.history(s.id)) == 2


def test_plain_record_requires_explicit_save(db):
    p = Plain(text="hello")
    p.text = "world"  # no-op in-memory change (not frozen) — no write
    assert p.version == 0
    assert len(Plain.history(p.id)) == 0
