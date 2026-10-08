import asyncio
import re
import time
import httpx
from .core import normalize, title_aliases


class Metadata:
    """Public metadata only: never send Telegram captions, filenames or channel IDs."""
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=8, headers={'User-Agent': 'TelegramStremio/1.0 (personal addon)'})
        self.cache = {}

    async def resolve(self, kind, imdb):
        key = (kind, imdb)
        if key in self.cache and self.cache[key][0] > time.time():
            return self.cache[key][1]
        aliases, year = [], None
        async def cinemeta():
            nonlocal year
            try:
                r = await self.http.get(f'https://v3-cinemeta.strem.io/meta/{kind}/{imdb}.json')
                r.raise_for_status()
                meta = r.json().get('meta') or {}
                if meta.get('name'):
                    aliases.append(meta['name'])
                match = re.search(r'\b\d{4}\b', str(meta.get('year') or meta.get('releaseInfo') or ''))
                year = int(match[0]) if match else None
            except (httpx.HTTPError, ValueError, TypeError):
                pass
        async def wikidata():
            query = '''SELECT ?label WHERE { ?item wdt:P345 "%s". { ?item rdfs:label ?label } UNION { ?item skos:altLabel ?label } FILTER(LANG(?label) IN ("he","en")) } LIMIT 100''' % imdb
            try:
                r = await self.http.get('https://query.wikidata.org/sparql', params={'query': query, 'format': 'json'})
                r.raise_for_status()
                aliases.extend(x['label']['value'] for x in r.json()['results']['bindings'])
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                pass
        await asyncio.gather(cinemeta(), wikidata())
        result = (list(dict.fromkeys(aliases)), year)
        if len(self.cache) >= 1000:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = (time.time() + (86400 if aliases else 60), result)
        return result

    async def match(self, store, kind, identifier):
        tmdb = getattr(self, 'tmdb', None)
        if kind == 'series' and tmdb:
            mapped = tmdb.streams(identifier)
            if mapped or identifier.startswith('tmdb:'):
                return mapped
            # A saved identity is authoritative even when this episode is absent.
            if store.db.execute("SELECT 1 FROM tmdb_series WHERE imdb=? AND status IN ('auto','manual')",
                                (identifier.split(':')[0],)).fetchone():
                return []
        pattern = r'(tt\d{7,10})(?::(\d+):(\d+))?'
        m = re.fullmatch(pattern, identifier)
        if not m or (kind != 'series' and m[2]):
            return []
        imdb, season, episode = m[1], int(m[2]) if m[2] else None, int(m[3]) if m[3] else None
        def episode_ok(r):
            if kind == 'series' and season is None:
                return r['season'] is not None and r['episode'] is not None
            return (r['season'], r['episode']) == (season, episode)
        found = {r['id']: r for r in store.explicit(imdb) if episode_ok(r)}
        excluded = tmdb.excluded_shows() if tmdb else set()
        aliases, year = await self.resolve(kind, imdb)
        allowed = {r['channel'] for r in store.db.execute('SELECT DISTINCT channel FROM videos')}
        for alias in aliases:
            for row in store.title_matches(alias, allowed):
                if row['series_id'] in excluded:
                    continue
                mapping = store.db.execute('SELECT imdb FROM mappings WHERE id=?', (row['id'],)).fetchone()
                explicit = mapping['imdb'] if mapping else row['imdb']
                if explicit and explicit != imdb:
                    continue
                titles = title_aliases(row)
                exact = normalize(alias) in {normalize(t) for t in titles}
                year_ok = kind == 'series' or (year is not None and row['year'] == year)
                if exact and year_ok and episode_ok(row):
                    found[row['id']] = row
        return list(found.values())


