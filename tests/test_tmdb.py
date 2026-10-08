import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from stremio_addon.app import create_app_with_runtime, safe_log_text
from stremio_addon.core import Settings, Store, series_id
from stremio_addon.debug import create_debug_app
from stremio_addon.runtime import Runtime
from stremio_addon.tmdb import TMDB, public_episode
from tests.test_series import Gateway, video


def tmdb_response(request, duplicate=False, missing_imdb=False):
    assert request.url.params['api_key'] == 'secret-tmdb'
    if request.url.path.endswith('/search/tv'):
        return httpx.Response(200, json={'results': [{'id': 42}, *([{'id': 43}] if duplicate else [])]})
    identity = int(request.url.path.rsplit('/', 1)[1])
    return httpx.Response(200, json={
        'id': identity, 'name': 'השמיניה', 'original_name': 'HaShminia', 'first_air_date': '2005-06-05',
        'external_ids': {'imdb_id': None if missing_imdb else 'tt1234567' if identity == 42 else 'tt9999999'},
        'alternative_titles': {'results': [{'title': 'The Eight'}]},
        'translations': {'translations': [{'iso_639_1': 'en', 'data': {'name': 'The Eight'}}]},
    })


@pytest.fixture
def configured(tmp_path):
    # Keep the background worker off; tests call one pass explicitly using mocks.
    cfg = Settings(8000, 'https://example.com', 'a' * 32, 1, 'hash', 'session', tmp_path)
    runtime = Runtime(cfg, Gateway)
    with TestClient(create_app_with_runtime(runtime)) as client:
        runtime.tmdb.key = 'secret-tmdb'
        runtime.tmdb.http = httpx.AsyncClient(transport=httpx.MockTransport(tmdb_response))
        runtime.store.upsert(video(1, 'hashminia.S01E01.mp4', caption='השמיניה עונה 1 פרק 1'))
        runtime.store.upsert(video(2, 'hashminia.S01E02.mp4', caption='השמיניה עונה 1 פרק 2'))
        runtime.store.upsert(video(3, 'hashminia.S01E01.mp4', channel=-200, caption='השמיניה'))
        runtime.store.upsert(video(4, 'hashminia.S01E01.mp4', channel=-300, caption='השמיניה'))
        yield cfg, runtime, client


def test_automatic_hebrew_matching_public_sources_and_persistence(configured):
    cfg, runtime, client = configured
    identity = series_id('השמיניה')
    asyncio.run(runtime.tmdb.match_show(identity))
    saved = runtime.tmdb.shows()[0]['mapping']
    assert saved['status'] == 'auto' and saved['tmdb'] == 42 and saved['imdb'] == 'tt1234567'
    assert saved['candidates'][0]['exact']

    def no_network(request):
        raise AssertionError('Saved identity should not require a metadata request')
    runtime.metadata.http = httpx.AsyncClient(transport=httpx.MockTransport(no_network))
    runtime.tmdb.http = httpx.AsyncClient(transport=httpx.MockTransport(no_network))
    for episode_id in ('tt1234567:1:1', 'tmdb:42:1:1', 'tmdb:tv:42:1:1'):
        streams = client.get(f'/{cfg.key}/stream/series/{episode_id}.json').json()['streams']
        assert len(streams) == 2
        assert {runtime.tokens.verify(s['url'].rsplit('/', 1)[-1], 'play') for s in streams} == {'tg:-100:1', 'tg:-200:3'}
    assert len(client.get(f'/{cfg.key}/stream/series/tt1234567:1:2.json').json()['streams']) == 1
    assert not client.get(f'/{cfg.key}/stream/series/tt1234567:1:99.json').json()['streams']
    assert not client.get(f'/{cfg.key}/stream/series/tmdb:42:2:1.json').json()['streams']
    # Show mapping covers episodes added later without another lookup.
    runtime.store.upsert(video(5, 'hashminia.S01E03.mp4', caption='השמיניה'))
    assert client.get(f'/{cfg.key}/stream/series/tmdb:42:1:3.json').json()['streams']
    assert 'tmdb:' in next(r for r in client.get(f'/{cfg.key}/manifest.json').json()['resources'] if r['name'] == 'stream')['idPrefixes']
    assert len(client.get(f'/{cfg.key}/meta/series/tt1234567.json').json()['meta']['videos']) == 3
    db = Store(cfg.data / 'index.sqlite3')
    service = TMDB(cfg, db, runtime.tg)
    assert len(service.streams('tt1234567:1:1')) == 2
    db.db.close()


def test_ambiguous_requires_manual_choice_and_blocks_title_fallback(configured):
    cfg, runtime, client = configured
    identity = series_id('השמיניה')
    runtime.tmdb.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: tmdb_response(r, duplicate=True)))
    asyncio.run(runtime.tmdb.match_show(identity))
    assert runtime.tmdb.shows()[0]['mapping']['status'] == 'ambiguous'
    async def aliases(kind, imdb):
        return ['השמיניה'], None
    runtime.metadata.resolve = aliases
    assert not client.get(f'/{cfg.key}/stream/series/tt1234567:1:1.json').json()['streams']
    asyncio.run(runtime.tmdb.select(identity, 43))
    assert runtime.tmdb.shows()[0]['mapping']['status'] == 'manual'
    assert client.get(f'/{cfg.key}/stream/series/tt9999999:1:2.json').json()['streams']
    assert not client.get(f'/{cfg.key}/stream/series/tt1234567:1:2.json').json()['streams']
    asyncio.run(runtime.tmdb.match_show(identity))
    assert runtime.tmdb.shows()[0]['mapping']['tmdb'] == 43
    asyncio.run(runtime.tmdb.block(identity))
    assert not client.get(f'/{cfg.key}/stream/series/tt9999999:1:2.json').json()['streams']


def test_offsets_and_file_override(configured):
    cfg, runtime, client = configured
    identity = series_id('השמיניה')
    asyncio.run(runtime.tmdb.select(identity, 42, 1, 10))
    runtime.store.upsert(video(10, 'hashminia.S02E11.mp4', caption='השמיניה'))
    runtime.store.upsert(video(11, 'hashminia.S02E12.mp4', caption='השמיניה\ntt9999999'))
    assert len(client.get(f'/{cfg.key}/stream/series/tt1234567:1:1.json').json()['streams']) == 1
    assert not client.get(f'/{cfg.key}/stream/series/tt1234567:1:2.json').json()['streams']
    assert not client.get(f'/{cfg.key}/stream/series/tmdb:42:2:11.json').json()['streams']
    # Telegram-native streams retain the original numbers.
    assert client.get(f'/{cfg.key}/stream/series/{identity}:2:11.json').json()['streams']


def test_debug_auth_candidates_select_and_block(configured):
    cfg, runtime, _ = configured
    identity = series_id('השמיניה')
    with TestClient(create_debug_app(runtime)) as client:
        assert client.get('/api/tmdb/shows').status_code == 401
        assert client.put('/api/tmdb/mapping', json={'series_id': identity, 'tmdb': 42}).status_code == 401
        headers = {'X-Debug-Key': cfg.key}
        assert len(client.get('/api/tmdb/shows', headers=headers).json()['shows']) == 1
        candidates = client.get('/api/tmdb/candidates', params={'series_id': identity, 'q': 'The Eight'}, headers=headers)
        assert candidates.status_code == 200 and candidates.json()['candidates'][0]['tmdb'] == 42
        response = client.put('/api/tmdb/mapping', headers=headers, json={'series_id': identity, 'tmdb': 42})
        assert response.status_code == 200 and response.json()['mapping']['imdb'] == 'tt1234567'
        assert client.put('/api/tmdb/mapping', headers=headers, json={'series_id': identity, 'tmdb': 0}).status_code == 422
        assert client.put('/api/tmdb/mapping', headers=headers, json={'series_id': 'missing', 'tmdb': 42}).status_code == 400
        assert client.delete('/api/tmdb/mapping', params={'series_id': identity}, headers=headers).status_code == 200
        assert runtime.tmdb.shows()[0]['mapping']['status'] == 'blocked'


def test_tmdb_only_show(configured):
    cfg, runtime, client = configured
    runtime.tmdb.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: tmdb_response(r, missing_imdb=True)))
    asyncio.run(runtime.tmdb.match_show(series_id('השמיניה')))
    assert client.get(f'/{cfg.key}/stream/series/tmdb:42:1:1.json').json()['streams']


@pytest.mark.asyncio
async def test_rate_limit_cooldown(configured):
    _, runtime, _ = configured
    requests = []
    def limited(request):
        requests.append(request)
        return httpx.Response(429, headers={'Retry-After': '120'})
    runtime.tmdb.http = httpx.AsyncClient(transport=httpx.MockTransport(limited))
    with pytest.raises(httpx.HTTPStatusError):
        await runtime.tmdb.match_show(series_id('השמיניה'))
    assert runtime.tmdb.retry_at > time.time() + 110
    with pytest.raises(RuntimeError):
        await runtime.tmdb.match_show(series_id('השמיניה'))
    assert len(requests) == 1
    assert runtime.tmdb.shows()[0]['mapping'] is None


@pytest.mark.parametrize('identifier', ['tmdb:0:1:1', 'tmdb:nope:1:1', 'tt1234567:1', 'tmdb:42:1:1:1', 'tg:-100:1', '../evil'])
def test_invalid_public_ids(identifier):
    assert public_episode(identifier) is None


def test_tmdb_key_redacted(configured):
    cfg, _, _ = configured
    cfg.tmdb_api_key = 'secret-tmdb'
    assert 'secret-tmdb' not in safe_log_text('error secret-tmdb', cfg)


@pytest.mark.asyncio
async def test_background_passes_skip_saved_matches_and_retry_unmatched(configured):
    _, runtime, _ = configured
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith('/search/tv') and request.url.params.get('query') == 'Unknown':
            return httpx.Response(200, json={'results': []})
        return tmdb_response(request)
    runtime.tmdb.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await runtime.tmdb.scan_once()
    total = len(requests)
    await runtime.tmdb.scan_once()
    assert len(requests) == total
    runtime.store.upsert(video(20, 'Unknown.S01E01.mp4'))
    await runtime.tmdb.scan_once()
    assert len(requests) == total + 2
    assert next(s for s in runtime.tmdb.shows() if s['title'] == 'Unknown')['mapping']['status'] == 'unmatched'
    await runtime.tmdb.scan_once()
    assert len(requests) == total + 2
    with runtime.store.db:
        runtime.store.db.execute('UPDATE tmdb_series SET checked_at=0 WHERE status=?', ('unmatched',))
    await runtime.tmdb.scan_once()
    assert len(requests) == total + 4


def test_manual_file_mapping_overrides_show_offset(configured):
    cfg, runtime, client = configured
    asyncio.run(runtime.tmdb.select(series_id('השמיניה'), 42, 1, 10))
    with runtime.store.db:
        runtime.store.db.execute('INSERT INTO mappings VALUES (?,?)', ('tg:-100:2', 'tt1234567'))
    streams = client.get(f'/{cfg.key}/stream/series/tt1234567:1:2.json').json()['streams']
    assert len(streams) == 1
    assert runtime.tokens.verify(streams[0]['url'].rsplit('/', 1)[-1], 'play') == 'tg:-100:2'


def test_disabled_tmdb_has_no_worker_and_debug_explains(configured):
    _, runtime, _ = configured
    assert runtime.tmdb_task is None
    runtime.tmdb.key = ''
    with TestClient(create_debug_app(runtime)) as client:
        headers = {'X-Debug-Key': runtime.cfg.key}
        assert client.get('/api/tmdb/shows', headers=headers).json()['enabled'] is False
        response = client.get('/api/tmdb/candidates', headers=headers,
                              params={'series_id': series_id('השמיניה')})
        assert response.status_code == 409 and 'TMDB_API_KEY' in response.json()['detail']


def test_httpx_url_key_redaction():
    import logging
    from stremio_addon.logging_setup import LogFormatter
    record = logging.LogRecord('httpx', logging.INFO, __file__, 1,
        'HTTP Request: GET https://api.themoviedb.org/3/search/tv?api_key=secret-tmdb&query=title', (), None)
    output = LogFormatter().format(record)
    assert 'secret-tmdb' not in output and 'api_key=[redacted]' in output and 'query=title' in output


def test_tmdb_key_environment_and_home_assistant_options(tmp_path, monkeypatch):
    from pathlib import Path
    import stremio_addon.core as core
    from addon.start import configure
    values = {'ADDON_URL': 'https://example.com', 'API_KEY': 'a' * 32,
              'API_ID': 1, 'API_HASH': 'hash', 'USER_SESSION_STRING': 'session',
              'tmdb': {'TMDB_API_KEY': 'option-key'}}
    options = tmp_path / 'options.json'
    import json
    options.write_text(json.dumps(values))
    monkeypatch.setattr(core, 'Path', lambda value: options if value == '/data/options.json' else Path(value))
    for name in (*values, 'TMDB_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    assert core.Settings.env().tmdb_api_key == 'option-key'
    monkeypatch.setenv('TMDB_API_KEY', 'env-key')
    assert core.Settings.env().tmdb_api_key == 'env-key'
    env = {}
    configure(values, env)
    assert env['TMDB_API_KEY'] == 'option-key'


def test_worker_lifecycle_shared_between_listeners(tmp_path, monkeypatch):
    started, stopped = [], []
    async def worker(self):
        started.append(self)
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(self)
    monkeypatch.setattr(TMDB, 'run', worker)
    cfg = Settings(8000, 'https://example.com', 'a' * 32, 1, 'hash', 'session', tmp_path,
                   tmdb_api_key='test-key')
    runtime = Runtime(cfg, Gateway)
    with TestClient(create_app_with_runtime(runtime)):
        task = runtime.tmdb_task
        with TestClient(create_debug_app(runtime)):
            assert runtime.tmdb_task is task
            assert len(started) == 1
        assert not task.done()
    assert task.cancelled() and len(stopped) == 1
