import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from telethon import errors

from addon.start import configure
from stremio_addon.core import Settings, Store
from stremio_addon.telegram import Telegram
from stremio_addon.tmdb import TMDB
from tests.test_streaming_performance import video


@pytest_asyncio.fixture
async def tg(tmp_path):
    store = Store(tmp_path / 'db')
    gateway = Telegram(Settings(8000, 'http://localhost', 'a'*32, 1, 'hash', '', tmp_path), store)
    yield gateway
    await store.close()


@pytest.mark.asyncio
async def test_page_commits_fts_and_checkpoint_once_and_rolls_back_on_failure(tg):
    statements = []
    def write(fail=False):
        tg.store.db.set_trace_callback(statements.append)
        def index(channel, message):
            tg.store.upsert(video(message.id))
            if fail and message.id == 3:
                raise RuntimeError('interrupted page')
        tg.index = index
        tg.index_batch(-1, [SimpleNamespace(id=i) for i in range(5, 0, -1)],
                       dict(channel=-1, oldest=0, newest=0, complete=1), 'history')

    with pytest.raises(RuntimeError, match='interrupted page'):
        await tg.store.run(write, True)
    assert tg.store.checkpoint(-1)['oldest'] == 0
    assert tg.store.catalog('Original') == []
    assert not tg.store.db.execute('SELECT * FROM search_rows').fetchall()
    statements.clear()
    await tg.store.run(write)
    assert statements.count('COMMIT') == 1
    assert len(tg.store.catalog('Original')) == 5
    assert tg.store.checkpoint(-1) == dict(channel=-1, oldest=1, newest=5, complete=1)


@pytest.mark.asyncio
@pytest.mark.parametrize('concurrency', [1, 2, 4])
async def test_channel_workers_are_bounded_and_each_channel_runs_once(tg, concurrency):
    tg.indexing_concurrency = concurrency
    tg.channels = {i: i for i in range(11)}
    active = peak = 0
    visited = []
    async def process(channel, entity):
        nonlocal active, peak
        visited.append(channel)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(.001)
        active -= 1
    await tg.scan_channels(process)
    assert peak == concurrency
    assert sorted(visited) == list(range(11))
    assert active == 0


@pytest.mark.asyncio
async def test_failed_worker_cancels_and_drains_sibling(tg):
    tg.channels = {1: 1, 2: 2, 3: 3}
    started = asyncio.Event()
    finished = asyncio.Event()
    async def process(channel, entity):
        if channel == 1:
            await started.wait()
            raise ValueError('failed scan')
        assert channel == 2
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()
    with pytest.raises(ValueError, match='failed scan'):
        await tg.scan_channels(process)
    assert finished.is_set()


@pytest.mark.asyncio
async def test_playback_pauses_fetched_pages_before_database_commit(tg):
    started, release = asyncio.Event(), asyncio.Event()
    async def get_messages(*args, **kwargs):
        started.set()
        await release.wait()
        return [SimpleNamespace(id=1)]
    tg.client = SimpleNamespace(get_messages=get_messages)
    tg.index = lambda channel, message: tg.store.upsert(video(message.id))
    task = asyncio.create_task(tg.scan_history(-1, -1))
    await asyncio.wait_for(started.wait(), 1)
    with tg.playback():
        release.set()
        await asyncio.sleep(.02)
        assert not task.done()
        assert tg.store.get('tg:-1:1') is None
        assert tg.store.checkpoint(-1)['oldest'] == 0
    assert tg.playback_until > time.monotonic()
    tg.playback_until = 0  # Skip the real grace interval in this scheduling test.
    await asyncio.wait_for(task, 1)
    assert tg.store.get('tg:-1:1')
    assert tg.store.checkpoint(-1)['complete']


@pytest.mark.asyncio
async def test_multiple_streams_pause_background_until_last_disconnect(tg):
    tg.chunk = AsyncMock(return_value=b'bytes')
    streams = [tg.stream(video(), None, 0, 4) for _ in range(2)]
    for stream in streams:
        assert await anext(stream) == b'bytes'
    assert tg.active_playbacks == 2
    request = AsyncMock(return_value='scanned')
    task = asyncio.create_task(tg.background_request(request))
    await asyncio.sleep(0)
    await streams[0].aclose()
    assert tg.active_playbacks == 1
    await asyncio.sleep(0)
    request.assert_not_called()
    assert not task.done()
    await streams[1].aclose()
    assert tg.active_playbacks == 0
    assert 1.9 < tg.playback_until - time.monotonic() <= 2
    tg.playback_until = 0
    assert await asyncio.wait_for(task, 1) == 'scanned'


@pytest.mark.asyncio
async def test_cancelled_stream_releases_playback_priority(tg):
    started = asyncio.Event()
    async def chunk(*args):
        started.set()
        await asyncio.Event().wait()
    tg.chunk = chunk
    stream = tg.stream(video(), None, 0, 4)
    task = asyncio.create_task(anext(stream))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tg.active_playbacks == 0
    assert tg.playback_idle.is_set()
    tg.playback_until = 0
    await asyncio.wait_for(tg.wait_background(), 1)


@pytest.mark.asyncio
async def test_background_flood_wait_is_shared_and_playback_bypasses_it(tg):
    calls = []
    flood = asyncio.Event()
    async def request(name):
        calls.append((name, time.monotonic()))
        if len(calls) == 1:
            flood.set()
            raise errors.FloodWaitError(request=None, capture=1)
        return name
    first = asyncio.create_task(tg.background_request(request, 'first'))
    await flood.wait()
    second = asyncio.create_task(tg.background_request(request, 'second'))
    # Playback can fetch its first bytes even while background requests back off.
    tg.chunk = AsyncMock(return_value=b'bytes')
    stream = tg.stream(video(), None, 0, 4)
    assert await asyncio.wait_for(anext(stream), .1) == b'bytes'
    await stream.aclose()
    tg.playback_until = 0
    assert await asyncio.wait_for(asyncio.gather(first, second), 2) == ['first', 'second']
    assert len(calls) == 3
    assert calls[1][1] - calls[0][1] >= .99
    assert calls[2][1] - calls[1][1] >= .19


@pytest.mark.asyncio
async def test_automatic_tmdb_matching_waits_for_playback(tg):
    tmdb = TMDB(SimpleNamespace(tmdb_api_key='key'), tg.store, tg)
    tmdb._scan_once = AsyncMock()
    try:
        with tg.playback():
            task = asyncio.create_task(tmdb.scan_once())
            await asyncio.sleep(0)
            tmdb._scan_once.assert_not_called()
        tg.playback_until = 0
        await asyncio.wait_for(task, 1)
        tmdb._scan_once.assert_awaited_once()
    finally:
        await tmdb.http.aclose()


@pytest.mark.parametrize('value', ['0', '5', '-1', '2.5', 'bad'])
def test_concurrency_rejects_values_outside_limit(monkeypatch, value):
    for name, v in dict(ADDON_URL='http://localhost', ADDON_ACCESS_KEY='a'*32,
                        TELEGRAM_APP_API_ID='1', TELEGRAM_APP_API_HASH='hash',
                        USER_SESSION_STRING='session', INDEXING_CONCURRENCY=value).items():
        monkeypatch.setenv(name, v)
    with pytest.raises(ValueError, match='INDEXING_CONCURRENCY must be an integer between 1 and 4'):
        Settings.env()


def test_concurrency_default_and_home_assistant_translation(monkeypatch):
    for name, v in dict(ADDON_URL='http://localhost', ADDON_ACCESS_KEY='a'*32,
                        TELEGRAM_APP_API_ID='1', TELEGRAM_APP_API_HASH='hash',
                        USER_SESSION_STRING='session').items():
        monkeypatch.setenv(name, v)
    monkeypatch.delenv('INDEXING_CONCURRENCY', raising=False)
    assert Settings.env().indexing_concurrency == 2
    env = {}
    configure({'INDEXING_CONCURRENCY': 4}, env)
    assert env['INDEXING_CONCURRENCY'] == '4'
    monkeypatch.setenv('INDEXING_CONCURRENCY', env['INDEXING_CONCURRENCY'])
    assert Settings.env().indexing_concurrency == 4


@pytest.mark.asyncio
async def test_playback_startup_gets_priority_but_thumbnail_lookup_does_not(tg):
    tg.channels = {-1: SimpleNamespace(title='Channel')}
    tg.index = lambda *args: None
    tg.next_scan_request = tg.flood_until = time.monotonic() + 3600
    active = []
    async def get_messages(*args, **kwargs):
        active.append(tg.active_playbacks)
        return SimpleNamespace(document=SimpleNamespace(id=99))
    tg.client = SimpleNamespace(get_messages=get_messages)
    await asyncio.wait_for(tg.message(video(), priority=True), .1)
    assert active == [1]
    assert tg.active_playbacks == 0
    assert tg.playback_until > time.monotonic()
    tg.playback_until = 0
    await tg.message(video())
    assert active == [1, 0]
    assert tg.playback_until == 0
