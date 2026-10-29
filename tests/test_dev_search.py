import asyncio
import time
from types import SimpleNamespace

import pytest
from telethon import errors, types, utils

from stremio_addon.core import Store
from stremio_addon.dev_search import DevSearch
from stremio_addon.telegram import DevSearchCooldown, _dev_search
from tests.test_series import video
from tests.test_hybrid_search import setup, channel, message
from fastapi.testclient import TestClient
from stremio_addon.core import Settings
from stremio_addon.debug import create_debug_app
from stremio_addon.runtime import Runtime
from tests.test_addon import FakeTelegram


@pytest.mark.asyncio
async def test_index_pagination_snapshot_dedup_and_retry(tmp_path):
    store = Store(tmp_path / 'db')
    gateway = SimpleNamespace(channels={-100: 'Test'})
    for i in range(250):
        store.upsert(video(i, 'Test.Movie.mp4'))
    pager = DevSearch(store, gateway)
    token = await pager.start('Test')
    try:
        first = await pager.page(token)
        assert len(first['matched']) == 100
        assert first['has_more']
        assert await pager.page(token) == first  # A lost HTTP response is retryable.
        store.upsert(video(999, 'Test.Movie.mp4'))  # Does not shift the snapshot.
        second = await pager.page(first['cursor'])
        last = await pager.page(second['cursor'])
        results = first['matched'] + second['matched'] + last['matched']
        assert len(results) == len({r['id'] for r, _ in results}) == 250
        assert not last['has_more'] and last['cursor'] is None
    finally:
        await store.close()


def test_paged_api_requires_auth_and_returns_complete_index_in_batches(tmp_path):
    cfg = Settings(8000, 'https://example.com', 'a' * 32, 1, 'hash', 'session', tmp_path)
    runtime = Runtime(cfg, FakeTelegram)
    with TestClient(create_debug_app(runtime)) as client:
        assert client.get('/api/search/pages', params={'q': 'Test'}).status_code == 401
        for i in range(205):
            runtime.store.upsert(video(i + 100, 'Test.Movie.mp4', channel=-100123))
        headers = {'X-Debug-Key': cfg.key}
        params = {'q': 'Test'}
        loaded = []
        sizes = []
        while True:
            response = client.get('/api/search/pages', params=params, headers=headers)
            assert response.status_code == 200
            data = response.json()
            sizes.append(data['result_count'])
            loaded.extend(r['id'] for r in data['results'])
            assert 'matched' not in data
            if not data['has_more']:
                break
            repeated = client.get('/api/search/pages', params=params, headers=headers).json() if 'cursor' in params else data
            assert repeated['results'] == data['results']
            params['cursor'] = data['cursor']
        assert sizes == [100, 100, 5]
        assert len(set(loaded)) == 205
        assert client.get('/api/search/pages', params={'cursor': 'missing:0'}, headers=headers).status_code == 410


@pytest.mark.asyncio
async def test_live_and_local_duplicates_do_not_produce_empty_pages(tmp_path):
    store = Store(tmp_path / 'db')
    for i in range(205):
        store.upsert(video(i, 'Test.Movie.mp4'))

    async def live(query, cursor):
        return [f'tg:-100:{i}' for i in range(100)], None

    pager = DevSearch(store, SimpleNamespace(channels={-100: 'Test'}, dev_search_page=live))
    try:
        token = await pager.start('Test')
        pages = []
        while token:
            response = await pager.page(token)
            pages.append(response['matched'])
            token = response['cursor']
        assert [len(p) for p in pages] == [100, 100, 5]
        assert len({r['id'] for p in pages for r, _ in p}) == 205
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_live_pages_merge_index_and_resume_after_cooldown(tmp_path):
    store = Store(tmp_path / 'db')
    for i in range(105):
        store.upsert(video(i, 'Test.Movie.mp4'))
    calls = []
    fail = False

    async def live(query, cursor):
        calls.append(cursor)
        if fail:
            raise DevSearchCooldown(5)
        return ([f'tg:-100:{i}' for i in range(100)], {'id': 100}) if cursor is None else (
            [f'tg:-100:{i}' for i in range(100, 105)], None)

    pager = DevSearch(store, SimpleNamespace(channels={-100: 'Test'}, dev_search_page=live))
    try:
        token = await pager.start('unindexed query', rows=None)
        first = await pager.page(token)
        assert len(first['matched']) == 100
        fail = True
        blocked = await pager.page(first['cursor'])
        assert blocked['warning'] and blocked['retry_after'] >= 5
        assert blocked['has_more'] and blocked['matched'] == []
        fail = False
        last = await pager.page(blocked['cursor'])
        assert len(last['matched']) == 5 and not last['has_more']
        assert calls == [None, {'id': 100}, {'id': 100}]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_concurrent_page_requests_do_not_consume_two_pages(tmp_path):
    store = Store(tmp_path / 'db')
    calls = 0

    async def live(query, cursor):
        nonlocal calls
        calls += 1
        await asyncio.sleep(.01)
        return [], {'id': calls}

    pager = DevSearch(store, SimpleNamespace(channels={}, dev_search_page=live))
    try:
        token = await pager.start('test')
        a, b = await asyncio.gather(pager.page(token), pager.page(token))
        assert a == b and calls == 1 and a['has_more']
        pager.sessions[token.split(':')[0]].touched -= pager.ttl + 1
        with pytest.raises(KeyError):
            await pager.page(a['cursor'])
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_global_cursor_continues_beyond_100_without_changing_catalog_cache(setup):
    _, store, tg, _, _ = setup
    entity = channel()
    requests = []

    async def client(request):
        requests.append(request)
        offset = request.offset_id
        messages = [message(entity, i) for i in range(200 - offset, max(0, 100 - offset), -1)] if not offset else [message(entity, 99)]
        if not offset:
            return types.messages.MessagesSlice(count=101, messages=messages, topics=[], chats=[entity], users=[], next_rate=77)
        return types.messages.Messages(messages=messages, topics=[], chats=[entity], users=[])

    tg.client = client
    ids, cursor = await tg.dev_search_page('Test')
    assert len(ids) == 100 and cursor['id'] == 101 and cursor['rate'] == 77
    more, end = await tg.dev_search_page('Test', cursor)
    assert len(more) == 1 and end is None
    assert requests[1].offset_id == 101 and requests[1].offset_rate == 77
    assert tg.search_cache == {} and tg.search_tasks == {}
    assert store.checkpoint(utils.get_peer_id(entity))['oldest'] == 0
    # The fixture closes the client through Telegram.close().
    tg.client = SimpleNamespace(disconnect=lambda: asyncio.sleep(0))


@pytest.mark.asyncio
async def test_scoped_search_keeps_cursor_and_moves_between_chats(setup):
    _, _, tg, _, _ = setup
    entities = [channel(1), channel(2)]
    tg.channels = {utils.get_peer_id(e): e for e in entities}
    tg.channel_ids = set(tg.channels)
    calls = []

    async def get_messages(entity, **kwargs):
        calls.append((entity.id, kwargs['offset_id']))
        if kwargs['offset_id']:
            return [message(entity, 1)]
        return [message(entity, i) for i in range(101, 1, -1)]

    tg.client = SimpleNamespace(get_messages=get_messages, disconnect=lambda: asyncio.sleep(0))
    _, cursor = await tg.dev_search_page('Test')
    assert cursor['id'] == 2
    _, second_chat = await tg.dev_search_page('Test', cursor)
    assert second_chat['source'] == 1
    assert calls[0][0] == calls[1][0] and calls[1][1] == 2
    await tg.dev_search_page('Test', second_chat)
    assert calls[2][0] != calls[1][0]


@pytest.mark.asyncio
async def test_dev_flood_wait_stops_instead_of_sleeping_and_keeps_shared_cooldown(setup):
    _, _, tg, _, _ = setup
    calls = 0

    async def rpc():
        nonlocal calls
        calls += 1
        raise errors.FloodWaitError(request=None, capture=10)

    token = _dev_search.set(True)
    try:
        with pytest.raises(DevSearchCooldown):
            await tg.background_request(rpc)
        assert tg.flood_until > time.monotonic()
        with pytest.raises(DevSearchCooldown):
            await tg.background_request(rpc)
        assert calls == 1
    finally:
        _dev_search.reset(token)
