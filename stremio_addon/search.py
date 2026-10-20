"""Search Telegram on demand and retain discovered videos for repeat use."""
import asyncio
import logging
import re

log = logging.getLogger(__name__)


class OnDemandSearch:
    def __init__(self, store, gateway, metadata):
        self.store, self.gateway, self.metadata = store, gateway, metadata
        self.timeout = 12

    async def live(self, queries, aliases=(), season=None, episode=None):
        if not hasattr(self.gateway, 'search'):
            return set()
        if hasattr(self.gateway, 'ensure_sources'):
            self.gateway.ensure_sources()
        ids = set()

        async def run():
            visited = set()
            markers = ([f'S{season:02d}E{episode:02d}', f'עונה {season} פרק {episode}']
                       if season is not None and episode is not None else [''])

            async def named_sources():
                for channel in self.gateway.series_sources(aliases):
                    if channel in visited:
                        continue
                    visited.add(channel)
                    for marker in markers:
                        ids.update(await self.gateway.search(marker, channel))

            # Query a confidently named source first for episode-only captions.
            await named_sources()
            for query in list(dict.fromkeys(q.strip() for q in queries if q.strip()))[:8]:
                ids.update(await self.gateway.search(query))
                # Initial dialog discovery may have progressed during the RPC.
                await named_sources()

        try:
            await asyncio.wait_for(run(), self.timeout)
        except Exception as exc:
            # Partial/local results remain usable on timeouts, flood waits and outages.
            # Queries and upstream exception text may contain private data.
            log.warning('Telegram live search incomplete: error=%s results=%s', type(exc).__name__, len(ids))
        return ids

    async def catalog(self, query='', skip=0, kind=None):
        def local():
            allowed = set(self.gateway.channels)
            if kind:
                return self.store.grouped_catalog(kind, allowed, query, 0, skip + 100)
            return [r for r in self.store.catalog(query, 0, skip + 100) if r['channel'] in allowed]
        rows = await self.store.run(local)
        live = await self.live([query], [query]) if query.strip() else set()

        def merged():
            # Re-read after Telegram results are upserted, so grouped series and
            # pagination include the newly discovered episodes as well.
            combined = {r['id']: r for r in local()}
            for item in live:
                row = self.store.get(item)
                if row and row['channel'] in self.gateway.channels and (
                        kind is None or bool(row['series_id']) == (kind == 'series')):
                    combined[item] = row
            ordered = sorted(combined.values(), key=lambda r: (-r['date'], r['id']))
            if kind:
                groups = {}
                for row in ordered:
                    groups.setdefault(row['series_id'] or row['id'], row)
                ordered = list(groups.values())
            return ordered[skip:skip + 100]
        if not query.strip():
            return rows[skip:skip + 100], live
        found = await self.store.run(merged)
        self.queue_matches(found)
        return found, live

    async def match(self, kind, identifier):
        # Persist live discoveries, then apply authoritative saved identity rules.
        rows = []
        live = set()
        if hasattr(self.gateway, 'search'):
            try:
                aliases, year, season, episode = await self.metadata.search_target(self.store, kind, identifier)
                # Exact and shortened names, localized/English/original aliases.
                titles = list(dict.fromkeys(t for alias in aliases[:3]
                              for t in (alias, re.split(r':|\s+[-–—]\s+', alias, maxsplit=1)[0])))
                queries = []
                if kind == 'series' and season is not None:
                    for pattern in ('{title} S{season:02d}E{episode:02d}',
                                    '{title} S{season}E{episode}',
                                    '{title} עונה {season} פרק {episode}'):
                        queries.extend(pattern.format(title=title, season=season, episode=episode) for title in titles)
                else:
                    queries.extend(titles)
                    if kind == 'movie' and year:
                        queries.extend(f'{title} {year}' for title in titles)
                if kind == 'series' and season is None and hasattr(self.gateway, 'search_series'):
                    await self.gateway.search_series(identifier, aliases)
                else:
                    live = await self.live(queries, aliases if kind == 'series' else (), season, episode)
            except Exception as exc:
                log.warning('Live search metadata unavailable: error=%s', type(exc).__name__)
        native = re.fullmatch(r'(tg:series:[0-9a-f]{24}):(\d+):(\d+)', identifier)
        if native:
            rows = await self.store.run(self.store.series, native[1], set(self.gateway.channels), int(native[2]), int(native[3]))
        elif not identifier.startswith('tg:'):
            rows = await self.metadata.match(self.store, kind, identifier)
        rows = [r for r in rows if r['channel'] in self.gateway.channels]
        self.queue_matches(rows)
        return rows, live

    def queue_matches(self, rows):
        tmdb = getattr(self.metadata, 'tmdb', None)
        if tmdb and tmdb.key:
            tmdb.enqueue(rows)

    async def show(self, identifier):
        aliases, _, _, _ = await self.metadata.search_target(self.store, 'series', identifier)
        if hasattr(self.gateway, 'search_series'):
            await self.gateway.search_series(identifier, aliases)
        else:
            await self.live(aliases, aliases)
        rows = await self.store.run(self.store.series, identifier, set(self.gateway.channels))
        self.queue_matches(rows)
        return rows

    async def discover_titles(self, titles, kind):
        await self.live(titles, titles if kind == 'series' else ())
        return set(self.gateway.channels)
