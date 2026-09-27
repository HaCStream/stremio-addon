import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def normalize(text):
    text = unicodedata.normalize('NFKD', text).casefold()
    text = ''.join(c for c in text if unicodedata.category(c) not in ('Mn', 'Cf'))
    text = re.sub(r"['\"׳״’‘]", '', text)
    return ' '.join(re.sub(r'[^\w]+', ' ', text, flags=re.UNICODE).replace('_', ' ').split())


def parse_title(filename, caption):
    raw = re.sub(r'\.(mp4|mkv|avi|mov|webm|m4v|ts)$', '', filename, flags=re.I) or next(iter(caption.splitlines()), 'Telegram video')
    combined = raw + ' ' + caption
    ep = re.search(r'\bS(\d{1,2})[ ._-]*E(\d{1,3})\b', combined, re.I) or re.search(r'עונה\s*(\d+)\s*[,.:/\- ]*פרק\s*(\d+)', combined)
    year = re.search(r'\b(19\d{2}|20\d{2})\b', combined)
    imdb = re.search(r'\btt\d{7,10}\b', combined)
    quality = re.search(r'\b(2160p|1080p|720p|480p|4k)\b', combined, re.I)
    title = re.split(r'\b(?:19\d{2}|20\d{2}|S\d{1,2}E\d{1,3}|2160p|1080p|720p|480p|WEB[ .-]?DL|BluRay)\b|עונה\s*\d+', raw, maxsplit=1, flags=re.I)[0]
    title = re.sub(r'[._]+', ' ', title).strip(' -[]()') or raw
    return dict(title=title, year=int(year[0]) if year else None,
                season=int(ep[1]) if ep else None, episode=int(ep[2]) if ep else None,
                imdb=imdb[0] if imdb else None, quality=quality[0] if quality else '')


@dataclass
class Settings:
    port: int
    url: str
    key: str
    api_id: int
    api_hash: str
    session: str
    data: Path
    cache_bytes: int = 512 * 1024 * 1024
    channel_ids: frozenset[int] | None = None
    debug_port: int = 8001
    debug_host: str = '0.0.0.0'
    debug_enabled: bool = True
    ai_search_enabled: bool = False
    gemini_api_key: str = ''
    ai_search_prefix_enabled: bool = False

    @classmethod
    def env(cls):
        options = {}
        options_path = Path('/data/options.json')
        if options_path.is_file():
            try:
                options = json.loads(options_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(
                    'Cannot read Home Assistant options from /data/options.json'
                ) from exc
        if not isinstance(options, dict):
            raise ValueError(
                'Home Assistant options in /data/options.json must be a JSON object'
            )

        for group_name in ('debug', 'ai'):
            group = options.get(group_name)
            if isinstance(group, dict):
                options.update(group)

        def get(name, default=None):
            # Normal container environment values take precedence. Home Assistant
            # stores add-on configuration in /data/options.json, so use it as a
            # direct fallback even when the generic image is pulled by Supervisor.
            value = os.getenv(name)
            if value is None or not str(value).strip():
                value = options.get(name, options.get(name.lower(), default))
            if value is None or not str(value).strip():
                raise ValueError(f'Missing environment variable: {name}')
            return str(value)

        url = get('ADDON_URL').rstrip('/')
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('ADDON_URL must be an HTTP(S) base URL without credentials, query or fragment')
        key = get('API_KEY')
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,}', key):
            raise ValueError('API_KEY needs at least 32 URL-safe letters, digits, underscores or hyphens')
        default_data = '/data/stremio' if options_path.is_file() else '/data'
        raw_ids = os.getenv('CHANNEL_IDS')
        if raw_ids is None:
            raw_ids = options.get('CHANNEL_IDS', options.get('channel_ids', ''))
        channel_ids = None
        if not isinstance(raw_ids, str):
            raise ValueError('CHANNEL_IDS must be a comma-separated string of negative channel IDs')
        if raw_ids.strip():
            parts = [part.strip() for part in raw_ids.split(',')]
            if any(not re.fullmatch(r'-[1-9][0-9]*', part) for part in parts):
                raise ValueError('CHANNEL_IDS must contain only comma-separated negative channel IDs')
            channel_ids = frozenset(int(part) for part in parts)
        port = 8000
        debug_port = 8001
        debug_host = get('DEBUG_HOST', '0.0.0.0')
        raw_debug_enabled = get('DEBUG_ENABLED', 'true').lower()
        if raw_debug_enabled not in ('1', 'true', 'yes', 'on', '0', 'false', 'no', 'off'):
            raise ValueError('DEBUG_ENABLED must be true or false')
        debug_enabled = raw_debug_enabled in ('1', 'true', 'yes', 'on')
        def boolean(name):
            value = get(name, 'false').lower()
            if value not in ('1', 'true', 'yes', 'on', '0', 'false', 'no', 'off'):
                raise ValueError(f'{name} must be true or false')
            return value in ('1', 'true', 'yes', 'on')
        ai_enabled = boolean('AI_SEARCH_ENABLED')
        ai_prefix = boolean('AI_SEARCH_PREFIX_ENABLED')
        gemini_key = get('GEMINI_API_KEY') if ai_enabled else ''
        if ai_enabled and not gemini_key:
            raise ValueError('GEMINI_API_KEY is required when AI_SEARCH_ENABLED is true')
        return cls(port, url, key, int(get('API_ID')), get('API_HASH'), get('USER_SESSION_STRING'),
                   Path(get('DATA_DIR', default_data)), int(get('CACHE_MB', '512')) * 1024**2,
                   channel_ids, debug_port, debug_host, debug_enabled, ai_enabled, gemini_key, ai_prefix)


class Tokens:
    def __init__(self, key):
        self.key = key.encode()

    def sign(self, item, scope, ttl=86400):
        payload = base64.urlsafe_b64encode(json.dumps([item, scope, int(time.time()) + ttl], separators=(',', ':')).encode()).decode().rstrip('=')
        return payload + '.' + hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()

    def verify(self, token, scope):
        try:
            payload, signature = token.split('.')
            expected = hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            item, purpose, expires = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
            if purpose != scope or expires <= time.time() or not re.fullmatch(r'tg:-?\d+:\d+', item):
                raise ValueError()
            return item
        except Exception:
            raise ValueError('Invalid or expired token') from None


def byte_range(value, size):
    if not value:
        return 0, size - 1, 200
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', value.strip())
    if not match or not any(match.groups()) or size <= 0:
        raise ValueError('Unsatisfiable range')
    left, right = match.groups()
    if not left:
        length = int(right)
        if length <= 0:
            raise ValueError('Unsatisfiable range')
        return max(0, size - length), size - 1, 206
    start, end = int(left), min(int(right), size - 1) if right else size - 1
    if start >= size or end < start:
        raise ValueError('Unsatisfiable range')
    return start, end, 206


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS videos(id TEXT PRIMARY KEY, channel INTEGER, message INTEGER, title TEXT,
          filename TEXT, caption TEXT, channel_name TEXT, size INTEGER, mime TEXT, date INTEGER,
          year INTEGER, season INTEGER, episode INTEGER, imdb TEXT, quality TEXT, file_id TEXT, search TEXT);
        CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(id UNINDEXED, text, tokenize='unicode61');
        CREATE TABLE IF NOT EXISTS checkpoints(channel INTEGER PRIMARY KEY, oldest INTEGER, newest INTEGER, complete INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS mappings(id TEXT PRIMARY KEY, imdb TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_embeddings(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
          model TEXT NOT NULL, vector BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_descriptions(title_key TEXT PRIMARY KEY,
          description TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_rate_limit(id INTEGER PRIMARY KEY CHECK (id=1),
          retry_at REAL NOT NULL, strikes INTEGER NOT NULL);
        ''')

    def upsert(self, row):
        row = dict(row, search=normalize(' '.join(str(row.get(k) or '') for k in ('title', 'filename', 'caption'))))
        with self.db:
            previous = self.db.execute('SELECT title,filename,caption FROM videos WHERE id=?', (row['id'],)).fetchone()
            if previous and any((previous[k] or '') != (row.get(k) or '') for k in ('title', 'filename', 'caption')):
                self.db.execute('DELETE FROM ai_embeddings WHERE id=?', (row['id'],))
            self.db.execute('INSERT OR REPLACE INTO videos (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', list(row.values()))
            self.db.execute('DELETE FROM search WHERE id=?', (row['id'],))
            self.db.execute('INSERT INTO search VALUES (?,?)', (row['id'], row['search']))

    def delete(self, item):
        with self.db:
            self.db.execute('DELETE FROM videos WHERE id=?', (item,))
            self.db.execute('DELETE FROM search WHERE id=?', (item,))
            self.db.execute('DELETE FROM ai_embeddings WHERE id=?', (item,))

    def get(self, item):
        row = self.db.execute('SELECT * FROM videos WHERE id=?', (item,)).fetchone()
        return dict(row) if row else None

    def catalog(self, query='', skip=0, limit=100):
        terms = normalize(query).split()[:30]
        if terms:
            expression = ' AND '.join('"' + t + '"*' for t in terms)
            rows = self.db.execute('SELECT v.* FROM videos v JOIN search s ON s.id=v.id WHERE s.text MATCH ? ORDER BY v.date DESC,v.id LIMIT ? OFFSET ?', (expression, limit, skip))
        else:
            rows = self.db.execute('SELECT * FROM videos ORDER BY date DESC,id LIMIT ? OFFSET ?', (limit, skip))
        return [dict(r) for r in rows]

    def explicit(self, imdb):
        return [dict(r) for r in self.db.execute('SELECT v.*, COALESCE(m.imdb,v.imdb) AS mapped FROM videos v LEFT JOIN mappings m ON m.id=v.id WHERE COALESCE(m.imdb,v.imdb)=?', (imdb,))]

    def checkpoint(self, channel):
        row = self.db.execute('SELECT * FROM checkpoints WHERE channel=?', (channel,)).fetchone()
        return dict(row) if row else dict(channel=channel, oldest=0, newest=0, complete=0)

    def save_checkpoint(self, p):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO checkpoints VALUES (:channel,:oldest,:newest,:complete)', p)
