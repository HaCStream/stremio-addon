import asyncio
import hashlib
import logging
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4
from telethon import TelegramClient, events, errors, types, utils, functions
from telethon.sessions import StringSession
from .core import parse_title, source_name, normalize, title_from_text
from .chunk_cache import ChunkCache

logger = logging.getLogger("stremio_addon.telegram")

CHUNK = 512 * 1024

# Scope pacing to discovery/search tasks; playback uses the same client without this gate.
_background_pacer = ContextVar('telegram_background_pacer', default=None)


class BackgroundClient(TelegramClient):
    async def __call__(self, *args, **kwargs):
        request = super().__call__
        pacer = _background_pacer.get()
        if pacer is not None:
            return await pacer(request, *args, **kwargs)
        return await request(*args, **kwargs)


class Telegram:
    def __init__(self, settings, store):
        self.store = store
        self.channel_ids = settings.channel_ids
        # Handle background flood waits explicitly and retain the cooldown across chat refreshes.
        self.client = BackgroundClient(StringSession(settings.session), settings.api_id, settings.api_hash, flood_sleep_threshold=0)
        self.channels = {}
        self.known_channels = None
        self.discovery_complete = False
        self.status = {'phase': 'connecting', 'last_error': None}
        self.cache = settings.data / 'chunks'
        self.cache.mkdir(exist_ok=True)
        self.cache_limit = max(0, settings.cache_bytes)
        self.download_slots = asyncio.Semaphore(4)
        self.chunk_cache = ChunkCache(self.cache, self.cache_limit)
        self.refresh_status = {"id": None, "state": "idle", "error": None}
        self.last_discovery = 0
        self.task = None
        self.active_playbacks = 0
        self.playback_until = 0
        self.playback_idle = asyncio.Event()
        self.playback_idle.set()
        self.scan_request_lock = asyncio.Lock()
        self.next_scan_request = 0
        self.flood_until = 0
        self.scan_request_interval = 1.0
        self.scan_request_successes = 0
        self.search_cache = {}
        self.search_tasks = {}
        self.search_timeout = 12
        self.search_ttl = 60

    @contextmanager
    def playback(self):
        self.active_playbacks += 1
        self.playback_idle.clear()
        try:
            yield
        finally:
            self.active_playbacks -= 1
            # Players often close one range request before opening the next.
            self.playback_until = time.monotonic() + 2
            if not self.active_playbacks:
                self.playback_idle.set()

    async def wait_background(self):
        """Pause background work throughout playback, including range-request gaps."""
        while True:
            await self.playback_idle.wait()
            delay = self.playback_until - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif not self.active_playbacks:
                return

    async def background_operation(self, function, *args, **kwargs):
        """Pace each RPC inside a high-level Telethon operation, including pagination."""
        await self.wait_background()
        token = _background_pacer.set(self.background_request)
        try:
            return await function(*args, **kwargs)
        finally:
            _background_pacer.reset(token)

    async def background_request(self, function, *args, **kwargs):
        while True:
            async with self.scan_request_lock:
                while True:
                    await self.wait_background()
                    delay = max(self.next_scan_request, self.flood_until) - time.monotonic()
                    if delay <= 0:
                        break
                    await asyncio.sleep(delay)
                try:
                    result = await function(*args, **kwargs)
                except errors.FloodWaitError as exc:
                    # Slow future requests too, rather than repeatedly hitting the same limit.
                    self.scan_request_interval = min(5.0, self.scan_request_interval * 2)
                    self.scan_request_successes = 0
                    self.flood_until = max(self.flood_until, time.monotonic() + exc.seconds + 1)
                    self.status.update(phase='rate_limited', last_error='Telegram flood wait',
                                       retry_after=exc.seconds + 1)
                    operation = type(args[0]).__name__ if args else 'unknown'
                    logger.warning('Telegram background requests paused: retry_after_seconds=%s '
                                   'request=%s request_interval_seconds=%.2f',
                                   exc.seconds + 1, operation, self.scan_request_interval)
                else:
                    self.scan_request_successes += 1
                    # Recover gradually only after a sustained run without flood waits.
                    if self.scan_request_successes >= 20:
                        self.scan_request_interval = max(1.0, self.scan_request_interval - .25)
                        self.scan_request_successes = 0
                    if self.status['phase'] == 'rate_limited':
                        self.status.update(last_error=None)
                        self.status.pop('retry_after', None)
                        self.status.update(phase='ready' if self.discovery_complete else 'discovering')
                    return result
                finally:
                    # Space actual RPCs, including ones hidden within get_messages/iter_dialogs.
                    self.next_scan_request = time.monotonic() + self.scan_request_interval

    async def start(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise RuntimeError('Telegram session is unauthorized; generate a new Telethon session')
        me = await self.client.get_me()
        if me.bot:
            await self.client.disconnect()
            raise RuntimeError('A Telegram user session is required, not a bot')
        self.me = me
        self.client.add_event_handler(self.update, events.MessageEdited())
        self.client.add_event_handler(self.deleted, events.MessageDeleted())
        self.request_refresh()

    async def close(self):
        for task in self.search_tasks.values():
            task.cancel()
        await asyncio.gather(*self.search_tasks.values(), return_exceptions=True)
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        await self.client.disconnect()

    def request_refresh(self):
        """Refresh chat identities only; never fetch message history."""
        if self.task and not self.task.done():
            return dict(self.refresh_status)
        self.refresh_status = {'id': uuid4().hex, 'state': 'queued', 'error': None}
        self.task = asyncio.create_task(self.refresh_sources())
        return dict(self.refresh_status)

    def ensure_sources(self):
        # Refresh source identities on demand, at most once every five minutes.
        if not self.discovery_complete or time.monotonic() - self.last_discovery >= 300:
            self.request_refresh()

    async def refresh_sources(self):
        self.refresh_status.update(state='running')
        self.status.update(phase='discovering')
        try:
            await self.discover()
            self.last_discovery = time.monotonic()
            self.refresh_status.update(state='done')
            self.status.update(phase='ready', last_error=None)
            logger.info('Chat discovery complete: chats=%s; search on demand', len(self.channels))
        except asyncio.CancelledError:
            self.refresh_status.update(state='failed', error='Chat refresh interrupted')
            raise
        except Exception as exc:
            self.refresh_status.update(state='failed', error=type(exc).__name__)
            self.status.update(phase='discovery_failed', last_error=type(exc).__name__)
            logger.warning('Chat discovery failed: error=%s', type(exc).__name__)

    async def discover(self):
        self.discovery_complete = False
        channels = {}
        dialogs = self.client.iter_dialogs().__aiter__()
        while True:
            await self.wait_background()
            try:
                dialog = await self.background_operation(anext, dialogs)
            except StopAsyncIteration:
                break
            entity = dialog.entity
            is_channel = (isinstance(entity, types.Channel)
                          and (entity.broadcast or entity.megagroup or entity.gigagroup)
                          and not entity.left)
            is_group = (isinstance(entity, types.Chat) and not entity.left
                        and not entity.deactivated and not entity.migrated_to)
            if is_channel or is_group or (isinstance(entity, types.User) and not entity.deleted):
                channel_id = utils.get_peer_id(entity)
                if self.channel_ids is None or channel_id in self.channel_ids:
                    channels[channel_id] = entity
                    self.channels[channel_id] = entity
        me = getattr(self, 'me', None)
        if me is not None and self.permitted(me):
            channels[utils.get_peer_id(me)] = me
        self.channels = channels
        if self.known_channels is None:
            self.known_channels = await self.store.run(self.store.stored_channels)
        removed = self.known_channels - channels.keys()
        if removed:
            logger.info('Removing unavailable channels from index: channels=%s', len(removed))
            await self.store.prune_channels(removed, wait_background=self.wait_background)
        self.known_channels = set(channels)
        self.discovery_complete = True

    def index(self, channel, message):
        item = f'tg:{channel}:{message.id}'
        doc = message.document
        file = message.file
        if not doc or not file or not ((doc.mime_type or '').startswith('video/') or any(isinstance(a, types.DocumentAttributeVideo) for a in doc.attributes) or Path(file.name or '').suffix.lower() in ('.mkv', '.mp4', '.avi', '.mov', '.webm', '.m4v', '.ts')):
            self.store.delete(item)
            return
        caption = message.message or ''
        filename = file.name or ''
        row = dict(id=item, channel=channel, message=message.id, filename=filename, caption=caption,
                   channel_name=source_name(self.channels[channel], channel), size=doc.size, mime=doc.mime_type or 'application/octet-stream', date=int(message.date.timestamp()), file_id=str(doc.id))
        entity = self.channels[channel]
        parsed = parse_title(filename, caption)
        context = getattr(entity, 'title', '') or ''
        if parsed['season'] is not None and context and not title_from_text(parsed['title']):
            parsed['title'] = title_from_text(context) or parsed['title']
            previous = self.store.get(item)
            if previous and previous['series_id'] and self.store.db.execute(
                    "SELECT 1 FROM tmdb_series WHERE series_id=? AND status IN ('auto','manual','blocked')", (previous['series_id'],)).fetchone():
                # A contextual title must not replace a saved show identity on
                # reconciliation, playback refresh or a live-search upsert.
                parsed['title'] = previous['title']
        row.update(parsed)
        self.store.upsert(row)
        return row['id']

    async def update(self, event):
        # Maintain already discovered videos; do not collect every new upload.
        if self.channel_ids is not None and event.chat_id not in self.channel_ids:
            return
        item = f'tg:{event.chat_id}:{event.message.id}'
        if not await self.store.run(self.store.get, item):
            return
        await self.wait_background()
        if event.chat_id in self.channels:
            await self.store.run(self.index, event.chat_id, event.message)

    async def deleted(self, event):
        await self.wait_background()
        if event.chat_id in self.channels:
            for message in event.deleted_ids:
                await self.store.run(self.store.delete, f'tg:{event.chat_id}:{message}')
        elif event.chat_id is None:
            # Non-channel message IDs share the account-wide namespace. Telegram
            # omits their chat ID in UpdateDeleteMessages (including private chats).
            def remove():
                with self.store.transaction():
                    for message in event.deleted_ids:
                        for row in self.store.db.execute(
                                'SELECT id FROM videos WHERE message=? AND channel>?',
                                (message, -1000000000000)).fetchall():
                            self.store.delete(row['id'])
            await self.store.run(remove)

    def permitted(self, entity):
        if isinstance(entity, types.Channel):
            accessible = not entity.left and not entity.min
        elif isinstance(entity, types.Chat):
            accessible = not entity.left and not entity.deactivated and not entity.migrated_to
        else:
            accessible = isinstance(entity, types.User) and not entity.deleted
        return accessible and (self.channel_ids is None or utils.get_peer_id(entity) in self.channel_ids)

    async def search(self, query, channel=None):
        """Bounded, coalesced live search; cache successful queries for 60 seconds.

        Every RPC uses the scanner's pacing/cooldown and yields to playback.
        Search results populate the local video cache.
        """
        if not query.strip() and channel is None:
            return []
        if channel is not None and (channel not in self.channels or
                                   (self.channel_ids is not None and channel not in self.channel_ids)):
            return []
        key = (query, channel)
        now = time.monotonic()
        self.search_cache = {k: v for k, v in self.search_cache.items() if v[0] > now}
        cached = self.search_cache.get(key)
        if cached:
            return cached[1]
        task = self.search_tasks.get(key)
        if task is None:
            if len(self.search_tasks) >= 16:
                raise RuntimeError('Telegram search busy')
            task = asyncio.create_task(self._cached_search(key))
            self.search_tasks[key] = task
            # Retrieve exceptions even if every waiting HTTP request disconnects.
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return await asyncio.shield(task)

    async def _cached_search(self, key):
        try:
            ids = await asyncio.wait_for(self._search(*key), self.search_timeout)
            if len(self.search_cache) >= 256:
                self.search_cache.pop(next(iter(self.search_cache)))
            self.search_cache[key] = (time.monotonic() + self.search_ttl, ids)
            logger.debug('Telegram live search completed: results=%s scoped=%s', len(ids), key[1] is not None)
            return ids
        finally:
            self.search_tasks.pop(key, None)

    async def _search(self, query, channel):
        ids = []
        if channel is not None:
            # A matching group name supplies context for episode-only videos.
            messages = await self.background_operation(
                self.client.get_messages, self.channels[channel], search=query, limit=100)
            for message in messages:
                if isinstance(message, types.Message):
                    item = await self.store.run(self.index, channel, message)
                    if item:
                        ids.append(item)
            return ids
        if self.channel_ids is not None:
            if not self.discovery_complete and self.task:
                await asyncio.shield(self.task)
            sources = [c for c in sorted(self.channels) if c in self.channel_ids]
            if not sources:
                raise RuntimeError('Selected chats have not been discovered yet')
            for source in sources:
                ids.extend(await self._search(query, source))
            return list(dict.fromkeys(ids))
        offset_rate, offset_id, offset_peer = 0, 0, types.InputPeerEmpty()
        cursors = set()
        for _ in range(3):
            response = await self.background_operation(self.client, functions.messages.SearchGlobalRequest(
                q=query, filter=types.InputMessagesFilterEmpty(),
                min_date=None,
                max_date=None, offset_rate=offset_rate, offset_peer=offset_peer,
                offset_id=offset_id, limit=100))
            entities = {utils.get_peer_id(e): e for e in [*getattr(response, 'chats', []),
                                                         *getattr(response, 'users', [])]
                        if self.permitted(e)}
            messages = [m for m in getattr(response, 'messages', []) if isinstance(m, types.Message)]
            for message in messages:
                peer = utils.get_peer_id(message.peer_id)
                entity = entities.get(peer)
                if entity is None:
                    continue
                self.channels[peer] = entity
                item = await self.store.run(self.index, peer, message)
                if item and item not in ids:
                    ids.append(item)
                if len(ids) >= 100:
                    return ids
            if isinstance(response, types.messages.Messages) or len(getattr(response, 'messages', [])) < 100 or not messages:
                break
            last = messages[-1]
            peer = utils.get_peer_id(last.peer_id)
            entity = entities.get(peer)
            if entity is None:
                break
            offset_rate = getattr(response, 'next_rate', None) or int(last.date.timestamp())
            offset_id = last.id
            cursor = (peer, offset_id, offset_rate)
            if cursor in cursors:
                break
            cursors.add(cursor)
            offset_peer = utils.get_input_peer(entity)
        return ids

    def series_sources(self, aliases):
        names = {normalize(title_from_text(alias)) for alias in aliases if normalize(title_from_text(alias))}
        return [channel for channel, entity in self.channels.items()
                if getattr(entity, 'title', None)
                and normalize(title_from_text(entity.title)) in names][:3]

    async def message(self, row, *, priority=False):
        if priority:
            with self.playback():
                return await self.message(row)
        entity = self.channels.get(row['channel'])
        if not entity:
            raise FileNotFoundError()
        try:
            message = await self.client.get_messages(entity, ids=row['message'])
        except (errors.ChannelPrivateError, errors.ChannelInvalidError):
            await self.store.run(self.store.delete, row['id'])
            raise FileNotFoundError() from None
        if not message or not message.document:
            await self.store.run(self.store.delete, row['id'])
            raise FileNotFoundError()
        await self.store.run(self.index, row['channel'], message)
        return message

    async def chunk(self, row, message, offset):
        # Includes Telegram document ID: edited media never reuses old cached bytes.
        key = hashlib.sha256(f"{row['id']}:{message.document.id}:{offset}".encode()).hexdigest()
        async with self.download_slots:
            data = await self.chunk_cache.get(key)
            expected = min(CHUNK, message.document.size - offset)
            if data is not None and len(data) == expected:
                return data
            for attempt in range(2):
                iterator = self.client.iter_download(message, offset=offset, request_size=CHUNK, chunk_size=CHUNK, limit=1)
                try:
                    data = bytes(await iterator.__anext__())
                    break
                except (errors.FileReferenceExpiredError, errors.FileReferenceInvalidError):
                    if attempt:
                        raise
                    message = await self.message(row)
                    if str(message.document.id) != row['file_id']:
                        raise IOError('Media changed during playback')
                finally:
                    await iterator.close()
            if len(data) != expected:
                raise IOError('Incomplete Telegram chunk')
            await self.chunk_cache.put(key, data)
            return data

    async def stream(self, row, message, start, end):
        with self.playback():
            iterator = self._stream(row, message, start, end)
            try:
                async for chunk in iterator:
                    yield chunk
            finally:
                await iterator.aclose()

    async def _stream(self, row, message, start, end):
        # Prefetch up to four 512 KiB chunks, sharing four download slots
        # globally. Start the next requests before waiting for the player to read.
        offset = start // CHUNK * CHUNK
        pending = deque()

        def fill():
            nonlocal offset
            while offset <= end and len(pending) < 4:
                pending.append((offset, asyncio.create_task(self.chunk(row, message, offset))))
                offset += CHUNK

        fill()
        try:
            while pending:
                chunk_offset, task = pending[0]
                data = await task
                pending.popleft()
                fill()
                yield data[max(0, start - chunk_offset): min(len(data), end - chunk_offset + 1)]
        finally:
            for _, task in pending:
                task.cancel()
            await asyncio.gather(*(task for _, task in pending), return_exceptions=True)


