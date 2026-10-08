"""Persistent show identity matching. Only extracted title aliases go to TMDB."""
import asyncio
import json
import logging
import re
import time

import httpx

from .core import normalize, title_aliases

log = logging.getLogger(__name__)


def public_episode(identifier):
    match = re.fullmatch(r'(tt\d{7,10}|tmdb:(?:tv:)?[1-9]\d*)(?::(\d+):(\d+))?', identifier)
    if not match:
        return None
    base = match[1]
    return (base, int(match[2]) if match[2] else None,
            int(match[3]) if match[3] else None)


class TMDB:
    def __init__(self, cfg, store, gateway):
        self.key, self.store, self.gateway = cfg.tmdb_api_key, store, gateway
        self.http = httpx.AsyncClient(timeout=10)
        self.lock = asyncio.Lock()
        self.retry_at = 0

    async def get(self, path, **params):
        if time.time() < self.retry_at:
            raise RuntimeError('TMDB is temporarily unavailable; retry later')
        response = await self.http.get('https://api.themoviedb.org/3' + path,
                                       params=dict(api_key=self.key, **params))
        if response.status_code == 429:
            try:
                delay = max(60, min(3600, int(response.headers.get('Retry-After', '60'))))
            except ValueError:
                delay = 60
            self.retry_at = time.time() + delay
        elif response.status_code in (401, 403):
            self.retry_at = time.time() + 3600
        response.raise_for_status()
        return response.json()

    def shows(self):
        allowed = sorted(self.gateway.channels)
        if not allowed:
            return []
        rows = self.store.db.execute(
            'SELECT series_id, MIN(title) AS title, count(*) AS episodes FROM videos '
            'WHERE series_id IS NOT NULL AND channel IN (' + ','.join('?' for _ in allowed) + ') '
            'GROUP BY series_id ORDER BY title', allowed)
        result = []
        for row in rows:
            item = dict(row)
            saved = self.store.db.execute('SELECT * FROM tmdb_series WHERE series_id=?',
                                          (row['series_id'],)).fetchone()
            item['mapping'] = dict(saved) if saved else None
            if saved:
                item['mapping']['candidates'] = json.loads(saved['candidates'])
            result.append(item)
        return result

    async def details(self, tmdb_id):
        detail = await self.get(f'/tv/{tmdb_id}', language='he-IL',
                                append_to_response='external_ids,alternative_titles,translations')
        aliases = [detail.get('name'), detail.get('original_name')]
        aliases += [x.get('title') for x in detail.get('alternative_titles', {}).get('results', [])]
        aliases += [x.get('data', {}).get('name') for x in detail.get('translations', {}).get('translations', [])
                    if x.get('iso_639_1') in ('he', 'en')]
        imdb = detail.get('external_ids', {}).get('imdb_id')
        if imdb and not re.fullmatch(r'tt\d{7,10}', imdb):
            imdb = None
        return {'tmdb': int(detail['id']), 'imdb': imdb, 'name': detail.get('name') or '',
                'year': (detail.get('first_air_date') or '')[:4],
                'aliases': list(dict.fromkeys(a for a in aliases if a))}

    async def candidates(self, identifier, query=''):
        rows = self.store.series(identifier, self.gateway.channels)
        if not rows:
            raise ValueError('Unknown or unavailable Telegram show')
        # Sample aliases across the show, not just one episode's filename.
        aliases = list(dict.fromkeys(a for row in rows for a in title_aliases(row)))[:4]
        queries = [query.strip()] if query.strip() else aliases
        ids = {}
        for title in queries:
            for language in ('he-IL', 'en-US'):
                data = await self.get('/search/tv', query=title, language=language, include_adult='false')
                for item in data.get('results', [])[:5]:
                    if len(ids) < 10:
                        ids[item['id']] = item
        candidates = []
        years = {row['year'] for row in rows if row['year']}
        names = {normalize(a) for a in aliases}
        for tmdb_id in ids:
            candidate = await self.details(tmdb_id)
            candidate['exact'] = bool(names & {normalize(a) for a in candidate['aliases']})
            candidate['year_match'] = bool(candidate['year'] and int(candidate['year']) in years)
            candidates.append(candidate)
        return sorted(candidates, key=lambda c: (not c['exact'], not c['year_match'], c['tmdb']))

    def save(self, identifier, candidate, status, candidates=(), season_offset=0, episode_offset=0):
        with self.store.db:
            self.store.db.execute(
                'INSERT OR REPLACE INTO tmdb_series VALUES (?,?,?,?,?,?,?,?,?)',
                (identifier, candidate.get('tmdb'), candidate.get('imdb'), candidate.get('name'),
                 status, json.dumps(candidates, ensure_ascii=False), time.time(), season_offset, episode_offset))

    async def match_show(self, identifier):
        async with self.lock:
            previous = self.store.db.execute('SELECT status FROM tmdb_series WHERE series_id=?',
                                             (identifier,)).fetchone()
            if previous and previous['status'] in ('auto', 'manual', 'blocked'):
                return
            candidates = await self.candidates(identifier)
            exact = [c for c in candidates if c['exact']]
            # A year from an episode upload need not be the show's premiere year.
            # Use it only to disambiguate multiple exact title matches.
            dated = [c for c in exact if c['year_match']]
            selected = exact[0] if len(exact) == 1 else dated[0] if len(dated) == 1 else None
            self.save(identifier, selected or {}, 'auto' if selected else 'ambiguous' if candidates else 'unmatched', candidates)
            log.info('TMDB show match: show=%s status=%s candidates=%d',
                     identifier, 'auto' if selected else 'needs_review', len(candidates))

    async def select(self, identifier, tmdb_id, season_offset=0, episode_offset=0):
        async with self.lock:
            if not self.store.series(identifier, self.gateway.channels):
                raise ValueError('Unknown or unavailable Telegram show')
            candidate = await self.details(tmdb_id)
            self.save(identifier, candidate, 'manual', season_offset=season_offset, episode_offset=episode_offset)
            log.info('TMDB manual mapping saved: show=%s tmdb=%s', identifier, tmdb_id)
            return candidate

    async def block(self, identifier):
        async with self.lock:
            if not self.store.series(identifier, self.gateway.channels):
                raise ValueError('Unknown or unavailable Telegram show')
            self.save(identifier, {}, 'blocked')

    def streams(self, identifier):
        parsed = public_episode(identifier)
        if not parsed:
            return []
        base, season, episode = parsed
        column, identity = ('imdb', base) if base.startswith('tt') else ('tmdb', int(base.rsplit(':', 1)[1]))
        mappings = self.store.db.execute(
            f"SELECT * FROM tmdb_series WHERE {column}=? AND status IN ('auto','manual')", (identity,)).fetchall()
        if not mappings:
            return []
        found = {}
        identities = {identity} if column == 'imdb' else {m['imdb'] for m in mappings if m['imdb']}
        for imdb_identity in identities:
            for row in self.store.explicit(imdb_identity):
                if row['channel'] not in self.gateway.channels or row['series_id'] is None:
                    continue
                if season is None or (row['season'], row['episode']) == (season, episode):
                    found[row['id']] = row
        for mapping in mappings:
            for row in self.store.series(mapping['series_id'], self.gateway.channels,
                                          season + mapping['season_offset'] if season is not None else None,
                                          episode + mapping['episode_offset'] if episode is not None else None):
                # Per-file manual mappings and caption IDs retain precedence.
                explicit = self.store.db.execute('SELECT imdb FROM mappings WHERE id=?', (row['id'],)).fetchone()
                if explicit:
                    continue
                imdb = row['imdb']
                if imdb and imdb != mapping['imdb']:
                    continue
                if season is None:
                    row = dict(row, season=row['season'] - mapping['season_offset'],
                               episode=row['episode'] - mapping['episode_offset'])
                    if row['season'] < 0 or row['episode'] < 1:
                        continue
                found[row['id']] = row
        return list(found.values())

    def excluded_shows(self):
        return {r['series_id'] for r in self.store.db.execute('SELECT series_id FROM tmdb_series')}

    async def scan_once(self):
        for show in self.shows():
            saved = show['mapping']
            if saved and (saved['status'] in ('auto', 'manual', 'blocked') or
                          time.time() - saved['checked_at'] < 86400):
                continue
            if time.time() < self.retry_at:
                break
            try:
                await self.match_show(show['series_id'])
            except (httpx.HTTPError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                # Never log the request URL: it includes the API key.
                log.warning('TMDB matching failed: show=%s error=%s', show['series_id'], type(exc).__name__)
                self.retry_at = max(self.retry_at, time.time() + 60)
                break

    async def run(self):
        while True:
            try:
                await self.scan_once()
            except Exception as exc:
                log.warning('TMDB matching pass failed: error=%s', type(exc).__name__)
            await asyncio.sleep(60)
