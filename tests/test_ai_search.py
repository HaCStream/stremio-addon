from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from stremio_addon.ai_search import AISearch, GENERATE_MODEL, ai_query
from stremio_addon.app import create_app_with_runtime
from stremio_addon.core import Settings, Store
from stremio_addon.runtime import Runtime


def item(id='tg:-100:1', title='Groundhog Day', caption=''):
    return dict(id=id, channel=-100, message=int(id.rsplit(':', 1)[1]), title=title,
                filename=title + '.mp4', caption=caption, channel_name='Private', size=10,
                mime='video/mp4', date=1, year=1993, season=None, episode=None,
                imdb=None, quality='', file_id='1')


def config(tmp_path, suffix_required=True):
    return Settings(8000, 'https://example.com', 'a' * 32, 1, 'hash', 'session',
                    tmp_path, ai_search_enabled=True, gemini_api_key='secret',
                    require_ai_suffix_for_ai_search=suffix_required)


@pytest.mark.parametrize('suffix', ['AI', 'Ai', 'ai', 'aI'])
def test_case_insensitive_ai_suffix(suffix):
    assert ai_query('  A Repeating Day ' + suffix + '  ') == 'A Repeating Day'
    assert ai_query('מחר\t' + suffix) == 'מחר'
    assert ai_query(suffix) == ''


@pytest.mark.parametrize(('text', 'required', 'expected'), [
    ('long description', True, None),
    ('AI long description', True, None),
    ('long description.', True, None),
    ('description ai more text', True, None),
    ('description samurai', True, None),
    ('descriptionai', True, None),
    ('description AI.', True, None),
    ('long description. AI', True, 'long description.'),
    ('AI about robots ai', True, 'AI about robots'),
    ('', True, None),
    ('long description', False, 'long description'),
    ('AI long description.', False, 'AI long description.'),
    ('long description Ai', False, 'long description'),
])
def test_ai_suffix(text, required, expected):
    assert ai_query(text, required) == expected


def test_ai_settings_from_environment(tmp_path, monkeypatch):
    import stremio_addon.core as core
    monkeypatch.setattr(core.Path, 'is_file', lambda self: False)
    for key, value in dict(ADDON_URL='https://example.com', API_KEY='a' * 32,
                           API_ID='1', API_HASH='hash', USER_SESSION_STRING='session',
                           AI_SEARCH_ENABLED='true').items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('REQUIRE_AI_SUFFIX_FOR_AI_SEARCH', raising=False)
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    with pytest.raises(ValueError, match='GEMINI_API_KEY'):
        Settings.env()
    monkeypatch.setenv('GEMINI_API_KEY', 'secret')
    settings = Settings.env()
    assert settings.ai_search_enabled
    assert settings.gemini_api_key == 'secret'
    assert settings.require_ai_suffix_for_ai_search
    monkeypatch.setenv('REQUIRE_AI_SUFFIX_FOR_AI_SEARCH', 'false')
    assert not Settings.env().require_ai_suffix_for_ai_search
    monkeypatch.setenv('REQUIRE_AI_SUFFIX_FOR_AI_SEARCH', 'maybe')
    with pytest.raises(ValueError, match='REQUIRE_AI_SUFFIX_FOR_AI_SEARCH'):
        Settings.env()


@pytest.mark.asyncio
async def test_429_pauses_all_requests_and_survives_restart(tmp_path, monkeypatch):
    import httpx
    import stremio_addon.ai_search as module
    monkeypatch.setattr(module, 'MIN_REQUEST_INTERVAL', 0)
    store = Store(tmp_path / 'index.sqlite3')
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(429, headers={'Retry-After': '120'}, json={
                'error': {'details': [{'retryDelay': '90s'}]}})
        return httpx.Response(200, json={'ok': True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        ai = AISearch(config(tmp_path), store, http)
        with pytest.raises(module.GeminiCooldown):
            await ai.request(GENERATE_MODEL, 'generateContent', {})
        assert ai.cooldown_remaining() >= 119
        with pytest.raises(module.GeminiCooldown):
            await ai.request(GENERATE_MODEL, 'generateContent', {})
        assert len(calls) == 1
        await ai.close()

        restarted = AISearch(config(tmp_path), store, http)
        with pytest.raises(module.GeminiCooldown):
            await restarted.request(GENERATE_MODEL, 'generateContent', {})
        assert len(calls) == 1
        restarted.retry_at = 0  # Simulate expiry without waiting two minutes.
        assert await restarted.request(GENERATE_MODEL, 'generateContent', {}) == {'ok': True}
        assert restarted.strikes == 0
        assert restarted.cooldown_remaining() == 0
        await restarted.close()
    store.db.close()



class FakeTelegram:
    def __init__(self, cfg, store):
        self.store = store
        self.channels = {-100: 'channel'}
        self.client = SimpleNamespace(is_connected=lambda: True)
        self.status = {'phase': 'ready'}
    async def start(self):
        self.store.upsert(item())
        self.store.upsert(dict(item('tg:-100:2', 'Russian Doll'), season=1, episode=2))
    async def close(self):
        pass


@pytest.mark.parametrize('suffix_required', [True, False])
def test_catalog_types_suffix_playback_and_errors(tmp_path, suffix_required):
    cfg = config(tmp_path, suffix_required=suffix_required)
    suffix = ' ai' if suffix_required else ''
    runtime = Runtime(cfg, FakeTelegram)
    with TestClient(create_app_with_runtime(runtime)) as client:
        manifest = client.get(f'/{cfg.key}/manifest.json').json()
        assert [(c['name'], c['type']) for c in manifest['catalogs']] == [
            ('Telegram Videos', 'movie'), ('Telegram AI Movies', 'movie'), ('Telegram AI Series', 'series')]
        calls = []
        async def discover(query):
            calls.append(query)
            return {'movies': ['Groundhog Day'], 'series': ['Russian Doll']}
        runtime.ai.discover = discover
        def path(kind, query):
            return f'/{cfg.key}/catalog/{kind}/telegram-ai-{kind}/search=' + quote(query, safe='') + '.json'
        if suffix_required:
            for kind in ('movie', 'series'):
                for query in ('repeating', 'repeating day', 'AI repeating day', 'repeating day.', 'repeating day a', 'samurai', 'ai', ''):
                    assert client.get(path(kind, query)).json()['metas'] == []
            assert not calls
        movies = client.get(path('movie', 'repeating day' + suffix)).json()['metas']
        series = client.get(path('series', 'repeating day' + (' AI' if suffix_required else ''))).json()['metas']
        assert [m['name'] for m in movies] == ['Groundhog Day']
        assert [(m['name'], m['type']) for m in series] == [('Russian Doll', 'series')]
        assert calls == ['repeating day']
        video = series[0]['videos'][0]
        assert video['season'] == 1 and video['episode'] == 2
        assert client.get(f'/{cfg.key}/meta/series/{series[0]["id"]}.json').json()['meta']['type'] == 'series'
        assert client.get(f'/{cfg.key}/stream/series/{video["id"]}.json').json()['streams']
        assert client.get(path('movie', 'ai')).json()['metas'] == []
        async def fail(query):
            raise ValueError('private upstream error')
        runtime.ai.discover = fail
        assert client.get(path('movie', 'Groundhog' + suffix)).json()['metas'] == []
        assert client.get(f'/{cfg.key}/catalog/movie/telegram/search=Groundhog.json').json()['metas']


@pytest.mark.asyncio
async def test_online_payload_and_matching_cache(tmp_path, monkeypatch):
    import asyncio
    import httpx
    import json
    import stremio_addon.ai_search as module
    monkeypatch.setattr(module, 'MIN_REQUEST_INTERVAL', 0)
    store = Store(tmp_path / 'index.sqlite3')
    store.upsert(item())
    store.upsert(item('tg:-100:2', 'Russian Doll'))
    store.upsert(item('tg:-100:3', 'Groundhog Day Sequel'))
    store.upsert(item('tg:-100:4', 'Other', caption='Groundhog Day'))
    store.upsert(dict(item('tg:-200:5'), channel=-200))
    bodies = []
    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert request.url.path.endswith(GENERATE_MODEL + ':generateContent')
        return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': json.dumps({
            'movies': ['Groundhog Day', 'Missing', 'Groundhog Day'], 'series': ['Russian Doll']})}]}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        ai = AISearch(config(tmp_path), store, http)
        movies, series = await asyncio.gather(ai.search('a repeating day', {-100}),
                                             ai.search('a repeating day', {-100}, kind='series'))
        assert [r['id'] for r in movies] == ['tg:-100:1']
        assert [r['id'] for r in series] == ['tg:-100:2']
        assert len(bodies) == 1
        assert bodies[0]['tools'] == [{'google_search': {}}]
        assert bodies[0]['generationConfig']['responseMimeType'] == 'application/json'
        assert bodies[0]['contents'][0]['parts'][0]['text'] == (
            'Search online for a movie or a serie that matches this description: "a repeating day". '
            'Give five results for each with only the titles in a json format: {"movies": [], "series": []}')
        assert 'Private' not in json.dumps(bodies)
        store.delete('tg:-100:1')
        assert not await ai.search('a repeating day', {-100})
        assert not await ai.search('a repeating day', set(), kind='series')
        assert len(bodies) == 1
        await ai.close()
    store.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', ['[]', '{}', '{"movies": [123], "series": []}', 'not json'])
async def test_invalid_response(tmp_path, answer):
    store = Store(tmp_path / 'index.sqlite3')
    ai = AISearch(config(tmp_path), store)
    async def request(*args):
        return {'candidates': [{'content': {'parts': [{'text': answer}]}}]}
    ai.request = request
    with pytest.raises(ValueError):
        await ai.search('query', {-100})
    assert not ai.cache
    await ai.close()
    store.db.close()


@pytest.mark.parametrize('suffix_required', [True, False])
def test_debug_cleanup_search_and_no_embedding_state(tmp_path, suffix_required):
    from stremio_addon.debug import create_debug_app
    cfg = config(tmp_path, suffix_required=suffix_required)
    suffix = ' ai' if suffix_required else ''
    runtime = Runtime(cfg, FakeTelegram)
    with TestClient(create_debug_app(runtime)) as client:
        headers = {'X-Debug-Key': cfg.key}
        assert client.post('/api/cleanup').status_code == 401
        assert 'ai_index' not in client.get('/api/overview', headers=headers).json()
        with runtime.store.db:
            runtime.store.db.executescript("""CREATE TABLE ai_embeddings(id TEXT, vector BLOB);
                INSERT INTO ai_embeddings VALUES ('legacy', X'01020304');
                CREATE TABLE ai_descriptions(title_key TEXT, description TEXT);
                INSERT INTO ai_descriptions VALUES ('legacy', 'old description');""")
        result = client.post('/api/cleanup', headers=headers).json()
        assert result['removed_records'] == 2
        assert client.post('/api/cleanup', headers=headers).json()['removed_records'] == 0
        assert runtime.store.get('tg:-100:1')
        assert runtime.store.catalog('Groundhog')
        assert not runtime.store.db.execute("SELECT name FROM sqlite_master WHERE name IN ('ai_embeddings','ai_descriptions')").fetchall()
        calls = []
        async def discover(query):
            calls.append(query)
            return {'movies': ['Groundhog Day'], 'series': ['Russian Doll']}
        runtime.ai.discover = discover
        assert client.get('/api/overview', headers=headers).json()['require_ai_suffix_for_ai_search'] == suffix_required
        if suffix_required:
            for query in ('repeating', 'repeating day', 'AI repeating day', 'repeating day.', 'repeating day a', 'samurai', 'ai', ''):
                assert client.get('/api/search', params={'mode': 'ai', 'q': query}, headers=headers).json()['results'] == []
            assert not calls
        result = client.get('/api/search', params={'mode': 'ai', 'q': 'repeating day' + suffix}, headers=headers).json()
        assert [r['title'] for r in result['movies']] == ['Groundhog Day']
        assert [r['title'] for r in result['series']] == ['Russian Doll']
        assert calls == ['repeating day']
        async def fail(query):
            raise ValueError('private upstream error')
        runtime.ai.discover = fail
        response = client.get('/api/search', params={'mode': 'ai', 'q': 'new query' + suffix}, headers=headers)
        assert response.status_code == 502
        assert 'private upstream' not in response.text
        html = client.get('/').text
        assert 'AI indexed' not in html and 'ai_index' not in html and 'AI index' not in html
        assert 'ai-search-form' in html and 'id="cleanup"' in html


def test_disabled_ai_and_cleanup(tmp_path):
    from stremio_addon.debug import create_debug_app
    cfg = Settings(8000, 'https://example.com', 'a' * 32, 1, 'hash', 'session', tmp_path)
    runtime = Runtime(cfg, FakeTelegram)
    with TestClient(create_app_with_runtime(runtime)) as client:
        assert [x['name'] for x in client.get(f'/{cfg.key}/manifest.json').json()['catalogs']] == ['Telegram Videos']
    with TestClient(create_debug_app(runtime)) as client:
        headers = {'X-Debug-Key': cfg.key}
        assert client.get('/api/search?mode=ai&q=anything', headers=headers).status_code == 409
        assert client.post('/api/cleanup', headers=headers).status_code == 200


@pytest.mark.asyncio
async def test_startup_and_sync_make_no_ai_calls(tmp_path, monkeypatch):
    async def forbidden(*args):
        raise AssertionError('AI must only run on search')
    monkeypatch.setattr(AISearch, 'request', forbidden)
    runtime = Runtime(config(tmp_path), FakeTelegram)
    await runtime.acquire()
    assert not hasattr(runtime.ai, 'task')
    runtime.store.upsert(item('tg:-100:3', 'New title'))
    assert not runtime.store.db.execute("SELECT name FROM sqlite_master WHERE name IN ('ai_embeddings','ai_descriptions')").fetchall()
    await runtime.release()
