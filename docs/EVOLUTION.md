# Schema evolution

## Versions and upcasters

Every revision row records `schema_version` and `meta_version`. A `Stream`
declaration carries `schema_version` (default 1) and an upcaster chain:

```python
from eventic import App, Stream
from eventic.evolution import make_upcaster

todos = Stream(
    TodoV2,
    name="todos",
    schema_version=2,
    upcasters={1: make_upcaster(1, 2, lambda tree: {**tree, "priority": "normal"})},
)
```

The chain must connect `1 → schema_version`; an incomplete chain is a
declaration error at `App`/`Stream` construction, never a read-time surprise.
Upcasters receive a JSON tree and return a JSON tree. They are deterministic
and have no clock, network, or context — a side-effecting upcaster is
impossible to write without lying about the protocol signature.

Every read path — `get`, `history`, `where`, and the worker's reconstruction —
upcasts before validation, so old rows read as current objects everywhere.

## The fingerprint ledger

`eventic_schema` records one `(stream, schema_version)` → fingerprint pair
(sha256 of the model's JSON schema). It is written on first commit. `eventic
schema check` compares declared fingerprints to stored ones:

- clean database: seeds the ledger, exits 0;
- model changed without a `schema_version` bump: drift, exits 3.

## Rolling upgrade

A v1 writer and a v2 reader can share one database: writes store
`schema_version=1` rows; the v2 reader upcasts them. Bump the version when the
shape changes; add an upcaster for every skipped version.
