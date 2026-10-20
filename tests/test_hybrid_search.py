import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from telethon import types, utils, functions

from stremio_addon.app import create_app_with_runtime
from stremio_addon.core import Settings, Store, parse_title, series_id, source_name
from stremio_addon.metadata import Metadata
from stremio_addon.runtime import Runtime
from stremio_addon.search import OnDemandSearch
from stremio_addon.telegram import Telegram
from tests.test_series import video


def user(identity=123, **kw):
    return types.User(id=identity, access_hash=identity, first_name='Contact', **kw)


def channel(identity=1, title='Test Show'):
    return types.Channel(id=identity, access_hash=identity, title=title,
                         photo=types.ChatPhotoEmpty(), date=datetime.now(timezone.utc), megagroup=True)


def message(peer, identity, filename='Test.Movie.2024.mp4', caption=''):
    doc = types.Document(id=identity, access_hash=1, file_reference=b'x',
                         date=datetime.now(timezone.utc), mime_type='video/mp4', size=10,
                         dc_id=1, attributes=[types.DocumentAttributeFilename(filename)])
    return types.Message(id=identity, peer_id=utils.get_peer(peer), date=datetime.now(timezone.utc),
                         message=caption, media=types.MessageMediaDocument(document=doc))


class Client:
    def __init__(self, entities, messages=()):
        self.entities, self.messages = list(entities), list(messages)
        self.requests = []
        self.fail = False

    def is_connected(self):
        return True

    async def disconnect(self):
        pass

    async def iter_dialogs(self):
        for entity in self.entities:
            yield SimpleNamespace(entity=entity)

    async def __call__(self, request):
        assert isinstance(request, functions.messages.SearchGlobalRequest)
        self.requests.append(request)
        await asyncio.sleep(0)
        if self.fail:
            raise OSError('private error details')
        return types.messages.Messages(self.messages, [],
                                        [e for e in self.entities if not isinstance(e, types.User)],
                                        [e for e in self.entities if isinstance(e, types.User)])

    async def get_messages(self, entity, **kwargs):
        self.requests.append((entity, kwargs))
        matched = [m for m in self.messages if utils.get_peer_id(m.peer_id) == utils.get_peer_id(entity)]
        if 'ids' in kwargs:
            return next((m for m in matched if m.id == kwargs['ids']), None)
        return matched


@pytest_asyncio.fixture
async def setup(tmp_path):
    cfg = Settings(8000, 'https://example.com', 'a' * 32, 1, 'hash', '', tmp_path)
    store = Store(tmp_path / 'db')
    tg = Telegram(cfg, store)
    # Keep tests deterministic; production pacing itself is exercised below.
    async def operation(function, *args, **kwargs):
        return await function(*args, **kwargs)
    tg.background_operation = operation
    metadata = Metadata()
    hybrid = OnDemandSearch(store, tg, metadata)
    yield cfg, store, tg, metadata, hybrid
    await tg.close()
    await metadata.http.aclose()
    await store.close()


@pytest.mark.asyncio
async def test_fresh_account_live_search_persists_playable_private_video_without_checkpoint(setup):
    _, store, tg, _, hybrid = setup
    contact = user()
    tg.client = Client([contact], [message(contact, 7)])
    rows, live = await hybrid.catalog('Test Movie', kind='movie')
    assert [r['id'] for r in rows] == ['tg:123:7']
    assert live == {'tg:123:7'}
    assert rows[0]['channel_name'] == 'Contact'
    assert store.checkpoint(123) == dict(channel=123, oldest=0, newest=0, complete=0)
    assert (await tg.message(rows[0])).id == 7


@pytest.mark.asyncio
async def test_live_runs_even_with_complete_index_and_merges_duplicates(setup):
    _, store, tg, _, hybrid = setup
    source = channel()
    peer = utils.get_peer_id(source)
    tg.channels[peer] = source
    store.upsert(video(1, 'Test.Movie.2024.mp4', channel=peer))
    store.save_checkpoint(dict(channel=peer, oldest=1, newest=1, complete=1))
    tg.client = Client([source], [message(source, 1), message(source, 2)])
    rows, _ = await hybrid.catalog('Test Movie', kind='movie')
    assert {r['message'] for r in rows} == {1, 2}
    assert store.checkpoint(peer)['newest'] == 1
    await hybrid.catalog('Test Movie', kind='movie')
    assert len(tg.client.requests) == 1
    tg.search_cache = {k: (0, ids) for k, (_, ids) in tg.search_cache.items()}
    await hybrid.catalog('Test Movie', kind='movie')
    assert len(tg.client.requests) == 2


@pytest.mark.asyncio
async def test_identical_requests_coalesce_and_cancellation_does_not_cancel_other_waiter(setup):
    _, _, tg, _, _ = setup
    tg.client = Client([user()], [message(user(), 1)])
    first = asyncio.create_task(tg.search('Test Movie'))
    second = asyncio.create_task(tg.search('Test Movie'))
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert await second == ['tg:123:1']
    assert len(tg.client.requests) == 1


@pytest.mark.asyncio
async def test_filter_applies_to_discovery_global_scoped_events_and_private_ids(setup):
    _, store, tg, _, hybrid = setup
    allowed, other = user(), channel()
    tg.channel_ids = frozenset({123})
    tg.client = Client([allowed, other], [message(allowed, 1), message(other, 2)])
    await tg.discover()
    assert set(tg.channels) == {123}
    rows, _ = await hybrid.catalog('Test Movie')
    assert [r['id'] for r in rows] == ['tg:123:1']
    assert await tg.search('Test', utils.get_peer_id(other)) == []
    assert not store.get(f'tg:{utils.get_peer_id(other)}:2')
    await tg.update(SimpleNamespace(chat_id=utils.get_peer_id(other)))
    assert set(tg.channels) == {123}


@pytest.mark.asyncio
async def test_discovery_saved_messages_bots_and_contact_update_delete(setup):
    _, store, tg, _, _ = setup
    me, contact, bot = user(100, is_self=True), user(200), user(300, bot=True)
    tg.me = me
    tg.client = Client([contact, bot, user(400, deleted=True)])
    await tg.discover()
    assert set(tg.channels) == {100, 200, 300}
    assert source_name(me) == 'Saved Messages'
    m = message(contact, 7)
    assert await store.run(tg.index, 200, m)
    await tg.update(SimpleNamespace(chat_id=200, message=m))
    assert store.get('tg:200:7')
    await tg.deleted(SimpleNamespace(chat_id=None, deleted_ids=[7]))
    assert not store.get('tg:200:7')
    # Account-global deletion must not remove a channel video with the same ID.
    store.upsert(video(7, 'Movie.mp4', channel=-1000000000001))
    await tg.deleted(SimpleNamespace(chat_id=None, deleted_ids=[7]))
    assert store.get('tg:-1000000000001:7')


@pytest.mark.asyncio
@pytest.mark.parametrize('marker', ['S01E03', 'עונה 1 פרק 3'])
async def test_series_source_name_context_local_and_live(setup, marker):
    _, store, tg, metadata, hybrid = setup
    source = channel(title='Test Show')
    peer = utils.get_peer_id(source)
    tg.channels[peer] = source
    tg.client = Client([source], [message(source, 3, marker + '.mp4', marker)])
    async def resolve(*args):
        return ['Test Show'], None
    metadata.resolve = resolve
    rows, _ = await hybrid.match('series', 'tt1234567:1:3')
    assert len(rows) == 1 and rows[0]['title'] == 'Test Show'
    assert rows[0]['series_id'] == series_id('Test Show')
    assert isinstance(tg.client.requests[0], tuple)  # scoped fallback
    rows, _ = await hybrid.match('series', series_id('Test Show') + ':1:3')
    assert len(rows) == 1
    assert parse_title('Other.Show.S01E03.mp4', '', 'Test Show')['title'] == 'Other Show'
    assert parse_title('S01E03.mp4', '', '')['title'] != 'Contact'


@pytest.mark.asyncio
async def test_movie_matching_missing_year_subtitle_and_conflicts(setup):
    _, store, _, metadata, _ = setup
    async def resolve(*args):
        return ['Test Movie: The Return', 'סרט בדיקה'], 2024
    metadata.resolve = resolve
    for number, filename in enumerate(['Test.Movie.The.Return.2024.mp4', 'Test.Movie.mp4',
                                        'Test.Movie.2020.mp4', 'סרט בדיקה.mp4',
                                        'Test.Other.Movie.mp4', 'Test.Movie.S01E01.mp4'], 1):
        store.upsert(video(number, filename))
    rows = await metadata.match(store, 'movie', 'tt1234567')
    assert [r['message'] for r in rows] == [1, 4, 2]
    with store.db:
        store.db.execute('INSERT INTO mappings VALUES (?,?)', ('tg:-100:2', 'tt9999999'))
    assert {r['message'] for r in await metadata.match(store, 'movie', 'tt1234567')} == {1, 4}


@pytest.mark.asyncio
async def test_live_failure_and_timeout_keep_local_results_and_do_not_cache_failure(setup, caplog):
    _, store, tg, _, hybrid = setup
    contact = user()
    tg.channels[123] = contact
    store.upsert(video(1, 'Test.Movie.mp4', channel=123))
    tg.client = Client([contact])
    tg.client.fail = True
    rows, _ = await hybrid.catalog('Test Movie')
    assert len(rows) == 1 and not tg.search_cache
    assert 'private error details' not in caplog.text
    async def stalled(*args):
        await asyncio.Event().wait()
    tg._search = stalled
    tg.search_timeout = .01
    rows, _ = await hybrid.catalog('Test Movie')
    assert len(rows) == 1 and not tg.search_cache


@pytest.mark.asyncio
async def test_scoped_cache_separated_and_empty_success_cached(setup):
    _, _, tg, _, _ = setup
    source = channel()
    peer = utils.get_peer_id(source)
    tg.channels[peer] = source
    tg.client = Client([source])
    assert await tg.search('S01E01') == []
    assert await tg.search('S01E01', peer) == []
    assert await tg.search('S01E01', peer) == []
    assert len(tg.client.requests) == 2


@pytest.mark.asyncio
async def test_search_uses_shared_pacing_flood_state_and_playback_priority(setup):
    _, _, tg, _, _ = setup
    # Run the real RPC gate (a mock high-level client doesn't invoke Telethon's __call__).
    calls = []
    async def rpc(*args):
        calls.append(True)
        return []
    tg.flood_until = time.monotonic() + .03
    tg.active_playbacks = 1
    tg.playback_idle.clear()
    request = asyncio.create_task(tg.background_request(rpc))
    await asyncio.sleep(.04)
    assert not calls
    tg.active_playbacks = 0
    tg.playback_idle.set()
    await request
    assert calls and tg.next_scan_request > time.monotonic()


@pytest.mark.asyncio
async def test_context_migration_preserves_checkpoint_mapping_and_fts(tmp_path):
    path = tmp_path / 'index'
    store = Store(path)
    entry = video(3, 'S01E03.mp4')
    entry['channel_name'] = 'Test Show'
    store.upsert(entry)
    store.save_checkpoint(dict(channel=-100, oldest=1, newest=3, complete=1))
    with store.db:
        store.db.execute('INSERT INTO mappings VALUES (?,?)', (entry['id'], 'tt1234567'))
        store.db.execute('PRAGMA user_version=3')
    await store.close()
    store = Store(path)
    assert store.get(entry['id'])['title'] == 'Test Show'
    assert store.catalog('Test Show')
    assert store.explicit('tt1234567')
    assert store.checkpoint(-100)['complete'] == 1
    assert store.db.execute('SELECT count(*) FROM search').fetchone()[0] == 1
    await store.close()


def test_http_catalog_lookup_debug_and_private_playback(tmp_path):
    class Gateway(Telegram):
        async def start(self):
            contact = user()
            self.client = Client([contact], [message(contact, 7)])
            async def operation(function, *args, **kwargs):
                return await function(*args, **kwargs)
            self.background_operation = operation
    cfg = Settings(8000, 'https://example.com', 'a' * 32, 1, 'hash', '', tmp_path)
    runtime = Runtime(cfg, Gateway)
    with TestClient(create_app_with_runtime(runtime)) as client:
        response = client.get(f'/{cfg.key}/catalog/movie/telegram/search=Test%20Movie.json')
        assert response.status_code == 200
        assert [m['id'] for m in response.json()['metas']] == ['tg:123:7']
        async def resolve(*args):
            return ['Test Movie'], None
        runtime.metadata.resolve = resolve
        streams = client.get(f'/{cfg.key}/stream/movie/tt1234567.json').json()['streams']
        assert len(streams) == 1
        play = streams[0]['url'].replace(cfg.url, '')
        response = client.head(play, headers={'Range': 'bytes=2-5'})
        assert response.status_code == 206 and response.headers['Content-Length'] == '4'
        from stremio_addon.debug import create_debug_app
        with TestClient(create_debug_app(runtime)) as debug:
            response = debug.get('/api/search', params={'q': 'Test Movie'}, headers={'X-Debug-Key': cfg.key})
            assert response.status_code == 200
            assert response.json()['results'][0]['matched_by'] == 'Telegram live search (60s cache)'
            assert response.json()['channels_queried'][0]['name'] == 'Contact'


@pytest.mark.asyncio
async def test_raw_search_paginates_past_nonvideo_messages_and_bounds_pages(setup):
    _, _, tg, _, _ = setup
    source = user()
    requests = []
    async def paginated(request):
        requests.append(request)
        if len(requests) == 1:
            messages = [types.Message(id=i, peer_id=types.PeerUser(123), date=datetime.now(timezone.utc),
                                      message='Test Movie') for i in range(200, 100, -1)]
        else:
            messages = [message(source, 100)]
        return types.messages.MessagesSlice(count=101, messages=messages, topics=[], chats=[], users=[source], next_rate=42)
    class PagedClient(Client):
        async def __call__(self, request):
            return await paginated(request)
    tg.client = PagedClient([source])
    assert await tg.search('Test Movie') == ['tg:123:100']
    assert len(requests) == 2
    assert requests[1].offset_id == 101 and requests[1].offset_rate == 42
    assert requests[1].offset_peer.user_id == 123


@pytest.mark.asyncio
async def test_public_tmdb_movie_supports_existing_imdb_mapping(setup):
    import httpx
    from stremio_addon.tmdb import TMDB
    cfg, store, tg, metadata, _ = setup
    cfg.tmdb_api_key = 'test'
    service = TMDB(cfg, store, tg)
    metadata.tmdb = service
    def response(request):
        assert request.url.path == '/3/movie/42'
        return httpx.Response(200, json={'title': 'סרט בדיקה', 'original_title': 'Test Movie',
                                        'release_date': '2024-01-01', 'imdb_id': 'tt1234567',
                                        'translations': {'translations': [
                                            {'iso_639_1': 'en', 'data': {'title': 'Test Movie'}}]}})
    await service.http.aclose()
    service.http = httpx.AsyncClient(transport=httpx.MockTransport(response))
    store.upsert(video(1, 'Test.Movie.mp4'))
    with store.db:
        store.db.execute('INSERT INTO mappings VALUES (?,?)', ('tg:-100:1', 'tt1234567'))
    assert [r['message'] for r in await metadata.match(store, 'movie', 'tmdb:42')] == [1]
    aliases, _, _, _ = await metadata.search_target(store, 'movie', 'tmdb:42')
    assert aliases == ['סרט בדיקה', 'Test Movie']
    await service.http.aclose()


@pytest.mark.asyncio
async def test_live_preserves_saved_show_identity_and_searches_offset_episode(setup):
    from stremio_addon.tmdb import TMDB
    cfg, store, tg, metadata, hybrid = setup
    source = channel(title='Test Show')
    peer = utils.get_peer_id(source)
    tg.channels[peer] = source
    # Reproduce an episode-only title that was already manually mapped.
    entry = video(1, 'S02E11.mp4', channel=peer)
    store.upsert(entry)
    saved_id = series_id(entry['title'])
    tmdb = TMDB(cfg, store, tg)
    metadata.tmdb = tmdb
    tmdb.save(saved_id, {'tmdb': 42, 'imdb': 'tt1234567', 'name': 'Test Show'},
              'manual', season_offset=1, episode_offset=10)
    tg.client = Client([source], [message(source, 1, 'S02E11.mp4'), message(source, 2, 'S02E12.mp4')])
    rows, _ = await hybrid.match('series', 'tt1234567:1:1')
    assert [r['message'] for r in rows] == [1]
    assert store.get(entry['id'])['series_id'] == saved_id
    assert tg.client.requests[0][1]['search'] == 'S02E11'
    # A normal title-based saved identity also discovers a new mapped episode.
    new = video(3, 'Test.Show.S02E11.mp4', channel=peer)
    store.upsert(new)
    tmdb.save(series_id('Test Show'), {'tmdb': 43, 'imdb': 'tt7654321', 'name': 'Test Show'},
              'manual', season_offset=1, episode_offset=10)
    tg.client.messages = [message(source, 4, 'S02E12.mp4')]
    tg.search_cache.clear()
    rows, _ = await hybrid.match('series', 'tt7654321:1:2')
    assert {r['message'] for r in rows} == {4, 2}
    await tmdb.http.aclose()


@pytest.mark.asyncio
async def test_startup_and_idle_discover_chats_without_reading_history(setup):
    _, store, tg, _, _ = setup
    handlers, reads = [], []
    class LifecycleClient(Client):
        async def connect(self):
            pass
        async def is_user_authorized(self):
            return True
        async def get_me(self):
            return user(999, is_self=True)
        def add_event_handler(self, callback, builder):
            handlers.append(type(builder).__name__)
        async def get_messages(self, *args, **kwargs):
            reads.append(kwargs)
            raise AssertionError('Startup must not read message history')
    tg.client = LifecycleClient([user()])
    # Old indexes stay available; they must not restart history scans.
    store.save_checkpoint(dict(channel=123, oldest=100, newest=200, complete=0))
    await tg.start()
    first = tg.task
    assert tg.request_refresh()['id'] == tg.refresh_status['id']
    await first
    await asyncio.sleep(.01)
    assert tg.task.done() and not reads
    assert set(tg.channels) == {123, 999}
    assert handlers == ['MessageEdited', 'MessageDeleted']
    assert store.checkpoint(123)['oldest'] == 100


@pytest.mark.asyncio
async def test_unsolicited_messages_do_not_expand_video_cache(setup):
    _, store, tg, _, _ = setup
    tg.channels[123] = user()
    await tg.update(SimpleNamespace(chat_id=123, message=message(user(), 99)))
    assert not store.catalog()


@pytest.mark.asyncio
async def test_opening_series_discovers_more_episodes_on_demand(setup):
    _, store, tg, _, search = setup
    source = channel()
    peer = utils.get_peer_id(source)
    tg.channels[peer] = source
    store.upsert(video(1, 'Test.Show.S01E01.mp4', channel=peer))
    tg.client = Client([source], [message(source, 2, 'S01E02.mp4')])
    rows = await search.show(series_id('Test Show'))
    assert {r['episode'] for r in rows} == {1, 2}
    assert store.checkpoint(peer)['complete'] == 0


@pytest.mark.asyncio
async def test_ai_search_finds_uncached_titles_on_telegram(setup):
    from stremio_addon.ai_search import AISearch
    cfg, store, tg, _, search = setup
    tg.client = Client([user()], [message(user(), 7)])
    ai = AISearch(cfg, store)
    async def titles(query):
        return {'movies': ['Test Movie'], 'series': []}
    ai.titles = titles
    ai.lookup = search.discover_titles
    try:
        assert not store.catalog()
        rows = await ai.search('a description', set())
        assert [r['id'] for r in rows] == ['tg:123:7']
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_metadata_worker_only_matches_user_requested_shows(setup):
    from unittest.mock import AsyncMock
    from stremio_addon.tmdb import TMDB
    cfg, store, tg, _, _ = setup
    store.upsert(video(1, 'Old.Show.S01E01.mp4'))
    service = TMDB(cfg, store, tg)
    service.match_show = AsyncMock()
    task = asyncio.create_task(service.run())
    try:
        await asyncio.sleep(0)
        service.match_show.assert_not_awaited()
        requested = dict(series_id=series_id('Requested Show'))
        service.enqueue([requested, requested])
        await asyncio.wait_for(service.pending.join(), 1)
        service.match_show.assert_awaited_once_with(requested['series_id'])
        with tg.playback():
            service.enqueue([dict(series_id=series_id('Next Show'))])
            await asyncio.sleep(0)
            assert service.match_show.await_count == 1
        tg.playback_until = 0
        await asyncio.wait_for(service.pending.join(), 1)
        assert service.match_show.await_count == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.http.aclose()


def test_legacy_scan_interval_is_ignored(monkeypatch):
    from addon.start import configure
    for key, value in dict(ADDON_URL='https://example.com', ADDON_ACCESS_KEY='a'*32,
                           TELEGRAM_APP_API_ID='1', TELEGRAM_APP_API_HASH='hash',
                           USER_SESSION_STRING='session', SCAN_INTERVAL_SECONDS='invalid').items():
        monkeypatch.setenv(key, value)
    assert not hasattr(Settings.env(), 'scan_interval_seconds')
    env = {}
    configure({'SCAN_INTERVAL_SECONDS': 60}, env)
    assert 'SCAN_INTERVAL_SECONDS' not in env
