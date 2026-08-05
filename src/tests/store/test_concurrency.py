"""I5 under concurrency — probe_06, the canary.

Eight threads race one ``(id, version)``: exactly **1 winner, 7 loud
``StaleVersionError``s**. This behavior is the one thing v2 got unambiguously
right and every exit gate must keep it. ContextVars do not propagate to new
threads (that is the point — I8), so each thread binds its own ``Store``.
"""

import threading

from eventic import Record, StaleVersionError, Store


class Counter(Record, stream="conc_counter"):
    n: int = 0


def test_one_winner_seven_loud_losers(tmp_path):
    url = f"sqlite:///{tmp_path / 'c.db'}"
    with Store(url, create_tables=True):
        base = Counter(n=0).save()
        oks, errs = [], []

        def w(v):
            with Store(url, create_tables=False):
                try:
                    base.update(n=v)
                    oks.append(v)
                except StaleVersionError:
                    errs.append(v)
                except Exception as e:  # a lock timeout is a loud failure too
                    errs.append(type(e).__name__)

        ts = [threading.Thread(target=w, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]

    assert len(oks) == 1
    assert len(errs) == 7
    with Store(url, create_tables=False):
        assert len(Counter.history(base.id)) == 2  # v0 + exactly one winner
        # the loser's write must not have leaked any state
        assert Counter.get(base.id).n == oks[0]
