# Writing a Store

The `Store` protocol is seven methods — `commit`, `head`, `revision`,
`history`, `search`, `claim`, `settle` — each one request in, one value out.
No callbacks, no generators, no session arguments, no returned open
transactions.

The spec is not the docstring: it is the published conformance suite,
`eventic.testing.conformance`. Run it against your backend:

```python
from eventic.testing.runner import run_all, summary

results = run_all(lambda: MyStore(url))
failed = [r for r in results if not r.passed]
assert not failed, summary(results)
```

## What the suite requires

- **CAS and replay** — `expected_revision` semantics, byte-identical replay as
  a silent no-op, loud conflicts otherwise.
- **Identity** — the same UUID in two streams is two aggregates.
- **Atomicity** — a batch with a mid-batch conflict writes nothing; an invalid
  intent aborts the whole commit.
- **Reads** — head, exact revision, paged history with cursors, `where`
  equality on top-level and dotted paths, missing-path distinct from JSON null.
- **Head integrity** — head digest equals log digest at every revision.
- **Intents** — staged in the same transaction; claim/lease/ack; retry;
  dead-letter; expired-lease reclaim.
- **Error translation** — every failure raises from the `eventic.errors`
  tree; no driver exception escapes.

## Capabilities

`Capabilities` describes behavior the suite tests, not marker attributes:
`outbox`, `json_paths`, `concurrent_drainers`, `max_batch`. Scenarios declare
the capabilities they require; the runner skips with a reason, never by
dialect name. If your dialect cannot express a semantic, set the flag `False`
and the suite skips by capability.

## Physical encoding never escapes

`StoredRevision.payload` is always the logical document. Encoding
(`snapshot/1`, `delta/1`) is chosen at the store and applied inside `commit`;
reads hand up decoded documents. The digest column is the content identity —
replay and verify compare digests, never a JSONB round trip.
