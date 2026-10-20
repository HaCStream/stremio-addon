import asyncio
import re
import time
import httpx
from .core import normalize, title_aliases, movie_title_score


class Metadata:
    """Public metadata only: never send Telegram captions, filenames or channel IDs."""
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=8, headers={'User-Agent': 'TelegramStremio/1.0 (personal addon)'})
        self.cache = {}
        self.identities = {}

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
                if match:
                    year = int(match[0])
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
        async def tmdb_aliases():
            nonlocal year
            service = getattr(self, 'tmdb', None)
            if not service or not service.key:
                return
            try:
                data = await service.get(f'/find/{imdb}', external_source='imdb_id')
                candidates = data.get('tv_results' if kind == 'series' else 'movie_results', [])
                if candidates:
                    titles, tmdb_year = await self.resolve_tmdb(kind, candidates[0]['id'])
                    aliases.extend(titles)
                    if year is None:
                        year = tmdb_year
            except (httpx.HTTPError, ValueError, KeyError, TypeError, RuntimeError):
                pass
        await asyncio.gather(cinemeta(), wikidata(), tmdb_aliases())
        result = (list(dict.fromkeys(aliases)), year)
        if len(self.cache) >= 1000:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = (time.time() + (86400 if aliases else 60), result)
        return result

    async def resolve_tmdb(self, kind, identity):
        key = ('tmdb', kind, identity)
        if key in self.cache and self.cache[key][0] > time.time():
            return self.cache[key][1]
        service = getattr(self, 'tmdb', None)
        if not service or not service.key:
            return [], None
        category = 'tv' if kind == 'series' else 'movie'
        detail = await service.get(f'/{category}/{identity}', language='he-IL',
                                   append_to_response='alternative_titles,translations,external_ids')
        imdb = detail.get('imdb_id') or detail.get('external_ids', {}).get('imdb_id')
        if imdb and re.fullmatch(r'tt\d{7,10}', imdb):
            if len(self.identities) >= 1000:
                self.identities.pop(next(iter(self.identities)))
            self.identities[(kind, int(identity))] = imdb
        name = 'name' if kind == 'series' else 'title'
        aliases = [detail.get(name), detail.get('original_' + name)]
        alternatives = detail.get('alternative_titles', {})
        aliases += [x.get('title') for x in alternatives.get('results', alternatives.get('titles', []))]
        aliases += [x.get('data', {}).get(name) for x in detail.get('translations', {}).get('translations', [])
                    if x.get('iso_639_1') in ('he', 'en')]
        date = detail.get('first_air_date' if kind == 'series' else 'release_date') or ''
        result = (list(dict.fromkeys(x for x in aliases if x)), int(date[:4]) if date[:4].isdigit() else None)
        if len(self.cache) >= 1000:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = (time.time() + 86400, result)
        return result

    async def search_target(self, store, kind, identifier):
        """Resolve public IDs or a saved native show to aliases and episode numbers."""
        native = re.fullmatch(r'(tg:series:[0-9a-f]{24})(?::(\d+):(\d+))?', identifier)
        if native:
            rows = await store.run(lambda: store.series(native[1], store.stored_channels()))
            aliases = list(dict.fromkeys(a for r in rows for a in title_aliases(r) if a))
            return aliases, None, int(native[2]) if native[2] else None, int(native[3]) if native[3] else None
        public = re.fullmatch(r'(tt\d{7,10}|tmdb:(?:tv:)?[1-9]\d*)(?::(\d+):(\d+))?', identifier)
        if not public or (kind == 'movie' and public[2]):
            return [], None, None, None
        base, season, episode = public[1], int(public[2]) if public[2] else None, int(public[3]) if public[3] else None
        # Saved mappings avoid another public lookup and preserve their title aliases.
        column, identity = ('imdb', base) if base.startswith('tt') else ('tmdb', int(base.rsplit(':', 1)[-1]))
        def saved_aliases():
            aliases, offsets = [], set()
            if kind == 'series':
                for mapping in store.db.execute(
                        f"SELECT * FROM tmdb_series WHERE {column}=? AND status IN ('auto','manual')", (identity,)):
                    aliases.append(mapping['name'])
                    offsets.add((mapping['season_offset'], mapping['episode_offset']))
                    for row in store.series(mapping['series_id'], store.stored_channels()):
                        aliases.extend(title_aliases(row))
            return list(dict.fromkeys(a for a in aliases if a)), offsets
        aliases, offsets = await store.run(saved_aliases)
        if aliases:
            if season is not None and len(offsets) == 1:
                season_offset, episode_offset = next(iter(offsets))
                season, episode = season + season_offset, episode + episode_offset
            return aliases, None, season, episode
        try:
            aliases, year = (await self.resolve(kind, base) if base.startswith('tt')
                             else await self.resolve_tmdb(kind, identity))
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError):
            aliases, year = [], None
        return aliases, year, season, episode

    async def match(self, store, kind, identifier):
        tmdb = getattr(self, 'tmdb', None)
        if kind == 'series' and tmdb:
            mapped = await store.run(tmdb.streams, identifier)
            if mapped:
                return mapped
            # A saved identity is authoritative even when this episode is absent.
            from .tmdb import public_episode
            parsed = public_episode(identifier)
            if parsed:
                base = parsed[0]
                column, identity = ('imdb', base) if base.startswith('tt') else ('tmdb', int(base.rsplit(':', 1)[-1]))
                if await store.run(lambda: store.db.execute(
                        f"SELECT 1 FROM tmdb_series WHERE {column}=? AND status IN ('auto','manual')",
                        (identity,)).fetchone()):
                    return []
        pattern = r'(tt\d{7,10}|tmdb:(?:tv:)?[1-9]\d*)(?::(\d+):(\d+))?'
        m = re.fullmatch(pattern, identifier)
        if not m or (kind != 'series' and m[2]):
            return []
        imdb, season, episode = m[1], int(m[2]) if m[2] else None, int(m[3]) if m[3] else None
        def episode_ok(r):
            if kind == 'series' and season is None:
                return r['season'] is not None and r['episode'] is not None
            return (r['season'], r['episode']) == (season, episode)
        aliases, year, _, _ = await self.search_target(store, kind, identifier)
        if imdb.startswith('tmdb:'):
            imdb = self.identities.get((kind, int(imdb.rsplit(':', 1)[-1])), imdb)
        def match_local():
            found = {r['id']: r for r in store.explicit(imdb) if episode_ok(r)}
            excluded = tmdb.excluded_shows() if tmdb else set()
            allowed = store.stored_channels()
            if kind == 'movie':
                scored = []
                for row in store.movie_candidates(aliases, allowed):
                    mapping = store.db.execute('SELECT imdb FROM mappings WHERE id=?', (row['id'],)).fetchone()
                    explicit = mapping['imdb'] if mapping else row['imdb']
                    if explicit and explicit != imdb:
                        continue
                    score = movie_title_score(row, aliases, year)
                    if score >= 80 and episode_ok(row):
                        scored.append((score, row))
                for _, row in sorted(scored, key=lambda item: (-item[0], -item[1]['date'], item[1]['id'])):
                    found.setdefault(row['id'], row)
                return list(found.values())
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
        return await store.run(match_local)
