import sys
sys.path.insert(0, 'src')
from eventic import Record, PropertiesBase, on

class Story(Record):
    title: str | None = None
    body: str | None = None
    @staticmethod
    def ping(x: int) -> int:
        return x

s = Story()
print("Story() ok; version:", s.version, "id:", s.id, "record_type:", s.properties.record_type)
print("Story.model_fields:", list(Story.model_fields))
print("_store:", Story._store)
print("queue_name:", getattr(Story, "_queue_name", None))
# staticmethod intact?
print("ping is staticmethod wrapper?", type(Story.__dict__.get("ping")), Story.ping(5) if not isinstance(Story.__dict__.get("ping"), property) else "n/a")
# Try mutation without store
try:
    s.title = "x"
    print("mutation ok")
except Exception as e:
    print("mutation FAILED:", type(e).__name__, str(e)[:100])
