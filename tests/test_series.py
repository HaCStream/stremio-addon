import sqlite3
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from stremio_addon.app import create_app_with_runtime
from stremio_addon.core import Settings, Store, parse_title, series_id
from stremio_addon.runtime import Runtime


def video(message, filename, channel=-100, caption=''):
    return dict(id=f'tg:{channel}:{message}', channel=channel, message=message,
                filename=filename, caption=caption, channel_name='Test', size=10,
                mime='video/mp4', date=message, file_id=str(message),
                **parse_title(filename, caption))


class Gateway:
    def __init__(self, cfg, store):
        self.channels = {-100: 'Test', -200: 'Other'}
    async def start(self):
        pass
    async def close(self):
        pass


@pytest.mark.parametrize('marker', [
    'S02E05', 's02e05', 'S02 E05', 'S02.E05', 'S02_E05',
    'עונה 2 פרק 5', 'עונה 2, פרק 5', 'עונה.2.פרק.5',
])
def test_episode_title_parsing(marker):
    parsed = parse_title(f'Show.Name.{marker}.1080p.mkv', '')
    assert (parsed['title'], parsed['season'], parsed['episode']) == ('Show Name', 2, 5)
    parsed = parse_title('', f'שם הסדרה {marker}\nכיתוב נוסף')
    assert (parsed['title'], parsed['season'], parsed['episode']) == ('שם הסדרה', 2, 5)


def test_grouped_series_catalog_details_and_streams(tmp_path):
    cfg = Settings(8000, 'https://example.com', 'a' * 32, 1, 'hash', 'session', tmp_path,
                   ai_search_enabled=True, gemini_api_key='test')
    runtime = Runtime(cfg, Gateway)
    with TestClient(create_app_with_runtime(runtime)) as client:
        store = runtime.store
        entries = [video(1, 'Show.Name.S01E01.mkv'),
                   video(2, 'show name עונה 1 פרק 2.mp4'),
                   video(3, 'Show_Name.S02 E01.1080p.mkv'),
                   video(4, 'Show.Name.S01E01.720p.mkv', channel=-200),
                   video(5, 'Show.Name.S09E01.mkv', channel=-300),
                   video(6, 'Standalone.2024.mp4')]
        for entry in entries:
            store.upsert(entry)
        base = f'/{cfg.key}'
        catalog = base + '/catalog/series/telegram-series'
        metas = client.get(catalog + '/search=show.json').json()['metas']
        assert len(metas) == 1
        meta = metas[0]
        identifier = meta['id']
        assert identifier == series_id('Show Name')
        assert 'defaultVideoId' not in meta.get('behaviorHints', {})
        assert [(v['season'], v['episode']) for v in meta['videos']] == [(1, 1), (1, 2), (2, 1)]
        assert client.get(base + f'/meta/series/{identifier}.json').json()['meta']['videos'] == meta['videos']
        # An episode-specific search still opens the complete show.
        specific = client.get(catalog + '/search=' + quote('עונה 1 פרק 2') + '.json').json()['metas']
        assert specific[0]['videos'] == meta['videos']
        assert [m['name'] for m in client.get(base + '/catalog/movie/telegram.json').json()['metas']] == ['Standalone']
        first = meta['videos'][0]['id']
        streams = client.get(base + f'/stream/series/{first}.json').json()['streams']
        assert len(streams) == 2
        for stream in streams:
            token = stream['url'].rsplit('/', 1)[-1]
            assert runtime.tokens.verify(token, 'play') in {'tg:-100:1', 'tg:-200:4'}
        assert client.get(base + f'/stream/series/{identifier}:9:1.json').json()['streams'] == []
        assert client.get(base + '/stream/series/tg:-100:1.json').json()['streams']
        assert len(client.get(base + '/meta/series/tg:-100:1.json').json()['meta']['videos']) == 3

        async def discover(query):
            return {'movies': [], 'series': ['Show Name']}
        runtime.ai.discover = discover
        ai = client.get(base + '/catalog/series/telegram-ai-series/search=description%20ai.json').json()['metas']
        assert len(ai) == 1 and ai[0]['id'] == identifier
        assert ai[0]['videos'] == meta['videos']
        # A show ID remains valid after its original file is deleted.
        store.delete('tg:-100:1')
        assert client.get(base + f'/meta/series/{identifier}.json').json()['meta']['id'] == identifier


def test_group_before_pagination(tmp_path):
    store = Store(tmp_path / 'db')
    for number in range(1, 205):
        store.upsert(video(number, f'Long.Show.S01E{number:03}.mkv'))
    store.upsert(video(500, 'Second.Show.S01E01.mkv'))
    store.upsert(video(600, 'Hidden.Show.S01E01.mkv', channel=-300))
    assert len(store.grouped_catalog('series', {-100})) == 2
    assert store.grouped_catalog('series', {-100}, skip=1, limit=1)[0]['title'] == 'Long Show'
    assert len(store.series(series_id('Long Show'), {-100})) == 204
    store.db.close()


def test_existing_index_migration(tmp_path):
    path = tmp_path / 'db'
    entry = video(1, 'Show.Name.S01 E02.mkv')
    entry.update(title='Show Name S01 E02', season=1, episode=2)
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE videos (id TEXT PRIMARY KEY,' + ','.join(k for k in entry if k != 'id') + ', search)')
    db.execute('INSERT INTO videos VALUES (' + ','.join('?' for _ in range(len(entry) + 1)) + ')',
               [*entry.values(), 'old search'])
    db.commit()
    db.close()
    for _ in range(2):
        store = Store(path)
        saved = store.get(entry['id'])
        assert saved['title'] == 'Show Name'
        assert saved['series_id'] == series_id('Show Name')
        assert store.catalog('show name')[0]['id'] == entry['id']
        store.db.close()


def test_external_catalog_series_metadata_and_episode_streams(tmp_path):
    cfg = Settings(8000, 'https://example.com', 'a' * 32, 1, 'hash', 'session', tmp_path)
    runtime = Runtime(cfg, Gateway)
    with TestClient(create_app_with_runtime(runtime)) as client:
        # More than the previous 500-candidate limit; episode 1 is the oldest.
        for n in range(1, 505):
            runtime.store.upsert(video(n, f'שם הסדרה עונה 1 פרק {n}.mkv'))
        runtime.store.upsert(video(600, 'שם הסדרה עונה 2 פרק 1.mkv', channel=-300))
        runtime.store.upsert(video(601, 'שם הסדרה עונה 1 פרק 1.mkv', caption='tt9999999'))
        async def resolve(kind, imdb):
            assert (kind, imdb) == ('series', 'tt1234567')
            return ['שם הסדרה'], None
        runtime.metadata.resolve = resolve
        base = f'/{cfg.key}'
        manifest = client.get(base + '/manifest.json').json()
        assert any(r['name'] == 'meta' and 'series' in r['types'] and 'tt' in r['idPrefixes']
                   for r in manifest['resources'] if r['name'] != 'catalog')
        meta = client.get(base + '/meta/series/tt1234567.json').json()['meta']
        assert meta['id'] == 'tt1234567'
        assert len(meta['videos']) == 504
        assert meta['videos'][0]['id'] == 'tt1234567:1:1'
        streams = client.get(base + '/stream/series/tt1234567:1:1.json').json()['streams']
        assert len(streams) == 1
        assert runtime.tokens.verify(streams[0]['url'].rsplit('/', 1)[-1], 'play') == 'tg:-100:1'
        assert not client.get(base + '/stream/series/tt1234567:2:1.json').json()['streams']
