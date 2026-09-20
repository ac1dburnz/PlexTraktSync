from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from plextraktsync.trakt.ArrImport import ArrImport, PreviewStore
from plextraktsync.trakt.ListPreview import poster_url
from plextraktsync.trakt.ListSources import BridgeError, select_items, validate_feed

spec = importlib.util.spec_from_file_location("visual_bridge", Path(__file__).parents[1] / "browser-helper/list_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
SECRET = "visual-test-secret-at-least-24-characters"


def reply(data, status=200, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {}, json=lambda: data)


def title(identifier=42, **extra):
    return {
        "title": "Arrival",
        "year": 2016,
        "language": "en",
        "rating": 8.1,
        "genres": ["science-fiction", "drama"],
        "ids": {"tmdb": identifier, "tvdb": identifier, "trakt": 100, "slug": "arrival-2016", "imdb": "tt2543164"},
        "images": {"poster": ["walter-r2.trakt.tv/images/movies/000/000/100/posters/medium/example.webp"]},
        **extra,
    }


def feed(**changes):
    return validate_feed({"name": "test", "source": "search", "query": "space", "media": "movies", **changes})


@pytest.fixture
def token(tmp_path):
    p = tmp_path / "token.json"
    p.write_text(json.dumps({"access_token": "private-token", "client_id": "private-client"}))
    return p


@pytest.mark.parametrize(
    "rules,expected",
    [
        ({"languages": ["EN"]}, True),
        ({"languages": ["fr"]}, False),
        ({"exclude_languages": ["en"]}, False),
        ({"year_min": 2016, "year_max": 2016}, True),
        ({"year_min": 2020}, False),
        ({"rating_min": 8}, True),
        ({"rating_min": 9}, False),
        ({"genres": ["drama", "comedy"]}, True),
        ({"exclude_genres": ["drama"]}, False),
    ],
)
def test_local_filters_on_source_without_server_filter_support(rules, expected):
    f = feed(local_filters=rules)
    assert bool(select_items([title()], f)) is expected


@pytest.mark.parametrize(
    "rules", [{"languages": ["en"]}, {"exclude_languages": ["en"]}, {"year_min": 2000}, {"rating_min": 5}, {"genres": ["drama"]}]
)
def test_unknown_metadata_policy(rules):
    missing = title(language=None, year=None, rating=None, genres=[])
    assert select_items([missing], feed(local_filters=rules)) == []
    assert select_items([missing], feed(local_filters={**rules, "keep_unknown": True})) == [missing]


@pytest.mark.parametrize(
    "changes",
    [
        {"local_filters": {"year_min": True}},
        {"local_filters": {"year_min": 2020, "year_max": 2000}},
        {"local_filters": {"rating_min": float("nan")}},
        {"local_filters": {"languages": "en"}},
        {"local_filters": {"languages": [1]}},
        {"local_filters": {"bogus": True}},
        {"exclude_ids": [True]},
        {"exclude_ids": [-1]},
        {"exclude_ids": "42"},
    ],
)
def test_invalid_local_rules(changes):
    with pytest.raises(BridgeError):
        feed(**changes)


def test_filters_and_manual_exclusions_happen_before_limit_and_keep_paging(token):
    responses = iter(
        [
            reply([title(1, language="fr"), title(2)], headers={"X-Pagination-Page-Count": "2"}),
            reply([title(3)], headers={"X-Pagination-Page-Count": "2"}),
        ]
    )
    client = bridge.TraktFeeds(token, get=lambda *a, **kw: next(responses))
    f = feed(limit=1, local_filters={"languages": ["en"]}, exclude_ids=[2])
    assert client.fetch(f) == [{"id": 3, "title": "Arrival"}]
    assert client.fetch(f, detailed=True)[0]["id"] == 3  # Reuses rich cache.


def test_rich_preview_uses_same_items_as_saved_feed(tmp_path, token):
    client = bridge.create_app(tmp_path, token, SECRET, get=lambda *a, **kw: reply([title(), title(43)])).test_client()
    headers = {"X-Bridge-Key": SECRET}
    f = feed(exclude_ids=[43])
    assert client.post("/api/preview/cards", json=f).status_code == 403
    response = client.post("/api/preview/cards", headers=headers, json=f)
    assert response.status_code == 200
    card = response.json["items"][0]
    assert card["id"] == 42 and card["year"] == 2016
    assert card["poster"].startswith("https://walter-r2.trakt.tv/")
    assert card["links"]["Trakt"] == "https://trakt.tv/movies/arrival-2016"
    assert "private-token" not in response.text
    assert client.post("/api/feeds", headers=headers, json=f).status_code == 201
    assert client.get("/radarr/test.json", headers=headers).json == [{"id": 42, "title": "Arrival"}]
    assert client.post("/api/preview", headers=headers, json=f).json == [{"id": 42, "title": "Arrival"}]


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "http://image.tmdb.org/a",
        "https://evil.example/a",
        "https://trakt.tv.evil.example/a",
        "https://u:p@trakt.tv/a",
        "https://trakt.tv/a?secret=x",
        "https://[bad",
    ],
)
def test_unsafe_posters_rejected(url):
    assert poster_url({"images": {"poster": [url]}}) is None


def test_preview_snapshot_membership_media_and_expiry():
    now = [0]
    store = PreviewStore(clock=lambda: now[0], ttl=10)
    key = store.save("movies", [{"id": 42, "title": "Example"}])
    assert store.read(key, "radarr", [42])[0]["id"] == 42
    for target, ids in [("sonarr", [42]), ("radarr", [99]), ("radarr", [42, 42]), ("radarr", [True])]:
        with pytest.raises(BridgeError):
            store.read(key, target, ids)
    now[0] = 10
    with pytest.raises(BridgeError, match="expired"):
        store.read(key, "radarr", [42])


class FakeArr:
    def __init__(self, target="radarr"):
        self.target = target
        self.id_key = "tmdbId" if target == "radarr" else "tvdbId"
        self.route = "movie" if target == "radarr" else "series"
        self.library = []
        self.calls = []
        self.fail_write = False
        self.wrong_lookup = False
        self.excluded = False

    def __call__(self, method, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        assert kwargs["headers"] == {"X-Api-Key": "private-arr-key"}
        route = url.split("/api/v3/")[1]
        self.calls.append((method, route, kwargs))
        if route == "qualityprofile":
            return reply([{"id": 7, "name": "HD"}])
        if route == "rootfolder":
            return reply([{"path": "/library"}])
        if route.endswith("/lookup"):
            ident = int(kwargs["params"]["term"].split(":")[1])
            return reply(
                [
                    {
                        self.id_key: ident + int(self.wrong_lookup),
                        "title": "Target metadata",
                        "images": [],
                        "seasons": [{"seasonNumber": 1}],
                        "isExcluded": self.excluded,
                    }
                ]
            )
        if route == self.route and method == "GET":
            return reply(list(self.library))
        if route == self.route and method == "POST":
            if self.fail_write:
                raise requests.Timeout("private-arr-key SECRET_URL")
            item = {**kwargs["json"], "id": len(self.library) + 1}
            self.library.append(item)
            return reply(item, 201)
        raise AssertionError((method, route))

    def client(self):
        return ArrImport({self.target: {"url": "http://arr.invalid/base", "key": "private-arr-key"}}, request_fn=self)


def settings(**changes):
    return {"quality_profile_id": 7, "root_folder": "/library", "monitored": False, "search": False, **changes}


@pytest.mark.parametrize("target", ["radarr", "sonarr"])
def test_add_and_repeat_skip_existing_without_changing_settings(target):
    fake = FakeArr(target)
    client = fake.client()
    items = [{"id": 42, "title": "Preview title"}]
    assert client.add(target, items, settings())[0]["status"] == "added"
    added = fake.library[0]
    assert added["title"] == "Target metadata"
    assert added["qualityProfileId"] == 7 and added["rootFolderPath"] == "/library"
    assert added["monitored"] is False
    assert added["addOptions"].get("searchForMovie", added["addOptions"].get("searchForMissingEpisodes")) is False
    assert client.add(target, items, settings(monitored=True, search=True))[0]["status"] == "existing"
    assert len([c for c in fake.calls if c[0] == "POST"]) == 1
    assert fake.library[0]["monitored"] is False


def test_timeout_never_retries_and_stops_batch():
    fake = FakeArr()
    fake.fail_write = True
    client = fake.client()
    items = [{"id": 42, "title": "First"}, {"id": 43, "title": "Second"}]
    result = client.add("radarr", items, settings())
    assert [x["status"] for x in result] == ["unknown", "not_attempted"]
    assert "private-arr-key" not in json.dumps(result) and "SECRET_URL" not in json.dumps(result)
    result = client.add("radarr", items, settings())
    assert result[0]["status"] == "unknown"
    assert len([c for c in fake.calls if c[0] == "POST"]) == 1


@pytest.mark.parametrize(
    "config",
    [
        {"url": "file:///tmp/test", "key": "secret"},
        {"url": "http://user:secret@arr", "key": "secret"},
        {"url": "http://arr?key=secret", "key": "secret"},
        {"url": "http://arr", "key": ""},
    ],
)
def test_invalid_importer_configuration(config):
    with pytest.raises(BridgeError):
        ArrImport({"radarr": config}).config("radarr")


@pytest.mark.parametrize(
    "changes",
    [
        {"quality_profile_id": 99},
        {"root_folder": "/other"},
        {"search": True},
        {"monitored": "true"},
        {"url": "http://evil"},
        {"series_type": "invalid"},
    ],
)
def test_invalid_settings_do_not_write(changes):
    fake = FakeArr()
    with pytest.raises(BridgeError):
        fake.client().add("radarr", [{"id": 42, "title": "First"}], settings(**changes))
    assert not [c for c in fake.calls if c[0] == "POST"]


def test_wrong_lookup_id_and_target_exclusion_do_not_write():
    for flag, status in [("wrong_lookup", "failed"), ("excluded", "excluded")]:
        fake = FakeArr()
        setattr(fake, flag, True)
        assert fake.client().add("radarr", [{"id": 42, "title": "First"}], settings())[0]["status"] == status
        assert not [c for c in fake.calls if c[0] == "POST"]


def test_http_add_requires_header_and_preview_and_never_accepts_metadata(tmp_path, token):
    fake = FakeArr()
    client = bridge.create_app(tmp_path, token, SECRET, get=lambda *a, **kw: reply([title()]), arr=fake.client()).test_client()
    headers = {"X-Bridge-Key": SECRET}
    result = client.post("/api/preview/cards", headers=headers, json=feed()).json
    body = {"preview_id": result["preview_id"], "ids": [42], "settings": settings()}
    assert client.get("/api/importers", headers=headers).json == {"radarr": True, "sonarr": False}
    assert client.post("/api/importers/radarr/add?key=" + SECRET, json=body).status_code == 403
    assert client.post("/api/importers/radarr/add", headers=headers, json={**body, "ids": [99]}).status_code == 400
    assert client.post("/api/importers/sonarr/add", headers=headers, json=body).status_code == 400
    assert client.post("/api/importers/radarr/add", headers=headers, json={**body, "url": "http://evil"}).status_code == 400
    assert not fake.library
    response = client.post("/api/importers/radarr/add", headers=headers, json=body)
    assert response.status_code == 200 and response.json["results"][0]["status"] == "added"
    assert "private-arr-key" not in response.text


def test_explicit_rejection_is_failed_and_can_be_corrected():
    fake = FakeArr()

    def reject(method, url, **kwargs):
        if method == "POST":
            return reply({"message": "private upstream details"}, 400)
        return fake(method, url, **kwargs)

    client = ArrImport({"radarr": {"url": "http://arr.invalid", "key": "private-arr-key"}}, request_fn=reject)
    result = client.add("radarr", [{"id": 42, "title": "First"}], settings())
    assert result[0]["status"] == "failed"
    assert not client.uncertain
    assert "private upstream details" not in json.dumps(result)
    client.request_fn = fake
    assert client.add("radarr", [{"id": 42, "title": "First"}], settings())[0]["status"] == "added"
