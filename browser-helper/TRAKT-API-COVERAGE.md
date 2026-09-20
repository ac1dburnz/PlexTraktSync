# Trakt discovery API coverage

Audited 2026-09-20 against the [official API contracts](https://github.com/trakt/trakt-api/tree/master/projects/api/src/contracts).

This is a read-only movie/series feed builder, not a client for every Trakt account-management endpoint. Existing feed URLs and browser authentication remain supported. Sources are allowlisted; there is no arbitrary URL passthrough.

## Feed sources

| Source key | Builder label | API path | Media |
| --- | --- | --- | --- |
| `favorited` | Most favorited | `/{media}/favorited/{period}` | movies, shows |
| `recommendations` | Personal recommendations | `/recommendations/{media}` | movies, shows |
| `social_recommendations` | Social recommendations | `/social_recommendations/{media}` | movies, shows |
| `trending` | Trending | `/{media}/trending` | movies, shows |
| `popular` | Popular | `/{media}/popular` | movies, shows |
| `anticipated` | Anticipated | `/{media}/anticipated` | movies, shows |
| `hot` | Hot titles (experimental) | `/{media}/hot` | movies, shows |
| `watched` | Most watched | `/{media}/watched/{period}` | movies, shows |
| `played` | Most played | `/{media}/played/{period}` | movies, shows |
| `collected` | Most collected | `/{media}/collected/{period}` | movies, shows |
| `streaming` | Recently streaming | `/{media}/streaming/{period}` | movies, shows |
| `boxoffice` | US weekend box office | `/movies/boxoffice` | movies |
| `recommended` | Recommended by period (legacy) | `/{media}/recommended/{period}` | movies, shows |
| `watchlist` | User watchlist | `/users/{user}/watchlist/{media}` | movies, shows |
| `user_watched` | User watched titles | `/users/{user}/watched/{media}` | movies, shows |
| `collection` | User collection | `/users/{user}/collection/{media}` | movies, shows |
| `favorites` | User favorites | `/users/{user}/favorites/{media}` | movies, shows |
| `ratings` | User ratings | `/users/{user}/ratings/{media}` | movies, shows |
| `history` | User watch history | `/users/{user}/history/{media}` | movies, shows |
| `list` | Named user list | `/users/{user}/lists/{list}/items/{media}` | movies, shows |
| `public_list` | Public list by numeric ID | `/lists/{list}/items/{kind}` | movies, shows |
| `smart_list` | Existing Trakt smart list | `/smart-lists/{list}/items` | movies, shows |
| `search` | Text search | `/search/{kind}` | movies, shows |
| `search_exact` | Exact text search | `/search/{kind}/exact` | movies, shows |
| `search_trending` | Trending searches | `/search/recent_by_id/global/{media}` | movies, shows |
| `lookup` | External ID lookup | `/search/{id_type}/{identifier}` | movies, shows |
| `related` | Related titles | `/{media}/{seed}/related` | movies, shows |
| `credits` | Person cast / crew credits | `/people/{person}/{media}` | movies, shows |
| `updates` | Recently updated metadata | `/{media}/updates/{since}` | movies, shows |
| `playback` | Your paused playback | `/sync/playback/{playback_type}` | movies, shows |
| `up_next` | Your up next shows | `/sync/progress/up_next` | shows |
| `progress` | Your watched progress | `/sync/progress/watched` | shows |
| `hidden` | Your hidden titles | `/users/hidden/{section}` | movies, shows |
| `calendar_new` | New series | `/calendars/{target}/shows/new/{start}/{days}` | shows |
| `calendar_premieres` | Season premieres | `/calendars/{target}/shows/premieres/{start}/{days}` | shows |
| `calendar_shows` | Airing episodes | `/calendars/{target}/shows/{start}/{days}` | shows |
| `calendar_finales` | Finales | `/calendars/{target}/shows/finales/{start}/{days}` | shows |
| `calendar_movies` | Movie releases | `/calendars/{target}/movies/{start}/{days}` | movies |
| `calendar_streaming` | Movie streaming releases | `/calendars/{target}/streaming/{start}/{days}` | movies |
| `calendar_dvd` | DVD / physical releases | `/calendars/{target}/dvd/{start}/{days}` | movies |
| `calendar_media` | All releases | `/calendars/{target}/media/{start}/{days}` | movies, shows |
| `calendar_hot` | Hot upcoming releases | `/calendars/releases/hot/{start}/{days}` | movies, shows |
| `calendar_hot_new` | Hot new series | `/calendars/releases/hot/new/{start}/{days}` | shows |
| `calendar_hot_premieres` | Hot season premieres | `/calendars/releases/hot/premieres/{start}/{days}` | shows |
| `calendar_hot_finales` | Hot finales | `/calendars/releases/hot/finales/{start}/{days}` | shows |

Paths expand only validated fields. Ratings optionally append `/1` through `/10`. `{kind}` is singular movie/show; paused show playback uses episodes. Calendar dates are calculated in UTC on each uncached fetch. A calendar episode maps to its parent show and is deduplicated by TVDB ID.

## Discovery and filters

- User lists, user smart lists, liked lists, popular/trending lists, list search, and person search populate the builder without copying IDs manually.
- Network, genre, language, country, and certification lookup supports Trakt pagination. Discovery returns only the display names/IDs needed by the UI, not full user profiles.
- Source-specific fields cover period, user/list/person/seed, search, external identifier lookup, calendar scope/window, ratings, credit role, hidden section, recommendation exclusions/window, and progress flags.
- Trakt filters include years, genres/subgenres, languages, countries, ratings, certifications, runtime, availability (`watchnow`), and applicable date ranges. Endpoint/account support still determines whether Trakt accepts or honors a filter. The bridge does not claim undocumented server filters work.
- Network filtering runs locally against extended `show.network`, with case-insensitive exact comma-separated names. Missing network data is excluded. It is not provider availability or a Netflix regional catalog.
- Output order can preserve Trakt order or sort by title/newest/oldest. Calendar sorting uses the release/episode event; other sources use original title release. Unknown dates sort last. Filters and deduplication happen before the title limit.
- Paging continues until enough usable titles are found or the source is exhausted. Sorted and calendar feeds scan the selected scope first. A ten-request/60-second ceiling returns an explicit error instead of a partial list. Rate limits retain the existing cooldown.

## Scope decisions

| API family | Treatment |
| --- | --- |
| Movie/show rankings and recommendations | Included, with legacy/experimental sources clearly labelled |
| All calendar families | Included; movie and show outputs remain separate for their importers |
| Text/exact/trending search, ID lookup, related titles and person credits | Included |
| User libraries, favorites, ratings, history; own playback/progress/hidden titles | Included |
| User/public/smart list items and list discovery | Included; existing smart lists are resolved, not created |
| Mixed `/media` rankings | Covered by movie/show ranking feeds; no redundant mixed importer output |
| Sync equivalents of user libraries; Up Next Nitro | Standard user-library/Up Next sources used; duplicate/optimized transports not separate feeds |
| Updated ID-only endpoints | Full metadata-update feed used to provide importer IDs |
| Per-title details, images, comments, statistics, videos, translations | Not standalone import lists; poster/detail enrichment is a later feature |
| Watch-now provider details | No promise of a provider-specific additions feed; regional availability requires separate verification |
| Social activity, notes, account settings and administrative APIs | Not exposed as discovery feeds |
| Writes, check-in/scrobble, OAuth management and Trakt list mutations | Not added; existing sync/browser components retain their responsibilities |

## Validation and live availability

- Backend: 140 bridge tests pass, including every advertised source/media combination through save → preview → importer HTTP routes.
- Full repository suite: 194 passed, 11 skipped. Existing background-worker tests emit DNS retry messages after completion in the network-restricted environment.
- All-in-one Docker build and offline Chromium UI smoke test pass. The UI test covers saved movie/TV feeds, Netflix calendar preset, network suggestions, list discovery selection, preview, persistence and authenticated importer URLs.
- 69 read-only source/media API probes: 67 HTTP 200, one HTTP 404, one HTTP 500. HTTP 200 alone is not proof of a usable feed: the Hot shows response was a single-title object, and some TV samples had no TVDB ID.
- Hot movies returned 404; Hot shows returned an object rather than a list. Both remain explicitly experimental and fail preview instead of importing a title accidentally resolved from the route name.
- Recently streaming movies returned 500; recently streaming shows returned a usable list. This limitation is shown in the builder.
- All 12 discovery modes returned valid responses. Named-user-list and public-list imports were checked; the account had no smart lists to live-test their items. Smart-list item behavior is fixture-tested and contract-backed, not live-proven.
- A Netflix new-series feed over the previous/next seven days returned two usable series.
- Empty personalized/social/library results are valid. Missing provider identifiers are omitted; a wholly unusable selected-media response is an error.
- Live checks are a dated observation using one account, not a guarantee of endpoint availability, account entitlements or complete catalogs. Trakt explicitly curates global calendars.
- Actual Radarr/Sonarr applications were not driven during this change; importer JSON formats are tested against their documented/source schemas.

## Follow-up intentionally outside this PR

Poster previews, title links, direct individual/bulk Radarr/Sonarr adds, and richer local language/year/exclusion filtering will be designed separately after confirmation. This PR does not store Radarr/Sonarr API keys or modify their libraries.
