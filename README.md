# Eventic

[![PyPI version](https://img.shields.io/pypi/v/eventic?color=brightgreen)](https://pypi.org/project/eventic/) [![Python versions](https://img.shields.io/pypi/pyversions/eventic.svg)](https://pypi.org/project/eventic/) [![CI](https://img.shields.io/github/actions/workflow/status/Bullish-Design/eventic/ci.yml)](https://github.com/Bullish-Design/eventic/actions) ![License: MIT](https://img.shields.io/github/license/Bullish-Design/eventic.svg)

> **Pydantic, on a hair‑trigger.** 

Eventic turns plain **Pydantic v2** models into immutable, version‑tracked aggregates that persist to Postgres and ride on **DBOS** durable queues & workflows.

---

## ✨ Features at a glance

| What                      | Why it matters                                                                 | Where it lives                                                                 |
| ------------------------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------ |
| **Copy‑on‑write records** | Every attribute assignment creates a *new* immutable version row               | `Record.__setattr__` implements the copy‑on‑write dance fileciteturn2file11 |
| **Single‑table storage**  | All versions sit in one `records` table with JSONB for ad‑hoc queries          | See `RecordRow` SQLAlchemy model fileciteturn1file18                        |
| **Per‑class queues**      | Each `Record` subclass gets its own DBOS queue; methods run now *and* later    | `eventic.queues.dispatcher.evented` decorator fileciteturn2file1            |
| **FastAPI one‑liner**     | `Eventic.create_app()` wires FastAPI + DBOS, auto‑launching workers on startup | Runtime helper fileciteturn2file10                                          |
| **Script‑friendly**       | Call `Eventic.launch()` once to spin up the DBOS worker threads                | Example usage fileciteturn2file6                                            |
| **Free‑form properties**  | JSONB property bag with `add / remove / list` helpers                          |                                                                                |
| **MIT‑licensed**          | Commercial & OSS friendly                                                      |                                                                                |

---

## 🚀 Quick start

1. **Install**

```bash
pip install "eventic[pg]"
```

2. **Create `.env`** (or export in your shell):

```bash
export DBOS_DATABASE_URL="postgresql://user:pass@localhost/eventic_demo"
```

3. **Run the canned demo**

```bash
python -m eventic.examples.demo
```

The demo spins up a FastAPI app, creates a `Story` record, walks it through several versions, and prints the final JSON snapshot.

---

## 🏗️ A minimal script (no web server)

```python
import os, uuid
from eventic import Eventic, Record

class Todo(Record):
    text: str

# 1️⃣  One‑time init (connects to Postgres & injects RecordStore)
Eventic.init(name="todo‑svc", database_url=os.getenv("DBOS_DATABASE_URL"))

# 2️⃣  Start DBOS worker threads
Eventic.launch()  # mandatory in scripts fileciteturn2file6

# 3️⃣  Use your record like any Pydantic model
item = Todo(text="Learn Eventic")
print(item.version)      # 0
item.text = "Ship v1"    # copy‑on‑write ➜ version 1 row inserted
print(item.version)      # 1
```

> **Why call `launch()`?** In a plain Python process you must kick off the background DBOS executor yourself. If you build a FastAPI app with `Eventic.create_app`, the executor is started automatically on the ASGI *startup* event fileciteturn2file10.

---

## 🖥️ Full FastAPI example (taken from `examples/demo.py`)

```python
import os, uuid
from pprint import pprint
from eventic import Record, Eventic

# ── 1. FastAPI + Eventic bootstrap ──────────────────────────────
app = Eventic.create_app("eventic-demo", db_url=os.getenv("DBOS_DATABASE_URL"))

# ── 2. Domain model ────────────────────────────────────────────
class Story(Record):
    title: str | None = None
    body: str | None = None

# Auto‑generated queue for heavy jobs
story_q = Eventic.queue(Story._queue_name)

# ── 3. DBOS steps ------------------------------------------------
@Eventic.transaction()
def create_story() -> uuid.UUID:
    return Story().id  # version 0 row

@Eventic.transaction()
def draft(sid: uuid.UUID, text: str):
    Story.hydrate(sid).body = text

@Eventic.step()
def snapshot(sid: uuid.UUID):
    pprint(Story.hydrate(sid).model_dump())

# ── 4. Durable workflow exposed at GET / ────────────────────────
@app.get("/")
@Eventic.workflow()
def demo_flow():
    sid = create_story()
    story_q.enqueue(draft, sid, "Once upon a time …")
    story_q.enqueue(snapshot, sid)
    return {"id": str(sid)}

# ── 5. Script entry‑point ───────────────────────────────────────
if __name__ == "__main__":
    Eventic.launch()            # run DBOS workers in this process
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

---

## ⚙️ How it works (deeper dive)

### Copy‑on‑write field mutation

Every `Record` is **frozen**; assigning to a public field actually constructs a *new* model with `version += 1`, writes it to Postgres via the injected `RecordStore`, and then mutates the in‑memory object so your code continues naturally fileciteturn2file11.

### Storage layout

```
records
┌────────────┬────────────┬────────┬──────────────┬───────────┐
│ version_id │ id         │ version│ class_type   │ data JSON │
└────────────┴────────────┴────────┴──────────────┴───────────┘
```

* `version_id` is the immutable primary key.
* `id` is the stable aggregate identifier you pass around.
* `data` holds the full model including `properties` for easy hydration fileciteturn1file18.

### Per‑class DBOS queues

The `RecordMeta` metaclass attaches `_queue_name = "queue_<snake_case>"` and wraps every **public** method with the `evented` decorator so calls run **synchronously** *and* re‑enqueue themselves for background execution fileciteturn2file1.

---

## 🛠️ Configuration & deployment tips

| Concern             | Script                                                                           | FastAPI                                           |
| ------------------- | -------------------------------------------------------------------------------- | ------------------------------------------------- |
| Connect Eventic     | `Eventic.init(name, database_url)`                                               | `Eventic.create_app(name, db_url)`                |
| Start DBOS workers  | `Eventic.launch()` (once)                                                        | automatic via ASGI startup fileciteturn2file10 |
| Production workers  | Run `dbos-runner` instead of in‑process `launch()`                               |                                                   |
| Database migrations | Eventic creates the `records` table at first run; use Alembic for further schema |                                                   |

---

## 🤝 Contributing

Bug reports and pull requests are welcome! Please run `pre‑commit run --all-files` and make sure `pytest` passes against Postgres 14+ before opening a PR.

---

## 📜 License

Eventic is released under the **MIT License**.

