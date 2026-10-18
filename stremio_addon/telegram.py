import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from uuid import uuid4
from telethon import TelegramClient, events, errors, types, utils
from telethon.sessions import StringSession
from .core import parse_title

logger = logging.getLogger("stremio_addon.telegram")

CHUNK = 512 * 1024


class Telegram:
    def __init__(self, settings, store):
        self.store = store
        self.channel_ids = settings.channel_ids
        self.client = TelegramClient(StringSession(settings.session), settings.api_id, settings.api_hash, flood_sleep_threshold=30)
        self.channels = {}
        self.known_channels = None
        self.status = {'phase': 'connecting', 'last_error': None}
        self.cache = settings.data / 'chunks'
        self.cache.mkdir(exist_ok=True)
        self.cache_limit = max(0, settings.cache_bytes)
        self.download_slots = asyncio.Semaphore(4)
        self.sync_requested = asyncio.Event()
        self.sync_status = {"id": None, "state": "idle", "error": None}
        self.sync_started = None
        self.task = None
        self.reconcile_cursor = ''
        self.scan_interval_seconds = settings.scan_interval_seconds
        self.pending_channels = set()

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
        """Wake the indexer and force channel discovery plus catch-up scans."""
        if self.sync_status['state'] in ('queued', 'running'):
            return dict(self.sync_status)
        if self.status['phase'] == 'unauthorized':
            raise RuntimeError('Telegram session revoked; restart with a valid session')
        self.sync_status = {'id': uuid4().hex, 'state': 'queued', 'error': None}
        self.sync_started = time.monotonic()
        self.sync_requested.set()
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
        async for dialog in self.client.iter_dialogs():
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
        if self.known_channels is None:
            self.known_channels = self.store.stored_channels()
        removed = self.known_channels - channels.keys()
        if removed:
            logger.info('Removing unavailable channels from index: channels=%s', len(removed))
            await self.store.prune_channels(removed)
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
        if event.chat_id in self.channels:
            self.index(event.chat_id, event.message)

    async def deleted(self, event):
        if event.chat_id in self.channels:
            for message in event.deleted_ids:
                self.store.delete(f'tg:{event.chat_id}:{message}')

    async def scan(self, channel, entity):
        await self.scan_new_messages(channel, entity)
        await self.scan_history(channel, entity)

    def scan_phase(self):
        self.status.update(phase='indexing' if self.pending_channels else 'checking',
                           pending_channels=len(self.pending_channels))

    async def scan_new_messages(self, channel, entity):
        p = self.store.checkpoint(channel)
        if p['newest']:
            self.scan_phase()
            # Freeze the target: a busy group cannot extend this catch-up forever.
            latest = await self.client.get_messages(entity, limit=1, offset_id=0)
            target = latest[0].id if latest else p['newest']
            if target > p['newest']:
                history_pending = channel in self.pending_channels
                self.pending_channels.add(channel)
                self.scan_phase()
                async for message in self.client.iter_messages(
                        entity, min_id=p['newest'], max_id=target + 1, reverse=True):
                    await asyncio.sleep(0)
                    self.index(channel, message)
                    p['newest'] = message.id
                    self.store.save_checkpoint(p)
                # The iterator reached its fixed target (including deleted-ID gaps).
                p['newest'] = target
                self.store.save_checkpoint(p)
                if not history_pending:
                    self.pending_channels.discard(channel)
                self.scan_phase()

    async def scan_history(self, channel, entity):
        p = self.store.checkpoint(channel)
        if not p['complete']:
            self.scan_phase()
            # One look-ahead message proves a backlog remains after this batch.
            batch = await self.client.get_messages(entity, limit=101, offset_id=p['oldest'])
            if batch:
                self.pending_channels.add(channel)
                self.scan_phase()
            for message in batch[:100]:
                await asyncio.sleep(0)
                self.index(channel, message)
                p['oldest'] = message.id
                p['newest'] = max(p['newest'], message.id)
                self.store.save_checkpoint(p)
            if len(batch) <= 100:
                p['complete'] = 1
                self.store.save_checkpoint(p)
                self.pending_channels.discard(channel)
            self.scan_phase()
            logger.debug('History batch: channel=%s messages=%s oldest=%s complete=%s',
                         channel, min(len(batch), 100), p['oldest'], bool(p['complete']))

    async def reconcile(self):
        # Rotating batches eventually check every indexed message, including old edits/deletions.
        rows = self.store.db.execute('SELECT id,channel,message FROM videos WHERE id>? ORDER BY id LIMIT 100', (self.reconcile_cursor,)).fetchall()
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
            messages = await self.client.get_messages(entity, ids=[r['message'] for r in group])
            for row, message in zip(group, messages):
                if message and not isinstance(message, types.MessageEmpty):
                    await asyncio.sleep(0)
                    self.index(channel, message)
                else:
                    self.store.delete(row['id'])
        self.reconcile_cursor = rows[-1]['id']

    async def run(self):
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
                for channel, entity in list(self.channels.items()):
                    try:
                        if self.sync_status['state'] == 'running':
                            if channel not in scanned_channels:
                                logger.info('Sync scanning channel: id=%s', channel)
                                await self.scan_new_messages(channel, entity)
                                scanned_channels.add(channel)
                            elif self.store.checkpoint(channel)['complete']:
                                continue
                            await self.scan_history(channel, entity)
                        elif channel not in scanned_channels:
                            await self.scan(channel, entity)
                            scanned_channels.add(channel)
                        elif not self.store.checkpoint(channel)['complete']:
                            await self.scan_history(channel, entity)
                        else:
                            continue
                    except (errors.ChannelPrivateError, errors.ChannelInvalidError):
                        logger.warning('Sync skipped inaccessible channel: id=%s', channel)
                        cycle_error = 'A channel could not be scanned'
                        self.pending_channels.discard(channel)
                        self.channels.pop(channel, None)
                        await self.store.prune_channels({channel})
                        if self.known_channels is not None:
                            self.known_channels.discard(channel)
                    await asyncio.sleep(.2)
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
                self.finish_sync("Sync interrupted by shutdown")
                raise
            except Exception as exc:
                self.finish_sync(type(exc).__name__)
                logger.error("Telegram indexing failed: error=%s", type(exc).__name__)
                # Do not log exception text: Telegram exceptions can contain private values.
                self.status.update(phase='scan_incomplete', last_error=type(exc).__name__)
                await asyncio.sleep(30)

    async def message(self, row):
        entity = self.channels.get(row['channel'])
        if not entity:
            raise FileNotFoundError()
        try:
            message = await self.client.get_messages(entity, ids=row['message'])
        except (errors.ChannelPrivateError, errors.ChannelInvalidError):
            self.store.delete(row['id'])
            raise FileNotFoundError() from None
        if not message or not message.document:
            self.store.delete(row['id'])
            raise FileNotFoundError()
        self.index(row['channel'], message)
        return message

    async def chunk(self, row, message, offset):
        # Includes Telegram document ID: edited media never reuses old cached bytes.
        key = hashlib.sha256(f"{row['id']}:{message.document.id}:{offset}".encode()).hexdigest()
        path = self.cache / key
        async with self.download_slots:
            if path.exists():
                data = path.read_bytes()
                os.utime(path, None)
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
            expected = min(CHUNK, message.document.size - offset)
            if len(data) != expected:
                raise IOError('Incomplete Telegram chunk')
            if self.cache_limit >= len(data):
                files = sorted(self.cache.iterdir(), key=lambda p: p.stat().st_mtime)
                total = sum(p.stat().st_size for p in files)
                for old in files:
                    if total + len(data) <= self.cache_limit:
                        break
                    total -= old.stat().st_size
                    old.unlink(missing_ok=True)
                path.write_bytes(data)
            return data

    async def stream(self, row, message, start, end):
        offset = start // CHUNK * CHUNK
        while offset <= end:
            data = await self.chunk(row, message, offset)
            yield data[max(0, start - offset): min(len(data), end - offset + 1)]
            offset += CHUNK


