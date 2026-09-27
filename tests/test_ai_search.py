from array import array
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from stremio_addon.ai_search import AISearch, DIMENSIONS, EMBED_MODEL, GENERATE_MODEL, ai_query
from stremio_addon.app import create_app_with_runtime
from stremio_addon.core import Settings, Store
from stremio_addon.runtime import Runtime


def item(id='tg:-100:1', title='Groundhog Day', caption=''):
    return dict(id=id, channel=-100, message=int(id.rsplit(':', 1)[1]), title=title,
                filename=title + '.mp4', caption=caption, channel_name='Private', size=10,
                mime='video/mp4', date=1, year=1993, season=None, episode=None,
                imdb=None, quality='', file_id='1')


def config(tmp_path, prefix=False):
    return Settings(8000, 'https://example.com', 'a' * 32, 1, 'hash', 'session',
                    tmp_path, ai_search_enabled=True, gemini_api_key='secret',
                    ai_search_prefix_enabled=prefix)


def test_prefix():
    assert ai_query('AI a repeating day', True) == 'a repeating day'
    assert ai_query('ai\tמחר', True) == 'מחר'
    assert ai_query('airplane', True) is None
    assert ai_query('aI', True) == ''
    assert ai_query('ordinary search', True) is None
    assert ai_query('ordinary search', False) == 'ordinary search'


@pytest.mark.asyncio
async def test_incremental_index_and_description_cache(tmp_path):
    store = Store(tmp_path / 'index.sqlite3')
    store.upsert(item())
    store.upsert(item('tg:-100:2'))
    ai = AISearch(config(tmp_path), store)
    calls = []

    async def generate(prompt):
        calls.append(('description', prompt))
        return {'description': 'A person repeats the same day.'}

    async def embed(text, task, title=None):
        calls.append(('embed', text))
        return array('f', [1.] + [0.] * (DIMENSIONS - 1))

    ai.generate = generate
    ai.embed = embed
    assert await ai.index_row(store.get('tg:-100:1'))
    assert not await ai.index_row(store.get('tg:-100:1'))
    assert await ai.index_row(store.get('tg:-100:2'))
    assert len([c for c in calls if c[0] == 'description']) == 1
    assert len([c for c in calls if c[0] == 'embed']) == 2
    store.upsert(item(caption='A different description of the plot.'))
    assert await ai.index_row(store.get('tg:-100:1'))
    assert len([c for c in calls if c[0] == 'embed']) == 3
    store.delete('tg:-100:1')
    assert not store.db.execute('SELECT 1 FROM ai_embeddings WHERE id=?', ('tg:-100:1',)).fetchone()
    await ai.close()
    store.db.close()


class FakeTelegram:
    def __init__(self, cfg, store):
        self.store = store
        self.channels = {-100: 'channel'}
        self.client = SimpleNamespace(is_connected=lambda: True)
        self.status = {'phase': 'ready'}
    async def start(self):
        self.store.upsert(item())
    async def close(self):
        pass


def test_catalog_separation_and_prefix(tmp_path):
    cfg = config(tmp_path, prefix=True)
    runtime = Runtime(cfg, FakeTelegram)
    app = create_app_with_runtime(runtime)
    with TestClient(app) as client:
        key = cfg.key
        manifest = client.get(f'/{key}/manifest.json').json()
        assert [c['name'] for c in manifest['catalogs']] == ['Telegram Videos', 'Telegram AI Search']
        path = f'/{key}/catalog/movie/telegram-ai/search='
        assert client.get(path + quote('Groundhog', safe='') + '.json').json()['metas'] == []
        calls = []
        async def search(query, allowed, skip):
            calls.append((query, allowed, skip))
            return [runtime.store.get('tg:-100:1')]
        runtime.ai.search = search
        result = client.get(path + quote('ai repeating day', safe='') + '.json').json()['metas']
        assert [x['name'] for x in result] == ['Groundhog Day']
        assert calls == [('repeating day', {-100}, 0)]
        assert len(client.get(f'/{key}/catalog/movie/telegram/search=Groundhog.json').json()['metas']) == 1


def test_ai_disabled_manifest(tmp_path):
    cfg = Settings(8000, 'https://example.com', 'a' * 32, 1, 'hash', 'session', tmp_path)
    with TestClient(create_app_with_runtime(Runtime(cfg, FakeTelegram))) as client:
        names = [x['name'] for x in client.get(f'/{cfg.key}/manifest.json').json()['catalogs']]
        assert names == ['Telegram Videos']

@pytest.mark.asyncio
async def test_semantic_lookup_without_shared_words(tmp_path):
    store = Store(tmp_path / 'index.sqlite3')
    store.upsert(item())
    ai = AISearch(config(tmp_path), store)
    calls = []

    async def generate(prompt):
        calls.append(prompt)
        if prompt.startswith('Give up to five'):
            return {'titles': []}
        assert 'repeats the same day' in prompt
        return {'ids': ['tg:-100:1', 'tg:-100:999']}

    async def embed(text, task, title=None):
        return array('f', [1.] + [0.] * (DIMENSIONS - 1))

    ai.generate = generate
    ai.embed = embed
    with store.db:
        store.db.execute('INSERT INTO ai_descriptions VALUES (?,?)',
                         ('groundhog day:1993', 'A person repeats the same day.'))
        store.db.execute('INSERT INTO ai_embeddings VALUES (?,?,?,?)',
                         ('tg:-100:1', ai.fingerprint(store.get('tg:-100:1')),
                          EMBED_MODEL, (await embed('', '')).tobytes()))
    assert not store.catalog('someone relives today')
    found = await ai.search('someone relives today', {-100})
    assert [r['id'] for r in found] == ['tg:-100:1']
    assert len(calls) == 2
    assert [r['id'] for r in await ai.search('someone relives today', {-100})] == ['tg:-100:1']
    assert len(calls) == 2  # Cached pagination makes no new API requests.
    await ai.close()
    store.db.close()

def test_ai_settings_from_environment(tmp_path, monkeypatch):
    import stremio_addon.core as core
    monkeypatch.setattr(core.Path, 'is_file', lambda self: False)
    for key, value in dict(ADDON_URL='https://example.com', API_KEY='a' * 32,
                           API_ID='1', API_HASH='hash', USER_SESSION_STRING='session',
                           AI_SEARCH_ENABLED='true', AI_SEARCH_PREFIX_ENABLED='yes').items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    with pytest.raises(ValueError, match='GEMINI_API_KEY'):
        Settings.env()
    monkeypatch.setenv('GEMINI_API_KEY', 'secret')
    settings = Settings.env()
    assert settings.ai_search_enabled and settings.ai_search_prefix_enabled
    assert settings.gemini_api_key == 'secret'
    monkeypatch.setenv('AI_SEARCH_PREFIX_ENABLED', 'maybe')
    with pytest.raises(ValueError, match='AI_SEARCH_PREFIX_ENABLED'):
        Settings.env()

@pytest.mark.asyncio
async def test_gemini_2_payloads(tmp_path):
    store = Store(tmp_path / 'index.sqlite3')
    ai = AISearch(config(tmp_path), store)
    requests = []

    async def request(model, action, body):
        requests.append((model, action, body))
        if action == 'embedContent':
            return {'embedding': {'values': [1.] + [0.] * (DIMENSIONS - 1)}}
        return {'candidates': [{'content': {'parts': [{'text': '{"titles": []}'}]}}]}

    ai.request = request
    await ai.embed('a time loop', 'RETRIEVAL_QUERY')
    await ai.embed('A repeated day', 'RETRIEVAL_DOCUMENT', 'Groundhog Day')
    await ai.generate('suggest titles')
    query = requests[0][2]
    document = requests[1][2]
    generation = requests[2][2]
    assert requests[0][:2] == (EMBED_MODEL, 'embedContent')
    assert query['content']['parts'][0]['text'] == 'task: search result | query: a time loop'
    assert document['content']['parts'][0]['text'] == 'title: Groundhog Day | text: A repeated day'
    assert query['outputDimensionality'] == document['outputDimensionality'] == DIMENSIONS
    assert 'taskType' not in query and 'taskType' not in document
    assert 'title' not in document
    assert requests[2][:2] == (GENERATE_MODEL, 'generateContent')
    assert generation['generationConfig']['thinkingConfig'] == {'thinkingLevel': 'low'}
    await ai.close()
    store.db.close()

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
            await ai.request(EMBED_MODEL, 'embedContent', {})
        assert ai.snapshot()['retry_after_seconds'] >= 119
        with pytest.raises(module.GeminiCooldown):
            await ai.request(GENERATE_MODEL, 'generateContent', {})
        assert len(calls) == 1
        await ai.close()

        restarted = AISearch(config(tmp_path), store, http)
        with pytest.raises(module.GeminiCooldown):
            await restarted.request(EMBED_MODEL, 'embedContent', {})
        assert len(calls) == 1
        restarted.retry_at = 0  # Simulate expiry without waiting two minutes.
        assert await restarted.request(EMBED_MODEL, 'embedContent', {}) == {'ok': True}
        assert restarted.strikes == 0
        assert restarted.snapshot()['retry_after_seconds'] == 0
        await restarted.close()
    store.db.close()


@pytest.mark.asyncio
async def test_indexer_stops_after_first_429(tmp_path, monkeypatch):
    import asyncio
    import httpx
    import stremio_addon.ai_search as module
    monkeypatch.setattr(module, 'MIN_REQUEST_INTERVAL', 0)
    store = Store(tmp_path / 'index.sqlite3')
    store.upsert(item())
    store.upsert(item('tg:-100:2', title='Another title'))
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(429, headers={'Retry-After': '180'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        ai = AISearch(config(tmp_path), store, http)
        await ai.start()
        for _ in range(20):
            if calls:
                break
            await asyncio.sleep(.01)
        await asyncio.sleep(.05)
        assert len(calls) == 1
        assert ai.snapshot()['retry_after_seconds'] >= 179
        await ai.close()
    store.db.close()
