"""Offline browser tests for posters, local curation and direct importer adds."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from list_bridge import create_app
from playwright.sync_api import expect, sync_playwright
from waitress import create_server

from plextraktsync.trakt.ArrImport import ArrImport
from plextraktsync.trakt.PosterLookup import PosterLookup


def main():
    def response(data, status=200):
        return SimpleNamespace(status_code=status, headers={}, json=lambda: data)

    with TemporaryDirectory() as directory:
        root = Path(directory)
        token = root / "token.json"
        token.write_text(json.dumps({"access_token": "offline-token", "client_id": "offline-client"}))

        def trakt(url, **kwargs):
            kind = "show" if "/show" in url else "movie"
            return response(
                [
                    {
                        kind: {
                            "title": f"Fixture {i}",
                            "year": year,
                            "language": language,
                            "rating": 8,
                            "images": {"poster": [] if i == 43 else ["https://walter-r2.trakt.tv/broken.png" if i == 44 else "https://walter-r2.trakt.tv/test-poster.png"]},
                            "ids": {"tmdb": i, "tvdb": i, "trakt": i, "slug": f"fixture-{i}"},
                        }
                    }
                    for i, year, language in [(42, 2024, "en"), (43, 2010, "fr"), (44, 2025, "en")]
                ]
            )

        libraries = {"radarr": [], "sonarr": []}
        writes = []

        def arr_request(method, url, **kwargs):
            target = "radarr" if "radarr.invalid" in url else "sonarr"
            id_key = "tmdbId" if target == "radarr" else "tvdbId"
            route = url.split("/api/v3/")[1]
            if route == "qualityprofile":
                return response([{"id": 1, "name": "HD"}])
            if route == "rootfolder":
                return response([{"path": "/library"}])
            if route.endswith("/lookup"):
                return response([{id_key: int(kwargs["params"]["term"].split(":")[1]), "title": "Resolved title", "images": []}])
            if method == "GET":
                return response(libraries[target])
            item = {**kwargs["json"], "id": len(libraries[target]) + 1}
            writes.append((target, item))
            libraries[target].append(item)
            return response(item, 201)

        arr = ArrImport({target: {"url": f"http://{target}.invalid", "key": "fixture-key"} for target in libraries}, request_fn=arr_request)
        secret = "offline-test-secret-at-least-24-chars"
        posters = PosterLookup(get=lambda *a, **kw: response({"poster_path": "/fallback.jpg"}), env={"TMDB_API_KEY": "fixture-only"})
        app = create_app(root, token, secret, get=trakt, arr=arr, posters=posters)
        server = create_server(app, host="127.0.0.1", port=0)
        threading.Thread(target=server.run, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1200, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await_image = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="300">'
                               '<rect width="200" height="300" fill="#285277"/><text x="20" y="150" fill="white">Test poster</text></svg>')
                page.route("https://walter-r2.trakt.tv/**", lambda route: route.fulfill(content_type="image/svg+xml", body=await_image))
                page.route("https://walter-r2.trakt.tv/broken.png", lambda route: route.fulfill(status=404))
                page.route("https://image.tmdb.org/**", lambda route: route.fulfill(content_type="image/svg+xml", body=await_image))
                page.goto(f"http://127.0.0.1:{server.effective_port}")
                page.get_by_label("Bridge key", exact=True).fill(secret)
                page.get_by_role("button", name="Connect", exact=True).click()
                expect(page.locator("#builder")).to_be_visible()
                # Search without naming a feed; curation is independent of upstream filters.
                page.locator("#source").select_option("search")
                page.locator("#query").fill("space")
                page.get_by_role("button", name="Preview titles", exact=True).click()
                expect(page.locator(".card")).to_have_count(3)
                for card in page.locator(".card").all():
                    card.scroll_into_view_if_needed()
                    expect(card.locator("img")).to_be_visible()
                expect(page.locator('.card[data-id="43"] img')).to_have_attribute("src", "https://image.tmdb.org/t/p/w500/fallback.jpg")
                expect(page.locator('.card[data-id="44"] img')).to_have_attribute("src", "https://image.tmdb.org/t/p/w500/fallback.jpg")
                expect(page.locator(".card").first.get_by_role("link", name="Trakt", exact=True)).to_have_attribute("href", "https://trakt.tv/movies/fixture-42")
                page.locator("#localFilters summary").click()
                page.locator("#local_languages").fill("en")
                page.locator("#local_year_min").fill("2020")
                expect(page.locator("#visualPreview")).to_be_hidden()
                page.get_by_role("button", name="Preview titles", exact=True).click()
                expect(page.locator(".card")).to_have_count(2)
                # Explicit per-title exclusion survives saving and importer output.
                page.locator(".card").first.get_by_role("button", name="Exclude", exact=True).click()
                expect(page.locator("#excludedIds")).to_have_value("42")
                page.locator("#name").fill("curated")
                page.get_by_role("button", name="Preview titles", exact=True).click()
                expect(page.locator(".card")).to_have_count(1)
                page.get_by_role("button", name="Save feed & get URL").click()
                expect(page.locator("#urlBox")).to_be_visible()
                assert page.request.get(page.locator("#url").input_value()).json() == [{"id": 44, "title": "Fixture 44"}]
                # Movie add: target options, review, cancel, then confirmed single add.
                page.locator("#directImport summary").click()
                page.get_by_role("button", name="Load target settings").click()
                expect(page.locator("#importSettings")).to_be_visible()
                page.locator("#qualityProfile").select_option("1")
                page.locator("#rootFolder").select_option("/library")
                page.locator(".card").get_by_role("button", name="Add…", exact=True).click()
                expect(page.locator("#addReview")).to_be_visible()
                page.get_by_role("button", name="Cancel", exact=True).click()
                assert not writes
                page.locator(".card").get_by_role("button", name="Add…", exact=True).click()
                page.get_by_role("button", name="Add these titles", exact=True).click()
                expect(page.locator("#importResults")).to_contain_text("added")
                assert len(writes) == 1 and writes[0][1]["addOptions"]["searchForMovie"] is False
                # Sonarr whole preview: a new selection, no local exclusions from movies.
                page.locator("#saved").select_option("")
                page.locator("#media").select_option("shows")
                page.locator("#source").select_option("search")
                page.locator("#query").fill("space")
                page.get_by_role("button", name="Preview titles", exact=True).click()
                expect(page.locator(".card")).to_have_count(3)
                if not page.locator("#directImport").evaluate("(e)=>e.open"):
                    page.locator("#directImport summary").click()
                page.get_by_role("button", name="Load target settings").click()
                expect(page.locator("#importSettings")).to_be_visible()
                page.locator("#qualityProfile").select_option("1")
                page.locator("#rootFolder").select_option("/library")
                page.get_by_role("button", name="Review whole preview").click()
                expect(page.locator("#reviewTitles li")).to_have_count(3)
                page.get_by_role("button", name="Add these titles", exact=True).click()
                expect(page.locator("#status")).to_contain_text("Finished 3 titles")
                assert len(writes) == 4
                assert all(not item["monitored"] for _, item in writes)
                assert all(item["addOptions"].get("searchForMissingEpisodes") is False for target, item in writes if target == "sonarr")
                page.screenshot(path="/tmp/visual-list-imports.png", full_page=True)
                assert not errors, errors
                browser.close()
            print("PASS: poster cards, links, unnamed search, local filters, exclusions, feed URL, reviewed individual/bulk adds")
        finally:
            server.close()


if __name__ == "__main__":
    main()
