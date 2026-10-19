import asyncio
import hashlib
import logging
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from telethon import TelegramClient, events, errors, types, utils
from telethon.sessions import StringSession
from .core import parse_title
from .chunk_cache import ChunkCache

logger = logging.getLogger("stremio_addon.telegram")

CHUNK = 512 * 1024


class Telegram:
    def __init__(self, settings, store):
        self.store = store
        self.channel_ids = settings.channel_ids
        # Handle flood waits ourselves so every scanner shares the same backoff.
        self.client = TelegramClient(StringSession(settings.session), settings.api_id, settings.api_hash, flood_sleep_threshold=0)
        self.channels = {}
        self.known_channels = None
        self.status = {'phase': 'connecting', 'last_error': None}
        self.cache = settings.data / 'chunks'
        self.cache.mkdir(exist_ok=True)
        self.cache_limit = max(0, settings.cache_bytes)
        self.download_slots = asyncio.Semaphore(4)
        self.chunk_cache = ChunkCache(self.cache, self.cache_limit)
        self.sync_requested = asyncio.Event()
        self.restart_requested = asyncio.Event()
        self.sync_status = {"id": None, "state": "idle", "error": None}
        self.sync_started = None
        self.task = None
        self.reconcile_cursor = ''
        self.scan_interval_seconds = settings.scan_interval_seconds
        self.pending_channels = set()
        self.history_remaining = {}
        self.new_remaining = {}
        self.indexing_concurrency = settings.indexing_concurrency
        self.active_playbacks = 0
        self.playback_until = 0
        self.playback_idle = asyncio.Event()
        self.playback_idle.set()
        self.scan_request_lock = asyncio.Lock()
        self.next_scan_request = 0
        self.flood_until = 0

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

    async def background_request(self, function, *args, **kwargs):
        while True:
            await self.wait_background()
            async with self.scan_request_lock:
                while True:
                    await self.wait_background()
                    delay = max(self.next_scan_request, self.flood_until) - time.monotonic()
                    if delay <= 0:
                        break
                    await asyncio.sleep(delay)
                self.next_scan_request = time.monotonic() + .2
            try:
                result = await function(*args, **kwargs)
                if self.status['phase'] == 'rate_limited' and time.monotonic() >= self.flood_until:
                    self.status.update(last_error=None)
                    self.status.pop('retry_after', None)
                    self.scan_phase()
                return result
            except errors.FloodWaitError as exc:
                self.flood_until = max(self.flood_until, time.monotonic() + exc.seconds)
                self.status.update(phase='rate_limited', last_error='Telegram flood wait',
                                   retry_after=exc.seconds)
                logger.warning('Telegram background requests paused: retry_after_seconds=%s', exc.seconds)

    def index_batch(self, channel, messages, point, direction):
        started = time.monotonic()
        # Videos, FTS entries and the checkpoint advance atomically on one worker.
        with self.store.transaction():
            for message in messages:
                self.index(channel, message)
                if direction == 'history':
                    point['oldest'] = message.id
                    point['newest'] = max(point['newest'], message.id)
                else:
                    point['newest'] = message.id
            self.store.save_checkpoint(point)
        logger.debug('Indexed page: channel=%s direction=%s messages=%s database_seconds=%.3f',
                     channel, direction, len(messages), time.monotonic() - started)

    async def scan_channels(self, process):
        # Finish catch-up for fully indexed chats before starting any backlog work.
        groups = ([], [])
        for channel, entity in list(self.channels.items()):
            groups[0 if self.store.checkpoint(channel)['complete'] else 1].append((channel, entity))
        for group in groups:
            await self._scan_channel_group(process, group)

    async def _scan_channel_group(self, process, group):
        channels = iter(group)

        async def worker():
            for channel, entity in channels:
                await self.wait_background()
                await process(channel, entity)

        tasks = [asyncio.create_task(worker()) for _ in
                 range(min(self.indexing_concurrency, len(group)))]
        try:
            await asyncio.gather(*tasks)
        finally:
            # Drain siblings before retrying a pass, pruning, or closing SQLite.
            for task in tasks:
                if not task.cancelling():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def queue(self, channel):
        """Estimated pending messages; ID gaps are included until scanning finishes."""
        point = self.store.checkpoint(channel)
        history = (0 if point['complete'] else self.history_remaining.get(
            channel, max(0, point['oldest'] - 1) if point['oldest'] else None))
        newer = self.new_remaining.get(channel)
        return None if history is None or newer is None else history + newer

    async def start(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise RuntimeError('Telegram session is unauthorized; generate a new Telethon session')
        me = await self.client.get_me()
        if me.bot:
            await self.client.disconnect()
            raise RuntimeError('A Telegram user session is required, not a bot')
        self.client.add_event_handler(self.update, events.NewMessage())
        self.client.add_event_handler(self.update, events.MessageEdited())
        self.client.add_event_handler(self.deleted, events.MessageDeleted())
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.finish_sync("Sync interrupted by shutdown")
        await self.client.disconnect()

    def request_sync(self):
        """Restart the indexer with fresh discovery and catch-up scans."""
        if self.sync_status['state'] == 'queued':
            return dict(self.sync_status)
        if self.status['phase'] == 'unauthorized':
            raise RuntimeError('Telegram session revoked; restart with a valid session')
        self.sync_status = {'id': uuid4().hex, 'state': 'queued', 'error': None}
        self.sync_started = time.monotonic()
        self.sync_requested.set()
        self.restart_requested.set()
        logger.info('Sync requested: id=%s', self.sync_status['id'])
        return dict(self.sync_status)

    def finish_sync(self, error=None):
        if self.sync_status['state'] not in ('queued', 'running'):
            return
        duration = round(time.monotonic() - self.sync_started, 1)
        self.sync_status.update(state='failed' if error else 'done', error=error)
        if error:
            logger.error('Sync failed: id=%s error=%s duration_seconds=%s',
                         self.sync_status['id'], error, duration)
        else:
            logger.info('Sync done: id=%s channels=%s duration_seconds=%s',
                        self.sync_status['id'], len(self.channels), duration)

    async def discover(self):
        channels = {}
        dialogs = self.client.iter_dialogs().__aiter__()
        while True:
            await self.wait_background()
            try:
                dialog = await anext(dialogs)
            except StopAsyncIteration:
                break
            entity = dialog.entity
            is_channel = (isinstance(entity, types.Channel)
                          and (entity.broadcast or entity.megagroup or entity.gigagroup)
                          and not entity.left)
            is_group = (isinstance(entity, types.Chat) and not entity.left
                        and not entity.deactivated and not entity.migrated_to)
            if is_channel or is_group:
                channel_id = utils.get_peer_id(entity)
                if self.channel_ids is None or channel_id in self.channel_ids:
                    channels[channel_id] = entity
        self.channels = channels
        self.history_remaining = {c: n for c, n in self.history_remaining.items() if c in channels}
        self.new_remaining = {c: n for c, n in self.new_remaining.items() if c in channels}
        if self.known_channels is None:
            self.known_channels = await self.store.run(self.store.stored_channels)
        removed = self.known_channels - channels.keys()
        if removed:
            logger.info('Removing unavailable channels from index: channels=%s', len(removed))
            await self.store.prune_channels(removed, wait_background=self.wait_background)
        self.known_channels = set(channels)

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
                   channel_name=self.channels[channel].title, size=doc.size, mime=doc.mime_type or 'application/octet-stream', date=int(message.date.timestamp()), file_id=str(doc.id))
        row.update(parse_title(filename, caption))
        self.store.upsert(row)

    async def update(self, event):
        await self.wait_background()
        if event.chat_id in self.channels:
            await self.store.run(self.index, event.chat_id, event.message)

    async def deleted(self, event):
        await self.wait_background()
        if event.chat_id in self.channels:
            for message in event.deleted_ids:
                await self.store.run(self.store.delete, f'tg:{event.chat_id}:{message}')

    async def scan(self, channel, entity):
        await self.scan_new_messages(channel, entity)
        await self.scan_history(channel, entity)

    def scan_phase(self):
        self.status.update(phase='indexing' if self.pending_channels else 'checking',
                           pending_channels=len(self.pending_channels))

    async def scan_new_messages(self, channel, entity):
        p = self.store.checkpoint(channel)
        self.new_remaining[channel] = None
        if p['newest']:
            self.scan_phase()
            # Freeze the target: a busy group cannot extend this catch-up forever.
            latest = await self.background_request(self.client.get_messages, entity, limit=1, offset_id=0)
            target = latest[0].id if latest else p['newest']
            if target > p['newest']:
                history_pending = channel in self.pending_channels
                self.pending_channels.add(channel)
                self.scan_phase()
                self.new_remaining[channel] = target - p['newest']
                while p['newest'] < target:
                    batch = await self.background_request(
                        self.client.get_messages, entity, limit=100,
                        min_id=p['newest'], max_id=target + 1, reverse=True)
                    if not batch:
                        break
                    await self.wait_background()
                    await self.store.run(self.index_batch, channel, batch, p, 'new')
                    self.new_remaining[channel] = max(0, target - p['newest'])
                # The iterator reached its fixed target (including deleted-ID gaps).
                p['newest'] = target
                await self.wait_background()
                await self.store.run(self.store.save_checkpoint, p)
                if not history_pending:
                    self.pending_channels.discard(channel)
                self.scan_phase()
        self.new_remaining[channel] = 0

    async def scan_history(self, channel, entity):
        p = self.store.checkpoint(channel)
        if not p['complete']:
            self.scan_phase()
            # One look-ahead message proves a backlog remains after this batch.
            batch = await self.background_request(self.client.get_messages, entity, limit=101, offset_id=p['oldest'])
            if batch:
                self.history_remaining[channel] = max(0, batch[0].id)
                self.pending_channels.add(channel)
                self.scan_phase()
            if len(batch) <= 100:
                p['complete'] = 1
            await self.wait_background()
            await self.store.run(self.index_batch, channel, batch[:100], p, 'history')
            if batch:
                self.history_remaining[channel] = max(0, p['oldest'] - 1)
            if p['complete']:
                self.pending_channels.discard(channel)
                self.history_remaining[channel] = 0
            self.scan_phase()
            logger.debug('History batch: channel=%s messages=%s oldest=%s complete=%s',
                         channel, min(len(batch), 100), p['oldest'], bool(p['complete']))

    async def reconcile(self):
        await self.wait_background()
        # Rotating batches eventually check every indexed message, including old edits/deletions.
        rows = await self.store.run(lambda: self.store.db.execute(
            'SELECT id,channel,message FROM videos WHERE id>? ORDER BY id LIMIT 100',
            (self.reconcile_cursor,)).fetchall())
        if not rows:
            self.reconcile_cursor = ''
            return
        grouped = {}
        for r in rows:
            grouped.setdefault(r['channel'], []).append(r)
        for channel, group in grouped.items():
            entity = self.channels.get(channel)
            if not entity:
                continue
            messages = await self.background_request(self.client.get_messages, entity, ids=[r['message'] for r in group])
            for row, message in zip(group, messages):
                if message and not isinstance(message, types.MessageEmpty):
                    await self.wait_background()
                    await self.store.run(self.index, channel, message)
                else:
                    await self.wait_background()
                    await self.store.run(self.store.delete, row['id'])
        self.reconcile_cursor = rows[-1]['id']

    async def run(self):
        try:
            while True:
                self.restart_requested.clear()
                cycle = asyncio.create_task(self._run_cycles())
                restart = asyncio.create_task(self.restart_requested.wait())
                try:
                    done, _ = await asyncio.wait((cycle, restart), return_when=asyncio.FIRST_COMPLETED)
                    if cycle in done:
                        await cycle
                        return
                    logger.info('Restarting indexing for requested sync: id=%s', self.sync_status['id'])
                finally:
                    # Drain channel workers and in-flight SQL before a replacement starts.
                    cycle.cancel()
                    restart.cancel()
                    await asyncio.gather(cycle, restart, return_exceptions=True)
        except asyncio.CancelledError:
            self.finish_sync("Sync interrupted by shutdown")
            raise

    async def _run_cycles(self):
        discover_needed = True
        sync_id = None
        scanned_channels = set()
        while True:
            try:
                forced = self.sync_requested.is_set()
                self.sync_requested.clear()
                if forced:
                    if sync_id != self.sync_status['id']:
                        sync_id = self.sync_status['id']
                        scanned_channels.clear()
                    self.sync_status.update(state='running')
                    logger.info('Sync started: id=%s; discovering channels', self.sync_status['id'])
                if forced or discover_needed:
                    self.scan_phase()
                    await self.discover()
                    self.pending_channels.intersection_update(self.channels)
                    discover_needed = False
                    logger.info('Channel discovery complete: channels=%s', len(self.channels))
                self.scan_phase()
                cycle_error = None
                async def process(channel, entity):
                    nonlocal cycle_error
                    try:
                        if self.sync_status['state'] == 'running':
                            if channel not in scanned_channels:
                                logger.info('Sync scanning channel: id=%s', channel)
                                await self.scan_new_messages(channel, entity)
                                scanned_channels.add(channel)
                            elif self.store.checkpoint(channel)['complete']:
                                return
                            await self.scan_history(channel, entity)
                        elif channel not in scanned_channels:
                            await self.scan(channel, entity)
                            scanned_channels.add(channel)
                        elif not self.store.checkpoint(channel)['complete']:
                            await self.scan_history(channel, entity)
                        else:
                            return
                    except (errors.ChannelPrivateError, errors.ChannelInvalidError):
                        logger.warning('Sync skipped inaccessible channel: id=%s', channel)
                        cycle_error = 'A channel could not be scanned'
                        self.pending_channels.discard(channel)
                        self.channels.pop(channel, None)
                        await self.store.prune_channels({channel}, wait_background=self.wait_background)
                        if self.known_channels is not None:
                            self.known_channels.discard(channel)
                await self.scan_channels(process)
                incomplete = any(not self.store.checkpoint(c)['complete'] for c in self.channels)
                # Reconciliation re-fetches indexed messages; leave it to background passes.
                if self.sync_status['state'] != 'running':
                    await self.reconcile()
                self.status.update(phase=('scan_incomplete' if cycle_error else
                                          'indexing' if self.pending_channels else
                                          'checking' if incomplete else 'ready'),
                                   last_error=cycle_error,
                                   pending_channels=len(self.pending_channels))
                logger.debug('Scan pass complete: pending_channels=%s incomplete_channels=%s',
                             len(self.pending_channels),
                             sum(not self.store.checkpoint(c)['complete'] for c in self.channels))
                if not incomplete and self.sync_status['state'] == 'running':
                    self.finish_sync()
                elif self.sync_status['state'] == 'running':
                    remaining = sum(not self.store.checkpoint(c)['complete'] for c in self.channels)
                    logger.info('Sync indexing history: incomplete_channels=%s', remaining)
                if not incomplete:
                    discover_needed = True
                    scanned_channels.clear()
                    logger.info('Telegram scan complete: channels=%s next_check_seconds=%s',
                                len(self.channels), self.scan_interval_seconds)
                try:
                    await asyncio.wait_for(self.sync_requested.wait(), 2 if incomplete else self.scan_interval_seconds)
                except TimeoutError:
                    pass
            except errors.FloodWaitError as exc:
                self.status.update(phase='rate_limited', last_error='Telegram flood wait', retry_after=exc.seconds)
                logger.warning('Telegram sync rate limited: retry_after_seconds=%s', exc.seconds)
                if self.sync_status['state'] in ('queued', 'running'):
                    self.sync_requested.set()
                await asyncio.sleep(exc.seconds)
            except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError):
                self.finish_sync('Telegram session revoked')
                self.status.update(phase='unauthorized', last_error='Telegram session revoked')
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.finish_sync(type(exc).__name__)
                logger.error("Telegram indexing failed: error=%s", type(exc).__name__)
                # Do not log exception text: Telegram exceptions can contain private values.
                self.status.update(phase='scan_incomplete', last_error=type(exc).__name__)
                await asyncio.sleep(30)

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


