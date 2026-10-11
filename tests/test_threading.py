from __future__ import annotations

from unittest.mock import patch

from tests.conftest import factory


def test_threading():
    # Keep mocks installed through queue shutdown; the final flush runs there.
    with (
        patch("trakt.sync.add_to_collection", return_value={}) as add_collection,
        patch("trakt.sync.remove_from_collection", return_value={}) as remove_collection,
        patch("trakt.sync.add_to_watchlist", return_value={}) as add_watchlist,
        patch("trakt.sync.remove_from_watchlist", return_value={}) as remove_watchlist,
    ):
        queue = factory.queue
        queue.add_to_collection(("movie", {"id": 1}))
        queue.add_to_collection(("movie", {"id": 2}))
        queue.remove_from_collection(("movie", {"id": 2}))
        queue.add_to_watchlist(("movie", {"id": 2}))
        queue.remove_from_watchlist(("movie", {"id": 2}))
        queue.close()
        add_collection.assert_called_once_with({"movie": [{"id": 1}, {"id": 2}]})
        remove_collection.assert_called_once_with({"movie": [{"id": 2}]})
        add_watchlist.assert_called_once_with({"movie": [{"id": 2}]})
        remove_watchlist.assert_called_once_with({"movie": [{"id": 2}]})
