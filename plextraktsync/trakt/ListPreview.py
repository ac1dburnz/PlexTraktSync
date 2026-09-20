"""Local selection rules and display-only metadata for Trakt feeds."""

from __future__ import annotations

import math
import re
from urllib.parse import quote, urlsplit


class LocalFilterError(ValueError):
    pass


def validate_local(value):
    if not isinstance(value, dict) or set(value) - {
        "languages",
        "exclude_languages",
        "year_min",
        "year_max",
        "rating_min",
        "genres",
        "exclude_genres",
        "keep_unknown",
    }:
        raise LocalFilterError("Unsupported local filter.")
    for key, item in value.items():
        if key in ("languages", "exclude_languages", "genres", "exclude_genres"):
            if (
                not isinstance(item, list)
                or len(item) > 50
                or any(not isinstance(x, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z -]{0,49}", x) for x in item)
            ):
                raise LocalFilterError("Local languages/genres must be lists of names or codes.")
        elif key in ("year_min", "year_max"):
            if type(item) is not int or not 1800 <= item <= 2200:
                raise LocalFilterError("Local years must be between 1800 and 2200.")
        elif key == "rating_min":
            if type(item) not in (int, float) or not math.isfinite(item) or not 0 <= item <= 10:
                raise LocalFilterError("Minimum rating must be between 0 and 10.")
        elif type(item) is not bool:
            raise LocalFilterError("Keep unknown must be a boolean.")
    if value.get("year_min", 1800) > value.get("year_max", 2200):
        raise LocalFilterError("Minimum year exceeds maximum year.")


def matches(item, rules):
    """Language is the title's original language, not available audio tracks."""
    keep_unknown = rules.get("keep_unknown", False)
    for field, include, exclude in [("language", "languages", "exclude_languages"), ("genres", "genres", "exclude_genres")]:
        wanted = {s.casefold() for s in rules.get(include, [])}
        unwanted = {s.casefold() for s in rules.get(exclude, [])}
        if not wanted and not unwanted:
            continue
        value = item.get(field)
        values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        values = {v.casefold() for v in values if isinstance(v, str) and v}
        if not values:
            if not keep_unknown:
                return False
        elif (wanted and not values & wanted) or values & unwanted:
            return False
    year = item.get("year")
    if "year_min" in rules or "year_max" in rules:
        if type(year) is not int:
            if not keep_unknown:
                return False
        elif not rules.get("year_min", 1800) <= year <= rules.get("year_max", 2200):
            return False
    if "rating_min" in rules:
        rating = item.get("rating")
        if type(rating) not in (int, float) or not math.isfinite(rating):
            if not keep_unknown:
                return False
        elif rating < rules["rating_min"]:
            return False
    return True


def poster_url(item):
    images = item.get("images")
    posters = images.get("poster", []) if isinstance(images, dict) else []
    if not isinstance(posters, list):
        return None
    for value in posters:
        if not isinstance(value, str) or len(value) > 2048:
            continue
        url = value if "://" in value else "https://" + value.lstrip("/")
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        host = parsed.hostname or ""
        if (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and (host == "trakt.tv" or host.endswith(".trakt.tv") or host in ("image.tmdb.org", "walter-r2.trakt.tv"))
        ):
            return url
    return None


def cards(items, media, limit):
    kind, id_key = ("movie", "tmdb") if media == "movies" else ("show", "tvdb")
    out, seen = [], set()
    for entry in items:
        item = entry.get(kind, entry)
        ids = item.get("ids") or {}
        identifier = ids.get(id_key)
        if type(identifier) is not int or identifier <= 0 or identifier in seen:
            continue
        seen.add(identifier)
        safe_ids = {k: v for k, v in ids.items() if k in ("trakt", "tmdb", "tvdb") and type(v) is int and v > 0}
        imdb = ids.get("imdb")
        if isinstance(imdb, str) and re.fullmatch(r"tt\d+", imdb):
            safe_ids["imdb"] = imdb
        slug = ids.get("slug")
        trakt_id = slug if isinstance(slug, str) and re.fullmatch(r"[a-zA-Z0-9_-]+", slug) else safe_ids.get("trakt")
        links = {}
        if trakt_id:
            links["Trakt"] = f"https://trakt.tv/{media}/{quote(str(trakt_id), safe='')}"
        if "imdb" in safe_ids:
            links["IMDb"] = "https://www.imdb.com/title/" + safe_ids["imdb"] + "/"
        if "tmdb" in safe_ids:
            links["TMDB"] = f"https://www.themoviedb.org/{'movie' if media == 'movies' else 'tv'}/{safe_ids['tmdb']}"
        if "tvdb" in safe_ids and media == "shows":
            links["TVDB"] = f"https://thetvdb.com/?tab=series&id={safe_ids['tvdb']}"
        out.append(
            {
                "id": identifier,
                "ids": safe_ids,
                "title": item.get("title") if isinstance(item.get("title"), str) else str(identifier),
                "year": item.get("year") if type(item.get("year")) is int else None,
                "language": item.get("language") if isinstance(item.get("language"), str) else None,
                "network": item.get("network") if isinstance(item.get("network"), str) else None,
                "genres": [x for x in item.get("genres", []) if isinstance(x, str)] if isinstance(item.get("genres"), list) else [],
                "rating": item.get("rating") if type(item.get("rating")) in (int, float) and math.isfinite(item["rating"]) else None,
                "overview": item.get("overview", "")[:2000] if isinstance(item.get("overview"), str) else "",
                "poster": poster_url(item),
                "links": links,
            }
        )
        if len(out) >= limit:
            break
    return out
