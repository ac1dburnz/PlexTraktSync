"""Allowlisted, read-only Trakt discovery sources and feed selection rules.

Contracts audited against trakt/trakt-api on 2026-09-20. These definitions also
 drive the builder UI; no arbitrary URL or query passthrough is accepted.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from plextraktsync.trakt.ListPreview import LocalFilterError, matches, validate_local


class BridgeError(Exception):
    def __init__(self, message, status=502):
        self.message, self.status = message, status
        super().__init__(message)


PERIODS = ["daily", "weekly", "monthly", "yearly", "all"]
FILTERS = ["years", "genres", "subgenres", "languages", "countries", "ratings", "certifications", "runtimes", "watchnow", "start_date", "end_date"]
WATCHNOW = ["favorites", "any", "any_all", "free", "free_all", "subscriptions", "subscriptions_all"]
SLUG = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
SORTS = ["rank", "added", "title", "released", "runtime", "popularity", "percentage", "votes"]
IGNORE = ["ignore_watched", "ignore_collected", "ignore_watchlisted"]


def option(label, *, choices=None, default=None, kind="text", minimum=None, maximum=None, required=False):
    return {
        k: v
        for k, v in {
            "label": label,
            "choices": choices,
            "default": default,
            "kind": kind,
            "min": minimum,
            "max": maximum,
            "required": required,
        }.items()
        if v is not None
    }


OPTIONS = {
    "user": option("Trakt username (me for your account)", default="me", kind="slug"),
    "list": option("List slug or ID", required=True, kind="slug"),
    "seed": option("Title slug or ID", required=True, kind="slug"),
    "person": option("Person slug or ID", required=True, kind="slug"),
    "query": option("Search text", required=True),
    "period": option("Period", choices=PERIODS, default="weekly"),
    "target": option("Calendar scope", choices=["all", "my"], default="all"),
    "start_offset": option("Start relative to today (days; negative = past)", kind="integer", minimum=-365, maximum=365, default=-30),
    "days": option("Calendar window (days)", kind="integer", minimum=1, maximum=93, default=60),
    "since_days": option("Look back (days)", kind="integer", minimum=1, maximum=30, default=7),
    "rating": option("User rating (1–10; blank = all)", kind="integer", minimum=1, maximum=10),
    "watch_window": option("Recommendation watch window (days)", kind="integer", minimum=1, maximum=3650),
    "sort_by": option("Trakt list order", choices=SORTS, default="rank"),
    "sort_how": option("Trakt list direction", choices=["asc", "desc"], default="asc"),
    "credit": option(
        "Credit role",
        choices=[
            "all",
            "cast",
            "crew",
            "directing",
            "writing",
            "production",
            "art",
            "costume & make-up",
            "sound",
            "camera",
            "visual effects",
            "lighting",
            "editing",
        ],
        default="cast",
    ),
    "section": option(
        "Hidden section", choices=["calendar", "recommendations", "progress_watched", "progress_collected", "dropped"], default="recommendations"
    ),
    "id_type": option("External identifier type", choices=["trakt", "imdb", "tmdb", "tvdb"], default="imdb"),
    "identifier": option("External identifier", required=True, kind="slug"),
    "hide_completed": option("Exclude completed shows", kind="boolean"),
    "hide_not_completed": option("Exclude incomplete shows", kind="boolean"),
    "only_rewatching": option("Only shows being rewatched", kind="boolean"),
    **{
        key: option(label, kind="boolean")
        for key, label in zip(IGNORE, ["Exclude watched titles", "Exclude collected titles", "Exclude watchlisted titles"], strict=True)
    },
}


def source(label, group, path, *, fields=(), filters=False, media=("movies", "shows"), **kwargs):
    return dict(label=label, group=group, path=path, fields=list(fields), filters=filters, media=list(media), **kwargs)


SOURCES = {
    "favorited": source("Most favorited", "Rankings", "/{media}/favorited/{period}", fields=["period", *IGNORE], filters=True),
    "recommendations": source(
        "Personal recommendations",
        "Recommendations",
        "/recommendations/{media}",
        fields=[*IGNORE, "watch_window"],
        filters=True,
        max_limit=100,
        single=True,
    ),
    "social_recommendations": source(
        "Social recommendations", "Recommendations", "/social_recommendations/{media}", fields=[*IGNORE, "watch_window"], max_limit=100, single=True
    ),
    **{
        key: source(label, "Rankings", "/{media}/" + key, fields=IGNORE, filters=True)
        for key, label in [("trending", "Trending"), ("popular", "Popular"), ("anticipated", "Anticipated"), ("hot", "Hot titles")]
    },
    **{
        key: source(label, "Rankings", "/{media}/" + key + "/{period}", fields=["period", *IGNORE], filters=True)
        for key, label in [("watched", "Most watched"), ("played", "Most played"), ("collected", "Most collected")]
    },
    "streaming": source(
        "Recently streaming",
        "Rankings",
        "/{media}/streaming/{period}",
        fields=["period", *IGNORE],
        filters=True,
        periods=PERIODS[:3],
        note="Recent streaming releases; not proof of availability on a particular provider in your country.",
    ),
    "boxoffice": source("US weekend box office", "Rankings", "/movies/boxoffice", media=["movies"], single=True),
    "recommended": source(
        "Recommended by period (legacy)",
        "Legacy",
        "/{media}/recommended/{period}",
        fields=["period"],
        filters=True,
        note="Legacy endpoint returned HTTP 500 in our earlier check. Most favorited is a separate source, not a silent replacement.",
    ),
    "watchlist": source("User watchlist", "User libraries", "/users/{user}/watchlist/{media}", fields=["user", "sort_by", "sort_how"], filters=True),
    "user_watched": source("User watched titles", "User libraries", "/users/{user}/watched/{media}", fields=["user"]),
    "collection": source("User collection", "User libraries", "/users/{user}/collection/{media}", fields=["user"]),
    "favorites": source("User favorites", "User libraries", "/users/{user}/favorites/{media}", fields=["user", "sort_by", "sort_how"]),
    "ratings": source("User ratings", "User libraries", "/users/{user}/ratings/{media}", fields=["user", "rating"]),
    "history": source(
        "User watch history",
        "User libraries",
        "/users/{user}/history/{media}",
        fields=["user", "since_days"],
        filters=True,
        note="Episode history is deduplicated into series for Sonarr.",
    ),
    "list": source(
        "Named user list", "Lists", "/users/{user}/lists/{list}/items/{media}", fields=["user", "list", "sort_by", "sort_how"], filters=True
    ),
    "public_list": source(
        "Public list by numeric ID", "Lists", "/lists/{list}/items/{kind}", fields=["list", "sort_by", "sort_how", *IGNORE], filters=True
    ),
    "smart_list": source(
        "Existing Trakt smart list",
        "Lists",
        "/smart-lists/{list}/items",
        fields=["list", "ignore_watched", "ignore_watchlisted"],
        filters=True,
        mixed=True,
        note="Resolves an existing smart list; it does not create one. Trakt privacy/account restrictions still apply.",
    ),
    "search": source("Text search", "Search & credits", "/search/{kind}", fields=["query"]),
    "search_exact": source("Exact text search", "Search & credits", "/search/{kind}/exact", fields=["query"]),
    "search_trending": source("Trending searches", "Search & credits", "/search/recent_by_id/global/{media}"),
    "lookup": source("External ID lookup", "Search & credits", "/search/{id_type}/{identifier}", fields=["id_type", "identifier"]),
    "related": source("Related titles", "Search & credits", "/{media}/{seed}/related", fields=["seed"]),
    "credits": source(
        "Person cast / crew credits", "Search & credits", "/people/{person}/{media}", fields=["person", "credit"], shape="credits", single=True
    ),
    "updates": source(
        "Recently updated metadata",
        "Search & credits",
        "/{media}/updates/{since}",
        fields=["since_days"],
        note="Metadata updates are not new releases.",
    ),
    "playback": source(
        "Your paused playback",
        "Your progress",
        "/sync/playback/{playback_type}",
        fields=["since_days"],
        note="Paused episodes become series for Sonarr.",
    ),
    "up_next": source("Your up next shows", "Your progress", "/sync/progress/up_next", media=["shows"]),
    "progress": source(
        "Your watched progress",
        "Your progress",
        "/sync/progress/watched",
        fields=["hide_completed", "hide_not_completed", "only_rewatching"],
        media=["shows"],
    ),
    "hidden": source(
        "Your hidden titles",
        "Your progress",
        "/users/hidden/{section}",
        fields=["section"],
        note="This intentionally imports hidden titles. It does not unhide anything on Trakt.",
    ),
}
for key, label, path, media in [
    ("calendar_new", "New series", "shows/new", ["shows"]),
    ("calendar_premieres", "Season premieres", "shows/premieres", ["shows"]),
    ("calendar_shows", "Airing episodes", "shows", ["shows"]),
    ("calendar_finales", "Finales", "shows/finales", ["shows"]),
    ("calendar_movies", "Movie releases", "movies", ["movies"]),
    ("calendar_streaming", "Movie streaming releases", "streaming", ["movies"]),
    ("calendar_dvd", "DVD / physical releases", "dvd", ["movies"]),
    ("calendar_media", "All releases", "media", ["movies", "shows"]),
    ("calendar_hot", "Hot upcoming releases", "releases/hot", ["movies", "shows"]),
    ("calendar_hot_new", "Hot new series", "releases/hot/new", ["shows"]),
    ("calendar_hot_premieres", "Hot season premieres", "releases/hot/premieres", ["shows"]),
    ("calendar_hot_finales", "Hot finales", "releases/hot/finales", ["shows"]),
]:
    hot = path.startswith("releases/")
    SOURCES[key] = source(
        label,
        "Release calendars",
        "/calendars/" + ("" if hot else "{target}/") + path + "/{start}/{days}",
        fields=[*([] if hot else ["target"]), "start_offset", "days", *([] if hot else IGNORE)],
        filters=True,
        media=media,
        calendar=True,
        single=True,
        mixed=key in ("calendar_media", "calendar_hot"),
        note="Rolling UTC window. Global calendars are curated and may omit titles. Episodes are deduplicated to series.",
    )

# Documented routes can lag the deployed API; keep them explicit, not silently
# mapped to another feed or treated as an individual title when routing collides.
SOURCES["hot"]["label"] = "Hot titles (experimental)"
SOURCES["hot"]["note"] = (
    "Documented by Trakt, but the 2026-09-20 live check returned HTTP 404 for movies "
    "and a single-title object for shows. Preview will report a failure until the API supports this route."
)
SOURCES["streaming"]["note"] += " The movies endpoint returned HTTP 500 in the 2026-09-20 check; shows returned a valid list."


def validate_feed(raw):
    if not isinstance(raw, dict):
        raise BridgeError("Feed must be an object.", 400)
    if any(not isinstance(raw.get(k), str) for k in ("name", "source", "media")):
        raise BridgeError("Missing name, source or media.", 400)
    if not SLUG.fullmatch(raw["name"]) or raw["source"] not in SOURCES or raw["media"] not in SOURCES[raw["source"]]["media"]:
        raise BridgeError("Invalid name, media type or source.", 400)
    spec = SOURCES[raw["source"]]
    allowed = {
        "name", "source", "media", "limit", "filters", "networks", "order",
        "local_filters", "exclude_ids", "hide_watched", "hide_collected",
    } | set(spec["fields"])
    if set(raw) - allowed:
        raise BridgeError("Unsupported option for this source.", 400)
    feed = dict(raw)
    for flag in ("hide_watched", "hide_collected"):
        if flag in feed and type(feed[flag]) is not bool:
            raise BridgeError("Watched/collected exclusions must be booleans.", 400)
    limit = feed.setdefault("limit", 100)
    if type(limit) is not int or not 1 <= limit <= spec.get("max_limit", 1000):
        raise BridgeError(f"Limit must be 1–{spec.get('max_limit', 1000)}.", 400)
    for field in spec["fields"]:
        rule = OPTIONS[field]
        if field not in feed and "default" in rule:
            feed[field] = rule["default"]
        if field not in feed:
            if rule.get("required"):
                raise BridgeError(f"Missing {field}.", 400)
            continue
        value = feed[field]
        kind = rule["kind"]
        valid = True
        if kind == "integer":
            valid = type(value) is int and rule["min"] <= value <= rule["max"]
        elif kind == "boolean":
            valid = type(value) is bool
        elif kind == "slug":
            valid = isinstance(value, str) and bool(SLUG.fullmatch(value))
        else:
            valid = isinstance(value, str) and 0 < len(value.strip()) <= 160 and not any(ord(c) < 32 for c in value)
        choices = spec.get("periods", rule.get("choices")) if field == "period" else rule.get("choices")
        if not valid or (choices and value not in choices):
            raise BridgeError(f"Invalid {field}.", 400)
    if feed["source"] == "public_list" and not feed["list"].isdigit():
        raise BridgeError("Public list requires a numeric Trakt list ID.", 400)
    if feed["source"] == "hidden" and feed["media"] == "movies" and feed["section"] not in ("calendar", "recommendations"):
        raise BridgeError("That hidden section supports shows only.", 400)
    filters = feed.setdefault("filters", {})
    if not isinstance(filters, dict) or set(filters) - set(FILTERS) or (filters and not spec["filters"]):
        raise BridgeError("Unsupported filters for this source.", 400)
    for field, value in filters.items():
        if not isinstance(value, str) or not value or len(value) > 160 or not re.fullmatch(r"[a-zA-Z0-9, ._-]+", value):
            raise BridgeError("Invalid filter value.", 400)
        if field == "watchnow" and value not in WATCHNOW:
            raise BridgeError("Invalid streaming availability filter.", 400)
        if field in ("start_date", "end_date"):
            try:
                date.fromisoformat(value)
            except ValueError:
                raise BridgeError("Date filters must be YYYY-MM-DD.", 400) from None
            if spec.get("calendar") or feed["source"] == "smart_list":
                raise BridgeError("Use the calendar window; date filters are unavailable for this source.", 400)
    if filters.get("start_date") and filters.get("end_date") and filters["start_date"] > filters["end_date"]:
        raise BridgeError("End date must not precede start date.", 400)
    networks = feed.get("networks", "")
    if not isinstance(networks, str) or len(networks) > 240 or any(ord(c) < 32 for c in networks):
        raise BridgeError("Invalid network names.", 400)
    if networks and (feed["media"] != "shows" or not all(n.strip() for n in networks.split(","))):
        raise BridgeError("Networks require TV shows and comma-separated nonempty names.", 400)
    order = feed.get("order", "upstream")
    if order not in ("upstream", "newest", "oldest", "title"):
        raise BridgeError("Invalid output order.", 400)
    try:
        validate_local(feed.get("local_filters", {}))
    except LocalFilterError as exc:
        raise BridgeError(str(exc), 400) from None
    excluded = feed.get("exclude_ids", [])
    if not isinstance(excluded, list) or len(excluded) > 1000 or any(type(x) is not int or x <= 0 for x in excluded):
        raise BridgeError("Excluded IDs must be a list of up to 1000 positive importer IDs.", 400)
    return feed


def upstream(feed, today=None):
    today = today or datetime.now(timezone.utc).date()
    spec = SOURCES[feed["source"]]
    values = {k: quote(str(v), safe="") for k, v in feed.items() if isinstance(v, (str, int))}
    values.update(
        kind="movie" if feed["media"] == "movies" else "show",
        playback_type="movies" if feed["media"] == "movies" else "episodes",
        since=(today - timedelta(days=feed.get("since_days", 7))).isoformat(),
        start=(today + timedelta(days=feed.get("start_offset", 0))).isoformat(),
    )
    params = dict(feed["filters"])
    params["extended"] = "full,images"
    for key in [*IGNORE, "watch_window", "sort_by", "sort_how", "query", "hide_completed", "hide_not_completed", "only_rewatching"]:
        if key in feed:
            params[key] = str(feed[key]).lower() if isinstance(feed[key], bool) else feed[key]
    if feed["source"] in ("history", "playback"):
        params["start_at"] = values["since"] + "T00:00:00Z"
    if feed["source"] in ("lookup", "hidden", "calendar_media", "calendar_hot"):
        params["type"] = values["kind"]
    path = spec["path"].format(**values)
    if feed["source"] == "ratings" and "rating" in feed:
        path += "/" + str(feed["rating"])
    return path, params


def requests_for(feed, today):
    """Split rolling calendars into <=31-day requests, freezing UTC today once."""
    if not SOURCES[feed["source"]].get("calendar"):
        return [upstream(feed, today)]
    result = []
    for offset in range(0, feed["days"], 31):
        chunk = {**feed, "start_offset": feed["start_offset"] + offset, "days": min(31, feed["days"] - offset)}
        result.append(upstream(chunk, today))
    return result


def normalize(data, feed):
    if SOURCES[feed["source"]].get("shape") == "credits":
        if not isinstance(data, dict) or not set(data) & {"cast", "crew"}:
            raise BridgeError("Trakt returned malformed credits.")
        cast, crew = data.get("cast") or [], data.get("crew") or {}
        if not isinstance(cast, list) or not isinstance(crew, dict) or any(not isinstance(v, list) for v in crew.values()):
            raise BridgeError("Trakt returned malformed credits.")
        role = feed["credit"]
        data = (cast if role in ("all", "cast") else []) + (
            [entry for group in crew.values() for entry in group] if role in ("all", "crew") else crew.get(role, []) if role != "cast" else []
        )
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise BridgeError("Trakt returned malformed list data.")
    kind = "movie" if feed["media"] == "movies" else "show"
    other = "show" if kind == "movie" else "movie"
    # A mixed smart list or calendar may contain only the other media type.
    return [item for item in data if not (item.get(other) is not None and item.get(kind) is None)]


def media_item(entry, media):
    kind = "movie" if media == "movies" else "show"
    item = entry.get(kind, entry)
    if not isinstance(item, dict):
        raise BridgeError("Trakt returned malformed title data.")
    return item


def select_items(items, feed):
    excluded = set(feed.get("exclude_ids", []))
    id_key = "tmdb" if feed["media"] == "movies" else "tvdb"

    def selected(entry):
        item = media_item(entry, feed["media"])
        ids = item.get("ids") or {}
        if not isinstance(ids, dict):
            raise BridgeError("Trakt returned malformed identifiers.")
        identifier = ids.get(id_key)
        return (type(identifier) is not int or identifier not in excluded) and matches(item, feed.get("local_filters", {}))

    items = [e for e in items if selected(e)]
    networks = {n.strip().casefold() for n in feed.get("networks", "").split(",") if n.strip()}
    if networks:
        # Missing network is excluded, not guessed from availability or country.
        items = [e for e in items if str(media_item(e, feed["media"]).get("network") or "").casefold() in networks]
    order = feed.get("order", "upstream")
    if order == "title":
        return sorted(items, key=lambda e: str(media_item(e, feed["media"]).get("title") or "").casefold())
    if order in ("newest", "oldest"):

        def release(entry):
            item = media_item(entry, feed["media"])
            value = (
                (entry.get("first_aired") or entry.get("released"))
                if SOURCES[feed["source"]].get("calendar")
                else (item.get("first_aired") or item.get("released"))
            )
            try:
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                return dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
            except (ValueError, TypeError, OverflowError):
                return None

        dated = [(release(e), e) for e in items]
        known = sorted((pair for pair in dated if pair[0] is not None), key=lambda p: p[0], reverse=order == "newest")
        return [e for _, e in known] + [e for d, e in dated if d is None]
    return items
