"""Short-lived, retryable pagination sessions used only by the developer UI."""
import asyncio
import secrets
import time
import logging
from collections import deque
from dataclasses import dataclass, field

from .telegram import DevSearchCooldown
from .core import search_expression

logger = logging.getLogger(__name__)


@dataclass
class Session:
    query: str
    pending: deque
    telegram: bool
    cursor: dict | None = None
    seen: set = field(default_factory=set)
    step: int = 0
    touched: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    previous: tuple | None = None


class DevSearch:
    ttl = 30 * 60
    max_sessions = 16

    def __init__(self, store, gateway):
        self.store, self.gateway = store, gateway
        self.sessions = {}

    async def start(self, query, rows=None):
        now = time.monotonic()
        self.sessions = {k: s for k, s in self.sessions.items()
                         if s.lock.locked() or now - s.touched < self.ttl}
        if len(self.sessions) >= self.max_sessions:
            idle = [k for k, s in self.sessions.items() if not s.lock.locked()]
            if not idle:
                raise RuntimeError('Developer searches are busy; retry shortly')
            del self.sessions[min(idle, key=lambda k: self.sessions[k].touched)]
        live = rows is None and bool(query.strip()) and hasattr(self.gateway, 'dev_search_page')
        allowed = set(self.gateway.channels)
        if rows is None:
            # Snapshot only IDs: new index entries cannot shift later pages, and
            # large captions are not retained for the lifetime of the search.
            def indexed_ids():
                expression = search_expression(query)
                if not expression:
                    return []
                return [r['id'] for r in self.store.db.execute(
                    'SELECT v.id,v.channel FROM videos v JOIN search s ON s.id=v.id '
                    'WHERE s.text MATCH ? ORDER BY v.date DESC,v.id', (expression,))
                        if r['channel'] in allowed]
            identifiers = await self.store.run(indexed_ids)
        else:
            identifiers = [r['id'] for r in rows if r['channel'] in allowed]
        pending = deque((identifier, 'full-text index') for identifier in identifiers)
        token = secrets.token_urlsafe(24)
        self.sessions[token] = Session(query, pending, live)
        return token + ':0'

    async def page(self, token):
        try:
            key, raw_step = token.rsplit(':', 1)
            step = int(raw_step)
            session = self.sessions[key]
        except (ValueError, KeyError):
            raise KeyError('Search expired; run the search again') from None
        async with session.lock:
            if time.monotonic() - session.touched >= self.ttl:
                raise KeyError('Search expired; run the search again')
            session.touched = time.monotonic()
            if session.previous and step == session.previous[0]:
                return session.previous[1]
            if step != session.step:
                raise KeyError('Search page is no longer available; run the search again')
            warning, retry_after = None, None
            # At most one Telegram RPC per HTTP request. Sparse/non-video pages
            # may return fewer than 100 rows with a continuation token.
            if session.telegram and (step == 0 or len(session.pending) < 100):
                try:
                    ids, cursor = await asyncio.wait_for(
                        self.gateway.dev_search_page(session.query, session.cursor), 20)
                    known = session.seen | {i for i, _ in session.pending}
                    for identifier in ids:
                        if identifier not in known:
                            session.pending.append((identifier, 'Telegram live search'))
                            known.add(identifier)
                    session.cursor = cursor
                    session.telegram = cursor is not None
                except DevSearchCooldown as exc:
                    warning, retry_after = str(exc), exc.seconds
                except TimeoutError:
                    warning = 'Telegram search timed out; loaded results were kept. Use Load more to retry.'
                except Exception as exc:
                    logger.warning('Developer UI Telegram page failed: error=%s', type(exc).__name__)
                    warning = 'Telegram search failed; loaded results were kept. Use Load more to retry.'

            def collect():
                results = []
                while session.pending and len(results) < 100:
                    identifier, reason = session.pending.popleft()
                    if identifier in session.seen:
                        continue
                    row = self.store.get(identifier)
                    session.seen.add(identifier)
                    if row and row['channel'] in self.gateway.channels:
                        results.append((row, reason))
                return results

            results = await self.store.run(collect)
            session.step += 1
            more = bool(session.pending) or session.telegram
            response = {'matched': results, 'has_more': more,
                        'cursor': key + ':' + str(session.step) if more else None,
                        'warning': warning, 'retry_after': retry_after}
            session.previous = (step, response)
            session.touched = time.monotonic()
            return response
