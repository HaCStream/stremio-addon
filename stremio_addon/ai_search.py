"""Private, incremental Gemini-assisted retrieval over locally indexed items."""
import asyncio
from array import array
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import heapq
import json
import math
import re
import time

import httpx

from .core import normalize

EMBED_MODEL = 'gemini-embedding-2'
GENERATE_MODEL = 'gemini-3.8-flash'
DIMENSIONS = 768
API = 'https://generativelanguage.googleapis.com/v1beta/models/'
MIN_REQUEST_INTERVAL = 2


class GeminiCooldown(httpx.HTTPError):
    """A prior 429 has paused all calls for this installation."""


def ai_query(text, prefix_required):
    """Return the clean query, or None when the optional prefix is missing."""
    text = text.strip()
    match = re.match(r'^ai(?:\s+|$)', text, re.I)
    if prefix_required and not match:
        return None
    return text[match.end():].strip() if match else text


class AISearch:
    def __init__(self, config, store, http=None):
        self.config = config
        self.store = store
        self.http = http or httpx.AsyncClient(timeout=10)
        self.owns_http = http is None
        self.task = None
        self.cache = {}
        self.retries = {}
        self.request_lock = asyncio.Lock()
        self.last_request = 0
        saved = store.db.execute('SELECT retry_at,strikes FROM ai_rate_limit WHERE id=1').fetchone()
        self.retry_at = saved['retry_at'] if saved else 0
        self.strikes = saved['strikes'] if saved else 0
        self.status = {'embedded': 0, 'pending': 0, 'last_error': None}

    def cooldown_remaining(self):
        return max(0, math.ceil(self.retry_at - time.time()))

    def snapshot(self):
        return {**self.status, 'retry_after_seconds': self.cooldown_remaining()}

    def _save_cooldown(self):
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO ai_rate_limit VALUES (1,?,?)',
                                  (self.retry_at, self.strikes))

    def _retry_delay(self, response):
        delay = min(3600, 60 * 2 ** min(self.strikes, 6))
        header = response.headers.get('retry-after', '')
        try:
            delay = max(delay, float(header))
        except ValueError:
            try:
                date = parsedate_to_datetime(header)
                delay = max(delay, (date - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
        try:
            details = response.json().get('error', {}).get('details', [])
            for detail in details:
                retry = detail.get('retryDelay', '')
                if isinstance(retry, str) and retry.endswith('s'):
                    delay = max(delay, float(retry[:-1]))
        except (ValueError, TypeError, AttributeError):
            pass
        return min(3600, max(60, delay))

    async def start(self):
        self.task = asyncio.create_task(self.index_loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.owns_http:
            await self.http.aclose()

    async def request(self, model, action, body):
        async with self.request_lock:
            if self.cooldown_remaining():
                raise GeminiCooldown('Gemini rate limit cooldown active')
            await asyncio.sleep(max(0, MIN_REQUEST_INTERVAL - (time.monotonic() - self.last_request)))
            response = await self.http.post(API + model + ':' + action,
                headers={'x-goog-api-key': self.config.gemini_api_key}, json=body)
            self.last_request = time.monotonic()
            if response.status_code == 429:
                delay = self._retry_delay(response)
                self.strikes += 1
                self.retry_at = time.time() + delay
                self.status['last_error'] = 'Gemini rate limited'
                self._save_cooldown()
                raise GeminiCooldown('Gemini rate limited')
            response.raise_for_status()
            if self.strikes:
                self.strikes = 0
                self.retry_at = 0
                self._save_cooldown()
            return response.json()

    async def generate(self, prompt):
        data = await self.request(GENERATE_MODEL, 'generateContent', {
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0.1,
                                 'maxOutputTokens': 1024, 'thinkingConfig': {'thinkingLevel': 'low'}},
        })
        parts = data['candidates'][0]['content']['parts']
        return json.loads(''.join(p.get('text', '') for p in parts))

    async def embed(self, text, task, title=None):
        if task == 'RETRIEVAL_QUERY':
            text = 'task: search result | query: ' + text
        elif task == 'RETRIEVAL_DOCUMENT':
            text = 'title: ' + (title[:200] if title else 'none') + ' | text: ' + text
        else:
            raise ValueError('Unsupported embedding task')
        body = {'model': 'models/' + EMBED_MODEL,
                'content': {'parts': [{'text': text}]},
                'outputDimensionality': DIMENSIONS}
        data = await self.request(EMBED_MODEL, 'embedContent', body)
        values = data['embedding']['values']
        if len(values) != DIMENSIONS:
            raise ValueError('Unexpected embedding dimensions')
        magnitude = math.sqrt(sum(x * x for x in values))
        if not magnitude:
            raise ValueError('Empty embedding')
        return array('f', (x / magnitude for x in values))

    def fingerprint(self, row):
        text = '\n'.join(str(row.get(k) or '') for k in ('title', 'filename', 'caption'))
        return hashlib.sha256(text.encode()).hexdigest()

    async def description(self, row):
        # Avoid making an ungrounded plot guess for unclear filenames/titles.
        if len((row['caption'] or '').strip()) >= 80:
            return ''
        title = (row['title'] or '').strip()
        if len(title) < 4:
            return ''
        key = normalize(title) + ':' + str(row['year'] or '')
        cached = self.store.db.execute('SELECT description FROM ai_descriptions WHERE title_key=?', (key,)).fetchone()
        if cached:
            return cached['description']
        answer = await self.generate(
            'Identify this title from your knowledge. Respond with JSON containing only '
            '{"description":"..."}. If the title/year is ambiguous or unknown, use an empty '
            'description. Otherwise write at most 50 words of accurate plot premise and themes. '
            'Do not infer a story from the filename. Title: ' + json.dumps(title[:200], ensure_ascii=False) +
            '; Year: ' + str(row['year'] or 'unknown'))
        description = str(answer.get('description') or '')[:400] if isinstance(answer, dict) else ''
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO ai_descriptions VALUES (?,?)', (key, description))
        return description

    async def index_row(self, row):
        fingerprint = self.fingerprint(row)
        current = self.store.db.execute('SELECT fingerprint,model FROM ai_embeddings WHERE id=?', (row['id'],)).fetchone()
        if current and current['fingerprint'] == fingerprint and current['model'] == EMBED_MODEL:
            return False
        description = await self.description(row)
        document = '\n'.join(filter(None, [row['title'], row['filename'],
                                            (row['caption'] or '')[:1200], description]))
        vector = await self.embed(document[:7000], 'RETRIEVAL_DOCUMENT', row['title'])
        # An edit/deletion while Gemini was responding must not resurrect stale text.
        latest = self.store.get(row['id'])
        if not latest or self.fingerprint(latest) != fingerprint:
            return False
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO ai_embeddings VALUES (?,?,?,?)',
                                  (row['id'], fingerprint, EMBED_MODEL, vector.tobytes()))
        self.cache.clear()
        return True

    async def index_loop(self):
        while True:
            try:
                if self.cooldown_remaining():
                    await asyncio.sleep(min(60, self.cooldown_remaining()))
                    continue
                pending = []
                rows = self.store.db.execute('''SELECT v.* FROM videos v LEFT JOIN ai_embeddings a ON a.id=v.id
                    ORDER BY v.id''').fetchall()
                for raw in rows:
                    row = dict(raw)
                    current = self.store.db.execute('SELECT fingerprint,model FROM ai_embeddings WHERE id=?', (row['id'],)).fetchone()
                    if not current or current['fingerprint'] != self.fingerprint(row) or current['model'] != EMBED_MODEL:
                        pending.append(row)
                self.status.update(embedded=len(rows) - len(pending), pending=len(pending))
                if not pending and not self.cooldown_remaining():
                    self.status['last_error'] = None
                processed = 0
                for row in pending:
                    if processed >= 20:
                        break
                    if self.retries.get(row['id'], 0) > time.monotonic():
                        continue
                    processed += 1
                    try:
                        await self.index_row(row)
                        self.retries.pop(row['id'], None)
                    except GeminiCooldown:
                        break
                    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                        self.status['last_error'] = type(exc).__name__
                        self.retries[row['id']] = time.monotonic() + 60
                    await asyncio.sleep(.15)
                await asyncio.sleep(min(60, self.cooldown_remaining()) if self.cooldown_remaining()
                                    else 2 if pending else 15)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never log API response bodies or private indexed text.
                self.status['last_error'] = type(exc).__name__
                await asyncio.sleep(30)

    def similar(self, vector, allowed, limit=60):
        best = []
        for raw in self.store.db.execute('''SELECT v.id,v.channel,a.vector FROM ai_embeddings a
                                            JOIN videos v ON v.id=a.id WHERE a.model=?''', (EMBED_MODEL,)):
            if raw['channel'] not in allowed:
                continue
            candidate = array('f')
            candidate.frombytes(raw['vector'])
            if len(candidate) != DIMENSIONS:
                continue
            score = sum(a * b for a, b in zip(vector, candidate))
            if len(best) < limit:
                heapq.heappush(best, (score, raw['id']))
            elif score > best[0][0]:
                heapq.heapreplace(best, (score, raw['id']))
        return sorted(best, reverse=True)

    async def search(self, query, allowed, skip=0):
        key = (query.casefold(), tuple(sorted(allowed)))
        cached = self.cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            ids = cached[1]
        else:
            vector = await self.embed(query, 'RETRIEVAL_QUERY')
            scores = self.similar(vector, allowed)
            ids = [item for _, item in scores]
            # Title suggestions can bridge a description-only query and a bare title.
            try:
                suggestions = await self.generate('Give up to five likely, real title names that match this request. '
                    'Return JSON {"titles":["..."]}; use [] if unsure. Request: ' + json.dumps(query[:500], ensure_ascii=False))
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                suggestions = {}
            titles = suggestions.get('titles', []) if isinstance(suggestions, dict) else []
            title_ids = []
            for title in titles[:5] if isinstance(titles, list) else []:
                if not isinstance(title, str) or not title.strip():
                    continue
                for row in self.store.catalog(title[:100], limit=20):
                    if row['channel'] in allowed and normalize(row['title']) == normalize(title):
                        title_ids.append(row['id'])
            for row in self.store.catalog(query, limit=60):
                if row['channel'] in allowed:
                    title_ids.append(row['id'])
            ids = list(dict.fromkeys(title_ids + ids))
            candidates = [self.store.get(item) for item in ids[:50]]
            candidates = [r for r in candidates if r and r['channel'] in allowed]
            if candidates:
                compact = []
                for row in candidates:
                    title_key = normalize(row['title']) + ':' + str(row['year'] or '')
                    cached_description = self.store.db.execute(
                        'SELECT description FROM ai_descriptions WHERE title_key=?', (title_key,)).fetchone()
                    compact.append({'id': row['id'], 'title': row['title'],
                                    'caption': (row['caption'] or '')[:240],
                                    'description': cached_description['description'] if cached_description else ''})
                try:
                    answer = await self.generate('Rank the relevant indexed entries for this request. Return JSON '
                        '{"ids":["..."]} in best-first order. Only choose IDs from these candidates; '
                        'omit irrelevant entries. Request: ' + json.dumps(query[:500], ensure_ascii=False) +
                        ' Candidates: ' + json.dumps(compact, ensure_ascii=False))
                    selected = answer.get('ids', []) if isinstance(answer, dict) else []
                    valid = {r['id'] for r in candidates}
                    ids = list(dict.fromkeys(item for item in selected if isinstance(item, str) and item in valid))
                except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                    ids = [r['id'] for r in candidates]
            else:
                ids = []
            self.cache[key] = (time.monotonic(), ids)
        return [row for item in ids[skip:skip + 100]
                if (row := self.store.get(item)) and row['channel'] in allowed]
