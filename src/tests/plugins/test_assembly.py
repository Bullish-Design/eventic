"""Plugin assembler tests (Step 6: PLUGINS §2,§3,§5 — fail-fast at definition)."""

import pytest

from eventic.connect import _reset, connect
from eventic.errors import MissingCapability, PluginConflictError
from eventic.plugins import (
    Seam,
    Plugin,
    _DELIVERY_INSTANCES,
    _DELIVERY_MODES,
    _reset_globals,
)
from eventic.plugins.codec import FullSnapshot
from eventic.plugins.interceptor import Interceptor, Veto
from eventic.plugins.persistence import SingleTableJSONB, TypedTable
from eventic.record import Record


@pytest.fixture(autouse=True)
def clean(tmp_path):
    """Fresh engine + pristine plugin state per test. The delivery registry is
    snapshot/restored (not cleared) so a backend registered by another suite
    at import time (e.g. ``durable`` from ``eventic.dbos``) survives."""
    _reset()
    _reset_globals()
    modes = dict(_DELIVERY_MODES)
    instances = dict(_DELIVERY_INSTANCES)
    connect(f"sqlite:///{tmp_path / 'e.db'}")
    yield
    _reset()
    _reset_globals()
    _DELIVERY_MODES.clear()
    _DELIVERY_MODES.update(modes)
    _DELIVERY_INSTANCES.clear()
    _DELIVERY_INSTANCES.update(instances)


class Doc(Record):
    title: str | None = None


# ---------------------------------------------------------------------- #
# exclusive seams — conflict at class definition
# ---------------------------------------------------------------------- #
def test_two_codecs_conflict_at_definition():
    class OtherCodec(Plugin):
        seam = Seam.CODEC
        provides = {"codec"}

        def encode(self, prev, new):
            return new.model_dump(mode="json")

        def decode(self, rows):
            return rows[-1].data

    with pytest.raises(PluginConflictError):

        class TwoCodecs(Record, FullSnapshot, OtherCodec):
            pass


def test_two_persistence_providers_conflict():
    class Persistence2(Plugin):
        seam = Seam.PERSISTENCE
        provides = {"persistence:json"}

    with pytest.raises(PluginConflictError):

        class TwoPersist(Record, SingleTableJSONB, Persistence2):
            pass


# ---------------------------------------------------------------------- #
# requires / provides — MissingCapability at definition
# ---------------------------------------------------------------------- #
def test_unmet_requires_raises_missing_capability():
    class NeedsJson(Plugin):
        seam = Seam.CODEC
        provides = {"codec"}
        requires = {"persistence:json"}

    # the JSON default persistence satisfies it
    class Ok(Record, NeedsJson):
        pass

    assert isinstance(Ok._codec, NeedsJson)

    class NeedsKafka(Plugin):
        seam = Seam.DELIVERY
        provides = {"delivery"}
        requires = {"kafka:client"}
        mode = "kafka"

    with pytest.raises(MissingCapability):

        class NoKafka(Record, NeedsKafka):
            pass


def test_typedtable_does_not_satisfy_json_requirement():
    """The guardrail: a chosen persistence REPLACES the default, so its
    capabilities stop counting (D7)."""
    class WantsJsonCodec(Plugin):
        seam = Seam.CODEC
        provides = {"codec"}
        requires = {"persistence:json"}

    with pytest.raises(MissingCapability):

        class TypedOnly(Record, TypedTable, WantsJsonCodec):
            pass


# ---------------------------------------------------------------------- #
# introspection + defaults
# ---------------------------------------------------------------------- #
def test_eventic_plugins_introspection():
    class Tagged(Plugin):
        seam = Seam.CODEC
        provides = {"codec"}

        def encode(self, prev, new):
            return new.model_dump(mode="json")

        def decode(self, rows):
            return rows[-1].data

    class Marked(Record, Tagged):
        pass

    assert Marked.__eventic_plugins__ == [Tagged]
    assert Doc.__eventic_plugins__ == []


def test_defaults_resolve_when_no_plugin_attached():
    assert isinstance(Doc._persistence, SingleTableJSONB)
    assert isinstance(Doc._codec, FullSnapshot)
    assert Doc._interceptors == []


def test_chosen_plugin_replaces_default():
    class Tagged(Plugin):
        seam = Seam.CODEC
        provides = {"codec"}

        def encode(self, prev, new):
            return new.model_dump(mode="json")

        def decode(self, rows):
            return rows[-1].data

    class Marked(Record, Tagged):
        pass

    assert isinstance(Marked._codec, Tagged)  # replaced, not doubled


# ---------------------------------------------------------------------- #
# delivery registry
# ---------------------------------------------------------------------- #
def test_delivery_plugin_registers_mode_and_delivers():
    delivered = []

    class FakeDelivery(Plugin):
        seam = Seam.DELIVERY
        provides = {"delivery"}
        mode = "fake"

        def deliver(self, event):
            delivered.append(event.kind)

    class Fancy(Record, FakeDelivery):
        pass

    d = Fancy(title="x").save()
    assert delivered == ["create"]  # the fake backend saw the event


def test_second_backend_for_same_mode_conflicts():
    class ModeA(Plugin):
        seam = Seam.DELIVERY
        mode = "dup"

        def deliver(self, event):
            pass

    class ModeB(Plugin):
        seam = Seam.DELIVERY
        mode = "dup"

        def deliver(self, event):
            pass

    class First(Record, ModeA):
        pass

    with pytest.raises(PluginConflictError):

        class Second(Record, ModeB):
            pass


# ---------------------------------------------------------------------- #
# interceptors
# ---------------------------------------------------------------------- #
def test_interceptor_priority_ordering_and_hooks():
    calls = []

    class Low(Interceptor):
        priority = 10

        def before_commit(self, record):
            calls.append("low.before")
            return record

        def after_commit(self, record):
            calls.append("low.after")

    class High(Interceptor):
        priority = 1

        def before_commit(self, record):
            calls.append("high.before")
            return record

        def after_commit(self, record):
            calls.append("high.after")

    class Watched(Record, High, Low):
        pass

    Watched(title="x").save()
    # before_commit outer→inner = priority order; after_commit reversed
    assert calls == ["high.before", "low.before", "low.after", "high.after"]


def test_before_commit_veto_aborts_write():
    class NoNasties(Interceptor):
        def before_commit(self, record):
            if getattr(record, "title", None) == "bad":
                raise Veto("no nasty titles")
            return record

    class Guarded(Record, NoNasties):
        pass

    Guarded(title="good").save()
    with pytest.raises(Veto):
        Guarded(title="bad").save()
    assert len(Guarded.where(title="good")) == 1


def test_after_hydrate_transforms_reads():
    class Upper(Interceptor):
        def after_hydrate(self, obj):
            if obj.title:
                obj = obj.model_copy(update={"title": obj.title.upper()})
            return obj

    class Loud(Record, Upper):
        pass

    Loud(title="hello").save()
    assert Loud.where(title="hello")[0].title == "HELLO"
    assert Loud.history(Loud.where(title="hello")[0].id)[0].title == "HELLO"
