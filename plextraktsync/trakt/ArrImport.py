"""Optional direct adds to configured Radarr/Sonarr instances (API v3)."""

from __future__ import annotations

import os
import secrets
import threading
import time
from urllib.parse import urlsplit

import requests

from plextraktsync.trakt.ListSources import BridgeError


class ArrRejected(BridgeError):
    """The target explicitly rejected a write before accepting it."""


class PreviewStore:
    def __init__(self, clock=time.monotonic, ttl=1800):
        self.clock, self.ttl = clock, ttl
        self.lock = threading.Lock()
        self.entries = {}

    def save(self, media, items):
        with self.lock:
            now = self.clock()
            self.entries = {k: v for k, v in self.entries.items() if now - v[0] < self.ttl}
            if len(self.entries) >= 32:
                self.entries.pop(next(iter(self.entries)))
            key = secrets.token_urlsafe(24)
            self.entries[key] = (now, media, {item["id"]: item for item in items})
            return key

    def read(self, key, target, identifiers):
        if not isinstance(key, str):
            raise BridgeError("Preview again before adding titles.", 400)
        if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 10 or any(type(x) is not int or x <= 0 for x in identifiers):
            raise BridgeError("Choose 1–10 preview title IDs per request.", 400)
        if len(set(identifiers)) != len(identifiers):
            raise BridgeError("Duplicate selected IDs.", 400)
        with self.lock:
            entry = self.entries.get(key)
            if not entry or self.clock() - entry[0] >= self.ttl:
                raise BridgeError("Preview expired. Preview again before adding.", 409)
            _, media, items = entry
            if media != ("movies" if target == "radarr" else "shows") or any(i not in items for i in identifiers):
                raise BridgeError("Selection does not match this preview or importer.", 400)
            return [items[i] for i in identifiers]


class ArrImport:
    def __init__(self, configs=None, request_fn=requests.request):
        self.configs = (
            configs
            if configs is not None
            else {
                target: {"url": os.environ.get(target.upper() + "_URL", ""), "key": os.environ.get(target.upper() + "_API_KEY", "")}
                for target in ("radarr", "sonarr")
            }
        )
        self.request_fn = request_fn
        self.locks = {target: threading.Lock() for target in ("radarr", "sonarr")}
        self.uncertain = set()

    def configured(self):
        return {target: bool(self.configs.get(target, {}).get("url") and self.configs.get(target, {}).get("key")) for target in self.locks}

    def config(self, target):
        if target not in self.locks:
            raise BridgeError("Unknown importer.", 404)
        config = self.configs.get(target, {})
        url, key = config.get("url", ""), config.get("key", "")
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise BridgeError(f"Invalid {target.upper()}_URL.", 503) from None
        if (
            not key
            or parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise BridgeError(f"Configure {target.upper()}_URL and {target.upper()}_API_KEY on the container.", 503)
        return url.rstrip("/"), key

    def call(self, target, method, route, **kwargs):
        url, key = self.config(target)
        try:
            response = self.request_fn(method, url + "/api/v3/" + route, headers={"X-Api-Key": key}, timeout=15, allow_redirects=False, **kwargs)
            if response.status_code not in (200, 201):
                error = ArrRejected if response.status_code in (400, 401, 403, 404, 409, 422) else BridgeError
                raise error(f"{target.title()} returned HTTP {response.status_code}. Check its logs/settings.", 502)
            return response.json()
        except (requests.RequestException, ValueError):
            # Never return URLs, API keys, exception text or upstream response bodies.
            raise BridgeError(f"{target.title()} request failed or returned invalid JSON.", 502) from None

    def options(self, target):
        profiles = self.call(target, "GET", "qualityprofile")
        roots = self.call(target, "GET", "rootfolder")
        if not isinstance(profiles, list) or not isinstance(roots, list):
            raise BridgeError("Importer returned invalid settings.")
        return {
            "profiles": [
                {"id": p["id"], "name": p.get("name", str(p["id"]))}
                for p in profiles
                if isinstance(p, dict) and type(p.get("id")) is int and isinstance(p.get("name", ""), str)
            ],
            "roots": [r["path"] for r in roots if isinstance(r, dict) and isinstance(r.get("path"), str)],
        }

    def add(self, target, items, settings):
        self.config(target)
        allowed = {"quality_profile_id", "root_folder", "monitored", "search", "series_type", "minimum_availability"}
        if not isinstance(settings, dict) or set(settings) - allowed:
            raise BridgeError("Invalid importer settings.", 400)
        profile, root = settings.get("quality_profile_id"), settings.get("root_folder")
        if type(profile) is not int or profile <= 0 or not isinstance(root, str):
            raise BridgeError("Choose a quality profile and root folder.", 400)
        if any(type(settings.get(k, False)) is not bool for k in ("monitored", "search")):
            raise BridgeError("Monitoring/search must be booleans.", 400)
        series_type = settings.get("series_type", "standard")
        availability = settings.get("minimum_availability", "released")
        if series_type not in ("standard", "daily", "anime") or availability not in ("announced", "inCinemas", "released"):
            raise BridgeError("Invalid series type or movie availability.", 400)
        if settings.get("search") and not settings.get("monitored"):
            raise BridgeError("Enable monitoring before requesting a download search.", 400)
        with self.locks[target]:
            options = self.options(target)
            if profile not in [p["id"] for p in options["profiles"]] or root not in options["roots"]:
                raise BridgeError("Quality profile or root folder is no longer available. Reload settings.", 400)
            route, id_key, prefix = ("movie", "tmdbId", "tmdb") if target == "radarr" else ("series", "tvdbId", "tvdb")
            library = self.call(target, "GET", route)
            if not isinstance(library, list) or any(not isinstance(x, dict) or type(x.get(id_key)) is not int for x in library):
                raise BridgeError("Importer returned an invalid library. No titles were added.")
            existing = {x[id_key] for x in library}
            results = []
            stop = False
            for item in items:
                identifier = item["id"]
                result = {"id": identifier, "title": item["title"]}
                if identifier in existing:
                    result["status"] = "existing"
                    self.uncertain.discard((target, identifier))
                elif stop:
                    result.update(status="not_attempted", message="Batch stopped after an uncertain result.")
                elif (target, identifier) in self.uncertain:
                    result.update(status="unknown", message="An earlier add had an uncertain result. Check the target library before retrying.")
                    stop = True
                else:
                    attempted = False
                    try:
                        lookup = self.call(target, "GET", route + "/lookup", params={"term": f"{prefix}:{identifier}"})
                        if not isinstance(lookup, list):
                            raise BridgeError("Importer lookup returned invalid metadata.")
                        match = next((x for x in lookup if isinstance(x, dict) and x.get(id_key) == identifier), None)
                        if not match:
                            raise BridgeError("Importer could not resolve the exact title ID.")
                        if match.get("isExcluded"):
                            result.update(status="excluded", message="Title is excluded in the target application.")
                        else:
                            # Use metadata resolved by the target, not client-submitted titles/URLs.
                            payload = {k: match[k] for k in ("title", "titleSlug", "year", "images", "seasons") if k in match}
                            payload.update(
                                {
                                    id_key: identifier,
                                    "qualityProfileId": profile,
                                    "rootFolderPath": root,
                                    "monitored": settings.get("monitored", False),
                                }
                            )
                            if target == "radarr":
                                payload.update(minimumAvailability=availability, addOptions={"searchForMovie": settings.get("search", False)})
                            else:
                                payload.update(
                                    seriesType=series_type,
                                    seasonFolder=True,
                                    addOptions={
                                        "searchForMissingEpisodes": settings.get("search", False),
                                        "monitor": "all" if settings.get("monitored") else "none",
                                    },
                                )
                            attempted = True
                            added = self.call(target, "POST", route, json=payload)
                            if not isinstance(added, dict) or added.get(id_key) != identifier or type(added.get("id")) is not int:
                                raise BridgeError("Importer did not confirm the added title.")
                            result["status"] = "added"
                            existing.add(identifier)
                    except BridgeError as exc:
                        uncertain = attempted and not isinstance(exc, ArrRejected)
                        if uncertain:
                            # A timeout/error may occur after the write succeeded. Never retry automatically.
                            self.uncertain.add((target, identifier))
                            stop = True
                        result.update(status="unknown" if uncertain else "failed", message=exc.message)
                results.append(result)
            return results
