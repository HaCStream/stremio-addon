"""Serialized disk cache with one directory scan per process, off the event loop."""
import asyncio
import os
from collections import OrderedDict
from uuid import uuid4


class ChunkCache:
    def __init__(self, directory, limit):
        self.directory = directory
        self.limit = limit
        self.entries = None
        self.total = 0
        self.lock = asyncio.Lock()

    def _load(self):
        if self.entries is not None:
            return
        files = []
        for path in self.directory.iterdir():
            if path.name.startswith('.'):
                path.unlink(missing_ok=True)
                continue
            stat = path.stat()
            files.append((stat.st_mtime_ns, path.name, stat.st_size))
        self.entries = OrderedDict((key, size) for _, key, size in sorted(files))
        self.total = sum(self.entries.values())
        self._evict(0)

    def _evict(self, required):
        while self.entries and self.total + required > self.limit:
            key, size = self.entries.popitem(last=False)
            (self.directory / key).unlink(missing_ok=True)
            self.total -= size

    def _get(self, key):
        self._load()
        if key not in self.entries:
            return None
        path = self.directory / key
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self.total -= self.entries.pop(key)
            return None
        self.entries.move_to_end(key)
        os.utime(path, None)
        return data

    def _put(self, key, data):
        if len(data) > self.limit:
            return
        self._load()
        if key in self.entries:
            self.total -= self.entries.pop(key)
        self._evict(len(data))
        temporary = self.directory / ('.' + uuid4().hex)
        try:
            temporary.write_bytes(data)
            temporary.replace(self.directory / key)
        finally:
            temporary.unlink(missing_ok=True)
        self.entries[key] = len(data)
        self.total += len(data)

    async def _run(self, function, *args):
        async with self.lock:
            task = asyncio.create_task(asyncio.to_thread(function, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Keep the lock until disk I/O finishes, even on player disconnect.
                await asyncio.shield(task)
                raise

    async def get(self, key):
        return await self._run(self._get, key)

    async def put(self, key, data):
        await self._run(self._put, key, data)
