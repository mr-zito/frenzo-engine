"""The editing queue: best moment first (not first come, first served)."""
import threading
import time


class MomentQueue:
    """Like queue.Queue, but get() returns the waiting moment with the highest `priority` (ties: oldest first).
    A moment's priority can change while it waits (viewer clips arriving, clips getting views fast)."""

    def __init__(self, best_first=True):
        self.items = []
        self.cond = threading.Condition()
        self.best_first = best_first
        self._n = 0

    def put(self, cand):
        with self.cond:
            self._n += 1
            cand._qorder = self._n
            self.items.append(cand)
            self.cond.notify()

    def _pick(self):
        if not self.best_first:
            return 0
        # among moments that are ready (enough recorded after them), the best; else the soonest ready
        now = time.time()
        ready = [i for i, c in enumerate(self.items) if getattr(c, "ready_at", 0) <= now]
        pool = ready or range(len(self.items))
        if ready:
            return max(pool, key=lambda i: (getattr(self.items[i], "priority", 0.0), -self.items[i]._qorder))
        return min(pool, key=lambda i: getattr(self.items[i], "ready_at", 0))

    def get(self):
        with self.cond:
            while not self.items:
                self.cond.wait()
            return self.items.pop(self._pick())

    def remove(self, cand):
        with self.cond:
            if cand in self.items:
                self.items.remove(cand)
                return True
        return False

    def waiting(self):
        with self.cond:
            return list(self.items)

    def qsize(self):
        with self.cond:
            return len(self.items)
