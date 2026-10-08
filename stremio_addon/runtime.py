import asyncio
import time
from contextlib import suppress
from collections import deque

from .core import Settings, Store, Tokens
from .metadata import Metadata
from .telegram import Telegram
from .ai_search import AISearch
from .tmdb import TMDB


class Runtime:
    """Resources shared by the addon and Developer UI HTTP listeners."""

    def __init__(self, settings=None, gateway_factory=Telegram):
        self.settings = settings
        self.gateway_factory = gateway_factory
        self.lock = asyncio.Lock()
        self.users = 0
        self.started_at = None
        self.events = deque(maxlen=200)

    async def acquire(self):
        async with self.lock:
            if not self.users:
                self.cfg = self.settings or Settings.env()
                self.cfg.data.mkdir(parents=True, exist_ok=True)
                self.store = Store(self.cfg.data / 'index.sqlite3')
                self.tg = self.gateway_factory(self.cfg, self.store)
                self.metadata = Metadata()
                self.tmdb = TMDB(self.cfg, self.store, self.tg)
                self.metadata.tmdb = self.tmdb
                self.tmdb_task = None
                self.tokens = Tokens(self.cfg.key)
                self.ai = AISearch(self.cfg, self.store) if self.cfg.ai_search_enabled else None
                try:
                    await self.tg.start()
                except Exception:
                    if self.ai:
                        await self.ai.close()
                    await self.metadata.http.aclose()
                    await self.tmdb.http.aclose()
                    self.store.db.close()
                    raise
                self.started_at = time.time()
                if self.cfg.tmdb_api_key:
                    self.tmdb_task = asyncio.create_task(self.tmdb.run())
            self.users += 1
        return self

    async def release(self):
        async with self.lock:
            self.users -= 1
            if not self.users:
                if self.tmdb_task:
                    self.tmdb_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self.tmdb_task
                if self.ai:
                    await self.ai.close()
                await self.tg.close()
                await self.metadata.http.aclose()
                await self.tmdb.http.aclose()
                self.store.db.close()

    def record(self, event):
        self.events.appendleft(dict(timestamp=int(time.time()), **event))
