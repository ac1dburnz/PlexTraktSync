from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from plextraktsync.trakt.ListSources import upstream

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
    assert upstream(feed())[0] == '/movies/recommended/weekly'
    assert upstream(feed(source='favorited', media='shows', period='monthly'))[0] == '/shows/favorited/monthly'
    assert upstream(feed(source='recommendations'))[0] == '/recommendations/movies'


def test_lists_and_watchlist_paths():
    assert upstream(feed(source='list', user='alice', list='later'))[0] == '/users/alice/lists/later/items/movies'
    assert upstream(feed(source='watchlist', media='shows'))[0] == '/users/me/watchlist/shows'


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


def show(identifier=101, network='Netflix', aired='2026-09-01T00:00:00Z'):
    return {'show': {'title': f'Show {identifier}', 'network': network, 'first_aired': aired, 'ids': {'tvdb': identifier}}}


def test_network_filter_keeps_fetching_and_deduplicates(token):
    responses = iter([
        reply([show(1, 'BBC'), show(2, None)], headers={'X-Pagination-Page-Count': '3'}),
        reply([show(3, 'Netflix'), show(3, 'Netflix')], headers={'X-Pagination-Page-Count': '3'}),
        reply([show(4, 'NETFLIX')], headers={'X-Pagination-Page-Count': '3'}),
    ])
    client = bridge.TraktFeeds(token, get=lambda *a, **kw: next(responses))
    result = client.fetch(feed(source='trending', media='shows', networks=' netflix ', limit=2))
    assert [x['tvdbId'] for x in result] == [3, 4]


def test_network_no_matches_is_valid_empty_but_bad_ids_are_not(token):
    client = bridge.TraktFeeds(token, get=lambda *a, **kw: reply([show(1, 'BBC')]))
    assert client.fetch(feed(source='trending', media='shows', networks='Netflix')) == []
    client = bridge.TraktFeeds(token, get=lambda *a, **kw: reply([{'show': {'title': 'Broken', 'network': 'BBC'}}]))
    with pytest.raises(bridge.BridgeError, match='identifiers'):
        client.fetch(feed(source='trending', media='shows', networks='Netflix'))


def test_calendar_chunks_cross_year_and_sort_event_dates(token, monkeypatch):
    from datetime import datetime, timezone

    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2026, 12, 31, tzinfo=timezone.utc)

    monkeypatch.setattr(bridge, 'datetime', Clock)
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return reply([{**show(1), 'first_aired': '2026-12-30T00:00:00Z'}])
        return reply([{**show(2), 'first_aired': '2027-01-31T00:00:00Z'}])

    result = bridge.TraktFeeds(token, get=get).fetch(feed(source='calendar_new', media='shows', start_offset=-1, days=33,
                                                        networks='Netflix', order='newest', limit=1))
    assert [x['tvdbId'] for x in result] == [2]
    assert calls == ['https://api.trakt.tv/calendars/all/shows/new/2026-12-30/31',
                     'https://api.trakt.tv/calendars/all/shows/new/2027-01-30/2']


def test_rolling_calendar_cache_changes_at_utc_midnight(token, monkeypatch):
    from datetime import datetime, timezone

    class Clock:
        day = 20

        @staticmethod
        def now(tz):
            return datetime(2026, 9, Clock.day, tzinfo=timezone.utc)

    monkeypatch.setattr(bridge, 'datetime', Clock)
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return reply([show()])

    client = bridge.TraktFeeds(token, get=get)
    f = feed(source='calendar_new', media='shows', start_offset=0, days=1)
    client.fetch(f)
    client.fetch(f)
    Clock.day += 1
    client.fetch(f)
    assert len(calls) == 2
    assert calls[0] != calls[1]


def test_sort_reads_later_pages_before_limit_and_puts_unknown_dates_last(token):
    responses = iter([
        reply([show(1, aired='2020-01-01'), show(3, aired=None)], headers={'X-Pagination-Page-Count': '2'}),
        reply([show(2, aired='2026-01-01')], headers={'X-Pagination-Page-Count': '2'}),
    ])
    client = bridge.TraktFeeds(token, get=lambda *a, **kw: next(responses))
    result = client.fetch(feed(source='popular', media='shows', order='newest', limit=2))
    assert [x['tvdbId'] for x in result] == [2, 1]


def test_exhaustive_sort_refuses_partial_scan(token):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return reply([movie()], headers={'X-Pagination-Page-Count': '11'})

    client = bridge.TraktFeeds(token, get=get)
    with pytest.raises(bridge.BridgeError, match='10 pages'):
        client.fetch(feed(order='title', limit=1))
    assert len(calls) == 10
    assert not client.cache


def test_calendar_failure_does_not_cache_first_window(token):
    responses = iter([reply([show()]), reply({}, 500)])
    client = bridge.TraktFeeds(token, get=lambda *a, **kw: next(responses))
    with pytest.raises(bridge.BridgeError):
        client.fetch(feed(source='calendar_new', media='shows', days=32))
    assert not client.cache


@pytest.mark.parametrize('role,expected', [('cast', [1]), ('crew', [2, 3]), ('directing', [2]), ('all', [1, 2, 3])])
def test_person_credits_flatten_and_deduplicate(token, role, expected):
    data = {'cast': [movie(1)], 'crew': {'directing': [movie(2)], 'writing': [movie(2), movie(3)]}}
    client = bridge.TraktFeeds(token, get=lambda *a, **kw: reply(data))
    result = client.fetch(feed(source='credits', person='example-person', credit=role))
    assert [x['id'] for x in result] == expected


@pytest.mark.parametrize('source', ['smart_list', 'calendar_media', 'calendar_hot'])
def test_mixed_feeds_never_import_other_media(token, source):
    data = [movie(), {'type': 'movie', 'movie': movie()['movie'], 'show': None}, show()]
    kwargs = {'list': 'my-list'} if source == 'smart_list' else {'days': 1}
    client = bridge.TraktFeeds(token, get=lambda *a, **kw: reply(data))
    result = client.fetch(feed(source=source, media='shows', **kwargs))
    assert result == [{'tvdbId': 101, 'title': 'Show 101'}]


def test_smart_list_only_other_media_is_empty_success(token):
    client = bridge.TraktFeeds(token, get=lambda *a, **kw: reply([movie()]))
    assert client.fetch(feed(source='smart_list', list='movies-list', media='shows')) == []


@pytest.mark.parametrize('changes', [
    {'source': 'calendar_new', 'media': 'movies'},
    {'source': 'streaming', 'period': 'all'},
    {'source': 'calendar_new', 'media': 'shows', 'days': 94},
    {'source': 'calendar_new', 'media': 'shows', 'start_offset': True},
    {'source': 'calendar_new', 'media': 'shows', 'filters': {'start_date': '2026-01-01'}},
    {'source': 'smart_list', 'list': 'test', 'filters': {'end_date': '2026-01-01'}},
    {'source': 'trending', 'networks': 'Netflix'},
    {'source': 'trending', 'media': 'shows', 'networks': 'Netflix,'},
    {'source': 'public_list', 'list': 'not-numeric'},
    {'source': 'credits', 'person': 'someone', 'credit': 'unknown'},
    {'source': 'search', 'query': ' '},
    {'source': 'search', 'query': 'query', 'ignore_watched': True},
    {'source': 'hidden', 'section': 'dropped'},
    {'source': 'ratings', 'rating': 11},
    {'source': 'trending', 'filters': {'watchnow': 'netflix'}},
    {'source': 'trending', 'filters': {'start_date': 'not-a-date'}},
    {'source': 'trending', 'filters': {'start_date': '2026-02-01', 'end_date': '2026-01-01'}},
    {'source': 'trending', 'order': []},
])
def test_new_options_reject_invalid_and_incompatible_values(changes):
    with pytest.raises(bridge.BridgeError):
        feed(**changes)


@pytest.mark.parametrize('source,media', [(k, media) for k, spec in bridge.SOURCES.items() for media in spec['media']])
def test_every_catalog_source_can_be_saved_previewed_and_imported(tmp_path, token, source, media):
    """Exercise the real HTTP service for every advertised source/media pair."""
    raw = {'name': 'catalog-test', 'source': source, 'media': media}
    examples = {'list': '123', 'person': 'example-person', 'seed': 'example-title', 'query': 'space', 'identifier': 'tt1234567'}
    raw.update({k: examples[k] for k in bridge.SOURCES[source]['fields'] if bridge.OPTIONS[k].get('required')})
    f = bridge.validate_feed(raw)
    seen = []

    def get(url, **kwargs):
        seen.append((url, kwargs['params']))
        assert url.startswith('https://api.trakt.tv/') and '{' not in url
        assert kwargs['allow_redirects'] is False
        data = [movie()] if media == 'movies' else [show()]
        return reply({'cast': data, 'crew': {}} if source == 'credits' else data)

    client = bridge.create_app(tmp_path, token, SECRET, get=get).test_client()
    headers = {'X-Bridge-Key': SECRET}
    assert client.post('/api/feeds', headers=headers, json=f).status_code == 201
    preview = client.post('/api/preview', headers=headers, json=f)
    assert preview.status_code == 200, preview.json
    target = 'radarr' if media == 'movies' else 'sonarr'
    result = client.get(f'/{target}/catalog-test.json', query_string={'key': SECRET})
    assert result.status_code == 200
    assert result.json == preview.json
    assert ('id' if media == 'movies' else 'tvdbId') in result.json[0]
    assert seen


def test_discovery_authenticated_and_does_not_expose_profiles(tmp_path, token):
    data = [{'list': {'name': 'Example', 'ids': {'trakt': 123}, 'user': {'private_field': 'SECRET'}}}]
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return reply(data, headers={'X-Pagination-Page-Count': '3'})

    client = bridge.create_app(tmp_path, token, SECRET, get=get).test_client()
    assert client.get('/api/browse?kind=popular_lists').status_code == 403
    result = client.get('/api/browse?kind=popular_lists', headers={'X-Bridge-Key': SECRET})
    assert result.json == {'items': [{'name': 'Example', 'id': 123}], 'page': 1, 'pages': 3}
    assert 'SECRET' not in result.text
    client.get('/api/browse?kind=popular_lists', headers={'X-Bridge-Key': SECRET})
    assert len(calls) == 1
    for url in ['/api/browse?kind=https://evil', '/api/browse?kind=my_lists&user=../settings',
                '/api/browse?kind=people', '/api/browse?kind=networks&page=bad']:
        assert client.get(url, headers={'X-Bridge-Key': SECRET}).status_code == 400


def test_metadata_discovery_country_grouped_certifications(token):
    client = bridge.TraktFeeds(token, get=lambda *a, **kw: reply({'us': [{'name': 'PG', 'slug': 'pg'}]}))
    assert client.browse('certifications')['items'] == [{'name': 'PG', 'slug': 'pg', 'country': 'us'}]


def test_deadline_failure_does_not_cache(token):
    now = [0]

    def get(*a, **kw):
        now[0] = 61
        return reply([movie()])

    client = bridge.TraktFeeds(token, get=get, clock=lambda: now[0])
    with pytest.raises(bridge.BridgeError, match='timed out'):
        client.fetch(feed())
    assert not client.cache


def test_network_search_reads_all_pages_and_reuses_catalog(token):
    calls = []

    def get(url, **kwargs):
        page = kwargs['params']['page']
        calls.append(page)
        data = [{'name': ''}, {'name': ' ABC ', 'country': 'us'}] if page == 1 else [{'name': 'Netflix', 'country': 'us'}]
        return reply(data, headers={'X-Pagination-Page-Count': '2'})

    client = bridge.TraktFeeds(token, get=get)
    assert client.browse('networks', query='NETFLIX')['items'] == [{'name': 'Netflix', 'country': 'us'}]
    assert client.browse('networks', query='abc')['items'] == [{'name': 'ABC', 'country': 'us'}]
    assert client.browse('networks', query='missing')['total'] == 0
    assert calls == [1, 2]


def test_network_catalog_rejects_partial_results(token):
    client = bridge.TraktFeeds(token, get=lambda *a, **kw: reply([{'name': 'ABC'}], headers={'X-Pagination-Page-Count': '11'}))
    with pytest.raises(bridge.BridgeError, match='no partial catalog'):
        client.browse('networks')


def test_poster_endpoint_authenticated_and_does_not_expose_keys(tmp_path, token):
    from plextraktsync.trakt.PosterLookup import PosterLookup
    posters = PosterLookup(get=lambda *a, **kw: reply({'poster_path': '/poster.jpg'}), env={'TMDB_API_KEY': 'never-output'})
    client = bridge.create_app(tmp_path, token, SECRET, posters=posters).test_client()
    assert client.get('/api/poster/movies/1').status_code == 403
    result = client.get('/api/poster/movies/1', headers={'X-Bridge-Key': SECRET})
    assert result.json == {'poster': 'https://image.tmdb.org/t/p/w500/poster.jpg'}
    assert 'never-output' not in result.text
