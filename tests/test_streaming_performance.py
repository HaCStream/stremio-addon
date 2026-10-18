import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from stremio_addon.chunk_cache import ChunkCache
from stremio_addon.core import Settings, Store
from stremio_addon.telegram import CHUNK, Telegram


def video(number=1, title='Original'):
    return dict(id=f'tg:-1:{number}', channel=-1, message=number, title=title,
                filename=f'{title}.mp4', caption='', channel_name='Channel',
                size=CHUNK * 10, mime='video/mp4', date=number, year=None,
                season=None, episode=None, imdb=None, quality='', file_id='99')


def gateway(tmp_path, cache=0):
    store = Store(tmp_path / 'db')
    cfg = Settings(8000, 'http://localhost', 'a' * 32, 1, 'hash', '', tmp_path, cache)
    return Telegram(cfg, store)


@pytest.mark.asyncio
async def test_database_work_does_not_block_playback_or_share_transactions(tmp_path):
    store = Store(tmp_path / 'db')
    store.upsert(video())
    main_db = store.db
    main_thread = threading.get_ident()
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def slow_index():
        assert threading.get_ident() != main_thread
        assert store.db is not main_db
        with store.db:
            store.db.execute("UPDATE videos SET title='Changed' WHERE id=?", (video()['id'],))
            loop.call_soon_threadsafe(started.set)
            assert release.wait(2)

    task = asyncio.create_task(store.run(slow_index))
    try:
        await asyncio.wait_for(started.wait(), 1)
        # The event loop can serve cached video bytes while a write is in flight.
        # Its connection must not see or accidentally commit the worker transaction.
        assert store.get(video()['id'])['title'] == 'Original'
        tg = Telegram(Settings(8000, 'http://localhost', 'a' * 32, 1, 'hash', '', tmp_path), store)
        async def chunk(*args):
            return b'playback'
        tg.chunk = chunk
        assert await anext(tg.stream(video(), None, 0, 7)) == b'playback'
    finally:
        release.set()
        await task
    assert store.get(video()['id'])['title'] == 'Changed'
    await store.close()


@pytest.mark.asyncio
async def test_cancelled_database_call_drains_before_shutdown(tmp_path):
    store = Store(tmp_path / 'db')
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def write():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(2)
        store.upsert(video())

    task = asyncio.create_task(store.run(write))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get(video()['id'])
    await store.close()
    with pytest.raises(RuntimeError, match='closed'):
        store.get(video()['id'])


@pytest.mark.asyncio
async def test_existing_index_migration_edits_deletes_and_unchanged_reconcile(tmp_path):
    path = tmp_path / 'db'
    store = Store(path)
    for i in range(40):
        store.upsert(video(i))
    # Simulate a current release's populated index without the new lookup table.
    store.db.execute('DROP TABLE search_rows')
    store.db.commit()
    original_fts = list(store.db.execute('SELECT rowid,id,text FROM search'))
    await store.close()
    store = Store(path)
    assert [tuple(r) for r in store.db.execute('SELECT rowid,id,text FROM search')] == [tuple(r) for r in original_fts]
    changes = store.db.total_changes
    store.upsert(video(1))
    assert store.db.total_changes == changes
    store.upsert(video(1, 'Edited'))
    store.delete(video(2)['id'])
    store.delete('tg:-1:999999')  # Non-video messages need no full FTS scan.
    assert [r['id'] for r in store.catalog('Edited')] == [video(1)['id']]
    assert video(1)['id'] not in {r['id'] for r in store.catalog('Original')}
    assert store.get(video(2)['id']) is None
    assert store.db.execute('SELECT count(*) FROM search').fetchone()[0] == 39
    plan = store.db.execute('EXPLAIN QUERY PLAN DELETE FROM search WHERE rowid='
                            '(SELECT fts_rowid FROM search_rows WHERE id=?)', (video(1)['id'],)).fetchall()
    assert any('SEARCH search_rows USING INDEX' in r[3] for r in plan)
    await store.prune_channels({-1}, batch_size=7)
    assert store.db.execute('SELECT count(*) FROM search_rows').fetchone()[0] == 0
    assert not store.catalog('Original')
    await store.close()


@pytest.mark.asyncio
async def test_prefetch_is_bounded_ordered_and_cancelled_on_disconnect(tmp_path):
    tg = gateway(tmp_path)
    started, cancelled = [], []
    ready = asyncio.Event()
    gates = [asyncio.Event() for _ in range(10)]

    async def chunk(row, message, offset):
        index = offset // CHUNK
        started.append(index)
        if len(started) >= 4:
            ready.set()
        try:
            await gates[index].wait()
            return bytes([index]) * CHUNK
        except asyncio.CancelledError:
            cancelled.append(index)
            raise

    tg.chunk = chunk
    stream = tg.stream(video(), None, CHUNK - 3, CHUNK * 10 - 1)
    first = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(ready.wait(), 1)
        assert started == [0, 1, 2, 3]
        gates[2].set()  # Later requests may finish first; output stays ordered.
        gates[0].set()
        assert await first == b'\0' * 3
        await asyncio.sleep(0)
        assert started == [0, 1, 2, 3, 4]
        gates[1].set()
        assert await anext(stream) == b'\1' * CHUNK
        assert await anext(stream) == b'\2' * CHUNK
    finally:
        await stream.aclose()
        await tg.store.close()
    assert 3 in cancelled and 4 in cancelled
    assert not [t for t in asyncio.all_tasks() if t.get_coro().__name__ == 'chunk']


@pytest.mark.asyncio
async def test_prefetch_failure_closes_other_downloads(tmp_path):
    tg = gateway(tmp_path)
    cancelled = []
    started = asyncio.Event()

    async def chunk(row, message, offset):
        if offset == 0:
            await started.wait()
            raise IOError('upstream failed')
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(offset)

    tg.chunk = chunk
    with pytest.raises(IOError, match='upstream failed'):
        await anext(tg.stream(video(), None, 0, CHUNK * 10 - 1))
    assert len(cancelled) == 3
    await tg.store.close()


@pytest.mark.asyncio
async def test_concurrent_streams_share_global_download_limit_and_cache_bound(tmp_path):
    tg = gateway(tmp_path, cache=CHUNK * 2)
    active = 0
    peak = 0
    closed = []

    class Iterator:
        def __init__(self, offset):
            self.offset = offset

        async def __anext__(self):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.001)
                return bytes([self.offset // CHUNK]) * CHUNK
            finally:
                active -= 1

        async def close(self):
            closed.append(self.offset)

    tg.client = SimpleNamespace(iter_download=lambda message, **kw: Iterator(kw['offset']))
    message = SimpleNamespace(document=SimpleNamespace(id=99, size=CHUNK * 6))

    async def play(start, end):
        return b''.join([data async for data in tg.stream(video(), message, start, end)])

    results = await asyncio.gather(play(CHUNK - 7, CHUNK * 4 + 12), play(0, CHUNK * 6 - 1))
    payload = b''.join(bytes([i]) * CHUNK for i in range(6))
    assert results == [payload[CHUNK - 7:CHUNK * 4 + 13], payload]
    assert 1 < peak <= 4
    assert len(closed) >= 6
    assert sum(p.stat().st_size for p in tg.cache.iterdir()) <= CHUNK * 2
    await tg.store.close()


@pytest.mark.asyncio
async def test_cache_scans_once_and_keeps_lru_across_concurrent_writes(tmp_path, monkeypatch):
    scans = []
    original = Path.iterdir

    def iterdir(path):
        scans.append(path)
        return original(path)

    monkeypatch.setattr(Path, 'iterdir', iterdir)
    cache = ChunkCache(tmp_path, 8)
    await asyncio.gather(cache.put('a', b'aaaa'), cache.put('b', b'bbbb'))
    assert await cache.get('a') == b'aaaa'
    await cache.put('c', b'cccc')
    assert await cache.get('b') is None
    assert await cache.get('a') == b'aaaa'
    assert await cache.get('c') == b'cccc'
    assert scans == [tmp_path]
    # An interrupted write cannot leave a partial published chunk.
    restarted = ChunkCache(tmp_path, 8)
    assert await restarted.get('c') == b'cccc'
    assert restarted.total <= 8


@pytest.mark.asyncio
async def test_cache_disk_io_leaves_event_loop_responsive(tmp_path, monkeypatch):
    cache = ChunkCache(tmp_path, 8)
    await cache.put('a', b'aaaa')
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = Path.read_bytes

    def slow_read(path):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(2)
        return original(path)

    monkeypatch.setattr(Path, 'read_bytes', slow_read)
    task = asyncio.create_task(cache.get('a'))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert cache.lock.locked()  # No racing mutation while disk I/O finishes.
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await cache.get('a') == b'aaaa'
