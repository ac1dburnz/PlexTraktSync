"""Saved Trakt feeds for Radarr and Sonarr; read-only access to Trakt."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

import requests
from click import ClickException
from flask import Flask, jsonify, request, send_file

from plextraktsync.trakt.BrowserTokenAuth import BrowserTokenAuth

PERIODS = ['daily', 'weekly', 'monthly', 'yearly', 'all']
FILTERS = ['years', 'genres', 'languages', 'countries', 'ratings', 'certifications', 'runtimes']
SOURCES = {
    'favorited': {'label': 'Most favorited by period (current Trakt API)', 'period': True, 'filters': True},
    'recommended': {'label': 'Recommended by period (legacy Trakt endpoint)', 'period': True, 'filters': True},
    'recommendations': {'label': 'Personal recommendations', 'recommendations': True, 'filters': True},
    'social_recommendations': {'label': 'Social recommendations', 'recommendations': True},
    'trending': {'label': 'Trending', 'filters': True},
    'popular': {'label': 'Popular', 'filters': True},
    'anticipated': {'label': 'Anticipated', 'filters': True},
    'watched': {'label': 'Most watched by period', 'period': True, 'filters': True},
    'played': {'label': 'Most played by period', 'period': True, 'filters': True},
    'collected': {'label': 'Most collected by period', 'period': True, 'filters': True},
    'boxoffice': {'label': 'Box office (movies only)', 'movies_only': True},
    'watchlist': {'label': 'User watchlist', 'user': True},
    'user_watched': {'label': 'User watched titles', 'user': True},
    'collection': {'label': 'User collection', 'user': True},
    'list': {'label': 'Named Trakt list', 'user': True, 'list': True},
    'related': {'label': 'Related to a movie or show', 'seed': True},
}
SLUG = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$')


class BridgeError(Exception):
    def __init__(self, message, status=502):
        self.message, self.status = message, status


def validate_feed(raw):
    if not isinstance(raw, dict):
        raise BridgeError('Feed must be an object.', 400)
    allowed = {'name', 'media', 'source', 'period', 'limit', 'user', 'list', 'seed', 'filters',
               'ignore_collected', 'ignore_watchlisted', 'watch_window'}
    if set(raw) - allowed:
        raise BridgeError('Unknown feed option.', 400)
    feed = dict(raw)
    for field in ('name', 'media', 'source'):
        if not isinstance(feed.get(field), str):
            raise BridgeError(f'Missing {field}.', 400)
    if not SLUG.fullmatch(feed['name']) or feed['media'] not in ('movies', 'shows') or feed['source'] not in SOURCES:
        raise BridgeError('Invalid name, media type or source.', 400)
    spec = SOURCES[feed['source']]
    if spec.get('movies_only') and feed['media'] != 'movies':
        raise BridgeError('This source supports movies only.', 400)
    limit = feed.setdefault('limit', 100)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise BridgeError('Limit must be an integer from 1 to 1000.', 400)
    if spec.get('recommendations') and limit > 100:
        raise BridgeError('Recommendation feeds support at most 100 results.', 400)
    if spec.get('period'):
        feed.setdefault('period', 'weekly')
        if feed['period'] not in PERIODS:
            raise BridgeError('Invalid period.', 400)
    elif 'period' in feed:
        raise BridgeError('Period is not supported for this source.', 400)
    for field in ('user', 'list', 'seed'):
        if spec.get(field):
            if field == 'user':
                feed.setdefault(field, 'me')
            value = feed.get(field)
            if not isinstance(value, str) or not SLUG.fullmatch(value):
                raise BridgeError(f'Invalid {field}. Use its Trakt URL slug or numeric ID.', 400)
        elif field in feed:
            raise BridgeError(f'{field} is not supported for this source.', 400)
    filters = feed.setdefault('filters', {})
    if not isinstance(filters, dict) or set(filters) - set(FILTERS) or (filters and not spec.get('filters')):
        raise BridgeError('Unsupported filters for this source.', 400)
    for value in filters.values():
        if not isinstance(value, str) or len(value) > 160 or not re.fullmatch(r'[a-zA-Z0-9, ._-]+', value):
            raise BridgeError('Invalid filter value.', 400)
    for field in ('ignore_collected', 'ignore_watchlisted', 'watch_window'):
        if field in feed:
            if not spec.get('recommendations'):
                raise BridgeError(f'{field} is only supported for recommendation feeds.', 400)
            value = feed[field]
            if field == 'watch_window':
                if type(value) is not int or not 1 <= value <= 3650:
                    raise BridgeError('Watch window must be 1–3650 days.', 400)
            elif type(value) is not bool:
                raise BridgeError(f'{field} must be a boolean.', 400)
    return feed


def upstream(feed):
    source, media = feed['source'], feed['media']
    params = dict(feed['filters'])
    if source in ('recommendations', 'social_recommendations'):
        path = f'/{source}/{media}'
        for key in ('ignore_collected', 'ignore_watchlisted', 'watch_window'):
            if key in feed:
                params[key] = str(feed[key]).lower()
    elif SOURCES[source].get('period'):
        path = f'/{media}/{source}/{feed["period"]}'
    elif SOURCES[source].get('user'):
        user = quote(feed['user'], safe='')
        if source == 'list':
            path = f'/users/{user}/lists/{quote(feed["list"], safe="")}/items/{media}'
        else:
            path = f'/users/{user}/{"watched" if source == "user_watched" else source}/{media}'
    elif source == 'related':
        path = f'/{media}/{quote(feed["seed"], safe="")}/related'
    else:
        path = f'/{media}/{source}'
    return path, params


def convert(items, media):
    out, seen = [], set()
    kind, id_key = ('movie', 'tmdb') if media == 'movies' else ('show', 'tvdb')
    for entry in items:
        if not isinstance(entry, dict):
            raise BridgeError('Trakt returned malformed list data.')
        item = entry.get(kind, entry)
        if not isinstance(item, dict):
            raise BridgeError('Trakt returned malformed title data.')
        ids = item.get('ids') or {}
        if not isinstance(ids, dict):
            raise BridgeError('Trakt returned malformed identifiers.')
        identifier = ids.get(id_key)
        if type(identifier) is not int or identifier <= 0 or identifier in seen:
            continue
        seen.add(identifier)
        title = item.get('title')
        if not isinstance(title, str):
            title = str(identifier)
        out.append({'id': identifier, 'title': title} if media == 'movies' else {'tvdbId': identifier, 'title': title})
    return out


class FeedStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            if not self.path.exists():
                return {}
            try:
                data = json.loads(self.path.read_text())
                if not isinstance(data, dict):
                    raise TypeError()
                if any(not isinstance(feed, dict) or name != feed.get('name') for name, feed in data.items()):
                    raise ValueError()
                return {name: validate_feed(feed) for name, feed in data.items()}
            except (ValueError, TypeError, OSError, AttributeError, BridgeError):
                raise BridgeError('Saved feeds cannot be read. Check the configuration file.', 503) from None

    def save(self, feed):
        with self.lock:
            data = self.read()
            data[feed['name']] = feed
            if len(data) > 100:
                raise BridgeError('At most 100 saved feeds are supported.', 400)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            try:
                with tmp.open('w') as handle:
                    os.chmod(tmp, 0o600)
                    json.dump(data, handle, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                tmp.replace(self.path)
            except OSError:
                raise BridgeError('Could not save feeds.', 503) from None


class TraktFeeds:
    def __init__(self, token_file, get=requests.get, ttl=3600, clock=time.monotonic):
        self.auth = BrowserTokenAuth(token_file)
        self.get, self.ttl, self.clock = get, ttl, clock
        self.lock = threading.Lock()
        self.cache = {}
        self.retry_at = 0

    def fetch(self, feed):
        # Lock coalesces requests and bounds upstream concurrency across feeds.
        with self.lock:
            try:
                token = self.auth.read()
            except ClickException:
                raise BridgeError('Trakt login required. Check browser authentication and Slack status.', 503) from None
            fingerprint = hashlib.sha256((token['access_token'] + ':' + token['client_id']).encode()).hexdigest()
            key = (fingerprint, json.dumps(feed, sort_keys=True))
            now = self.clock()
            cached = self.cache.get(key)
            if cached and now - cached[0] < self.ttl:
                return cached[1]
            if now < self.retry_at:
                raise BridgeError('Trakt rate limit reached. Please retry later.', 503)
            path, params = upstream(feed)
            params['limit'] = min(feed['limit'], 100)
            items = []
            deadline = now + 60
            for page in range(1, 11):
                if self.clock() >= deadline:
                    raise BridgeError('Trakt list fetch timed out. No partial list was published.', 503)
                params['page'] = page
                try:
                    response = self.get('https://api.trakt.tv' + path, params=dict(params), auth=self.auth,
                                        headers={'trakt-api-version': '2', 'User-Agent': 'PlexTraktSync', 'Accept': 'application/json'},
                                        timeout=15, allow_redirects=False)
                    if response.status_code == 429:
                        try:
                            wait = max(1, min(3600, int(response.headers.get('Retry-After', '60'))))
                        except ValueError:
                            wait = 60
                        self.retry_at = self.clock() + wait
                        raise BridgeError('Trakt rate limit reached. Please retry later.', 503)
                    if response.status_code != 200:
                        hint = ''
                        if feed['source'] == 'recommended':
                            hint = ' Try the separate Most favorited source; the legacy endpoint may be unavailable.'
                        raise BridgeError(f'Trakt returned HTTP {response.status_code}.{hint}')
                    data = response.json()
                    if not isinstance(data, list):
                        raise BridgeError('Trakt did not return a list.')
                    items.extend(data)
                    result = convert(items, feed['media'])
                    pages = int(response.headers.get('X-Pagination-Page-Count', '1'))
                except ClickException:
                    raise BridgeError('Trakt authentication failed or redirected. Check browser authentication.', 503) from None
                except (requests.RequestException, ValueError, TypeError):
                    raise BridgeError('Could not fetch valid Trakt data. No partial list was published.', 502) from None
                if len(result) >= feed['limit'] or page >= pages or not data:
                    break
            else:
                raise BridgeError('Trakt pagination exceeded the 10-page limit. Reduce the feed scope.', 502)
            if items and not result:
                raise BridgeError('No usable TMDB/TVDB identifiers were returned; refusing to publish an empty feed.')
            result = result[:feed['limit']]
            if len(self.cache) >= 100:
                self.cache.clear()
            self.cache[key] = (self.clock(), result)
            return result


def create_app(config_dir, token_file, secret, get=requests.get, ttl=3600):
    if not secret or len(secret) < 24 or not secret.isascii():
        raise ValueError('LIST_BRIDGE_SECRET must contain at least 24 ASCII characters.')
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 16384
    store = FeedStore(Path(config_dir) / 'list-feeds.json')
    client = TraktFeeds(token_file, get=get, ttl=ttl)

    @app.before_request
    def authenticate():
        if request.path in ('/', '/healthz'):
            return
        supplied = request.headers.get('X-Bridge-Key', '')
        if request.method == 'GET' and not supplied:
            supplied = request.args.get('key', '')
        if not hmac.compare_digest(supplied.encode(), secret.encode()):
            raise BridgeError('Invalid bridge key.', 403)

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
        )
        return response

    @app.errorhandler(BridgeError)
    def error(exc):
        return jsonify(error=exc.message), exc.status

    @app.get('/')
    def index():
        return send_file(Path(__file__).with_name('list_bridge.html'))

    @app.get('/healthz')
    def health():
        return jsonify(ok=True, service='list-bridge')

    @app.get('/api/catalog')
    def catalog():
        return jsonify(sources=SOURCES, periods=PERIODS, filters=FILTERS)

    @app.get('/api/feeds')
    def feeds():
        return jsonify(store.read())

    @app.post('/api/feeds')
    def save():
        feed = validate_feed(request.get_json(silent=True))
        store.save(feed)
        return jsonify(feed), 201

    @app.post('/api/preview')
    def preview():
        feed = validate_feed(request.get_json(silent=True))
        return jsonify(client.fetch(feed))

    @app.get('/<target>/<name>.json')
    def output(target, name):
        if target not in ('radarr', 'sonarr'):
            raise BridgeError('Unknown importer.', 404)
        feed = store.read().get(name)
        if not feed or feed['media'] != ('movies' if target == 'radarr' else 'shows'):
            raise BridgeError('Feed not found for this importer.', 404)
        return jsonify(client.fetch(feed))

    return app


if __name__ == '__main__':
    from waitress import serve

    app = create_app(os.environ.get('PTS_CONFIG_DIR', '/app/config'),
                     os.environ.get('TRAKT_BROWSER_TOKEN_FILE', '/app/config/browser-token.json'),
                     os.environ.get('LIST_BRIDGE_SECRET', ''), ttl=int(os.environ.get('LIST_BRIDGE_CACHE_SECONDS', '3600')))
    # Waitress does not emit request access logs containing the feed query secret.
    serve(app, host='0.0.0.0', port=int(os.environ.get('LIST_BRIDGE_PORT', '8090')), threads=4)
