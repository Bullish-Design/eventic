# Benchmarks and complexity contracts

Regenerate with `python benchmarks/bench.py` (SQLite locally; the CI Postgres
matrix prints the same table against a live service).

## SQLite (2026-08-05, single process, tmpfs)

| Operation | ms/op |
|---|---|
| commit (snapshot/1) | 4.9 |
| point read at revision 99 | 0.58 |
| head read | 0.31 |
| history limit=100 | 26.9 |
| `where bucket=3` over 10⁴ heads | 2.8 |

## Complexity contracts

| API | Complexity | Bound |
|---|---|---|
| `create` / `change` / `replace` | one transaction, ~6 statements | `O(1)` statements; `O(doc)` serialization |
| `get(id)` (latest) | one indexed head lookup | `O(1)` rows |
| `get(id, revision=n)` | one window | ≤ `K + 1` rows for `delta/1` (`K = every`), `1` for `snapshot/1` |
| `history(id, limit=L)` | `L` decoded revisions | `O(L)` rows read, each bounded by `K` |
| `where(...)` | indexed head scan | paged, `limit` rows per page |
| `verify` / `heads rebuild` | chunked log stream, per-aggregate fold | `O(total rows)` I/O; peak memory ≈ one in-flight document + one chunk of rows, plus `O(aggregates)` key bookkeeping (heads to rebuild, orphan keys) |
| `worker` drain | claim + deliver + settle | `batch_size` intents per pass |

`history`, `where`, and `verify` are paged/chunked; `intents list` is paged
with an opaque cursor. The log fold in `verify` / `heads rebuild` finalises
each aggregate's document the moment its key changes, so it never holds one
document per aggregate — memory is bounded by the chunk size and the number
of aggregates' *keys*, never by `aggregates × document`.

## Notes

- `delta/1` trades commit cost for long-history storage: a delta commit writes
  one delta row; a point read reconstructs a bounded window.
- `committed_at` comes from the database clock; `verify` compares digests, so
  a JSONB numeric-normalization round trip can never fake a replay.
