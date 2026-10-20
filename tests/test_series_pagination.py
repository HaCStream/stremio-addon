import asyncio
import json

import pytest
from telethon import types, utils, functions

from stremio_addon.core import Settings, Store, series_id
from stremio_addon.telegram import Telegram
from tests.test_hybrid_search import setup, user, channel, message, Client


class PagedClient(Client):
    def __init__(self, source, count):
        self.source = source
        messages = [message(source, i, f'Test.Show.S{i // 100 + 1:02d}E{i % 100:02d}.mp4')
                    for i in range(count, 0, -1)]
        super().__init__([source], messages)

    def page(self, offset):
        return [m for m in self.messages if not offset or m.id < offset][:100]

    async def __call__(self, request):
        self.requests.append(request)
        assert isinstance(request, functions.messages.SearchGlobalRequest)
        page = self.page(request.offset_id)
        return types.messages.MessagesSlice(count=len(self.messages), messages=page, topics=[],
                                            chats=[] if isinstance(self.source, types.User) else [self.source],
                                            users=[self.source] if isinstance(self.source, types.User) else [], next_rate=42)

    async def get_messages(self, entity, **kwargs):
        self.requests.append((entity, kwargs))
        return self.page(kwargs.get('offset_id', 0))


@pytest.mark.asyncio
async def test_global_series_pagination_exceeds_old_limit_and_reuses_completed_pass(setup):
    _, store, tg, _, _ = setup
    tg.client = PagedClient(user(), 450)
    identity = series_id('Test Show')
    await tg.search_series(identity, ['Test Show'])
    assert len(store.series(identity, {123})) == 450
    assert len(tg.client.requests) == 5
    assert tg.client.requests[1].offset_id == 351
    assert tg.client.requests[1].offset_peer.user_id == 123
    assert tg.client.requests[1].offset_rate == 42
    assert store.series_page(identity, 'Test Show', None)['complete']
    await tg.search_series(identity, ['Test Show'])
    assert len(tg.client.requests) == 5
    assert store.checkpoint(123)['newest'] == 0


@pytest.mark.asyncio
async def test_page_budget_progress_resumes_after_restart(setup):
    cfg, store, tg, _, _ = setup
    tg.client = PagedClient(user(), 1200)
    identity = series_id('Test Show')
    await tg.search_series(identity, ['Test Show'])
    assert len(tg.client.requests) == 8
    assert len(store.series(identity, {123})) == 800
    state = store.series_page(identity, 'Test Show', None)
    assert not state['complete'] and json.loads(state['cursor'])['offset_id'] == 401
    reopened = Store(store.path)
    other = Telegram(cfg, reopened)
    other.client = PagedClient(user(), 1200)
    async def operation(function, *args, **kwargs):
        return await function(*args, **kwargs)
    other.background_operation = operation
    try:
        await other.search_series(identity, ['Test Show'])
        assert other.client.requests[0].offset_id == 401
        assert len(reopened.series(identity, {123})) == 1200
        assert reopened.series_page(identity, 'Test Show', None)['complete']
        assert len(other.client.requests) == 5  # Exact 100-page end needs an empty proof page.
    finally:
        await other.close()
        await reopened.close()


@pytest.mark.asyncio
async def test_selected_chat_and_named_source_pages_are_filtered(setup):
    _, store, tg, _, _ = setup
    source = channel(title='Test Show')
    peer = utils.get_peer_id(source)
    tg.channel_ids = frozenset({peer})
    tg.client = PagedClient(source, 250)
    await tg.discover()
    await tg.search_series(series_id('Test Show'), ['Test Show'])
    assert len(store.series(series_id('Test Show'), {peer})) == 250
    assert all(isinstance(r, tuple) and r[0] is source for r in tg.client.requests)
    assert any(r[1]['search'] == '' for r in tg.client.requests)
    assert {r[1].get('offset_id') for r in tg.client.requests} >= {0, 151, 51}
    assert not store.series(series_id('Test Show'), {123})


@pytest.mark.asyncio
async def test_concurrent_opens_share_page_job_even_if_aliases_change(setup):
    _, store, tg, _, _ = setup
    tg.client = PagedClient(user(), 250)
    identity = series_id('Test Show')
    await asyncio.gather(tg.search_series(identity, ['Test Show']),
                         tg.search_series(identity, ['Test Show', 'Alternative']))
    assert len(tg.client.requests) == 3
    assert len(store.series(identity, {123})) == 250


@pytest.mark.asyncio
async def test_timeout_and_failed_page_do_not_advance_cursor(setup):
    _, store, tg, _, _ = setup
    identity = series_id('Test Show')
    tg.client = PagedClient(user(), 1200)
    await tg.search_series(identity, ['Test Show'])
    saved = store.series_page(identity, 'Test Show', None)
    async def stalled(*args):
        await asyncio.Event().wait()
    original = tg._series_page
    tg._series_page = stalled
    tg.search_timeout = .01
    await tg.search_series(identity, ['Test Show'])
    assert store.series_page(identity, 'Test Show', None) == saved
    async def failure(*args):
        raise OSError('private details')
    tg._series_page = failure
    await tg.search_series(identity, ['Test Show'])
    assert store.series_page(identity, 'Test Show', None) == saved
    tg._series_page = original
    tg.search_timeout = 12
    await tg.search_series(identity, ['Test Show'])
    assert len(store.series(identity, {123})) == 1200


@pytest.mark.asyncio
async def test_recent_head_refresh_keeps_older_progress(setup):
    _, store, tg, _, _ = setup
    identity = series_id('Test Show')
    tg.client = PagedClient(user(), 2000)
    await tg.search_series(identity, ['Test Show'])
    old_offset = json.loads(store.series_page(identity, 'Test Show', None)['cursor'])['offset_id']
    with store.db:
        store.db.execute('UPDATE series_search_pages SET head_checked_at=0')
    tg.client.requests.clear()
    await tg.search_series(identity, ['Test Show'])
    assert tg.client.requests[0].offset_id == 0
    assert tg.client.requests[1].offset_id == old_offset
    assert len(store.series(identity, {123})) == 1500  # One refreshed head + seven older pages.


@pytest.mark.asyncio
async def test_open_series_route_loads_more_and_other_series_are_untouched(setup):
    _, store, tg, _, search = setup
    source = user()
    tg.client = PagedClient(source, 1200)
    # Seed only this show's identity so the normal show lookup can resolve aliases.
    tg.channels[123] = source
    await store.run(tg.index, 123, tg.client.messages[0])
    first = await search.show(series_id('Test Show'))
    second = await search.show(series_id('Test Show'))
    assert len(first) == 800 and len(second) == 1200
    assert set(r['series'] for r in store.db.execute('SELECT DISTINCT series FROM series_search_pages')) == {series_id('Test Show')}


def test_http_series_reopen_loads_next_saved_pages(tmp_path):
    from fastapi.testclient import TestClient
    from stremio_addon.runtime import Runtime
    from stremio_addon.app import create_app_with_runtime
    class Gateway(Telegram):
        async def start(self):
            self.client = PagedClient(user(), 1200)
            async def operation(function, *args, **kwargs):
                return await function(*args, **kwargs)
            self.background_operation = operation
            self.channels[123] = user()
            await self.store.run(self.index, 123, self.client.messages[0])
    cfg = Settings(8000, 'https://example.com', 'a'*32, 1, 'hash', '', tmp_path)
    runtime = Runtime(cfg, Gateway)
    with TestClient(create_app_with_runtime(runtime)) as client:
        route = f'/{cfg.key}/meta/series/{series_id("Test Show")}.json'
        first = client.get(route)
        assert first.status_code == 200 and len(first.json()['meta']['videos']) == 800
        second = client.get(route)
        assert second.status_code == 200 and len(second.json()['meta']['videos']) == 1200
        count = len(runtime.tg.client.requests)
        assert len(client.get(route).json()['meta']['videos']) == 1200
        assert len(runtime.tg.client.requests) == count


@pytest.mark.asyncio
async def test_page_data_and_cursor_roll_back_together_on_database_failure(setup):
    _, store, tg, _, _ = setup
    tg.client = PagedClient(user(), 200)
    identity = series_id('Test Show')
    original = tg.index
    def broken(channel_id, msg):
        if msg.id == 195:
            raise RuntimeError('failed page')
        return original(channel_id, msg)
    tg.index = broken
    await tg.search_series(identity, ['Test Show'])
    assert not store.catalog()
    assert store.series_page(identity, 'Test Show', None) is None
    tg.index = original
    await tg.search_series(identity, ['Test Show'])
    assert len(store.series(identity, {123})) == 200
    assert store.series_page(identity, 'Test Show', None)['complete']


@pytest.mark.asyncio
async def test_completed_series_refreshes_head_after_cache_window(setup):
    _, store, tg, _, _ = setup
    tg.client = PagedClient(user(), 50)
    identity = series_id('Test Show')
    await tg.search_series(identity, ['Test Show'])
    tg.client.messages.insert(0, message(user(), 51, 'Test.Show.S01E51.mp4'))
    with store.db:
        store.db.execute('UPDATE series_search_pages SET head_checked_at=0')
    await tg.search_series(identity, ['Test Show'])
    assert len(store.series(identity, {123})) == 51
    assert len(tg.client.requests) == 2


@pytest.mark.asyncio
async def test_invalid_saved_peer_restarts_query_without_removing_cached_episodes(setup):
    from telethon import errors
    _, store, tg, _, _ = setup
    identity = series_id('Test Show')
    tg.client = PagedClient(user(), 1200)
    await tg.search_series(identity, ['Test Show'])
    original = tg._series_page
    async def invalid_peer(*args):
        raise errors.PeerIdInvalidError(request=None)
    tg._series_page = invalid_peer
    await tg.search_series(identity, ['Test Show'])
    assert len(store.series(identity, {123})) == 800
    assert json.loads(store.series_page(identity, 'Test Show', None)['cursor']) == {}
    tg._series_page = original
    tg.client.requests.clear()
    await tg.search_series(identity, ['Test Show'])
    assert tg.client.requests[0].offset_id == 0
