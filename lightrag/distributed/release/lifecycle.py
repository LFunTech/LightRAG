"""Instance-local drain counters; never cancel a writer or lock a workspace."""

import asyncio
from contextlib import asynccontextmanager


class InstanceLifecycle:
    def __init__(self):
        self.counts = {
            key: 0
            for key in (
                "http",
                "queries",
                "writers",
                "waiters",
                "backend",
                "background",
            )
        }
        self.retiring = False
        self.changed = asyncio.Event()

    @asynccontextmanager
    async def track(self, kind):
        if kind not in self.counts:
            raise ValueError("Unknown instance lifetime category")
        self.counts[kind] += 1
        self.changed.set()
        try:
            yield
        finally:
            self.counts[kind] -= 1
            self.changed.set()

    def begin_retire(self):
        self.retiring = True
        self.changed.set()

    def snapshot(self):
        return dict(self.counts)

    async def drain(self):
        if not self.retiring:
            raise RuntimeError("Instance retirement must be explicit")
        while any(self.counts.values()):
            self.changed.clear()
            await self.changed.wait()
