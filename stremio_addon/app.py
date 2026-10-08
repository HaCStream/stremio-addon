import asyncio
import hmac
import json
import logging
import re
import time
import unicodedata
from contextlib import asynccontextmanager
from urllib.parse import parse_qs
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from .core import byte_range
from .ai_search import ai_query
from .runtime import Runtime
from .telegram import Telegram
from .version import get_version

interaction_log = logging.getLogger('stremio_addon.interactions')
interaction_log.setLevel(logging.INFO)


def safe_log_text(value, cfg):
    text = str(value)
    for secret in (cfg.key, cfg.session, cfg.api_hash, cfg.gemini_api_key):
        if secret:
            text = text.replace(secret, '[redacted]')
    text = re.sub(r'https?://\S+|/(?:play|thumb)/\S+', '[url]', text)
    text = ''.join(' ' if unicodedata.category(c).startswith('C') else c for c in text)
    return text[:200]


def create_app(settings=None, gateway_factory=Telegram):
    runtime = Runtime(settings, gateway_factory)
    return create_app_with_runtime(runtime)


def create_app_with_runtime(runtime):
    @asynccontextmanager
    async def lifespan(app):
        shared = await runtime.acquire()
        app.state.runtime = shared
        app.state.cfg, app.state.store, app.state.tg = shared.cfg, shared.store, shared.tg
        app.state.tokens, app.state.metadata = shared.tokens, shared.metadata
        try:
            yield
        finally:
            await runtime.release()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['GET', 'HEAD', 'OPTIONS', 'PUT'], allow_headers=['Range', 'Content-Type'], expose_headers=['Content-Range', 'Content-Length', 'Accept-Ranges'])

    @app.middleware('http')
    async def private(request, call_next):
        started = time.perf_counter()
        status_code = 500
        error = None
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            summary = getattr(request.state, 'interaction', None)
            if summary is not None:
                summary.update(status=status_code, duration_ms=round((time.perf_counter() - started) * 1000, 1))
                if error:
                    summary['error'] = error
                interaction_log.log(logging.WARNING if status_code >= 400 else logging.INFO,
                                    '%s', json.dumps(summary, ensure_ascii=False))
                runtime.record(summary)
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def interaction(request, event, **fields):
        request.state.interaction = dict(event=event, **{
            name: safe_log_text(value, app.state.cfg) if isinstance(value, str) else value
            for name, value in fields.items()
        })

    def results(request, rows):
        request.state.interaction.update(
            result_count=len(rows),
            channel_count=len({r['channel'] for r in rows}),
            sample_titles=[safe_log_text(r['title'], app.state.cfg) for r in rows[:3]],
        )

    def auth(key):
        if not hmac.compare_digest(key.encode(), app.state.cfg.key.encode()):
            raise HTTPException(404)

    def token_row(token, scope):
        try:
            item = app.state.tokens.verify(token, scope)
        except ValueError:
            raise HTTPException(403, 'Invalid or expired token') from None
        row = app.state.store.get(item)
        if not row or row['channel'] not in app.state.tg.channels:
            raise HTTPException(404)
        return row

    def url(row, scope):
        return f'{app.state.cfg.url}/{scope}/{app.state.tokens.sign(row["id"], scope)}'

    def meta(row, kind='movie', episodes=None, identifier=None):
        result = {'id': row['id'], 'type': kind, 'name': row['title'], 'posterShape': 'landscape', 'poster': url(row, 'thumb'), 'description': row['caption'] + '\n\n' + row['channel_name'], 'releaseInfo': str(row['year'] or ''), 'behaviorHints': {'defaultVideoId': row['id']}}
        if kind == 'series':
            identifier = identifier or row['series_id'] or row['id']
            if episodes is None:
                episodes = app.state.store.series(identifier, app.state.tg.channels) or [row]
            result['id'] = identifier
            result.pop('behaviorHints')
            videos = {}
            for episode in episodes:
                season = episode['season'] if episode['season'] is not None else 1
                number = episode['episode'] if episode['episode'] is not None else 1
                videos.setdefault((season, number), {
                    'id': f'{identifier}:{season}:{number}' if identifier != row['id'] else episode['id'],
                    'title': f'Episode {number}', 'season': season, 'episode': number})
            result['videos'] = [videos[key] for key in sorted(videos)]
        return result

    @app.get('/healthz')
    async def health():
        return {'ok': True}

    @app.get('/{key}/manifest.json')
    async def manifest(key):
        auth(key)
        catalogs = [{'type': 'movie', 'id': 'telegram', 'name': 'Telegram Videos', 'extra': [{'name': 'search', 'isRequired': False}, {'name': 'skip', 'isRequired': False}]}]
        catalogs.append({'type': 'series', 'id': 'telegram-series', 'name': 'Telegram Series',
                         'extra': [{'name': 'search', 'isRequired': False}, {'name': 'skip', 'isRequired': False}]})
        if app.state.cfg.ai_search_enabled:
            for kind, name in (('movie', 'Movies'), ('series', 'Series')):
                catalogs.append({'type': kind, 'id': 'telegram-ai-' + kind,
                                 'name': 'Telegram AI ' + name,
                                 'extra': [{'name': 'search', 'isRequired': True},
                                           {'name': 'skip', 'isRequired': False}]})
        return {'id': 'community.private.telegram', 'version': get_version(), 'name': 'Private Telegram Videos',
                'description': 'Stream your private Telegram videos',
                'logo': 'https://raw.githubusercontent.com/HaCStream/stremio-addon/main/stremio_addon/icon.png',
                'types': ['movie', 'series'],
                'resources': [{'name': 'catalog', 'types': ['movie', 'series']},
                              {'name': 'meta', 'types': ['movie'], 'idPrefixes': ['tg:']},
                              {'name': 'meta', 'types': ['series'], 'idPrefixes': ['tg:', 'tt']},
                              {'name': 'stream', 'types': ['movie', 'series'], 'idPrefixes': ['tg:', 'tt']}],
                'catalogs': catalogs}

    @app.get('/{key}/catalog/{kind}/{catalog_id}.json')
    @app.get('/{key}/catalog/{kind}/{catalog_id}/{extras}.json')
    async def catalog(key, kind, catalog_id, request: Request, extras=''):
        auth(key)
        args = parse_qs(extras)
        query = args.get('search', [''])[0][:500]
        interaction(request, 'catalog_search' if query else 'catalog_browse', query=query,
                    type=kind, catalog=catalog_id, result_count=0)
        ai_catalog = kind in ('movie', 'series') and catalog_id == 'telegram-ai-' + kind
        if not ai_catalog and (kind, catalog_id) not in (('movie', 'telegram'), ('series', 'telegram-series')):
            return {'metas': []}
        try:
            skip = int(args.get('skip', ['0'])[0])
            if skip < 0 or skip > 10_000_000:
                raise ValueError()
        except ValueError:
            raise HTTPException(400, 'Invalid skip') from None
        request.state.interaction['skip'] = skip
        if ai_catalog:
            clean = ai_query(query, app.state.cfg.require_ai_suffix_for_ai_search)
            if not app.state.cfg.ai_search_enabled or not clean:
                rows = []
            else:
                try:
                    rows = await app.state.runtime.ai.search(clean, set(app.state.tg.channels), skip, kind)
                    request.state.interaction['ai_status'] = 'ok'
                except Exception as exc:
                    request.state.interaction['ai_status'] = type(exc).__name__
                    rows = []
        else:
            rows = app.state.store.grouped_catalog(kind, app.state.tg.channels, query, skip)
        results(request, rows)
        return {'metas': [meta(r, kind) for r in rows]}

    @app.get('/{key}/meta/{kind}/{item}.json')
    async def detail(key, kind, item, request: Request):
        auth(key)
        interaction(request, 'meta_lookup', type=kind, item=item)
        rows = []
        identifier = None
        if kind == 'series' and re.fullmatch(r'tg:series:[0-9a-f]{24}', item):
            rows = app.state.store.series(item, app.state.tg.channels)
            identifier = item
        elif kind == 'series' and re.fullmatch(r'tt\d{7,10}', item):
            rows = await app.state.metadata.match(app.state.store, kind, item)
            rows = [r for r in rows if r['channel'] in app.state.tg.channels]
            identifier = item
        else:
            row = app.state.store.get(item)
            if kind in ('movie', 'series') and row and row['channel'] in app.state.tg.channels:
                rows = [row]
                if kind == 'series' and row['series_id']:
                    identifier = row['series_id']
                    rows = app.state.store.series(identifier, app.state.tg.channels)
        results(request, rows)
        return {'meta': meta(rows[0], kind, rows if identifier else None, identifier) if rows else None}

    @app.get('/{key}/stream/{kind}/{item}.json')
    async def sources(key, kind, item, request: Request):
        auth(key)
        interaction(request, 'stream_lookup', type=kind, item=item,
                    match_mode='telegram_id' if item.startswith('tg:') else 'metadata', result_count=0)
        if kind not in ('movie', 'series'):
            return {'streams': []}
        episode = re.fullmatch(r'(tg:series:[0-9a-f]{24}):(\d+):(\d+)', item)
        if kind == 'series' and episode:
            rows = app.state.store.series(episode[1], app.state.tg.channels, int(episode[2]), int(episode[3]))
        elif item.startswith('tg:'):
            row = app.state.store.get(item)
            rows = [row] if row else []
        else:
            rows = await app.state.metadata.match(app.state.store, kind, item)
        rows = [r for r in rows if r['channel'] in app.state.tg.channels]
        results(request, rows)
        return {'streams': [{'name': 'Telegram ' + r['quality'], 'title': f"{r['title']}\n{r['channel_name']} · {r['size'] / 1024**3:.2f} GB", 'url': url(r, 'play'), 'behaviorHints': {'notWebReady': True}} for r in rows if r['channel'] in app.state.tg.channels]}

    @app.get('/{key}/status')
    async def status(key):
        auth(key)
        return {**app.state.tg.status, 'connected': app.state.tg.client.is_connected(), 'channels': len(app.state.tg.channels), 'videos': app.state.store.db.execute('SELECT count(*) FROM videos').fetchone()[0], 'checkpoints': [dict(r) for r in app.state.store.db.execute('SELECT * FROM checkpoints')]}

    class Mapping(BaseModel):
        imdb: str

    @app.put('/{key}/mapping/{item}')
    async def mapping(key, item, body: Mapping):
        auth(key)
        if not app.state.store.get(item) or not re.fullmatch(r'tt\d{7,10}', body.imdb):
            raise HTTPException(400, 'Expected indexed Telegram ID and IMDb ID')
        with app.state.store.db:
            app.state.store.db.execute('INSERT OR REPLACE INTO mappings VALUES (?,?)', (item, body.imdb))
        return {'ok': True}

    @app.api_route('/play/{token}', methods=['GET', 'HEAD'])
    async def play(token, request: Request):
        row = token_row(token, 'play')
        try:
            message = await app.state.tg.message(row)
        except FileNotFoundError:
            raise HTTPException(404) from None
        row = app.state.store.get(row['id'])
        size = message.document.size
        try:
            start, end, code = byte_range(request.headers.get('range'), size)
        except ValueError:
            return Response(status_code=416, headers={'Content-Range': f'bytes */{size}', 'Accept-Ranges': 'bytes'})
        headers = {'Accept-Ranges': 'bytes', 'Content-Length': str(max(0, end - start + 1))}
        if code == 206:
            headers['Content-Range'] = f'bytes {start}-{end}/{size}'
        if request.method == 'HEAD' or size == 0:
            return Response(status_code=code, headers=headers, media_type=row['mime'])
        iterator = app.state.tg.stream(row, message, start, end)
        # Fetch first chunk before sending success headers, allowing a clean upstream error.
        try:
            first = await anext(iterator)
        except Exception:
            await iterator.aclose()
            raise HTTPException(502, 'Telegram download unavailable') from None
        async def body():
            try:
                yield first
                async for chunk in iterator:
                    yield chunk
            finally:
                await iterator.aclose()
        return StreamingResponse(body(), status_code=code, headers=headers, media_type=row['mime'])

    @app.get('/thumb/{token}')
    async def thumb(token):
        row = token_row(token, 'thumb')
        try:
            message = await app.state.tg.message(row)
            data = await app.state.tg.client.download_media(message, file=bytes, thumb=-1)
            if data:
                return Response(data, media_type='image/jpeg')
        except FileNotFoundError:
            raise HTTPException(404) from None
        except Exception:
            pass
        return Response('<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360"><rect width="640" height="360" fill="#182936"/><path d="M280 120v120l100-60z" fill="#64b5f6"/></svg>', media_type='image/svg+xml')

    return app

app = create_app()

