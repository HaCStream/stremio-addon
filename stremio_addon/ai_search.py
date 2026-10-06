"""Online Gemini discovery matched against the ordinary Telegram index."""
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import time

import httpx

GENERATE_MODEL = 'gemini-3.8-flash'
API = 'https://generativelanguage.googleapis.com/v1beta/models/'
MIN_REQUEST_INTERVAL = 2


class GeminiCooldown(httpx.HTTPError):
    """A prior 429 has paused all calls for this installation."""


def ai_query(text, prefix_required):
    """Return the clean query, or None when the optional prefix is missing."""
    text = text.strip()
    words = text.lower().split(maxsplit=1)
    prefixed = bool(words and words[0] == 'ai')
    if prefix_required and not prefixed:
        return None
    return text[2:].strip() if prefixed else text


class AISearch:
    def __init__(self, config, store, http=None):
        self.config = config
        self.store = store
        self.http = http or httpx.AsyncClient(timeout=30)
        self.owns_http = http is None
        self.cache = {}
        self.search_lock = asyncio.Lock()
        self.request_lock = asyncio.Lock()
        self.last_request = 0
        saved = store.db.execute('SELECT retry_at,strikes FROM ai_rate_limit WHERE id=1').fetchone()
        self.retry_at = saved['retry_at'] if saved else 0
        self.strikes = saved['strikes'] if saved else 0

    def cooldown_remaining(self):
        return max(0, math.ceil(self.retry_at - time.time()))

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

    async def close(self):
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
                self._save_cooldown()
                raise GeminiCooldown('Gemini rate limited')
            response.raise_for_status()
            if self.strikes:
                self.strikes = 0
                self.retry_at = 0
                self._save_cooldown()
            return response.json()

    async def discover(self, query):
        prompt = ('Search online for a movie or a serie that matches this description: '
                  + json.dumps(query, ensure_ascii=False)
                  + '. Give five results for each with only the titles in a json format: '
                  + '{"movies": [], "series": []}')
        schema = {'type': 'object', 'properties': {
            name: {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 5}
            for name in ('movies', 'series')}, 'required': ['movies', 'series']}
        data = await self.request(GENERATE_MODEL, 'generateContent', {
            'contents': [{'parts': [{'text': prompt}]}],
            'tools': [{'google_search': {}}],
            'generationConfig': {'responseMimeType': 'application/json',
                                 'responseJsonSchema': schema, 'temperature': 0.1,
                                 'maxOutputTokens': 2048,
                                 'thinkingConfig': {'thinkingLevel': 'low'}},
        })
        parts = data['candidates'][0]['content']['parts']
        answer = json.loads(''.join(p.get('text', '') for p in parts if not p.get('thought')))
        if not isinstance(answer, dict):
            raise ValueError('Expected movie and series title arrays')
        titles = {}
        for name in ('movies', 'series'):
            values = answer.get(name)
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise ValueError('Expected movie and series title arrays')
            titles[name] = list(dict.fromkeys(v.strip() for v in values if v.strip()))[:5]
        return titles

    async def titles(self, query):
        # Share discovery between catalogs and concurrent debug requests. Cache
        # titles only: availability is always checked against the current index.
        key = query.casefold()
        async with self.search_lock:
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < 300:
                return cached[1]
            titles = await self.discover(query)
            if len(self.cache) >= 100:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (time.monotonic(), titles)
            return titles

    async def search(self, query, allowed, skip=0, kind='movie'):
        if kind not in ('movie', 'series'):
            raise ValueError('Unsupported AI catalog type')
        if not query.strip():
            return []
        titles = await self.titles(query)
        rows = {}
        for title in titles['movies' if kind == 'movie' else 'series']:
            # FTS retrieves candidates; normalized title equality prevents a
            # caption mention or similarly named sequel from becoming a result.
            for row in self.store.title_matches(title, allowed):
                rows.setdefault(row['id'], row)
        return list(rows.values())[skip:skip + 100]
