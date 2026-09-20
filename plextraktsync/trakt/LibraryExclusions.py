"""Read-only account-wide watched/collection exclusions, independent of feed source."""

from __future__ import annotations

import re

from plextraktsync.trakt.ListSources import BridgeError, media_item


def identifiers(item):
    ids = item.get("ids")
    if not isinstance(ids, dict):
        raise BridgeError("Trakt returned malformed library identifiers; no partial list was published.")
    keys = {(kind, value) for kind, value in ids.items() if kind in ("trakt", "tmdb", "tvdb") and type(value) is int and value > 0}
    imdb = ids.get("imdb")
    if isinstance(imdb, str) and re.fullmatch(r"tt\d+", imdb):
        keys.add(("imdb", imdb))
    return keys


class LibraryExclusions:
    def __init__(self, request, clock, ttl):
        self.request, self.clock, self.ttl = request, clock, ttl
        self.cache = {}

    def load(self, feed, fingerprint, deadline):
        """Caller holds the feed client's lock. Never cache incomplete snapshots."""
        blocked = set()
        oldest = self.clock()
        for option, endpoint in (("hide_watched", "watched"), ("hide_collected", "collection")):
            if not feed.get(option, False):
                continue
            key = (fingerprint, feed["media"], endpoint)
            snapshot = self.cache.get(key)
            if snapshot is None or self.clock() - snapshot[0] >= self.ttl:
                ids = set()
                for page in range(1, 101):
                    data, pages = self.request(f"/sync/{endpoint}/{feed['media']}", {"page": page, "limit": 250}, deadline)
                    if not isinstance(data, list) or pages > 100 or (not data and page < pages):
                        raise BridgeError("Trakt library snapshot is incomplete or too large; no partial list was published.")
                    for entry in data:
                        if not isinstance(entry, dict):
                            raise BridgeError("Trakt returned malformed library data; no partial list was published.")
                        item = entry.get("movie" if feed["media"] == "movies" else "show")
                        if not isinstance(item, dict):
                            raise BridgeError("Trakt returned malformed library titles; no partial list was published.")
                        found = identifiers(item)
                        if not found:
                            raise BridgeError("A Trakt library title has no usable identifiers; no partial list was published.")
                        ids.update(found)
                    if page >= pages:
                        break
                snapshot = (self.clock(), ids)
                if len(self.cache) >= 16:
                    self.cache.clear()
                self.cache[key] = snapshot
            oldest = min(oldest, snapshot[0])
            blocked.update(snapshot[1])
        return blocked, oldest

    @staticmethod
    def filter(items, media, blocked):
        if not blocked:
            return items
        return [entry for entry in items if not identifiers(media_item(entry, media)) & blocked]
