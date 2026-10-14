"""Conservative Trakt pacing and cooldowns shared by processes on one config directory."""

from __future__ import annotations

import json
import logging
import math
import os
import random
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
HOSTS = frozenset(("api.trakt.tv", "apiz.trakt.tv", "auth.trakt.tv"))


class RateLimiter:
    def __init__(self, path=None, clock=time.time, sleep=time.sleep):
        if path is None:
            from plextraktsync.path import config_dir

            path = Path(os.environ.get("PTS_CONFIG_DIR", str(config_dir))) / "trakt-rate-limit.sqlite"
        self.path, self.clock, self.sleep = Path(path), clock, sleep
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS calls (bucket TEXT, at REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS cooldown (id INTEGER PRIMARY KEY, until REAL, failures INTEGER)")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def acquire(self, method):
        bucket = "get" if method.upper() in ("GET", "HEAD", "OPTIONS") else "write"
        spacing = 0.75 if bucket == "get" else 1.1
        while True:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                now = self.clock()
                db.execute("DELETE FROM calls WHERE at <= ?", (now - 300,))
                last = db.execute("SELECT MAX(at) FROM calls WHERE bucket=?", (bucket,)).fetchone()[0]
                row = db.execute("SELECT until FROM cooldown WHERE id=1").fetchone()
                wait = max(0, (last + spacing - now) if last is not None else 0, row[0] - now if row else 0)
                if bucket == "get":
                    calls = db.execute("SELECT at FROM calls WHERE bucket=? ORDER BY at", (bucket,)).fetchall()
                    if len(calls) >= 450:
                        wait = max(wait, calls[-450][0] + 300 - now)
                if wait <= 0:
                    db.execute("INSERT INTO calls VALUES (?, ?)", (bucket, now))
                    return
            self.sleep(wait)

    def observe(self, response, method, url, authenticated=False):
        if response.status_code != 429:
            # A successful final request can announce an exhausted shared bucket.
            try:
                bucket = json.loads(response.headers.get("X-Ratelimit", ""))
                if int(bucket["remaining"]) <= 0:
                    until = datetime.fromisoformat(bucket["until"].replace("Z", "+00:00")).timestamp()
                    with self.connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        row = db.execute("SELECT until, failures FROM cooldown WHERE id=1").fetchone()
                        db.execute("INSERT OR REPLACE INTO cooldown VALUES (1, ?, ?)", (max(until, row[0] if row else 0), row[1] if row else 0))
            except (ValueError, TypeError, KeyError, AttributeError):
                pass
            return
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT until, failures FROM cooldown WHERE id=1").fetchone()
            failures = (row[1] if row else 0) + 1
            raw = response.headers.get("Retry-After")
            try:
                delay = float(raw)
                if not math.isfinite(delay) or delay < 0:
                    raise ValueError()
            except (ValueError, TypeError):
                delay = min(300, 2 ** min(failures, 9)) + random.uniform(0, 1)
            until = max(row[0] if row else 0, self.clock() + delay)
            db.execute("INSERT OR REPLACE INTO cooldown VALUES (1, ?, ?)", (until, failures))
        # Never log query strings, credentials, response bodies or account identifiers.
        logger.warning(
            "Trakt throttled status=429 method=%s endpoint=%s authenticated=%s retry_after=%s ratelimit=%s",
            method,
            self.endpoint(url),
            authenticated,
            raw,
            response.headers.get("X-Ratelimit"),
        )

    @staticmethod
    def endpoint(url):
        parts = urlsplit(url).path.split("/")
        if len(parts) > 2 and parts[1] == "users" and parts[2] not in ("settings", "likes"):
            parts[2] = "<user>"
        return "/".join(parts)

    def remaining(self):
        with self.connect() as db:
            row = db.execute("SELECT until FROM cooldown WHERE id=1").fetchone()
        return max(0, row[0] - self.clock()) if row else 0

    def blocked(self):
        return self.remaining() > 0
