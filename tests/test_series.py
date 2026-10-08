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


@pytest.mark.parametrize('title', ['hashminia', 'Ha.Shminia', 'Ha Shminia'])
def test_search_displayed_title_and_hebrew_caption(tmp_path, title):
    cfg = Settings(8000, 'https://example.com', 'a' * 32, 1, 'hash', 'session', tmp_path)
    runtime = Runtime(cfg, Gateway)
    with TestClient(create_app_with_runtime(runtime)) as client:
        runtime.store.upsert(video(1, title + '.S01E01.mkv', caption='השמיניה עונה 1 פרק 1'))
        expected = series_id(parse_title(title + '.S01E01.mkv', '')['title'])
        for query in ('השמיניה', 'hashminia', 'HASHMINIA', 'ha shminia'):
            results = client.get(f'/{cfg.key}/catalog/series/telegram-series/search='
                                 + quote(query, safe='') + '.json').json()['metas']
            assert [m['id'] for m in results] == [expected]
            assert runtime.store.catalog(query)
        # Removing a caption must also remove its search terms.
        runtime.store.upsert(video(1, title + '.S01E01.mkv'))
        assert not runtime.store.catalog('השמיניה')
        assert runtime.store.catalog('hashminia')


def test_search_index_rebuild_repairs_old_caption_only_entries(tmp_path):
    path = tmp_path / 'db'
    store = Store(path)
    entry = video(1, 'hashminia.S01E01.mkv', caption='השמיניה')
    missing = video(2, 'Another.Show.S01E01.mkv')
    store.upsert(entry)
    store.upsert(missing)
    with store.db:
        store.db.execute('UPDATE videos SET search=? WHERE id=?', ('השמיניה', entry['id']))
        store.db.execute('DELETE FROM search')
        store.db.execute('INSERT INTO search VALUES (?,?)', (entry['id'], 'השמיניה'))
        store.db.execute('PRAGMA user_version=0')
    assert not store.catalog('hashminia')
    store.db.close()
    store = Store(path)
    assert store.catalog('hashminia')[0]['id'] == entry['id']
    assert store.catalog('השמיניה')[0]['id'] == entry['id']
    assert store.catalog('another show')[0]['id'] == missing['id']
    assert store.db.execute('PRAGMA user_version').fetchone()[0] == 2
    store.db.close()
    store = Store(path)
    assert len(store.catalog()) == 2
    assert store.db.execute('SELECT count(*) FROM search').fetchone()[0] == 2
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


@pytest.mark.parametrize('filename', [
    'hashminia.S5E21_480P.mp4', 'hashminia_S5E21_480P.mp4',
    'hashminia.S5E21.480P.mp4', 'hashminia S5E21 480P.mp4',
])
def test_episode_and_quality_separators(filename):
    parsed = parse_title(filename, 'השמיניה')
    assert (parsed['title'], parsed['season'], parsed['episode'], parsed['quality']) == ('hashminia', 5, 21, '480P')


def test_repair_malformed_titles_in_already_migrated_index(tmp_path):
    path = tmp_path / 'index.sqlite3'
    store = Store(path)
    store.upsert(video(14, 'hashminia.S5E14_480P.mp4', caption='השמיניה'))
    broken = video(21, 'hashminia.S5E21_480P.mp4', caption='השמיניה עונה 5 פרק 21')
    broken.update(title='hashminia S5E21 480P', quality='')
    store.upsert(broken)
    movie = video(99, 'Unrelated.2024.mp4')
    movie['title'] = 'Keep this title'
    store.upsert(movie)
    store.save_checkpoint(dict(channel=-100, oldest=1, newest=99, complete=1))
    with store.db:
        store.db.execute('INSERT INTO mappings VALUES (?,?)', (broken['id'], 'tt1234567'))
        # Reproduce an installation that already has the series column and
        # version-1 search migration, but still contains a legacy title.
        store.db.execute('PRAGMA user_version=1')
    assert len(store.grouped_catalog('series', {-100}, 'השמיניה')) == 2
    store.db.close()

    cfg = Settings(8000, 'https://example.com', 'a' * 32, 1, 'hash', 'session', tmp_path)
    runtime = Runtime(cfg, Gateway)
    with TestClient(create_app_with_runtime(runtime)) as client:
        for query in ('השמיניה', 'hashminia'):
            metas = client.get(f'/{cfg.key}/catalog/series/telegram-series/search='
                               + quote(query, safe='') + '.json').json()['metas']
            assert len(metas) == 1
            assert metas[0]['name'] == 'hashminia'
            assert [(v['season'], v['episode']) for v in metas[0]['videos']] == [(5, 14), (5, 21)]
        episode = metas[0]['videos'][1]['id']
        streams = client.get(f'/{cfg.key}/stream/series/{episode}.json').json()['streams']
        assert len(streams) == 1
        assert runtime.tokens.verify(streams[0]['url'].rsplit('/', 1)[-1], 'play') == broken['id']
        assert runtime.store.get(broken['id'])['title'] == 'hashminia'
        assert runtime.store.get(movie['id'])['title'] == movie['title']
        assert runtime.store.explicit('tt1234567')[0]['id'] == broken['id']
        assert runtime.store.checkpoint(-100)['complete'] == 1
        assert runtime.store.db.execute('PRAGMA user_version').fetchone()[0] == 2
    store = Store(path)
    assert len(store.series(series_id('hashminia'), {-100})) == 2
    assert store.db.execute('SELECT count(*) FROM search').fetchone()[0] == 3
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
