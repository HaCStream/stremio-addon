"""Persistent, independently concurrent downloads owned by the Developer UI."""
import asyncio
import json
import logging
import os
import re
import shutil
import unicodedata
from pathlib import Path
from uuid import uuid4

from telethon import errors
from .telegram import CHUNK

logger = logging.getLogger(__name__)


async def disk(function, *args):
    # A cancelled to_thread call keeps running. Join it before deleting/renaming
    # the file, so pause/stop never race an outstanding disk write.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def filename(value):
    value = ''.join(c for c in value if not unicodedata.category(c).startswith('C'))
    value = re.sub(r'[/\\:*?"<>|]', '_', value).strip(' .')
    # Leave room for the unique prefix and .part within NAME_MAX (UTF-8 bytes).
    return value.encode('utf-8')[:180].decode('utf-8', errors='ignore') or 'video.bin'


class Downloads:
    def __init__(self, runtime, root=Path('/media/telegram-downloads')):
        self.runtime = runtime
        self.root = Path(root)
        self.manifest = runtime.cfg.data / 'downloads.json'
        self.jobs = {}
        self.tasks = {}
        self.lock = asyncio.Lock()
        self.save_lock = asyncio.Lock()
        self.flood_until = 0

    async def start(self):
        if not self.manifest.exists():
            return
        # Fail visibly rather than silently orphaning files on malformed state.
        data = await disk(lambda: json.loads(self.manifest.read_text()))
        for job in data:
            if not re.fullmatch(r'[a-f0-9]{32}', job['id']):
                raise ValueError('Invalid saved download ID')
            if job['name'] != job['id'] + '-' + filename(job['row']['filename'] or 'video.bin'):
                raise ValueError('Invalid saved download filename')
            self.jobs[job['id']] = job
            final, part = self.paths(job)
            if final.is_file() and not final.is_symlink():
                job.update(status='completed', downloaded=final.stat().st_size, error=None)
            else:
                job.update(status='paused', downloaded=part.stat().st_size if part.is_file() else 0,
                           error=None)
        await self.save()

    def paths(self, job):
        final = self.root / job['name']
        return final, final.with_name(final.name + '.part')

    async def save(self):
        async with self.save_lock:
            payload = json.dumps(list(self.jobs.values()), ensure_ascii=False)
            def write():
                temp = self.manifest.with_suffix('.tmp')
                temp.write_text(payload, encoding='utf-8')
                temp.replace(self.manifest)
            await disk(write)

    def listing(self):
        return [dict(id=j['id'], source_id=j['row']['id'], caption=j['row']['caption'],
                     filename=j['row']['filename'], saved_filename=j['name'],
                     channel=j['row']['channel_name'], size=j['row']['size'],
                     downloaded=j['downloaded'], status=j['status'], error=j['error'])
                for j in self.jobs.values()]

    async def create(self, row):
        async with self.lock:
            existing = next((j for j in self.jobs.values() if j['row']['id'] == row['id']), None)
            if existing:
                return existing['id']
            await disk(self.root.mkdir, 0o755, True, True)
            identifier = uuid4().hex
            job = dict(id=identifier, name=identifier + '-' + filename(row['filename'] or 'video.bin'),
                       row=dict(row), downloaded=0, status='downloading', error=None)
            self.jobs[identifier] = job
            try:
                await self.save()
            except BaseException:
                del self.jobs[identifier]
                raise
            self.tasks[identifier] = asyncio.create_task(self.run(job))
            return identifier

    async def action(self, identifier, action):
        async with self.lock:
            job = self.jobs[identifier]
            task = self.tasks.get(identifier)
            if action == 'resume':
                if job['status'] == 'completed' or task and not task.done():
                    return
                job.update(status='downloading', error=None)
                await self.save()
                self.tasks[identifier] = asyncio.create_task(self.run(job))
                return
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if action == 'pause':
                if job['status'] != 'completed':
                    job['status'] = 'paused'
            elif action == 'delete':
                # Unlink only our exact generated paths, never arbitrary UI paths.
                for path in self.paths(job):
                    await disk(path.unlink, True)
                del self.jobs[identifier]
                self.tasks.pop(identifier, None)
            else:
                raise ValueError('Unknown download action')
            await self.save()

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for job in self.jobs.values():
            if job['status'] == 'downloading':
                job['status'] = 'paused'
        await self.save()

    async def wait(self):
        while True:
            await self.runtime.tg.wait_background()
            delay = self.flood_until - asyncio.get_running_loop().time()
            if delay <= 0:
                return
            await asyncio.sleep(delay)

    def flood_wait(self, seconds):
        self.flood_until = max(self.flood_until, asyncio.get_running_loop().time() + seconds)

    async def message(self, row):
        while True:
            await self.wait()
            try:
                return await self.runtime.tg.message(row)
            except errors.FloodWaitError as exc:
                self.flood_wait(exc.seconds)

    async def run(self, job):
        row = job['row']
        tg = self.runtime.tg
        final, part = self.paths(job)
        try:
            await disk(self.root.mkdir, 0o755, True, True)
            if final.exists() or final.is_symlink():
                raise FileExistsError('Destination already exists')
            message = await self.message(row)
            if str(message.document.id) != row['file_id'] or message.document.size != row['size']:
                raise ValueError('Telegram media changed; delete this download and search again')
            def prepare():
                fd = os.open(part, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
                with os.fdopen(fd, 'r+b') as output:
                    size = os.fstat(output.fileno()).st_size
                    # Resume from a Telegram-aligned offset; at most one chunk is repeated.
                    offset = size if size == row['size'] else size // CHUNK * CHUNK
                    if size > row['size']:
                        raise ValueError('Partial file exceeds video size')
                    output.truncate(offset)
                    return offset
            offset = await disk(prepare)
            job['downloaded'] = offset
            refreshed = False
            while offset < row['size']:
                await self.wait()
                iterator = tg.client.iter_download(message, offset=offset, request_size=CHUNK,
                                                   chunk_size=CHUNK, limit=1)
                try:
                    chunk = bytes(await iterator.__anext__())
                except errors.FloodWaitError as exc:
                    self.flood_wait(exc.seconds)
                    continue
                except (errors.FileReferenceExpiredError, errors.FileReferenceInvalidError):
                    if refreshed:
                        raise
                    refreshed = True
                    message = await self.message(row)
                    if str(message.document.id) != row['file_id']:
                        raise ValueError('Telegram media changed') from None
                    continue
                finally:
                    await iterator.close()
                if len(chunk) != min(CHUNK, row['size'] - offset):
                    raise IOError('Incomplete Telegram chunk')
                def write():
                    if shutil.disk_usage(self.root).free < len(chunk):
                        raise OSError('Insufficient disk space')
                    fd = os.open(part, os.O_WRONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, 'r+b') as output:
                        output.seek(offset)
                        output.write(chunk)
                        output.flush()
                await disk(write)
                refreshed = False
                offset += len(chunk)
                job['downloaded'] = offset
            await disk(part.rename, final)
            job.update(status='completed', error=None)
            logger.info('Local download completed: id=%s', job['id'])
        except asyncio.CancelledError:
            # The disk helper has joined pending writes before we report paused.
            if final.is_file() and not final.is_symlink():
                job.update(status='completed', downloaded=final.stat().st_size)
            else:
                job.update(status='paused', downloaded=part.stat().st_size if part.exists() else 0)
            raise
        except Exception as exc:
            job.update(status='failed', error=type(exc).__name__)
            logger.warning('Local download failed: id=%s error=%s', job['id'], type(exc).__name__)
        finally:
            await self.save()
