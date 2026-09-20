from __future__ import annotations

from unittest.mock import Mock

import requests

from plextraktsync.trakt.PosterLookup import PosterLookup


def test_optional_and_cached_lookup():
    get = Mock(return_value=Mock(status_code=200, json=lambda: {'poster_path': '/abc.jpg'}))
    assert PosterLookup(get=get, env={}).lookup('movies', 1) is None
    get.assert_not_called()
    lookup = PosterLookup(get=get, env={'TMDB_API_KEY': 'private'})
    assert lookup.lookup('movies', 1) == 'https://image.tmdb.org/t/p/w500/abc.jpg'
    assert lookup.lookup('movies', 1) == 'https://image.tmdb.org/t/p/w500/abc.jpg'
    assert get.call_count == 1
    assert get.call_args.kwargs['params'] == {'api_key': 'private'}
    assert not get.call_args.kwargs['allow_redirects']


def test_token_precedence_and_tv_endpoint():
    get = Mock(return_value=Mock(status_code=200, json=lambda: {'poster_path': None}))
    lookup = PosterLookup(get=get, env={'TMDB_API_KEY': 'key', 'TMDB_READ_ACCESS_TOKEN': 'token'})
    assert lookup.lookup('shows', 2) is None
    assert get.call_args.args[0].endswith('/tv/2')
    assert get.call_args.kwargs['headers'] == {'Authorization': 'Bearer token'}
    assert get.call_args.kwargs['params'] == {}


def test_failure_redacted_and_cache_expires():
    now = [0]
    get = Mock(side_effect=requests.Timeout('private key in URL'))
    lookup = PosterLookup(get=get, env={'TMDB_API_KEY': 'private'}, clock=lambda: now[0])
    assert lookup.lookup('movies', 1) is None
    assert lookup.lookup('movies', 1) is None
    assert get.call_count == 1
    now[0] = 301
    assert lookup.lookup('movies', 1) is None
    assert get.call_count == 2


def test_rejects_external_poster_path():
    get = Mock(return_value=Mock(status_code=200, json=lambda: {'poster_path': '//evil.example/a.jpg'}))
    lookup = PosterLookup(get=get, env={'TMDB_API_KEY': 'private'})
    assert lookup.lookup('movies', 1) is None
    assert lookup.lookup('wrong', 1) is None
    assert get.call_count == 1
