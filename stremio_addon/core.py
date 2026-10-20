import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from contextlib import contextmanager
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


def search_text(row):
    text = normalize(' '.join(str(row.get(k) or '') for k in ('title', 'filename', 'caption')))
    # Make the displayed title searchable even when release filenames split a
    # word with dots/spaces (Ha.Shminia -> hashminia). Keep caption tokens too.
    aliases = title_aliases(row)
    joined = [normalize(alias).replace(' ', '') for alias in aliases]
    return ' '.join([text, *joined])


def search_expression(query):
    terms = normalize(query).split()[:30]
    if not terms:
        return None
    expression = ' AND '.join('"' + t + '"*' for t in terms)
    if len(terms) > 1:
        expression = '(' + expression + ') OR "' + ''.join(terms) + '"*'
    return expression


EPISODE_PATTERN = r'(?<![A-Za-z0-9])S(\d{1,2})[ ._-]*E(\d{1,3})(?!\d)|עונה[\s._-]*(\d+)[\s,.:/_-]*פרק[\s._-]*(\d+)'


def series_id(title):
    return 'tg:series:' + hashlib.sha256(normalize(title).encode()).hexdigest()[:24]


def title_from_text(text):
    raw = re.sub(r'\.(mp4|mkv|avi|mov|webm|m4v|ts)$', '', text.strip(), flags=re.I)
    raw = re.sub(r'[._]+', ' ', raw)
    title = re.split(EPISODE_PATTERN, raw, maxsplit=1, flags=re.I)[0]
    title = re.split(r'\b(?:19\d{2}|20\d{2}|2160p|1080p|720p|480p|WEB[ .-]?DL|BluRay)\b', title, maxsplit=1, flags=re.I)[0]
    # Remove decorative symbols and invisible direction marks at the edges.
    title = title.strip(' -[]()')
    while title and not title[0].isalnum():
        title = title[1:]
    while title and not title[-1].isalnum():
        title = title[:-1]
    return title.strip()


def caption_title(caption):
    line = next((line.strip() for line in caption.splitlines() if line.strip()), '')
    # Links, channel handles and ID-only captions are metadata, not titles.
    if re.search(r'https?://|t\.me/|@\w+', line, re.I):
        return ''
    title = title_from_text(line)
    if not any(c.isalpha() for c in title) or re.fullmatch(r'tt\d{7,10}', title, re.I):
        return ''
    return title


def title_aliases(row):
    aliases = re.split(r'[/|\n]', row.get('title') or '')
    filename_title = title_from_text(row.get('filename') or '')
    if filename_title:
        aliases.extend(re.split(r'[/|\n]', filename_title))
    return aliases


def source_name(entity, fallback=''):
    if isinstance(entity, str):
        return entity
    if getattr(entity, 'is_self', False):
        return 'Saved Messages'
    return (getattr(entity, 'title', None)
            or ' '.join(x for x in (getattr(entity, 'first_name', None),
                                    getattr(entity, 'last_name', None)) if x)
            or str(fallback))


def movie_title_score(row, aliases, year):
    """Accept strong missing-year/subtitle matches without confusing known remakes."""
    names = {normalize(t) for t in title_aliases(row) if t}
    score = 0
    for alias in aliases:
        full = normalize(alias)
        if full and full in names:
            score = max(score, 100)
        # A subtitle may be absent in releases, but a single common word is too weak.
        base = normalize(re.split(r':|\s+[-–—]\s+', alias, maxsplit=1)[0])
        for name in names:
            if len(base.split()) >= 2 and (name == base or name.startswith(full + ' ')):
                score = max(score, 85)
    if year is not None and row.get('year') is not None:
        score += 20 if row['year'] == year else -60
    return score


def parse_title(filename, caption, source_title=''):
    raw = re.sub(r'\.(mp4|mkv|avi|mov|webm|m4v|ts)$', '', filename, flags=re.I) or next(iter(caption.splitlines()), 'Telegram video')
    # Underscores are regex word characters. Normalize filename separators
    # before matching so S5E21_480P and S5E21.480P parse identically.
    raw = re.sub(r'[._]+', ' ', raw)
    combined = raw + ' ' + caption
    ep = re.search(EPISODE_PATTERN, combined, re.I)
    numbers = [int(n) for n in ep.groups() if n is not None] if ep else []
    year = re.search(r'\b(19\d{2}|20\d{2})\b', combined)
    imdb = re.search(r'\btt\d{7,10}\b', combined)
    quality = re.search(r'\b(2160p|1080p|720p|480p|4k)\b', combined, re.I)
    title = caption_title(caption) or title_from_text(raw)
    # Episode-only messages inherit the group/channel name, never a private contact name.
    if not title and ep and source_title:
        title = title_from_text(source_title)
    title = title or raw
    return dict(title=title, year=int(year[0]) if year else None,
                season=numbers[0] if ep else None, episode=numbers[1] if ep else None,
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
    skip_debug_auth: bool = False
    require_ai_suffix_for_ai_search: bool = True
    tmdb_api_key: str = ''
    display_collection: bool = False
    debug_level: str = "INFO"
    dev_ui_password: str = ''

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

        for group_name in ('developer_ui', 'ai', 'tmdb'):
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
        key = get('ADDON_ACCESS_KEY')
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,}', key):
            raise ValueError('ADDON_ACCESS_KEY needs at least 32 URL-safe letters, digits, underscores or hyphens')
        default_data = '/data/stremio' if options_path.is_file() else '/data'
        raw_ids = os.getenv('CHANNEL_IDS')
        if raw_ids is None:
            raw_ids = options.get('CHANNEL_IDS', options.get('channel_ids', ''))
        channel_ids = None
        if not isinstance(raw_ids, str):
            raise ValueError('CHANNEL_IDS must be a comma-separated string of Telegram chat IDs')
        if raw_ids.strip():
            parts = [part.strip() for part in raw_ids.split(',')]
            if any(not re.fullmatch(r'-?[1-9][0-9]*', part) for part in parts):
                raise ValueError('CHANNEL_IDS must contain only comma-separated nonzero Telegram chat IDs')
            channel_ids = frozenset(int(part) for part in parts)
        port = 8000
        debug_port = 8001
        debug_host = get('DEV_UI_HOST', '0.0.0.0')
        raw_debug_enabled = get('DEV_UI_ENABLED', 'true').lower()
        if raw_debug_enabled not in ('1', 'true', 'yes', 'on', '0', 'false', 'no', 'off'):
            raise ValueError('DEV_UI_ENABLED must be true or false')
        debug_enabled = raw_debug_enabled in ('1', 'true', 'yes', 'on')
        def boolean(name, default='false'):
            value = get(name, default).lower()
            if value not in ('1', 'true', 'yes', 'on', '0', 'false', 'no', 'off'):
                raise ValueError(f'{name} must be true or false')
            return value in ('1', 'true', 'yes', 'on')
        ai_enabled = boolean('AI_SEARCH_ENABLED')
        ai_suffix = boolean('REQUIRE_AI_SUFFIX_FOR_AI_SEARCH', 'true')
        skip_debug_auth = boolean('SKIP_DEV_UI_AUTH')
        gemini_key = get('GEMINI_API_KEY') if ai_enabled else ''
        if ai_enabled and not gemini_key:
            raise ValueError('GEMINI_API_KEY is required when AI_SEARCH_ENABLED is true')
        debug_level = get('DEBUG_LEVEL', 'INFO').upper()
        if debug_level not in ('NOTSET', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
            raise ValueError('DEBUG_LEVEL must be NOTSET, DEBUG, INFO, WARNING, ERROR or CRITICAL')
        return cls(port, url, key, int(get('TELEGRAM_APP_API_ID')), get('TELEGRAM_APP_API_HASH'), get('USER_SESSION_STRING'),
                   Path(get('DATA_DIR', default_data)), int(get('CACHE_MB', '512')) * 1024**2,
                   channel_ids, debug_port, debug_host, debug_enabled, ai_enabled, gemini_key,
                   skip_debug_auth, ai_suffix,
                   str(os.getenv('TMDB_API_KEY') or options.get('TMDB_API_KEY') or '').strip(),
                   display_collection=boolean('DISPLAY_COLLECTION'), debug_level=debug_level,
                   dev_ui_password=str(os.getenv('DEV_UI_PASSWORD') or options.get('DEV_UI_PASSWORD') or '').strip())


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
        self.path = path
        self._local = threading.local()
        self._connections = []
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='index-db')
        self._closed = False
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS videos(id TEXT PRIMARY KEY, channel INTEGER, message INTEGER, title TEXT,
          filename TEXT, caption TEXT, channel_name TEXT, size INTEGER, mime TEXT, date INTEGER,
          year INTEGER, season INTEGER, episode INTEGER, imdb TEXT, quality TEXT, file_id TEXT, search TEXT);
        CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(id UNINDEXED, text, tokenize='unicode61');
        CREATE TABLE IF NOT EXISTS checkpoints(channel INTEGER PRIMARY KEY, oldest INTEGER, newest INTEGER, complete INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS series_search_pages(
          series TEXT, query TEXT, channel INTEGER, cursor TEXT NOT NULL,
          complete INTEGER NOT NULL, updated_at REAL NOT NULL, head_checked_at REAL NOT NULL,
          PRIMARY KEY(series,query,channel));
        CREATE TABLE IF NOT EXISTS mappings(id TEXT PRIMARY KEY, imdb TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tmdb_series(series_id TEXT PRIMARY KEY,
          tmdb INTEGER, imdb TEXT, name TEXT, status TEXT NOT NULL,
          candidates TEXT NOT NULL DEFAULT '[]', checked_at REAL NOT NULL,
          season_offset INTEGER NOT NULL DEFAULT 0, episode_offset INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS tmdb_series_imdb ON tmdb_series(imdb);
        CREATE INDEX IF NOT EXISTS tmdb_series_tmdb ON tmdb_series(tmdb);
        CREATE TABLE IF NOT EXISTS ai_rate_limit(id INTEGER PRIMARY KEY CHECK (id=1),
          retry_at REAL NOT NULL, strikes INTEGER NOT NULL);
        ''')
        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(videos)')}
        if 'series_id' not in columns:
            with self.db:
                self.db.execute('ALTER TABLE videos ADD COLUMN series_id TEXT')
        self.db.execute('CREATE INDEX IF NOT EXISTS videos_channel ON videos(channel)')
        self.db.execute('CREATE INDEX IF NOT EXISTS videos_message ON videos(message,channel)')
        self.db.execute('CREATE INDEX IF NOT EXISTS videos_series ON videos(series_id,season,episode)')
        self.db.commit()
        rebuilt_search = self.db.execute('PRAGMA user_version').fetchone()[0] < 3
        if rebuilt_search:
            # Prefer saved captions and rebuild grouping/search without fetching
            # Telegram history again. Also repair legacy malformed episode titles.
            with self.db:
                self.db.execute('DELETE FROM search')
                for saved in self.db.execute('SELECT * FROM videos').fetchall():
                    row = dict(saved)
                    parsed = parse_title(row['filename'] or '', row['caption'] or '')
                    if parsed['season'] is not None or caption_title(row['caption'] or ''):
                        row.update(parsed)
                    row['series_id'] = series_id(row['title']) if row['season'] is not None and row['episode'] is not None else None
                    row['search'] = search_text(row)
                    self.db.execute('UPDATE videos SET title=?,year=?,season=?,episode=?,imdb=?,quality=?,series_id=?,search=? WHERE id=?',
                                    [row[k] for k in ('title', 'year', 'season', 'episode', 'imdb', 'quality', 'series_id', 'search', 'id')])
                    self.db.execute('INSERT INTO search VALUES (?,?)', (row['id'], row['search']))
                self.db.execute('PRAGMA user_version=3')

        # FTS5's UNINDEXED id cannot support point updates. Keep a small B-tree
        # mapping to the existing FTS rowids; no full text rebuild is needed.
        if rebuilt_search or not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='search_rows'").fetchone():
            with self.db:
                self.db.execute('BEGIN')
                self.db.execute('CREATE TABLE IF NOT EXISTS search_rows(id TEXT PRIMARY KEY, fts_rowid INTEGER NOT NULL UNIQUE)')
                self.db.execute('DELETE FROM search_rows')
                self.db.execute('INSERT INTO search_rows SELECT id,rowid FROM search')

        if self.db.execute('PRAGMA user_version').fetchone()[0] < 4:
            with self.transaction():
                for saved in self.db.execute(
                        "SELECT * FROM videos WHERE season IS NOT NULL AND "
                        "(title GLOB '[sS][0-9]*' OR title LIKE 'עונה%' OR title='Telegram video')"):
                    row = dict(saved)
                    # Previously saved identities remain authoritative.
                    if self.db.execute("SELECT 1 FROM tmdb_series WHERE series_id=? AND status IN ('auto','manual','blocked')",
                                       (row['series_id'],)).fetchone():
                        continue
                    if row['channel'] < 0:
                        parsed = parse_title(row['filename'] or '', row['caption'] or '',
                                             row['channel_name'] or '')
                        if parsed['title'] != row['title']:
                            row.update(parsed)
                            row.pop('search', None)
                            row.pop('series_id', None)
                            self.upsert(row)
                self.db.execute('PRAGMA user_version=4')

    @property
    def db(self):
        # Worker transactions never share a connection with HTTP handlers.
        # WAL lets the main connection keep reading while indexing commits.
        if self._closed:
            raise RuntimeError('Store is closed')
        if not hasattr(self._local, 'db'):
            db = sqlite3.connect(self.path, check_same_thread=False)
            db.row_factory = sqlite3.Row
            self._local.db = db
            self._connections.append(db)
        return self._local.db

    async def run(self, function, *args, **kwargs):
        """Serialize background SQL on one worker, including whole transactions."""
        future = asyncio.get_running_loop().run_in_executor(
            self._worker, partial(function, *args, **kwargs))
        # A cancelled caller must not close connections while its SQL is running.
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await asyncio.shield(future)
            raise

    async def close(self):
        await asyncio.to_thread(self._worker.shutdown, wait=True)
        self._closed = True
        for db in self._connections:
            db.close()

    def _delete_search(self, item):
        self.db.execute('DELETE FROM search WHERE rowid=(SELECT fts_rowid FROM search_rows WHERE id=?)', (item,))
        self.db.execute('DELETE FROM search_rows WHERE id=?', (item,))

    @contextmanager
    def transaction(self):
        """Join a page transaction without committing its individual messages."""
        if self.db.in_transaction:
            yield
        else:
            with self.db:
                self.db.execute('BEGIN')
                yield

    def upsert(self, row):
        row = dict(row, series_id=series_id(row['title'])
                   if row.get('season') is not None and row.get('episode') is not None else None)
        row = dict(row, search=search_text(row))
        previous = self.get(row['id'])
        if previous and all(previous.get(k) == v for k, v in row.items()):
            return
        with self.transaction():
            self.db.execute('INSERT OR REPLACE INTO videos (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', list(row.values()))
            self._delete_search(row['id'])
            cursor = self.db.execute('INSERT INTO search VALUES (?,?)', (row['id'], row['search']))
            self.db.execute('INSERT INTO search_rows VALUES (?,?)', (row['id'], cursor.lastrowid))

    def delete(self, item):
        with self.transaction():
            self.db.execute('DELETE FROM videos WHERE id=?', (item,))
            self._delete_search(item)

    def stored_channels(self):
        # Seek to each distinct channel using the index, rather than walking
        # every video entry (SELECT DISTINCT still scans the covering index).
        channels = set()
        channel = self.db.execute('SELECT MIN(channel) FROM videos').fetchone()[0]
        while channel is not None:
            channels.add(channel)
            channel = self.db.execute(
                'SELECT MIN(channel) FROM videos WHERE channel>?', (channel,)).fetchone()[0]
        channels.update(r[0] for r in self.db.execute('SELECT channel FROM checkpoints'))
        return channels

    async def prune_channels(self, channels, batch_size=500, wait_background=None):
        """Remove inaccessible channels in bounded transactions, yielding between batches."""
        if not channels:
            return
        params = tuple(channels)

        def batch(channel):
            rows = self.db.execute('SELECT id FROM videos WHERE channel=? LIMIT ?',
                                   (channel, batch_size)).fetchall()
            with self.db:
                for row in rows:
                    self._delete_search(row['id'])
                    self.db.execute('DELETE FROM videos WHERE id=?', (row['id'],))
            return bool(rows)

        def checkpoint(channel):
            with self.db:
                self.db.execute('DELETE FROM checkpoints WHERE channel=?', (channel,))
                self.db.execute('DELETE FROM series_search_pages WHERE channel=?', (channel,))

        for channel in params:
            while True:
                if wait_background:
                    await wait_background()
                if not await self.run(batch, channel):
                    break
                await asyncio.sleep(0)
            if wait_background:
                await wait_background()
            await self.run(checkpoint, channel)

    def get(self, item):
        row = self.db.execute('SELECT * FROM videos WHERE id=?', (item,)).fetchone()
        return dict(row) if row else None

    def catalog(self, query='', skip=0, limit=100):
        expression = search_expression(query)
        if expression:
            rows = self.db.execute('SELECT v.* FROM videos v JOIN search s ON s.id=v.id WHERE s.text MATCH ? ORDER BY v.date DESC,v.id LIMIT ? OFFSET ?', (expression, limit, skip))
        else:
            rows = self.db.execute('SELECT * FROM videos ORDER BY date DESC,id LIMIT ? OFFSET ?', (limit, skip))
        return [dict(r) for r in rows]

    def grouped_catalog(self, kind, allowed, query='', skip=0, limit=100):
        if not allowed:
            return []
        expression = search_expression(query)
        join = ' JOIN search s ON s.id=v.id' if expression else ''
        where = 'v.channel IN (' + ','.join('?' for _ in allowed) + ')'
        params = list(sorted(allowed))
        where += ' AND v.series_id IS ' + ('NOT NULL' if kind == 'series' else 'NULL')
        if expression:
            where += ' AND s.text MATCH ?'
            params.append(expression)
        # Group before pagination; hundreds of episodes still count as one show.
        rows = self.db.execute(
            'SELECT * FROM (SELECT v.*, ROW_NUMBER() OVER ('
            'PARTITION BY COALESCE(v.series_id,v.id) ORDER BY v.date DESC,v.id) AS position '
            'FROM videos v' + join + ' WHERE ' + where + ') WHERE position=1 '
            'ORDER BY date DESC,id LIMIT ? OFFSET ?', (*params, limit, skip))
        return [dict(r) for r in rows]

    def series(self, identifier, allowed, season=None, episode=None):
        if not allowed:
            return []
        where = 'series_id=? AND channel IN (' + ','.join('?' for _ in allowed) + ')'
        params = [identifier, *sorted(allowed)]
        if season is not None and episode is not None:
            where += ' AND season=? AND episode=?'
            params.extend((season, episode))
        return [dict(r) for r in self.db.execute(
            'SELECT * FROM videos WHERE ' + where + ' ORDER BY season,episode,date DESC,id', params)]

    def title_matches(self, title, allowed):
        expression = search_expression(title)
        if not expression or not allowed:
            return []
        placeholders = ','.join('?' for _ in allowed)
        rows = self.db.execute(
            'SELECT v.* FROM videos v JOIN search s ON s.id=v.id '
            'WHERE s.text MATCH ? AND v.channel IN (' + placeholders + ') '
            'ORDER BY v.date DESC,v.id', (expression, *sorted(allowed)))
        return [dict(row) for row in rows
                if normalize(title) in {normalize(alias) for alias in title_aliases(dict(row))}]

    def movie_candidates(self, aliases, allowed):
        if not allowed:
            return []
        found = {}
        for alias in aliases:
            base = re.split(r':|\s+[-–—]\s+', alias, maxsplit=1)[0]
            expression = search_expression(base)
            if not expression:
                continue
            rows = self.db.execute(
                'SELECT v.* FROM videos v JOIN search s ON s.id=v.id '
                'WHERE s.text MATCH ? AND v.series_id IS NULL AND v.channel IN ('
                + ','.join('?' for _ in allowed) + ') ORDER BY v.date DESC LIMIT 500',
                (expression, *sorted(allowed)))
            found.update((r['id'], dict(r)) for r in rows)
        return list(found.values())

    def explicit(self, imdb):
        return [dict(r) for r in self.db.execute('SELECT v.*, COALESCE(m.imdb,v.imdb) AS mapped FROM videos v LEFT JOIN mappings m ON m.id=v.id WHERE COALESCE(m.imdb,v.imdb)=?', (imdb,))]

    def checkpoint(self, channel):
        row = self.db.execute('SELECT * FROM checkpoints WHERE channel=?', (channel,)).fetchone()
        return dict(row) if row else dict(channel=channel, oldest=0, newest=0, complete=0)

    def save_checkpoint(self, p):
        with self.transaction():
            self.db.execute('INSERT OR REPLACE INTO checkpoints VALUES (:channel,:oldest,:newest,:complete)', p)

    def series_page(self, series, query, channel):
        row = self.db.execute('SELECT * FROM series_search_pages WHERE series=? AND query=? AND channel=?',
                              (series, query, channel or 0)).fetchone()
        return dict(row) if row else None

    def save_series_page(self, series, query, channel, cursor, complete, updated_at, head_checked_at):
        self.db.execute('INSERT OR REPLACE INTO series_search_pages VALUES (?,?,?,?,?,?,?)',
                        (series, query, channel or 0, json.dumps(cursor), int(complete), updated_at, head_checked_at))
