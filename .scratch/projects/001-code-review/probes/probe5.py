# Check start_workflow registration error without DBOS - simulate evented flow
import sys
sys.path.insert(0, 'src')
from eventic.queues.dispatcher import evented

# callable without DBOS instance - Queue creation works?
try:
    deco = evented("queue_probe")
    @deco
    def meth(self, x):
        return x
    class Fake: pass
    f = Fake()
    try:
        print("calling wrapped method...")
        r = meth(f, 42)
        print("result:", r)
    except Exception as e:
        print("CALL RAISED:", type(e).__name__, str(e)[:120])
except Exception as e:
    print("DECORATOR CREATION FAILED:", type(e).__name__, str(e)[:200])
