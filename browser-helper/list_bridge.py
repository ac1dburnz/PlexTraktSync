"""Saved Trakt feeds for Radarr and Sonarr; read-only access to Trakt."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from click import ClickException
from flask import Flask, jsonify, request, send_file

from plextraktsync.trakt.ArrImport import ArrImport, PreviewStore
from plextraktsync.trakt.BrowserTokenAuth import BrowserTokenAuth
from plextraktsync.trakt.ListPreview import cards
from plextraktsync.trakt.ListSources import (
    FILTERS,
    OPTIONS,
    PERIODS,
    SLUG,
    SOURCES,
    WATCHNOW,
    BridgeError,
    normalize,
    requests_for,
    select_items,
    validate_feed,
)
from plextraktsync.trakt.PosterLookup import PosterLookup


def convert(items, media):
    out, seen = [], set()
    kind, id_key = ("movie", "tmdb") if media == "movies" else ("show", "tvdb")
    for entry in items:
        if not isinstance(entry, dict):
            raise BridgeError("Trakt returned malformed list data.")
        item = entry.get(kind, entry)
        if not isinstance(item, dict):
            raise BridgeError("Trakt returned malformed title data.")
        ids = item.get("ids") or {}
        if not isinstance(ids, dict):
            raise BridgeError("Trakt returned malformed identifiers.")
        identifier = ids.get(id_key)
        if type(identifier) is not int or identifier <= 0 or identifier in seen:
            continue
        seen.add(identifier)
        title = item.get("title")
        if not isinstance(title, str):
            title = str(identifier)
        out.append({"id": identifier, "title": title} if media == "movies" else {"tvdbId": identifier, "title": title})
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
                if any(not isinstance(feed, dict) or name != feed.get("name") for name, feed in data.items()):
                    raise ValueError()
                return {name: validate_feed(feed) for name, feed in data.items()}
            except (ValueError, TypeError, OSError, AttributeError, BridgeError):
                raise BridgeError("Saved feeds cannot be read. Check the configuration file.", 503) from None

    def save(self, feed):
        with self.lock:
            data = self.read()
            data[feed["name"]] = feed
            if len(data) > 100:
                raise BridgeError("At most 100 saved feeds are supported.", 400)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            try:
                with tmp.open("w") as handle:
                    os.chmod(tmp, 0o600)
                    json.dump(data, handle, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                tmp.replace(self.path)
            except OSError:
                raise BridgeError("Could not save feeds.", 503) from None


class TraktFeeds:
    def __init__(self, token_file, get=requests.get, ttl=3600, clock=time.monotonic):
        self.auth = BrowserTokenAuth(token_file)
        self.get, self.ttl, self.clock = get, ttl, clock
        self.lock = threading.Lock()
        self.cache = {}
        self.retry_at = 0

    def fingerprint(self):
        try:
            token = self.auth.read()
        except ClickException:
            raise BridgeError("Trakt login required. Check browser authentication and Slack status.", 503) from None
        return hashlib.sha256((token["access_token"] + ":" + token["client_id"]).encode()).hexdigest()

    def request(self, path, params, deadline):
        if self.clock() < self.retry_at:
            raise BridgeError("Trakt rate limit reached. Please retry later.", 503)
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise BridgeError("Trakt fetch timed out. No partial list was published.", 503)
        try:
            response = self.get(
                "https://api.trakt.tv" + path,
                params=params,
                auth=self.auth,
                headers={"trakt-api-version": "2", "User-Agent": "PlexTraktSync", "Accept": "application/json"},
                timeout=min(15, remaining),
                allow_redirects=False,
            )
            if response.status_code == 429:
                try:
                    wait = max(1, min(3600, int(response.headers.get("Retry-After", "60"))))
                except ValueError:
                    wait = 60
                self.retry_at = self.clock() + wait
                raise BridgeError("Trakt rate limit reached. Please retry later.", 503)
            if response.status_code != 200:
                hint = " Legacy Recommended may be unavailable; Most favorited is a separate source." if "/recommended/" in path else ""
                raise BridgeError(f"Trakt returned HTTP {response.status_code}.{hint}")
            data = response.json()
            pages = int(response.headers.get("X-Pagination-Page-Count", "1"))
            pages = max(pages, 1)  # Some empty responses advertise zero pages.
            if self.clock() >= deadline:
                raise BridgeError("Trakt fetch timed out. No partial list was published.", 503)
            return data, pages
        except ClickException:
            raise BridgeError("Trakt authentication failed or redirected. Check browser authentication.", 503) from None
        except (requests.RequestException, ValueError, TypeError):
            raise BridgeError("Could not fetch valid Trakt data. No partial list was published.", 502) from None

    def fetch(self, feed, detailed=False):
        feed = validate_feed(feed)
        with self.lock:
            today = datetime.now(timezone.utc).date()
            # Date included so rolling windows cannot return yesterday's cached scope.
            key = (self.fingerprint(), today.isoformat(), json.dumps(feed, sort_keys=True))
            now = self.clock()
            cached = self.cache.get(key)
            if cached and now - cached[0] < self.ttl:
                return cached[1] if detailed else convert(cached[1], feed["media"])
            spec = SOURCES[feed["source"]]
            items = []
            deadline = now + 60
            exhaustive = spec.get("calendar") or feed.get("order", "upstream") != "upstream"
            calls = 0
            for path, params in requests_for(feed, today):
                for page in range(1, 11):
                    if calls >= 10:
                        raise BridgeError("Trakt fetch exceeded 10 requests. Narrow the window or filters; no partial list was published.")
                    calls += 1
                    # Fetch full pages before local network filtering, deduplication or sorting.
                    data, pages = self.request(path, {**params, "limit": 100, "page": page}, deadline)
                    batch = normalize(data, feed)
                    items.extend(batch)
                    result = convert(select_items(items, feed), feed["media"])
                    if spec.get("single") or page >= pages:
                        break
                    if not exhaustive and len(result) >= feed["limit"]:
                        break
                    if not data:
                        raise BridgeError("Trakt ended pagination unexpectedly; no partial list was published.")
                else:
                    raise BridgeError("Trakt pagination exceeded 10 pages. Narrow the feed scope; no partial list was published.")
            if items and not convert(items, feed["media"]):
                raise BridgeError("No usable TMDB/TVDB identifiers were returned; refusing to publish an empty feed.")
            result = cards(select_items(items, feed), feed["media"], feed["limit"])
            if len(self.cache) >= 100:
                self.cache.clear()
            self.cache[key] = (self.clock(), result)
            return result if detailed else convert(result, feed["media"])

    def browse(self, kind, user="me", query="", page=1, media="shows"):
        if not isinstance(user, str) or not SLUG.fullmatch(user) or media not in ("movies", "shows"):
            raise BridgeError("Invalid browse user or media.", 400)
        if type(page) is not int or not 1 <= page <= 1000 or len(query) > 160:
            raise BridgeError("Invalid browse query or page.", 400)
        paths = {
            "networks": "/networks",
            "genres": f"/genres/{media}",
            "languages": f"/languages/{media}",
            "countries": f"/countries/{media}",
            "certifications": f"/certifications/{media}",
            "my_lists": f"/users/{quote(user, safe='')}/lists",
            "smart_lists": f"/users/{quote(user, safe='')}/smart-lists",
            "liked_lists": "/users/likes/lists",
            "popular_lists": "/lists/popular",
            "trending_lists": "/lists/trending",
            "search_lists": "/search/list",
            "people": "/search/person",
        }
        if kind not in paths:
            raise BridgeError("Unknown discovery type.", 400)
        if kind in ("search_lists", "people") and not query.strip():
            raise BridgeError("Enter search text.", 400)
        with self.lock:
            key = ("browse", self.fingerprint(), kind, user, query, page, media)
            cached = self.cache.get(key)
            if cached and self.clock() - cached[0] < self.ttl:
                return cached[1]
            if kind == "networks":
                catalog_key = ("network_catalog", self.fingerprint())
                catalog = self.cache.get(catalog_key)
                if catalog and self.clock() - catalog[0] < self.ttl:
                    networks = catalog[1]
                else:
                    networks = []
                    deadline = self.clock() + 60
                    current, total = 1, 1
                    while current <= total:
                        batch, total = self.request("/networks", {"page": current, "limit": 1000}, deadline)
                        if not isinstance(batch, list) or total > 10:
                            raise BridgeError("Network catalog is too large or malformed; no partial catalog was returned.")
                        for entry in batch:
                            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                                raise BridgeError("Trakt returned malformed network metadata.")
                            name = entry["name"].strip()
                            if name:
                                networks.append({"name": name, **({"country": entry["country"]} if isinstance(entry.get("country"), str) else {})})
                        current += 1
                    networks.sort(key=lambda entry: entry["name"].casefold())
                    if len(self.cache) >= 100:
                        self.cache.clear()
                    self.cache[catalog_key] = (self.clock(), networks)
                matches = [entry for entry in networks if query.strip().casefold() in entry["name"].casefold()]
                return {"items": matches[(page - 1) * 20:page * 20], "page": page, "pages": max(1, (len(matches) + 19) // 20), "total": len(matches)}
            data, pages = self.request(paths[kind], {"page": page, "limit": 20, **({"query": query} if query else {})}, self.clock() + 30)
            if not isinstance(data, list):
                # Certifications are grouped by country.
                if kind == "certifications" and isinstance(data, dict) and all(isinstance(v, list) for v in data.values()):
                    data = [dict(entry, country=country) for country, entries in data.items() for entry in entries if isinstance(entry, dict)]
                else:
                    raise BridgeError("Trakt returned malformed discovery results.")
            out = []
            for entry in data:
                if not isinstance(entry, dict):
                    raise BridgeError("Trakt returned malformed discovery results.")
                item = entry.get("list") or entry.get("person") or entry
                if not isinstance(item, dict):
                    raise BridgeError("Trakt returned malformed discovery results.")
                ids = item.get("ids") or {}
                if not isinstance(ids, dict):
                    raise BridgeError("Trakt returned malformed discovery identifiers.")
                # Return only display metadata needed to select a feed, not full profiles.
                out.append(
                    {
                        k: v
                        for k, v in {
                            "name": item.get("name"),
                            "id": ids.get("trakt"),
                            "slug": ids.get("slug") or item.get("slug"),
                            "code": item.get("code"),
                            "country": item.get("country"),
                            "media_type": item.get("media_type"),
                        }.items()
                        if isinstance(v, (str, int)) and not isinstance(v, bool)
                    }
                )
            result = {"items": out, "page": page, "pages": pages}
            if len(self.cache) >= 100:
                self.cache.clear()
            self.cache[key] = (self.clock(), result)
            return result


def create_app(config_dir, token_file, secret, get=requests.get, ttl=3600, arr=None, posters=None):
    if not secret or len(secret) < 24 or not secret.isascii():
        raise ValueError("LIST_BRIDGE_SECRET must contain at least 24 ASCII characters.")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16384
    store = FeedStore(Path(config_dir) / "list-feeds.json")
    client = TraktFeeds(token_file, get=get, ttl=ttl)
    importers = arr if arr is not None else ArrImport()
    previews = PreviewStore()
    artwork = posters if posters is not None else PosterLookup()

    @app.before_request
    def authenticate():
        if request.path in ("/", "/healthz", "/preview.js"):
            return
        supplied = request.headers.get("X-Bridge-Key", "")
        if request.method == "GET" and not supplied:
            supplied = request.args.get("key", "")
        if not hmac.compare_digest(supplied.encode(), secret.encode()):
            raise BridgeError("Invalid bridge key.", 403)

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' https://trakt.tv https://*.trakt.tv https://image.tmdb.org; frame-ancestors 'none'"
        )
        return response

    @app.errorhandler(BridgeError)
    def error(exc):
        return jsonify(error=exc.message), exc.status

    @app.get("/")
    def index():
        return send_file(Path(__file__).with_name("list_bridge.html"))

    @app.get("/preview.js")
    def preview_script():
        return send_file(Path(__file__).with_name("preview.js"))

    @app.get("/api/importers")
    def available_importers():
        return jsonify(importers.configured())

    @app.get("/api/importers/<target>/options")
    def importer_options(target):
        return jsonify(importers.options(target))

    @app.post("/api/preview/cards")
    def preview_cards():
        feed = validate_feed(request.get_json(silent=True))
        items = client.fetch(feed, detailed=True)
        return jsonify(items=items, media=feed["media"], tmdb_enabled=artwork.enabled, preview_id=previews.save(feed["media"], items))

    @app.get("/api/poster/<media>/<int:identifier>")
    def poster(media, identifier):
        if media not in ("movies", "shows") or identifier <= 0:
            raise BridgeError("Invalid poster identifier.", 400)
        return jsonify(poster=artwork.lookup(media, identifier))

    @app.post("/api/importers/<target>/add")
    def importer_add(target):
        if target not in ("radarr", "sonarr"):
            raise BridgeError("Unknown importer.", 404)
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {"preview_id", "ids", "settings"}:
            raise BridgeError("Invalid add request.", 400)
        items = previews.read(data["preview_id"], target, data["ids"])
        return jsonify(results=importers.add(target, items, data["settings"]))

    @app.get("/healthz")
    def health():
        return jsonify(ok=True, service="list-bridge")

    @app.get("/api/catalog")
    def catalog():
        return jsonify(sources=SOURCES, options=OPTIONS, periods=PERIODS, filters=FILTERS, watchnow=WATCHNOW)

    @app.get("/api/browse")
    def browse():
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            raise BridgeError("Invalid browse page.", 400) from None
        return jsonify(
            client.browse(
                request.args.get("kind", ""), request.args.get("user", "me"), request.args.get("query", ""), page, request.args.get("media", "shows")
            )
        )

    @app.get("/api/feeds")
    def feeds():
        return jsonify(store.read())

    @app.post("/api/feeds")
    def save():
        feed = validate_feed(request.get_json(silent=True))
        store.save(feed)
        return jsonify(feed), 201

    @app.post("/api/preview")
    def preview():
        feed = validate_feed(request.get_json(silent=True))
        return jsonify(client.fetch(feed))

    @app.get("/<target>/<name>.json")
    def output(target, name):
        if target not in ("radarr", "sonarr"):
            raise BridgeError("Unknown importer.", 404)
        feed = store.read().get(name)
        if not feed or feed["media"] != ("movies" if target == "radarr" else "shows"):
            raise BridgeError("Feed not found for this importer.", 404)
        return jsonify(client.fetch(feed))

    return app


if __name__ == "__main__":
    from waitress import serve

    app = create_app(
        os.environ.get("PTS_CONFIG_DIR", "/app/config"),
        os.environ.get("TRAKT_BROWSER_TOKEN_FILE", "/app/config/browser-token.json"),
        os.environ.get("LIST_BRIDGE_SECRET", ""),
        ttl=int(os.environ.get("LIST_BRIDGE_CACHE_SECONDS", "3600")),
    )
    # Waitress does not emit request access logs containing the feed query secret.
    serve(app, host="0.0.0.0", port=int(os.environ.get("LIST_BRIDGE_PORT", "8090")), threads=4)
