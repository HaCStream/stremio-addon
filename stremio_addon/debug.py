import hmac
import re
import time
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse

from .core import normalize
from .version import get_version


def create_debug_app(runtime):
    @asynccontextmanager
    async def lifespan(app):
        shared = await runtime.acquire()
        app.state.runtime = shared
        try:
            yield
        finally:
            await runtime.release()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def private_headers(request, call_next):
        # Home Assistant ingress can prepend ingress_entry ('/') to an already
        # slash-prefixed request path, so the backend may receive // or //api/....
        # Frigate's nginx layer normalizes this; FastAPI does not, so do it here.
        path = request.scope.get('path', '/')
        normalized_path = '/' + path.lstrip('/')
        if normalized_path != path:
            request.scope['path'] = normalized_path
            request.scope['raw_path'] = normalized_path.encode('utf-8')

        response = await call_next(request)
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        return response

    def authorize(key):
        expected = app.state.runtime.cfg.key
        if not key or not hmac.compare_digest(key.encode(), expected.encode()):
            raise HTTPException(401, 'Invalid API key')

    def safe_text(value):
        text = str(value)
        for secret in (app.state.runtime.cfg.key, app.state.runtime.cfg.session,
                       app.state.runtime.cfg.api_hash, app.state.runtime.cfg.gemini_api_key):
            if secret:
                text = text.replace(secret, '[redacted]')
        text = re.sub(r'https?://\S+', '[url]', text)
        return ''.join(' ' if unicodedata.category(c).startswith('C') else c for c in text)[:200]

    def channel_title(entity, channel):
        return str(getattr(entity, 'title', None) or channel)

    def channels():
        shared = app.state.runtime
        counts = {r['channel']: r['count'] for r in shared.store.db.execute(
            'SELECT channel, count(*) AS count FROM videos GROUP BY channel')}
        result = []
        for channel, entity in shared.tg.channels.items():
            point = shared.store.checkpoint(channel)
            result.append({
                'id': channel,
                'name': channel_title(entity, channel),
                'videos': counts.get(channel, 0),
                'oldest_message': point['oldest'],
                'newest_message': point['newest'],
                'complete': bool(point['complete']),
            })
        return sorted(result, key=lambda item: item['name'].casefold())

    def result_row(row, matched_by):
        return {
            'id': row['id'], 'title': row['title'], 'filename': row['filename'],
            'caption': row['caption'], 'year': row['year'], 'season': row['season'],
            'episode': row['episode'], 'quality': row['quality'], 'size': row['size'],
            'mime': row['mime'], 'date': row['date'], 'channel_id': row['channel'],
            'channel': row['channel_name'], 'message_id': row['message'],
            'imdb': row['imdb'], 'matched_by': matched_by,
        }

    @app.get('/')
    async def index():
        return FileResponse(Path(__file__).with_name('debug.html'))

    @app.get('/api/overview')
    async def overview(x_debug_key: str | None = Header(None)):
        authorize(x_debug_key)
        shared = app.state.runtime
        return {
            'version': get_version(),
            'connected': shared.tg.client.is_connected(),
            'phase': shared.tg.status.get('phase'),
            'last_error': shared.tg.status.get('last_error'),
            'uptime_seconds': int(time.time() - shared.started_at),
            'videos': shared.store.db.execute('SELECT count(*) FROM videos').fetchone()[0],
            'channels': len(shared.tg.channels),
            'debug_port': shared.cfg.debug_port,
            'addon_port': shared.cfg.port,
            'cache_mb': shared.cfg.cache_bytes // 1024**2,
            'ai_search_enabled': shared.cfg.ai_search_enabled,
            'ai_search_prefix_enabled': shared.cfg.ai_search_prefix_enabled,
            'ai_index': shared.ai.snapshot() if shared.ai else None,
        }

    @app.get('/api/channels')
    async def channel_list(x_debug_key: str | None = Header(None)):
        authorize(x_debug_key)
        return {'channels': channels()}

    @app.get('/api/activity')
    async def activity(x_debug_key: str | None = Header(None)):
        authorize(x_debug_key)
        return {'events': list(app.state.runtime.events)}

    @app.post('/api/sync', status_code=202)
    async def sync(x_debug_key: str | None = Header(None)):
        authorize(x_debug_key)
        shared = app.state.runtime
        shared.tg.request_sync()
        shared.record({'event': 'sync_requested', 'status': 202})
        return {'accepted': True}

    @app.get('/api/search')
    async def search(
        q: str = Query('', max_length=500),
        mode: str = Query('text', pattern='^(text|imdb|ai)$'),
        kind: str = Query('movie', pattern='^(movie|series)$'),
        skip: int = Query(0, ge=0, le=10_000_000),
        x_debug_key: str | None = Header(None),
    ):
        authorize(x_debug_key)
        shared = app.state.runtime
        started = time.perf_counter()
        available = channels()
        allowed = set(shared.tg.channels)
        aliases, resolved_year = [], None
        if mode == 'ai':
            if not shared.ai:
                raise HTTPException(409, 'AI features are not enabled')
            # The dedicated debug form explicitly selects AI, even when the
            # Stremio catalog requires a prefix for automatic routing.
            clean = q.strip()
            if not clean:
                matched = []
            else:
                try:
                    rows = await shared.ai.search(clean, allowed, skip)
                    matched = [(row, 'AI search') for row in rows]
                except Exception:
                    matched = []
        elif mode == 'text':
            rows = shared.store.catalog(q, skip, 100) if q.strip() else []
            matched = [(row, 'full-text index') for row in rows if row['channel'] in allowed]
        else:
            rows = await shared.metadata.match(shared.store, kind, q.strip())
            aliases, resolved_year = await shared.metadata.resolve(kind, q.split(':', 1)[0].strip())
            matched = []
            for row in rows:
                if row['channel'] not in allowed:
                    continue
                mapping = shared.store.db.execute('SELECT imdb FROM mappings WHERE id=?', (row['id'],)).fetchone()
                explicit = mapping['imdb'] if mapping else row['imdb']
                matched.append((row, 'explicit IMDb mapping' if explicit else 'metadata title/year match'))
        items = [result_row(row, reason) for row, reason in matched]
        per_channel = {str(item['id']): 0 for item in available}
        for item in items:
            per_channel[str(item['channel_id'])] = per_channel.get(str(item['channel_id']), 0) + 1
        duration = round((time.perf_counter() - started) * 1000, 1)
        event = {
            'event': 'debug_search', 'query': safe_text(q), 'mode': mode, 'type': kind,
            'result_count': len(items), 'channel_count': len(allowed),
            'duration_ms': duration, 'status': 200,
        }
        shared.record(event)
        return {
            'query': q, 'normalized_query': normalize(q), 'mode': mode, 'type': kind,
            'result_count': len(items), 'duration_ms': duration, 'results': items,
            'channels_queried': available, 'results_by_channel': per_channel,
            'resolved_aliases': aliases, 'resolved_year': resolved_year,
        }

    return app
