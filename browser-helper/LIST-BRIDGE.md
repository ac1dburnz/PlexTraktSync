# Trakt list bridge (all-in-one image)

Create saved movie and TV feeds that Radarr/Sonarr can import over HTTP. The bridge shares the browser-token file with PlexTraktSync. It never creates or modifies a list on Trakt, and does not need another OAuth application or login.

See [Visual previews and direct adds](VISUAL-IMPORTS.md) for poster cards, title links, local filters/exclusions, and optional individual/bulk Radarr/Sonarr additions.

## Enable in TrueNAS / Compose

Add these settings to your existing **all-in-one** service, keeping its existing `/app/config` and `/browser` volumes and network:

```yaml
environment:
  LIST_BRIDGE_ENABLED: "true"
  LIST_BRIDGE_SECRET: "REPLACE_WITH_A_RANDOM_SECRET_AT_LEAST_24_CHARACTERS"
ports:
  - "8090:8090"
```

Generate a secret with `openssl rand -hex 32`. Put it in your private Compose configuration. Do not commit it. Merge the environment/ports entries into the existing mappings; do not duplicate the YAML keys. Retain port 6080 if you use the browser login interface. For a container with its own LAN IP, access port 8090 on that IP; Docker port publication also makes it available through the host where the network driver supports it.

See [`../deploy/truenas-lists.yml`](../deploy/truenas-lists.yml) for a complete example using your existing network and config volume. This feature must be built/published before an existing `all-in-one` tag contains it.

Open **http://192.168.2.98:8090/** (or your container/host address). Enter the bridge secret, then:

1. Choose Movies or TV and a Trakt source.
2. Enter a feed name, period/user/list/seed options as applicable, result limit and optional filters.
3. Click **Preview titles**. This performs a real authenticated fetch and reports any Trakt rejection.
4. Click **Save feed & get URL**.
5. In Radarr, add **Radarr List**, and paste the URL into its URL field. In Sonarr, add **Custom List**, and paste the URL. Select the normal destination, quality and monitoring settings in that application, then test/save.

Example URL shapes (the UI generates the actual URLs):

```text
http://192.168.2.98:8090/radarr/weekly-movies.json?key=YOUR_BRIDGE_SECRET
http://192.168.2.98:8090/sonarr/weekly-shows.json?key=YOUR_BRIDGE_SECRET
```

Feed definitions persist in `/app/config/list-feeds.json`. Selecting a saved feed loads its settings; saving with the same name replaces the definition without changing its URL. Changing its media type changes which importer route is valid. Changing the bridge secret requires updating importer URLs. The key is held in page memory, not browser local storage.

## Sources and the “Recommended by” distinction

The expanded builder groups **45 feed types** and provides list/person discovery, rolling release calendars, network selection and output sorting. See [the API coverage audit](TRAKT-API-COVERAGE.md) for all sources, limitations and live verification.

For Netflix premieres, click **Preset: Netflix new series**, preview, then save. It selects the Netflix network, a rolling 60-day UTC window starting 30 days ago, and newest-first order. Network metadata is not country-specific streaming availability. The discovery panel can find user/public/smart lists and people; click **Use** to populate the source fields. Network suggestions load one page; the discovery panel supports paging through network names.


| UI source | Trakt request |
| --- | --- |
| Recommended by period (legacy) | `/{movies|shows}/recommended/{period}` |
| Most favorited by period | `/{movies|shows}/favorited/{period}` |
| Personal recommendations | `/recommendations/{movies|shows}` |
| Social recommendations | `/social_recommendations/{movies|shows}` |
| Trending / Popular / Anticipated | `/{movies|shows}/{source}` |
| Most watched / played / collected | `/{movies|shows}/{source}/{period}` |
| Box office (movies only) | `/movies/boxoffice` |
| User watchlist / watched / collection | `/users/{user}/{source}/{movies|shows}` |
| Named list | `/users/{user}/lists/{list}/items/{movies|shows}` |
| Related titles | `/{movies|shows}/{seed}/related` |

“Recommended by” in Radarr currently uses the legacy `/movies/recommended/{period}` endpoint. Trakt's current API contract documents **favorited** instead. The bridge deliberately exposes both, with distinct labels: it does **not** claim they are identical, or silently substitute one for the other. A browser token cannot guarantee a removed or restricted endpoint becomes available. Preview your chosen source; if legacy Recommended is unavailable, explicitly select Most favorited if that is the feed you want.

Personal recommendations are a third, distinct feed. Website-only recommendations and account/VIP-limited sources are not automatically supported simply because the browser is logged in. Requests use the public `api.trakt.tv` host with the existing browser credentials; there is no HTML scraping or arbitrary upstream-URL option.

Periods offered: daily, weekly, monthly, yearly, all. Trakt can reject a particular period or filter for an endpoint. Optional filters use Trakt values (e.g. years `2020-2026`, genres `science-fiction,drama`, ratings `70-100`, languages `en`). Personal/social recommendations offer collection/watchlist exclusions and a watch window; their result limit is at most 100. Other feeds allow 1–1000 titles, bounded by 10 upstream requests and a 60-second deadline. Streaming rankings offer daily/weekly/monthly only. Calendar windows cover 1–93 days and are split into requests of at most 31 days. Changing UTC date invalidates rolling-window cache entries. Local sorting scans the scope before applying the title limit; a scope that exceeds the request budget fails rather than returning a misleading partial ranking. Missing/duplicate TMDB or TVDB IDs are omitted; fewer results than requested are possible.

## Formats, caching and failures

- Radarr payload: `[{"id": 42, "title": "Movie"}]` where `id` is **TMDB**.
- Sonarr payload: `[{"tvdbId": 123, "title": "Series"}]`.
- Default cache: 3600 seconds, configurable with `LIST_BRIDGE_CACHE_SECONDS`.
- Cache is separated by feed definition and token fingerprint. Token rotation automatically invalidates old cache entries. Every upstream request rereads the token file.
- Rate-limit responses respect numeric Retry-After (bounded to 1–3600 seconds). Other upstream failures return non-200 errors, not empty successful lists or partial results.
- Empty success is also valid when network/media filtering removes every usable title. A nonempty selected-media response with no usable identifiers is an error. Missing TVDB IDs can reduce Sonarr results.
- Cache is in memory and resets with the container; feed definitions persist.
- Importers decide when to poll; changing the bridge cache does not override their refresh schedule.
- Existing browser helper remains responsible for authentication status and Slack login-expiry alerts. The bridge neither refreshes tokens itself nor sends duplicate Slack messages.

## Operations and access

`LIST_BRIDGE_ENABLED` defaults to `false`. `LIST_BRIDGE_PORT` defaults to `8090`. The service starts before browser authentication completes, so you can configure feeds while waiting to log in. It is supervised with the other all-in-one processes; exiting causes the container to exit and its restart policy to apply.

The UI shell and `/healthz` are public; health only reports that the bridge process is responding, **not** that Trakt login works. Feed/catalog/preview endpoints require the bridge key. The API supports `X-Bridge-Key`; saved importer GET URLs also accept `?key=` because importers cannot necessarily set a custom header. Writes require the header. Preview requires POST with the header and a JSON feed definition.

Use this on a trusted LAN or behind an authenticated HTTPS reverse proxy. The key grants both feed reads and feed configuration access. Import URLs contain the key; keep them out of public logs and screenshots. Waitress does not emit access logs; if you add a proxy, redact query strings there. The bridge never returns Trakt credentials to a client.

## Validation

Run backend tests:

```sh
python -m pip install -r tests/requirements.txt
pytest tests/test_list_bridge.py
```

Build and test the complete image without upstream credentials:

```sh
docker build -f Dockerfile.all-in-one -t plextraktsync:list-bridge .
docker run --rm --network none --entrypoint python \
  -e PYTHONPATH=/app:/helper plextraktsync:list-bridge /helper/smoke_list_bridge.py
```

The Chromium smoke test creates movie/TV feeds in the real UI, previews fixture responses through HTTP, fetches both importer URLs, checks authentication, and reloads saved definitions. This proves the bridge wiring, not current availability of any particular live Trakt endpoint. Test the actual URL in your Radarr/Sonarr version before enabling automated imports.

Upstream format references:
- [Radarr parser](https://github.com/Radarr/Radarr/blob/develop/src/NzbDrone.Core/ImportLists/RadarrList/RadarrListParser.cs)
- [Sonarr schema](https://github.com/Sonarr/Sonarr/blob/develop/src/NzbDrone.Core/ImportLists/Custom/CustomAPIResource.cs)
- [Radarr Recommended by mapping](https://github.com/Radarr/Radarr/blob/develop/src/NzbDrone.Core/ImportLists/Trakt/Popular/TraktPopularRequestGenerator.cs)
- [Trakt movie API contract](https://github.com/trakt/trakt-api/blob/master/projects/api/src/contracts/movies/index.ts)

### Live endpoint check

A read-only check with the existing local browser token returned HTTP 500 for both
`movies/recommended/weekly` and `shows/recommended/weekly`, and HTTP 200 with arrays
for both `movies/favorited/weekly`, `shows/favorited/weekly`, and personal
`recommendations/movies` and `recommendations/shows`. Most favorited is therefore
the first source in the builder. These responses establish availability at the
time of testing, not semantic equivalence between old and new ranking algorithms.
