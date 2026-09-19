"""Offline browser-to-HTTP smoke test, run inside the all-in-one image."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from list_bridge import create_app
from playwright.sync_api import expect, sync_playwright
from waitress import create_server


def main():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        token = root / 'token.json'
        token.write_text(json.dumps({'access_token': 'offline-token', 'client_id': 'offline-client'}))

        def get(url, **kwargs):
            key = 'show' if '/shows/' in url else 'movie'
            data = [{key: {'title': 'Offline fixture', 'ids': {'tmdb': 42, 'tvdb': 123}}}]
            return SimpleNamespace(status_code=200, headers={}, json=lambda: data)

        secret = 'offline-test-secret-at-least-24-chars'
        app = create_app(root, token, secret, get=get)
        server = create_server(app, host='127.0.0.1', port=0)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1100, 'height': 1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.effective_port}')
                page.get_by_label('Bridge key', exact=True).fill(secret)
                page.get_by_role('button', name='Connect', exact=True).click()
                expect(page.locator('#builder')).to_be_visible()
                for media, name, identifier in [('movies', 'weekly-movies', 'id'), ('shows', 'weekly-shows', 'tvdbId')]:
                    page.locator('#media').select_option(media)
                    page.locator('#name').fill(name)
                    page.locator('#source').select_option('recommended')
                    page.get_by_role('button', name='Preview titles').click()
                    expect(page.locator('#results')).to_contain_text('Offline fixture')
                    expect(page.locator('#results')).to_contain_text(identifier)
                    page.get_by_role('button', name='Save feed & get URL').click()
                    expect(page.locator('#urlBox')).to_be_visible()
                    url = page.locator('#url').input_value()
                    response = page.request.get(url)
                    assert response.status == 200
                    assert identifier in response.json()[0]
                    assert page.request.get(url.split('?')[0]).status == 403
                page.screenshot(path='/tmp/list-bridge-ui.png', full_page=True)
                page.reload()
                page.get_by_label('Bridge key', exact=True).fill(secret)
                page.get_by_role('button', name='Connect', exact=True).click()
                expect(page.locator('#saved option')).to_have_count(3)
                page.locator('#saved').select_option('weekly-movies')
                expect(page.locator('#name')).to_have_value('weekly-movies')
                assert not errors, errors
                browser.close()
            print('PASS: feed builder, previews, persisted feeds, Radarr/Sonarr URLs and access control')
        finally:
            server.close()


if __name__ == '__main__':
    main()
