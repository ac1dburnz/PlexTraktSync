from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

spec = importlib.util.spec_from_file_location('list_bridge', Path(__file__).parents[1] / 'browser-helper/list_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
SECRET = 'test-secret-at-least-24-characters'


def reply(data, status=200, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {}, json=lambda: data)


def movie(identifier=42):
    return {'movie': {'title': 'Example', 'ids': {'tmdb': identifier, 'imdb': 'tt0000042'}}}


@pytest.fixture
def token(tmp_path):
    path = tmp_path / 'token.json'
    path.write_text(json.dumps({'access_token': 'token-one', 'client_id': 'client'}))
    return path


def feed(**overrides):
    return bridge.validate_feed({'name': 'weekly', 'media': 'movies', 'source': 'recommended', **overrides})


def test_correct_import_formats_and_deduplication():
    assert bridge.convert([movie(), movie(), {'title': 'No ID'}], 'movies') == [{'id': 42, 'title': 'Example'}]
    assert bridge.convert([{'show': {'title': 'Series', 'ids': {'tvdb': 123}}}], 'shows') == [{'tvdbId': 123, 'title': 'Series'}]


@pytest.mark.parametrize('changes', [
    {'name': '../secret'}, {'limit': True}, {'limit': 1001}, {'period': 'anything'},
    {'source': 'boxoffice', 'media': 'shows'}, {'source': 'watchlist', 'user': '../../foo'},
    {'filters': {'url': 'https://example.com'}}, {'source': 'recommendations', 'limit': 101},
    {'source': 'popular', 'watch_window': 7}, {'source': 'list', 'list': 'x/y'},
])
def test_invalid_definitions(changes):
    with pytest.raises(bridge.BridgeError):
        feed(**changes)


def test_legacy_and_current_recommendations_are_distinct():
    assert bridge.upstream(feed())[0] == '/movies/recommended/weekly'
    assert bridge.upstream(feed(source='favorited', media='shows', period='monthly'))[0] == '/shows/favorited/monthly'
    assert bridge.upstream(feed(source='recommendations'))[0] == '/recommendations/movies'


def test_lists_and_watchlist_paths():
    assert bridge.upstream(feed(source='list', user='alice', list='later'))[0] == '/users/alice/lists/later/items/movies'
    assert bridge.upstream(feed(source='watchlist', media='shows'))[0] == '/users/me/watchlist/shows'


def test_pagination_cache_and_token_rotation(token):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        page = kwargs['params']['page']
        return reply([movie(page)], headers={'X-Pagination-Page-Count': '2'})

    client = bridge.TraktFeeds(token, get=get)
    assert [x['id'] for x in client.fetch(feed())] == [1, 2]
    client.fetch(feed())
    assert len(calls) == 2
    token.write_text(json.dumps({'access_token': 'token-two', 'client_id': 'client'}))
    client.fetch(feed())
    assert len(calls) == 4
    prepared = requests.Request('GET', calls[-1][0]).prepare()
    calls[-1][1]['auth'](prepared)
    assert prepared.headers['Authorization'] == 'Bearer token-two'
    assert calls[-1][1]['allow_redirects'] is False


def test_failed_second_page_never_publishes_partial_list(token):
    responses = iter([reply([movie()], headers={'X-Pagination-Page-Count': '2'}), reply({}, 500)])
    client = bridge.TraktFeeds(token, get=lambda *a, **kw: next(responses))
    with pytest.raises(bridge.BridgeError):
        client.fetch(feed())
    assert not client.cache


@pytest.mark.parametrize('response', [
    reply({}, 401), reply({}, 403), reply({}, 302), reply({}, 500), reply({'bad': 'shape'}), reply([{'title': 'No IDs'}]),
])
def test_upstream_errors_are_not_empty_success(token, tmp_path, response):
    app = bridge.create_app(tmp_path, token, SECRET, get=lambda *a, **kw: response)
    client = app.test_client()
    headers = {'X-Bridge-Key': SECRET}
    client.post('/api/feeds', headers=headers, json=feed())
    result = client.get('/radarr/weekly.json', query_string={'key': SECRET})
    assert result.status_code >= 500
    assert 'error' in result.json


def test_rate_limit_backoff(token):
    calls = []

    def get(*args, **kwargs):
        calls.append(1)
        return reply({}, 429, {'Retry-After': '120'})

    client = bridge.TraktFeeds(token, get=get, clock=lambda: 10)
    for _ in range(2):
        with pytest.raises(bridge.BridgeError):
            client.fetch(feed())
    assert len(calls) == 1


def test_persistent_feeds_authentication_and_import_urls(tmp_path, token):
    app = bridge.create_app(tmp_path, token, SECRET, get=lambda *a, **kw: reply([movie()]))
    client = app.test_client()
    assert client.get('/').status_code == 200
    assert client.get('/api/feeds').status_code == 403
    assert client.post('/api/feeds?key=' + SECRET, json=feed()).status_code == 403
    headers = {'X-Bridge-Key': SECRET}
    assert client.post('/api/feeds', headers=headers, json=feed()).status_code == 201
    assert (tmp_path / 'list-feeds.json').stat().st_mode & 0o777 == 0o600
    # New process/application uses the persisted definition.
    client = bridge.create_app(tmp_path, token, SECRET, get=lambda *a, **kw: reply([movie()])).test_client()
    assert client.get('/radarr/weekly.json', query_string={'key': SECRET}).json == [{'id': 42, 'title': 'Example'}]
    assert client.get('/sonarr/weekly.json', query_string={'key': SECRET}).status_code == 404
    assert client.get('/api/feeds', headers=headers).json['weekly']['source'] == 'recommended'


def test_missing_token_even_when_cache_exists(token):
    client = bridge.TraktFeeds(token, get=lambda *a, **kw: reply([movie()]))
    client.fetch(feed())
    token.unlink()
    with pytest.raises(bridge.BridgeError, match='login required'):
        client.fetch(feed())


def test_valid_empty_upstream_and_preview(tmp_path, token):
    client = bridge.create_app(tmp_path, token, SECRET, get=lambda *a, **kw: reply([])).test_client()
    response = client.post('/api/preview', headers={'X-Bridge-Key': SECRET}, json=feed())
    assert response.status_code == 200
    assert response.json == []


def test_secret_required(tmp_path, token):
    with pytest.raises(ValueError):
        bridge.create_app(tmp_path, token, '')


def test_network_exception_sanitized(token):
    def fail(*args, **kwargs):
        raise requests.ConnectionError('SECRET upstream details')

    with pytest.raises(bridge.BridgeError) as err:
        bridge.TraktFeeds(token, get=fail).fetch(feed())
    assert 'SECRET' not in str(err.value)
