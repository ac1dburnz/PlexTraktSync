"""Optional server-side TMDB poster lookup with bounded, expiring cache."""
from __future__ import annotations

import os
import re
import threading
import time

import requests


class PosterLookup:
    def __init__(self, get=requests.get, env=None, clock=time.monotonic):
        env = os.environ if env is None else env
        self.key = env.get("TMDB_API_KEY", "").strip()
        self.token = env.get("TMDB_READ_ACCESS_TOKEN", "").strip()
        self.get = get
        self.clock = clock
        self.cache = {}
        self.lock = threading.Lock()

    @property
    def enabled(self):
        return bool(self.key or self.token)

    def lookup(self, media, identifier):
        if not self.enabled or media not in ("movies", "shows") or type(identifier) is not int or identifier <= 0:
            return None
        key = (media, identifier)
        with self.lock:
            cached = self.cache.get(key)
            if cached and self.clock() < cached[0]:
                return cached[1]
            url = f"https://api.themoviedb.org/3/{'movie' if media == 'movies' else 'tv'}/{identifier}"
            params = {} if self.token else {"api_key": self.key}
            headers = {"Authorization": "Bearer " + self.token} if self.token else {}
            poster = None
            ttl = 300
            try:
                response = self.get(url, params=params, headers=headers, timeout=8, allow_redirects=False)
                if response.status_code == 200:
                    data = response.json()
                    path = data.get("poster_path") if isinstance(data, dict) else None
                    if isinstance(path, str) and re.fullmatch(r"/[A-Za-z0-9_-]+\.(jpg|png|webp)", path):
                        poster = "https://image.tmdb.org/t/p/w500" + path
                    ttl = 86400
            except (requests.RequestException, ValueError):
                pass  # Optional artwork must not break feeds or expose credential-bearing errors.
            if len(self.cache) >= 2000:
                self.cache.clear()
            self.cache[key] = (self.clock() + ttl, poster)
            return poster
