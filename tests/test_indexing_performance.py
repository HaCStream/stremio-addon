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
async def test_background_flood_wait_is_shared_and_playback_bypasses_it(tg, clock):
    calls = []
    flood = asyncio.Event()
    async def request(name):
        calls.append((name, clock.monotonic()))
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
    assert await asyncio.wait_for(asyncio.gather(first, second), 1) == ['first', 'second']
    assert len(calls) == 3
    assert calls[1][1] - calls[0][1] >= 2
    assert calls[2][1] - calls[1][1] >= 2
    assert tg.scan_request_interval == 2
    assert clock.delays
    assert all(delay > 0 for delay in clock.delays)


def test_removed_concurrency_options_are_ignored(monkeypatch):
    for name, value in dict(ADDON_URL='http://localhost', ADDON_ACCESS_KEY='a'*32,
                           TELEGRAM_APP_API_ID='1', TELEGRAM_APP_API_HASH='hash',
                           USER_SESSION_STRING='session', INDEXING_CONCURRENCY='bad').items():
        monkeypatch.setenv(name, value)
    assert not hasattr(Settings.env(), 'indexing_concurrency')
    env = {}
    configure({'INDEXING_CONCURRENCY': 4}, env)
    assert 'INDEXING_CONCURRENCY' not in env


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


@pytest.fixture
def clock(monkeypatch):
    import stremio_addon.telegram as module
    class Clock:
        now = 100.0
        def __init__(self):
            self.delays = []
        def monotonic(self):
            return self.now
        async def sleep(self, delay):
            self.delays.append(delay)
            self.now += delay
            await asyncio.sleep(0)
    clock = Clock()
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=clock.monotonic, time=clock.monotonic))
    monkeypatch.setattr(module, 'asyncio', SimpleNamespace(**(vars(asyncio) | {'sleep': clock.sleep})))
    return clock


@pytest.mark.asyncio
async def test_hidden_pagination_is_paced_and_foreground_bypasses_gate(tg, clock, monkeypatch):
    from datetime import datetime, timezone
    from telethon import types, functions
    entity = types.InputPeerChannel(1, 2)
    calls = []
    async def wire(sender, request, **kwargs):
        calls.append(clock.now)
        if isinstance(request, functions.channels.GetMessagesRequest):
            return types.messages.Messages(messages=[], topics=[], chats=[], users=[])
        offset = request.offset_id or 1000
        return types.messages.MessagesSlice(count=1000, users=[], chats=[], topics=[], messages=[
            types.Message(id=i, peer_id=types.PeerChannel(1), date=datetime.now(timezone.utc), message='')
            for i in range(offset - 1, offset - request.limit - 1, -1)])
    monkeypatch.setattr(tg.client, '_call', wire)
    # Even a multi-page high-level call must space each RPC, not just its entry.
    page = await tg.background_operation(tg.client.get_messages, entity, limit=201)
    assert len(page) == 201
    assert len(calls) == 3
    assert calls == [100, 101, 102]
    tg.flood_until = clock.now + 3600
    # No background ContextVar leaks into the foreground after the operation.
    await tg.client.get_messages(entity, ids=[1])
    assert calls[-1] == 102


@pytest.mark.asyncio
async def test_adaptive_pacing_caps_and_recovers_gradually(tg, clock):
    intervals = []
    attempts = 0
    async def request():
        nonlocal attempts
        attempts += 1
        intervals.append(tg.scan_request_interval)
        if attempts <= 4:
            raise errors.FloodWaitError(request=None, capture=3)
        return 'ok'
    assert await tg.background_request(request) == 'ok'
    assert intervals == [1, 2, 4, 5, 5]
    assert tg.status['phase'] == 'discovering'
    assert 'retry_after' not in tg.status
    for _ in range(19):
        await tg.background_request(AsyncMock())
    assert tg.scan_request_interval == 4.75
    for _ in range(320):
        await tg.background_request(AsyncMock())
    assert tg.scan_request_interval == 1


@pytest.mark.asyncio
async def test_cancelled_operation_resets_background_context(tg, clock, monkeypatch):
    from telethon import types
    tg.flood_until = clock.now + 10
    with pytest.raises(asyncio.CancelledError):
        await tg.background_operation(AsyncMock(side_effect=asyncio.CancelledError))
    wire = AsyncMock(return_value=types.messages.Messages(messages=[], topics=[], chats=[], users=[]))
    monkeypatch.setattr(tg.client, '_call', wire)
    await tg.client.get_messages(types.InputPeerChannel(1, 2), ids=[1])
    assert clock.now == 100
    wire.assert_awaited_once()


@pytest.mark.asyncio
async def test_flood_on_second_rpc_retries_only_that_page(tg, clock, monkeypatch, caplog):
    from datetime import datetime, timezone
    from telethon import types
    calls = []
    async def wire(sender, request, **kwargs):
        calls.append((request.offset_id, clock.now))
        if len(calls) == 2:
            raise errors.FloodWaitError(request=request, capture=1)
        offset = request.offset_id or 1000
        return types.messages.MessagesSlice(count=1000, users=[], chats=[], topics=[], messages=[
            types.Message(id=i, peer_id=types.PeerChannel(1), date=datetime.now(timezone.utc), message='')
            for i in range(offset - 1, offset - request.limit - 1, -1)])
    monkeypatch.setattr(tg.client, '_call', wire)
    result = await tg.background_operation(tg.client.get_messages, types.InputPeerChannel(1, 2), limit=201)
    assert [m.id for m in result] == list(range(999, 798, -1))
    assert calls == [(0, 100), (900, 101), (900, 103), (800, 105)]
    assert tg.scan_request_interval == 2
    assert tg.status['phase'] == 'discovering'
    assert 'request=GetHistoryRequest' in caplog.text
