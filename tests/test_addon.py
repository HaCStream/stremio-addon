from types import SimpleNamespace
import json
from urllib.parse import quote
import pytest
from fastapi.testclient import TestClient
from stremio_addon.core import Settings, Store, Tokens, byte_range, normalize, parse_title
from stremio_addon.app import create_app
from stremio_addon.debug import create_debug_app
from stremio_addon.metadata import Metadata
from stremio_addon.runtime import Runtime
from stremio_addon.telegram import Telegram, CHUNK


def row(**updates):
    result = dict(id='tg:-100123:7', channel=-100123, message=7, title='שם הסרט', filename='שם הסרט 2024.mp4', caption='כיתוב בעברית', channel_name='ערוץ', size=10, mime='video/mp4', date=1, year=2024, season=None, episode=None, imdb=None, quality='1080p', file_id='99')
    result.update(updates)
    return result


@pytest.mark.parametrize('build_version,expected', [
    ('1.1.0', '1.1.0'), ('v1.2.3', '1.2.3'),
    ('1.2.0-rc.1', '1.2.0-rc.1'), ('2.0.0+build.1', '2.0.0+build.1'),
    ('main', '0.0.0-dev'), (None, '0.0.0-dev'),
    ('01.2.3', '0.0.0-dev'), ('1.2.3-01', '0.0.0-dev'),
])
def test_manifest_build_version(tmp_path, monkeypatch, build_version, expected):
    if build_version is None:
        monkeypatch.delenv('APP_VERSION', raising=False)
    else:
        monkeypatch.setenv('APP_VERSION', build_version)
    key = 'a' * 32
    cfg = Settings(8000, 'https://example.com', key, 1, 'hash', 'session', tmp_path)
    with TestClient(create_app(cfg, FakeTelegram)) as client:
        response = client.get(f'/{key}/manifest.json')
        assert response.status_code == 200
        assert response.json()['version'] == expected


@pytest.mark.parametrize('value,expected', [(None,(0,9,200)),('bytes=2-5',(2,5,206)),('bytes=7-',(7,9,206)),('bytes=-3',(7,9,206)),('bytes=-99',(0,9,206)),('bytes=0-99',(0,9,206))])
def test_ranges(value, expected):
    assert byte_range(value, 10) == expected


@pytest.mark.parametrize('value', ['bytes=10-', 'bytes=7-2', 'bytes=-0', 'bytes=-', 'bytes=1-2,4-5', 'bad'])
def test_invalid_ranges(value):
    with pytest.raises(ValueError):
        byte_range(value, 10)


def test_hebrew():
    assert normalize('\u200fשָׁלוֹם־עוֹלָם') == normalize('שלום עולם')
    assert normalize('צה״ל') == normalize('צה"ל')
    p = parse_title('שם.הסדרה עונה 2 פרק 5 1080p.mkv', '')
    assert (p['title'], p['season'], p['episode']) == ('שם הסדרה', 2, 5)
    assert parse_title('Show.S02E05.mkv', '')['episode'] == 5


def test_store(tmp_path):
    db = Store(tmp_path / 'db')
    db.upsert(row())
    assert db.catalog('שֵׁם')[0]['title'] == 'שם הסרט'
    assert db.catalog('כיתוב')
    assert not db.catalog('" OR *')
    db.upsert(row(caption='חדש'))
    assert not db.catalog('כיתוב')
    assert db.catalog('חדש')
    db.delete('tg:-100123:7')
    assert not db.catalog()
    db.db.close()


def test_tokens():
    t = Tokens('a' * 32)
    token = t.sign('tg:-100123:7', 'play')
    assert t.verify(token, 'play') == 'tg:-100123:7'
    for value, scope in [(token + 'x', 'play'), (token, 'thumb'), (t.sign('tg:-100123:7', 'play', -1), 'play')]:
        with pytest.raises(ValueError):
            t.verify(value, scope)


@pytest.mark.parametrize('value,expected', [
    (None, None), ('', None), ('  ', None),
    ('-100123,-100456', frozenset({-100123, -100456})),
    (' -100123, -100123 ', frozenset({-100123})),
])
def test_channel_ids_settings(monkeypatch, value, expected):
    import stremio_addon.core as core
    monkeypatch.setattr(core.Path, 'is_file', lambda self: False)
    for key, setting in dict(ADDON_URL='https://example.com', API_KEY='a'*32,
                             API_ID='123', API_HASH='hash', USER_SESSION_STRING='session').items():
        monkeypatch.setenv(key, setting)
    monkeypatch.delenv('channel_ids', raising=False)
    monkeypatch.delenv('CHANNEL_IDS', raising=False)
    if value is not None:
        monkeypatch.setenv('CHANNEL_IDS', value)
    assert Settings.env().channel_ids == expected


@pytest.mark.parametrize('value', ['123', '-0', '-123,', ',-123', '-123,, -456', 'channel'])
def test_channel_ids_reject_invalid(monkeypatch, value):
    with pytest.raises(ValueError, match='CHANNEL_IDS'):
        test_channel_ids_settings(monkeypatch, value, None)


@pytest.mark.asyncio
async def test_channel_filter_and_reselection(tmp_path):
    from datetime import datetime, timezone
    from telethon import types, utils
    store = Store(tmp_path / 'db')
    cfg = Settings(8000, 'http://localhost', 'a'*32, 1, 'hash', '', tmp_path)
    gateway = Telegram(cfg, store)
    entities = [types.Channel(id=i, title=str(i), photo=types.ChatPhotoEmpty(),
                              date=datetime.now(timezone.utc), broadcast=True)
                for i in (123, 456)]
    ids = [utils.get_peer_id(e) for e in entities]
    class Client:
        async def iter_dialogs(self):
            for entity in entities:
                yield SimpleNamespace(entity=entity)
    gateway.client = Client()
    await gateway.discover()
    assert set(gateway.channels) == set(ids)
    store.upsert(row(id=f'tg:{ids[1]}:7', channel=ids[1]))
    store.save_checkpoint(dict(channel=ids[1], oldest=1, newest=7, complete=1))
    gateway.channel_ids = frozenset({ids[0]})
    await gateway.discover()
    assert set(gateway.channels) == {ids[0]}
    assert not store.catalog()
    assert store.checkpoint(ids[1])['complete'] == 0
    gateway.channel_ids = None
    await gateway.discover()
    assert set(gateway.channels) == set(ids)
    assert store.checkpoint(ids[1])['oldest'] == 0
    store.db.close()


@pytest.mark.asyncio
async def test_discover_public_channels_and_membership(tmp_path):
    from datetime import datetime, timezone
    from telethon import types, utils
    store = Store(tmp_path / 'db')
    cfg = Settings(8000, 'http://localhost', 'a'*32, 1, 'hash', '', tmp_path)
    gateway = Telegram(cfg, store)

    def channel(id, **kwargs):
        return types.Channel(id=id, title=str(id), photo=types.ChatPhotoEmpty(),
                             date=datetime.now(timezone.utc), **kwargs)

    private = channel(123, broadcast=True)
    public = channel(456, broadcast=True, username='public_channel')
    public_alias = channel(789, broadcast=True,
                           usernames=[types.Username(username='public_alias', active=True)])
    entities = [private]

    class Client:
        async def iter_dialogs(self):
            for entity in entities:
                yield SimpleNamespace(entity=entity)

    gateway.client = Client()
    await gateway.discover()
    assert set(gateway.channels) == {utils.get_peer_id(private)}

    # A later discovery (including Sync now) must pick up newly joined channels.
    entities.extend([public, public_alias,
                     channel(900, broadcast=True, username='left_channel', left=True),
                     channel(901, megagroup=True, username='public_group'),
                     types.Chat(id=902, title='group', photo=types.ChatPhotoEmpty(),
                                participants_count=1, date=datetime.now(timezone.utc), version=1)])
    await gateway.discover()
    assert set(gateway.channels) == {utils.get_peer_id(entity)
                                     for entity in (private, public, public_alias)}

    gateway.channel_ids = frozenset({utils.get_peer_id(public)})
    await gateway.discover()
    assert set(gateway.channels) == {utils.get_peer_id(public)}
    store.db.close()


def test_home_assistant_options_fallback(tmp_path, monkeypatch):
    import stremio_addon.core as core
    options = tmp_path / 'options.json'
    options.write_text('''{
      "PORT": 9123,
      "ADDON_URL": "https://telegram.example.com",
      "API_KEY": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "API_ID": 12345,
      "API_HASH": "hash",
      "USER_SESSION_STRING": "session",
      "CACHE_MB": 42,
      "CHANNEL_IDS": "-100123,-100456"
    }''')
    real_path = core.Path
    monkeypatch.setattr(core, 'Path', lambda value: options if value == '/data/options.json' else real_path(value))
    monkeypatch.delenv('CHANNEL_IDS', raising=False)
    monkeypatch.delenv('channel_ids', raising=False)
    for name in ('PORT', 'ADDON_URL', 'API_KEY', 'API_ID', 'API_HASH', 'USER_SESSION_STRING', 'CACHE_MB',
                 'port', 'addon_url', 'api_key', 'api_id', 'api_hash', 'user_session_string', 'cache_mb'):
        monkeypatch.delenv(name, raising=False)
    settings = core.Settings.env()
    assert settings.port == 8000
    assert settings.url == 'https://telegram.example.com'
    assert settings.api_id == 12345
    assert settings.cache_bytes == 42 * 1024**2
    assert settings.channel_ids == frozenset({-100123, -100456})
    assert settings.data == real_path('/data/stremio')


def test_environment_overrides_home_assistant_options(tmp_path, monkeypatch):
    import stremio_addon.core as core
    options = tmp_path / 'options.json'
    options.write_text('{"ADDON_URL":"https://ha.example.com"}')
    real_path = core.Path
    monkeypatch.setattr(core, 'Path', lambda value: options if value == '/data/options.json' else real_path(value))
    monkeypatch.setenv('ADDON_URL', 'https://environment.example.com')
    monkeypatch.setenv('API_KEY', 'a' * 32)
    monkeypatch.setenv('API_ID', '123')
    monkeypatch.setenv('API_HASH', 'hash')
    monkeypatch.setenv('USER_SESSION_STRING', 'session')
    assert core.Settings.env().url == 'https://environment.example.com'


def test_nested_home_assistant_debug_and_ai_options(tmp_path, monkeypatch):
    import stremio_addon.core as core
    options = tmp_path / 'options.json'
    options.write_text(json.dumps({
        'ADDON_URL': 'https://ha.example.com', 'API_KEY': 'a' * 32,
        'API_ID': 123, 'API_HASH': 'hash', 'USER_SESSION_STRING': 'session',
        'debug': {'DEBUG_ENABLED': False, 'SKIP_DEBUG_AUTH': True},
        'ai': {'AI_SEARCH_ENABLED': True, 'GEMINI_API_KEY': 'secret',
               'REQUIRE_AI_SUFFIX_FOR_AI_SEARCH': False},
    }))
    real_path = core.Path
    monkeypatch.setattr(core, 'Path', lambda value: options if value == '/data/options.json' else real_path(value))
    for name in ('ADDON_URL', 'API_KEY', 'API_ID', 'API_HASH', 'USER_SESSION_STRING',
                 'DEBUG_ENABLED', 'DEBUG_PORT', 'SKIP_DEBUG_AUTH', 'AI_SEARCH_ENABLED', 'GEMINI_API_KEY',
                 'REQUIRE_AI_SUFFIX_FOR_AI_SEARCH'):
        monkeypatch.delenv(name, raising=False)
    settings = core.Settings.env()
    assert not settings.debug_enabled
    assert settings.skip_debug_auth
    assert settings.debug_port == 8001
    assert settings.ai_search_enabled
    assert not settings.require_ai_suffix_for_ai_search
    assert settings.gemini_api_key == 'secret'


def test_legacy_home_assistant_options_remain_readable(tmp_path, monkeypatch):
    import stremio_addon.core as core
    options = tmp_path / 'options.json'
    options.write_text('{"addon_url":"https://legacy.example.com",'
                       '"api_key":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
                       '"api_id":123,"api_hash":"hash",'
                       '"user_session_string":"session"}')
    real_path = core.Path
    monkeypatch.setattr(core, 'Path', lambda value: options if value == '/data/options.json' else real_path(value))
    for name in ('ADDON_URL', 'API_KEY', 'API_ID', 'API_HASH', 'USER_SESSION_STRING'):
        monkeypatch.delenv(name, raising=False)
    assert core.Settings.env().url == 'https://legacy.example.com'


def test_lowercase_container_environment_is_ignored(monkeypatch):
    import stremio_addon.core as core
    monkeypatch.setattr(core.Path, 'is_file', lambda self: False)
    monkeypatch.setenv('addon_url', 'https://legacy.example.com')
    monkeypatch.delenv('ADDON_URL', raising=False)
    with pytest.raises(ValueError, match='ADDON_URL'):
        core.Settings.env()


def test_legacy_port_options_and_environment_cannot_change_listeners(monkeypatch):
    import stremio_addon.core as core
    monkeypatch.setattr(core.Path, 'is_file', lambda self: False)
    for name, value in dict(PORT='8000', DEBUG_PORT='8000', ADDON_URL='https://example.com',
                            API_KEY='a' * 32, API_ID='1', API_HASH='hash',
                            USER_SESSION_STRING='session').items():
        monkeypatch.setenv(name, value)
    settings = Settings.env()
    assert (settings.port, settings.debug_port) == (8000, 8001)


class FakeTelegram:
    def __init__(self, cfg, store):
        self.store = store
        self.channels = {-100123: 'channel'}
        self.client = SimpleNamespace(is_connected=lambda: True)
        self.status = {'phase': 'ready'}
        self.sync_requests = 0
        self.sync_status = {"id": None, "state": "idle", "error": None}
    async def start(self):
        self.store.upsert(row())
    async def close(self):
        pass
    async def message(self, row):
        return SimpleNamespace(document=SimpleNamespace(size=10))
    async def stream(self, row, message, start, end):
        yield b'0123456789'[start:end + 1]
    def request_sync(self):
        self.sync_requests += 1
        self.sync_status = {"id": "test-sync", "state": "queued", "error": None}
        return dict(self.sync_status)


def test_debug_dashboard_is_read_only_and_shows_queried_channels(tmp_path):
    key = 'a' * 32
    cfg = Settings(8000, 'https://public.example.com/prefix', key, 1, 'hash',
                   'session', tmp_path, debug_port=9123)
    app = create_debug_app(Runtime(cfg, FakeTelegram))
    with TestClient(app) as client:
        assert client.get('/').status_code == 200
        assert client.get('/api/overview').status_code == 401
        assert client.get('/api/auth-config').json() == {'skip_debug_auth': False}
        headers = {'X-Debug-Key': key}
        overview = client.get('/api/overview', headers=headers).json()
        assert overview['debug_port'] == 9123
        assert overview['videos'] == 1
        response = client.get('/api/search', params={'q': 'שם', 'mode': 'text'},
                              headers=headers)
        assert response.status_code == 200
        result = response.json()
        assert result['result_count'] == 1
        assert result['channels_queried'][0]['id'] == -100123
        assert result['results_by_channel']['-100123'] == 1
        assert result['results'][0]['matched_by'] == 'full-text index'
        assert 'url' not in result['results'][0]
        assert client.get('/play/anything', headers=headers).status_code == 404
        assert client.get('/thumb/anything', headers=headers).status_code == 404
        assert client.get('/api/sync').status_code == 401
        assert client.post('/api/sync').status_code == 401
        response = client.post('/api/sync', headers=headers)
        assert response.status_code == 202
        assert response.json()['sync']['state'] == 'queued'
        assert client.get('/api/sync', headers=headers).json()['sync']['id'] == 'test-sync'
        app.state.runtime.tg.sync_status.update(state='done')
        assert client.get('/api/sync', headers=headers).json()['sync']['state'] == 'done'
        assert app.state.runtime.tg.sync_requests == 1
        events = client.get('/api/activity', headers=headers).json()['events']
        assert events[0]['event'] == 'sync_requested'


def test_skip_debug_auth_only_applies_to_debug_dashboard(tmp_path):
    key = 'a' * 32
    cfg = Settings(8000, 'https://example.com', key, 1, 'hash', 'session', tmp_path,
                   skip_debug_auth=True)
    debug_app = create_debug_app(Runtime(cfg, FakeTelegram))
    with TestClient(debug_app) as client:
        assert client.get('/api/auth-config').json() == {'skip_debug_auth': True}
        assert client.get('/api/overview').status_code == 200
        assert client.get('/api/channels').status_code == 200
        assert client.get('/api/activity').status_code == 200
        assert client.get('/api/search', params={'q': 'שם'}).status_code == 200
        assert client.post('/api/sync').status_code == 202
    with TestClient(create_app(cfg, FakeTelegram)) as client:
        assert client.get('/wrong/manifest.json').status_code == 404
        assert client.get(f'/{key}/manifest.json').status_code == 200


def test_http(tmp_path):
    key = 'a' * 32
    cfg = Settings(8000, 'https://example.com', key, 1, 'hash', 'session', tmp_path)
    app = create_app(cfg, FakeTelegram)
    with TestClient(app) as client:
        assert client.get('/wrong/manifest.json').status_code == 404
        assert client.get('/docs').status_code == 404
        r = client.get(f'/{key}/manifest.json', headers={'Origin':'https://web.stremio.com'})
        assert r.headers['access-control-allow-origin'] == '*'
        assert r.headers['cache-control'] == 'private, no-store'
        assert r.json()['catalogs'][0]['extra'][0]['name'] == 'search'
        assert len(client.get(f'/{key}/catalog/movie/telegram/search={quote("שֵׁם")}.json').json()['metas']) == 1
        assert client.get(f'/{key}/catalog/movie/telegram/skip=-1.json').status_code == 400
        stream = client.get(f'/{key}/stream/movie/tg:-100123:7.json').json()['streams'][0]['url']
        path = stream.removeprefix('https://example.com')
        assert client.get(path).content == b'0123456789'
        r = client.get(path, headers={'Range': 'bytes=2-5'})
        assert r.status_code == 206 and r.content == b'2345'
        assert r.headers['content-range'] == 'bytes 2-5/10'
        assert client.head(path, headers={'Range':'bytes=-3'}).headers['content-length'] == '3'
        assert client.head(path).content == b''
        assert client.get(path, headers={'Range':'bytes=99-'}).status_code == 416
        assert client.get(path + 'x').status_code == 403
        thumb = client.get(f'/{key}/meta/movie/tg:-100123:7.json').json()['meta']['poster']
        assert client.get(thumb.replace('/thumb/', '/play/')).status_code == 403
        assert client.put(f'/{key}/mapping/tg:-100123:7', json={'imdb':'tt1234567'}).status_code == 200
        app.state.tg.channels.clear()
        assert client.get(path).status_code == 404
        assert client.get(f'/{key}/catalog/movie/telegram.json').json()['metas'] == []


@pytest.mark.asyncio
async def test_matching(tmp_path):
    store = Store(tmp_path / 'db')
    store.upsert(row())
    store.upsert(row(id='tg:-100123:8', message=8, year=2023))
    metadata = Metadata()
    async def resolve(kind, imdb):
        return ['English Title', 'שם הסרט'], 2024
    metadata.resolve = resolve
    assert [r['message'] for r in await metadata.match(store, 'movie', 'tt1234567')] == [7]
    store.upsert(row(imdb='tt7654321'))
    assert not await metadata.match(store, 'movie', 'tt1234567')
    store.upsert(row(season=2, episode=5, imdb='tt1234567'))
    assert await metadata.match(store, 'series', 'tt1234567:2:5')
    assert not await metadata.match(store, 'series', 'tt1234567:2:6')
    assert not await metadata.match(store, 'movie', 'tt1234567:2:5')
    await metadata.http.aclose()
    store.db.close()


def test_interaction_logs(tmp_path, caplog):
    import json
    key = 'a' * 32
    cfg = Settings(8000, 'https://example.com', key, 1, 'private-hash', 'private-session', tmp_path)
    app = create_app(cfg, FakeTelegram)
    with TestClient(app) as client:
        caplog.clear()
        client.get(f'/{key}/catalog/movie/telegram/search={quote("שם")}.json')
        client.get(f'/{key}/catalog/movie/telegram/search=missing.json')
        client.get(f'/{key}/meta/movie/tg:-100123:7.json')
        client.get(f'/{key}/stream/movie/tg:-100123:7.json')
        async def no_matches(*args):
            return []
        app.state.metadata.match = no_matches
        client.get(f'/{key}/stream/movie/tt1234567.json')
        client.get(f'/{key}/catalog/movie/telegram/skip=-1.json')
        secret_query = f'{key} private-session private-hash\nhttps://example.com/play/secret-token'
        client.get(f'/{key}/catalog/movie/telegram/search={quote(secret_query, safe="")}.json')
        def records():
            return [r.getMessage() for r in caplog.records if r.name == 'stremio_addon.interactions']
        messages = records()
        summaries = [json.loads(m) for m in messages]
        assert summaries[0]['query'] == 'שם'
        assert summaries[0]['result_count'] == 1
        assert summaries[0]['sample_titles'] == ['שם הסרט']
        assert summaries[1]['result_count'] == 0
        assert summaries[2]['event'] == 'meta_lookup'
        assert summaries[3]['event'] == 'stream_lookup'
        assert summaries[3]['result_count'] == 1
        assert summaries[4]['match_mode'] == 'metadata'
        assert summaries[4]['result_count'] == 0
        assert summaries[5]['status'] == 400
        assert all(s['duration_ms'] >= 0 for s in summaries)
        assert all('\n' not in m for m in messages)
        for secret in (key, 'private-session', 'private-hash', 'secret-token'):
            assert secret not in ''.join(messages)
        count = len(messages)
        client.get('/healthz')
        client.get('/wrong/catalog/movie/telegram.json')
        assert len(records()) == count


def test_interaction_error_summary(tmp_path, caplog):
    import json
    key = 'a' * 32
    app = create_app(Settings(8000, 'https://example.com', key, 1, 'hash', 'session', tmp_path), FakeTelegram)
    with TestClient(app, raise_server_exceptions=False) as client:
        async def fail(*args):
            raise RuntimeError('secret exception payload')
        app.state.metadata.match = fail
        response = client.get(f'/{key}/stream/movie/tt1234567.json')
        assert response.status_code == 500
        messages = [r.getMessage() for r in caplog.records if r.name == 'stremio_addon.interactions']
        summary = json.loads(messages[-1])
        assert summary['status'] == 500
        assert summary['error'] == 'RuntimeError'
        assert 'secret exception payload' not in messages[-1]


@pytest.mark.asyncio
async def test_chunk_alignment_and_cache(tmp_path):
    cfg = Settings(8000, 'http://localhost', 'a'*32, 1, 'hash', '', tmp_path, CHUNK)
    store = Store(tmp_path / 'db')
    gateway = Telegram(cfg, store)
    payload = bytes(range(256)) * 5000
    calls = []
    class Iterator:
        async def __anext__(self):
            return payload[self.offset:self.offset+CHUNK]
        async def close(self):
            pass
    class Client:
        def iter_download(self, message, **kwargs):
            assert kwargs['offset'] % CHUNK == 0
            calls.append(kwargs['offset'])
            result = Iterator()
            result.offset = kwargs['offset']
            return result
    gateway.client = Client()
    message = SimpleNamespace(document=SimpleNamespace(id=99, size=len(payload)))
    start, end = CHUNK-13, CHUNK+21
    result = b''.join([x async for x in gateway.stream(row(), message, start, end)])
    assert result == payload[start:end+1]
    assert calls == [0, CHUNK]
    assert sum(p.stat().st_size for p in gateway.cache.iterdir()) <= CHUNK
    await gateway.chunk(row(), message, CHUNK)
    assert calls == [0, CHUNK]
    store.db.close()

@pytest.mark.asyncio
async def test_expired_reference_and_cancellation(tmp_path):
    import asyncio
    from telethon import errors
    cfg = Settings(8000, 'http://localhost', 'a'*32, 1, 'hash', '', tmp_path, 0)
    store = Store(tmp_path / 'db')
    gateway = Telegram(cfg, store)
    message = SimpleNamespace(document=SimpleNamespace(id=99, size=10))
    closed, attempts, refreshed = [], [], []
    class Iterator:
        async def __anext__(self):
            if len(attempts) == 1:
                raise errors.FileReferenceExpiredError(request=None)
            return b'0123456789'
        async def close(self):
            closed.append(True)
    class Client:
        def iter_download(self, message, **kwargs):
            attempts.append(True)
            return Iterator()
    async def refresh(r):
        refreshed.append(True)
        return message
    gateway.client = Client()
    gateway.message = refresh
    assert await gateway.chunk(row(), message, 0) == b'0123456789'
    assert len(closed) == 2 and len(refreshed) == 1
    class CancelIterator(Iterator):
        async def __anext__(self):
            raise asyncio.CancelledError()
    gateway.client.iter_download = lambda *a, **kw: CancelIterator()
    with pytest.raises(asyncio.CancelledError):
        await gateway.chunk(row(), message, 0)
    assert len(closed) == 3
    store.db.close()


@pytest.mark.asyncio
async def test_history_restart_checkpoint(tmp_path):
    cfg = Settings(8000, 'http://localhost', 'a'*32, 1, 'hash', '', tmp_path, 0)
    store = Store(tmp_path / 'db')
    gateway = Telegram(cfg, store)
    messages = [SimpleNamespace(id=i) for i in range(205, 0, -1)]
    indexed = []
    gateway.index = lambda channel, message: indexed.append(message.id)
    class Client:
        async def get_messages(self, entity, limit, offset_id):
            return [m for m in messages if not offset_id or m.id < offset_id][:limit]
        async def iter_messages(self, entity, min_id, reverse):
            for message in sorted(messages, key=lambda m: m.id):
                if message.id > min_id:
                    yield message
    gateway.client = Client()
    await gateway.scan(123, None)
    assert store.checkpoint(123)['oldest'] == 106
    store.db.close()
    gateway.store = Store(tmp_path / 'db')
    messages.insert(0, SimpleNamespace(id=206))
    await gateway.scan(123, None)
    await gateway.scan(123, None)
    assert set(indexed) == set(range(1, 207))
    assert gateway.store.checkpoint(123) == dict(channel=123, oldest=1, newest=206, complete=1)
    gateway.store.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [None, 'error', 'revoked', 'flood'])
async def test_manual_sync_lifecycle(tmp_path, caplog, failure):
    import asyncio
    import logging
    from telethon import errors

    caplog.set_level(logging.INFO, logger='stremio_addon.telegram')
    store = Store(tmp_path / 'db')
    gateway = Telegram(Settings(8000, 'http://localhost', 'a'*32, 1, 'hash', '', tmp_path), store)
    passes = []
    discoveries = []
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def discover():
        discoveries.append(True)
        gateway.channels = {-100123: 'channel'}

    async def scan(channel, entity):
        passes.append(True)
        if len(passes) == 1:
            waiting.set()
            await release.wait()
            if failure == 'error':
                raise RuntimeError('private exception payload')
            if failure == 'revoked':
                raise errors.SessionRevokedError(request=None)
            if failure == 'flood':
                raise errors.FloodWaitError(request=None, capture=0)
        checkpoint = store.checkpoint(channel)
        checkpoint['complete'] = int(len(passes) >= 2)
        store.save_checkpoint(checkpoint)

    async def reconcile():
        pass

    gateway.discover, gateway.scan_history, gateway.reconcile = discover, scan, reconcile
    async def catch_up(channel, entity):
        pass
    gateway.scan_new_messages = catch_up
    requested = gateway.request_sync()
    assert requested['state'] == 'queued'
    assert gateway.request_sync()['id'] == requested['id']
    task = asyncio.create_task(gateway.run())
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert gateway.sync_status['state'] == 'running'
        assert gateway.request_sync()['id'] == requested['id']
        release.set()

        async def finished():
            while gateway.sync_status['state'] in ('queued', 'running'):
                await asyncio.sleep(.01)
        await asyncio.wait_for(finished(), 8)
        if failure in ('error', 'revoked'):
            assert gateway.sync_status['state'] == 'failed'
            assert 'Sync failed' in caplog.text
            assert 'private exception payload' not in caplog.text
            if failure == 'revoked':
                with pytest.raises(RuntimeError, match='session revoked'):
                    gateway.request_sync()
        else:
            assert gateway.sync_status['state'] == 'done'
            assert len(passes) == 2
            assert len(discoveries) == (2 if failure == 'flood' else 1)
            assert 'Sync done' in caplog.text
            if failure == 'flood':
                assert 'rate limited' in caplog.text
            else:
                assert 'Sync indexing history' in caplog.text
        assert 'Sync requested' in caplog.text
        assert 'Sync started' in caplog.text
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        store.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('flood', [False, True])
async def test_sync_scans_each_channel_once_and_resumes_history(tmp_path, flood):
    import asyncio
    from telethon import errors

    store = Store(tmp_path / 'db')
    gateway = Telegram(Settings(8000, 'http://localhost', 'a'*32, 1, 'hash', '', tmp_path), store)
    store.save_checkpoint(dict(channel=1, newest=10, oldest=1, complete=1))
    store.save_checkpoint(dict(channel=2, newest=305, oldest=206, complete=0))
    catch_ups, history_offsets, indexed, reconciliations = [], [], [], []
    flood_pending = flood

    class Client:
        async def iter_messages(self, entity, min_id, reverse):
            catch_ups.append((entity, min_id))
            if entity == 2 and min_id == 305:
                yield SimpleNamespace(id=306)

        async def get_messages(self, entity, limit, offset_id):
            nonlocal flood_pending
            assert entity == 2, 'Completed channels must not be scanned again'
            if flood_pending:
                flood_pending = False
                raise errors.FloodWaitError(request=None, capture=0)
            history_offsets.append(offset_id)
            return [SimpleNamespace(id=i) for i in range(offset_id - 1, 0, -1)][:limit]

    async def discover():
        gateway.channels = {1: 1, 2: 2}

    async def reconcile():
        reconciliations.append(True)

    gateway.client = Client()
    gateway.discover, gateway.reconcile = discover, reconcile
    gateway.index = lambda channel, message: indexed.append((channel, message.id))
    gateway.request_sync()
    task = asyncio.create_task(gateway.run())

    async def finished():
        while gateway.sync_status['state'] in ('queued', 'running'):
            await asyncio.sleep(.01)
        assert gateway.sync_status['state'] == 'done'

    try:
        await asyncio.wait_for(finished(), 8)
        assert catch_ups == [(1, 10), (2, 305)]
        assert history_offsets == [206, 106, 6]
        assert indexed == [(2, 306)] + [(2, i) for i in range(205, 0, -1)]
        assert store.checkpoint(2) == dict(channel=2, newest=306, oldest=1, complete=1)
        assert reconciliations == []

        # A new sync session must check both channels for new messages again.
        gateway.request_sync()
        await asyncio.wait_for(finished(), 2)
        assert catch_ups == [(1, 10), (2, 305), (1, 10), (2, 306)]
        assert history_offsets == [206, 106, 6]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        store.db.close()


@pytest.mark.asyncio
async def test_sync_requested_during_background_scan_waits_for_discovery(tmp_path):
    import asyncio
    store = Store(tmp_path / 'db')
    gateway = Telegram(Settings(8000, 'http://localhost', 'a'*32, 1, 'hash', '', tmp_path), store)
    scanning, release, rediscovering = asyncio.Event(), asyncio.Event(), asyncio.Event()
    discoveries = []

    async def discover():
        discoveries.append(True)
        gateway.channels = {-100123: 'channel'}
        if len(discoveries) == 2:
            rediscovering.set()
            await asyncio.Event().wait()

    async def scan(channel, entity):
        scanning.set()
        await release.wait()
        checkpoint = store.checkpoint(channel)
        checkpoint['complete'] = 1
        store.save_checkpoint(checkpoint)

    async def reconcile():
        pass

    gateway.discover, gateway.scan, gateway.reconcile = discover, scan, reconcile
    task = asyncio.create_task(gateway.run())
    try:
        await asyncio.wait_for(scanning.wait(), 1)
        gateway.request_sync()
        release.set()
        await asyncio.wait_for(rediscovering.wait(), 1)
        assert gateway.sync_status['state'] == 'running'
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gateway.sync_status['state'] == 'failed'
        store.db.close()
