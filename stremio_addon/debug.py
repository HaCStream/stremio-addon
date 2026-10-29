import hmac
import logging
import re
import time
import traceback
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
from pydantic import BaseModel, Field

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from .ai_search import ai_query, GeminiCooldown, gemini_diagnostics
from .core import normalize, source_name
from .version import get_version
from .downloads import InsufficientSpace


logger = logging.getLogger(__name__)


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

        try:
            response = await call_next(request)
        except Exception:
            # Keep credentials and upstream response bodies out of the browser.
            # Log a redacted traceback without the request query or headers.
            trace = redact_diagnostics(traceback.format_exc())
            trace = re.sub(r'https?://\S+', '[url]', trace)
            logger.error('Developer UI request failed: %s %s\n%s',
                         request.method, normalized_path, trace)
            response = JSONResponse(status_code=500, content={
                'detail': 'Internal server error; check the add-on logs for details.'})
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        return response

    def authorize(key):
        if app.state.runtime.cfg.skip_debug_auth:
            return
        expected = app.state.runtime.cfg.dev_ui_password or app.state.runtime.cfg.key
        if not key or not hmac.compare_digest(key.encode(), expected.encode()):
            raise HTTPException(401, 'Invalid Developer UI password')

    def safe_text(value):
        text = str(value)
        for secret in (app.state.runtime.cfg.key, app.state.runtime.cfg.dev_ui_password,
                       app.state.runtime.cfg.session,
                       app.state.runtime.cfg.api_hash, app.state.runtime.cfg.gemini_api_key,
                       app.state.runtime.cfg.tmdb_api_key):
            if secret:
                text = text.replace(secret, '[redacted]')
        text = re.sub(r'https?://\S+', '[url]', text)
        return ''.join(' ' if unicodedata.category(c).startswith('C') else c for c in text)[:200]

    def channel_title(entity, channel):
        return source_name(entity, channel)

    def redact_diagnostics(value):
        if isinstance(value, dict):
            return {redact_diagnostics(k): redact_diagnostics(v) for k, v in value.items()}
        if isinstance(value, list):
            return [redact_diagnostics(v) for v in value]
        if isinstance(value, str):
            for secret in (app.state.runtime.cfg.key, app.state.runtime.cfg.dev_ui_password,
                           app.state.runtime.cfg.session,
                           app.state.runtime.cfg.api_hash, app.state.runtime.cfg.gemini_api_key,
                           app.state.runtime.cfg.tmdb_api_key):
                if secret:
                    value = value.replace(secret, '[redacted]')
        return value

    async def channels():
        shared = app.state.runtime
        counts = await shared.store.run(lambda: {r['channel']: r['count'] for r in shared.store.db.execute(
            'SELECT channel, count(*) AS count FROM videos GROUP BY channel')})
        result = []
        for channel, entity in shared.tg.channels.items():
            point = shared.store.checkpoint(channel)
            result.append({
                'id': channel,
                'name': channel_title(entity, channel),
                'videos': counts.get(channel, 0),
                'queue': shared.tg.queue(channel) if shared.cfg.catalog_indexing_enabled else None,
                'complete': bool(point['complete']),
                'phase': ('on_demand' if not shared.cfg.catalog_indexing_enabled else
                          'indexing' if channel in getattr(shared.tg, 'pending_channels', set())
                          else 'ready' if point['complete'] else 'checking'),
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
        skip_auth = app.state.runtime.cfg.skip_debug_auth
        html = Path(__file__).with_name('debug.html').read_text(encoding='utf-8')
        html = html.replace('__SKIP_DEV_UI_AUTH__', 'true' if skip_auth else 'false')
        html = html.replace('__LOGIN_CLASS__', 'hidden' if skip_auth else '')
        html = html.replace('__APP_CLASS__', '' if skip_auth else 'hidden')
        return HTMLResponse(html)

    @app.get('/api/auth-config')
    async def auth_config():
        return {'skip_debug_auth': app.state.runtime.cfg.skip_debug_auth}

    @app.get('/api/overview')
    async def overview(x_debug_key: str | None = Header(None)):
        authorize(x_debug_key)
        shared = app.state.runtime
        return {
            'version': get_version(),
            'install_url': f'{shared.cfg.url.rstrip("/")}/{shared.cfg.key}/manifest.json',
            'connected': shared.tg.client.is_connected(),
            'phase': shared.tg.status.get('phase'),
            'last_error': shared.tg.status.get('last_error'),
            'pending_channels': shared.tg.status.get('pending_channels', 0),
            'scan_interval_seconds': shared.cfg.scan_interval_seconds,
            'catalog_indexing_enabled': shared.cfg.catalog_indexing_enabled,
            'uptime_seconds': int(time.time() - shared.started_at),
            'videos': await shared.store.run(lambda: shared.store.db.execute('SELECT count(*) FROM videos').fetchone()[0]),
            'channels': len(shared.tg.channels),
            'debug_port': shared.cfg.debug_port,
            'addon_port': shared.cfg.port,
            'cache_mb': shared.cfg.cache_bytes // 1024**2,
            'ai_search_enabled': shared.cfg.ai_search_enabled,
            'require_ai_suffix_for_ai_search': shared.cfg.require_ai_suffix_for_ai_search,
            'tmdb_enabled': bool(shared.cfg.tmdb_api_key),
        }

    @app.get('/api/channels')
    async def channel_list(x_debug_key: str | None = Header(None)):
        authorize(x_debug_key)
        return {'channels': await channels()}

    def tmdb_service():
        service = app.state.runtime.tmdb
        if not service.key:
            raise HTTPException(409, 'Set TMDB_API_KEY to enable TMDB matching')
        return service

    @app.get('/api/tmdb/shows')
    async def tmdb_shows(x_debug_key: str | None = Header(None)):
        authorize(x_debug_key)
        return {'enabled': bool(app.state.runtime.cfg.tmdb_api_key),
                'shows': await app.state.runtime.store.run(
                    app.state.runtime.tmdb.shows, set(app.state.runtime.tg.channels))}

    @app.get('/api/tmdb/candidates')
    async def tmdb_candidates(series_id: str, q: str = Query('', max_length=200),
                              x_debug_key: str | None = Header(None)):
        authorize(x_debug_key)
        try:
            return {'candidates': await tmdb_service().candidates(series_id, q)}
        except ValueError:
            raise HTTPException(400, 'Invalid Telegram show or TMDB response') from None
        except (httpx.HTTPError, RuntimeError, KeyError, TypeError):
            raise HTTPException(502, 'TMDB lookup failed; check the API key or retry later') from None

    class TMDBMapping(BaseModel):
        series_id: str
        tmdb: int = Field(gt=0)
        season_offset: int = Field(default=0, ge=-1000, le=1000)
        episode_offset: int = Field(default=0, ge=-10000, le=10000)

    @app.put('/api/tmdb/mapping')
    async def tmdb_mapping(body: TMDBMapping, x_debug_key: str | None = Header(None)):
        authorize(x_debug_key)
        try:
            candidate = await tmdb_service().select(body.series_id, body.tmdb,
                                                    body.season_offset, body.episode_offset)
            return {'mapping': candidate}
        except ValueError:
            raise HTTPException(400, 'Invalid Telegram show or TMDB response') from None
        except (httpx.HTTPError, RuntimeError, KeyError, TypeError):
            raise HTTPException(502, 'TMDB lookup failed; check the API key or retry later') from None

    @app.delete('/api/tmdb/mapping')
    async def tmdb_block(series_id: str, x_debug_key: str | None = Header(None)):
        authorize(x_debug_key)
        try:
            await app.state.runtime.tmdb.block(series_id)
        except ValueError:
            raise HTTPException(400, 'Unknown Telegram show') from None
        return {'ok': True}

    @app.get('/api/activity')
    async def activity(x_debug_key: str | None = Header(None)):
        authorize(x_debug_key)
        return {'events': list(app.state.runtime.events)}

    @app.post('/api/sync', status_code=202)
    async def sync(x_debug_key: str | None = Header(None)):
        authorize(x_debug_key)
        shared = app.state.runtime
        try:
            state = shared.tg.request_sync()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        shared.record({'event': 'sync_requested', 'status': 202})
        return {'accepted': True, 'sync': state}

    @app.get('/api/sync')
    async def sync_status(x_debug_key: str | None = Header(None)):
        authorize(x_debug_key)
        return {'sync': dict(app.state.runtime.tg.sync_status)}

    class DownloadRequest(BaseModel):
        source_id: str = Field(min_length=1, max_length=200)

    class DownloadSelection(BaseModel):
        ids: list[str] = Field(min_length=1, max_length=10000)

    @app.get('/api/downloads')
    async def downloads(x_debug_key: str | None = Header(None)):
        authorize(x_debug_key)
        return {'downloads': app.state.runtime.downloads.listing()}

    def space_error(exc):
        return HTTPException(409, detail={'code': 'insufficient_space', 'message': str(exc),
                                         'required_bytes': exc.required, 'available_bytes': exc.available})

    @app.post('/api/downloads', status_code=202)
    async def download_create(body: DownloadRequest, x_debug_key: str | None = Header(None)):
        authorize(x_debug_key)
        shared = app.state.runtime
        row = await shared.store.run(shared.store.get, body.source_id)
        if not row or row['channel'] not in shared.tg.channels:
            raise HTTPException(404, 'Video is not available in the configured chats')
        try:
            identifier = await shared.downloads.create(row)
        except InsufficientSpace as exc:
            raise space_error(exc) from None
        except OSError:
            raise HTTPException(409, 'Download storage is not writable; check the media mount and permissions') from None
        return {'id': identifier}

    @app.post('/api/downloads/delete')
    async def download_delete(body: DownloadSelection, x_debug_key: str | None = Header(None)):
        authorize(x_debug_key)
        manager = app.state.runtime.downloads
        for identifier in dict.fromkeys(body.ids):
            try:
                await manager.action(identifier, 'delete')
            except KeyError:
                pass  # Deletion is idempotent, including another tab's selection.
        return {'downloads': manager.listing()}

    @app.post('/api/downloads/{identifier}/{action}')
    async def download_action(identifier: str, action: str,
                              x_debug_key: str | None = Header(None)):
        authorize(x_debug_key)
        if action not in ('pause', 'resume', 'delete'):
            raise HTTPException(400, 'Unknown download action')
        try:
            await app.state.runtime.downloads.action(identifier, action)
        except InsufficientSpace as exc:
            raise space_error(exc) from None
        except KeyError:
            raise HTTPException(404, 'Download not found') from None
        return {'downloads': app.state.runtime.downloads.listing()}

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
        available = await channels()
        allowed = set(shared.tg.channels)
        aliases, resolved_year = [], None
        if mode == 'ai':
            if not shared.ai:
                raise HTTPException(409, 'AI features are not enabled')
            clean = ai_query(q, shared.cfg.require_ai_suffix_for_ai_search)
            diagnostics = {}
            token = gemini_diagnostics.set(diagnostics)
            try:
                grouped = {name: await shared.ai.search(clean, allowed, skip, media_type) if clean else []
                           for name, media_type in (('movies', 'movie'), ('series', 'series'))}
                if not clean:
                    diagnostics.update(source='skipped', sent=False, reason=(
                        'Finish your description with a separate AI word to submit it to Gemini.'
                        if clean is None else 'Enter a non-empty description before the AI suffix.'))
            except GeminiCooldown:
                return JSONResponse(status_code=429, content={
                    'detail': 'Gemini rate limited; retry in '
                              + str(shared.ai.cooldown_remaining()) + ' seconds',
                    'gemini': redact_diagnostics(diagnostics)})
            except Exception as exc:
                diagnostics['error_type'] = type(exc).__name__
                return JSONResponse(status_code=502, content={
                    'detail': 'AI search failed; inspect the Gemini exchange below.',
                    'gemini': redact_diagnostics(diagnostics)})
            finally:
                gemini_diagnostics.reset(token)
            matched = [(row, 'AI online title match') for rows in grouped.values() for row in rows]
        elif mode == 'text':
            rows, live = await shared.search.catalog(q, skip) if q.strip() else ([], set())
            matched = [(row, 'Telegram live search (60s cache)' if row['id'] in live else 'full-text index') for row in rows]
        else:
            rows, live = await shared.search.match(kind, q.strip())
            allowed = set(shared.tg.channels)
            aliases, resolved_year, _, _ = await shared.metadata.search_target(shared.store, kind, q.strip())
            matched = []
            for row in rows:
                if row['channel'] not in allowed:
                    continue
                mapping = shared.store.db.execute('SELECT imdb FROM mappings WHERE id=?', (row['id'],)).fetchone()
                explicit = mapping['imdb'] if mapping else row['imdb']
                matched.append((row, ('Telegram live search + metadata match' if row['id'] in live else
                                      'explicit IMDb mapping' if explicit else 'metadata title/year match')))
        available = await channels()
        allowed = set(shared.tg.channels)
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
            **({'movies': [result_row(row, 'AI online title match') for row in grouped['movies']],
                'series': [result_row(row, 'AI online title match') for row in grouped['series']],
                'gemini': redact_diagnostics(diagnostics)}
               if mode == 'ai' else {}),
        }

    return app

