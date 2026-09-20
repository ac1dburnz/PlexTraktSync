# Visual previews, local curation and direct adds

The all-in-one list bridge can preview movie/series posters, link to title pages, curate results locally, and add individual or selected titles directly to Radarr/Sonarr. Saved feed URLs still work for recurring imports. Direct adds are a one-time action on the current preview.

## Browse and curate

1. Open the bridge, enter its key, and choose a source. For a specific title use **Text search** or **Exact text search**. You can preview without naming a saved feed.
2. Click **Preview titles**. Cards show posters when supplied by Trakt, original year/language, network, rating, title links and overview. Images load lazily; unavailable posters have a placeholder. No extra TMDB API key is needed.
3. Open **Local filters and exclusions** to include/exclude original-language codes or genres, set a year range or minimum Trakt rating (0–10). These filters work on every source, independently of Trakt's endpoint filters. They run before the result limit, and pagination continues for usable matches within the existing request budget.
4. Unknown metadata fails an active local filter by default. **Keep titles with unknown metadata** changes this policy for local language/genre/year/rating rules. Network filtering still excludes unknown networks. Language is the original language, not available audio tracks; year is the title's original year, not a new season's year.
5. **Exclude** a card, or select cards and **Exclude selected from feed**. Preview again to refill the results. Excluded IDs are TMDB for movies and TVDB for shows. Switching media in the UI clears exclusions to avoid applying IDs from the wrong namespace.
6. Name and save the feed to persist filters/exclusions and get the recurring importer URL. Selection checkboxes alone do not change the saved URL; exclusions do. The source remains dynamic, so later refreshes may discover new matching titles.

The same selection rules feed the rich preview, plain JSON preview and saved importer URL. Posters and display metadata are not added to the importer JSON schema. Editing feed settings invalidates the visual preview so direct adds cannot accidentally use a stale selection.

## Configure optional direct adds

Add credentials to the **same all-in-one container**, using addresses reachable from that container:

```yaml
environment:
  RADARR_URL: "http://RADARR_HOST:7878"
  RADARR_API_KEY: "YOUR_RADARR_API_KEY"
  SONARR_URL: "http://SONARR_HOST:8989"
  SONARR_API_KEY: "YOUR_SONARR_API_KEY"
```

Keep existing environment entries, volumes and ports. Do not duplicate the YAML `environment` key. You can configure only one target. An installation with a URL base is supported, e.g. `http://HOST:7878/radarr`; do not append `/api/v3`. Obtain each key from that application's Settings → General. Keys stay in server configuration and are never returned to the browser, stored in feed definitions, or included in importer URLs.

Recreate the container after changing its environment. No new service, container or port is required. `deploy/truenas-lists.yml` contains commented configuration examples. Direct adds use the applications' API v3; they do not use Plex credentials.

## Add titles

1. Preview titles, expand **Direct add to Radarr / Sonarr**, and click **Load target settings**.
2. Choose a quality profile and root folder loaded from the target. For TV choose standard/daily/anime; for movies choose minimum availability.
3. Choose monitoring and whether to start a download search. Both default off. Search requires monitoring. Target settings can trigger normal downstream automation when enabled.
4. Click **Add…** on one card, select multiple cards and **Review selected titles**, or use **Review whole preview**.
5. Review the titles, target, folder/profile and search/monitoring choices, then click **Add these titles**. Cancel performs no writes.

Existing library titles are skipped without changing their quality profile, monitoring or files. Metadata is resolved by exact TMDB/TVDB ID through the target before adding; no title-string guessing. Radarr lookup exclusions are respected when reported by the target.

Whole-preview adds run in batches of ten. **Stop after current batch** prevents later batches; closing/reloading the browser also prevents unsent batches, but cannot cancel an HTTP request already accepted by the server. Preview snapshots expire after 30 minutes or container restart, and at most 32 are retained. A very large/slow import may need another preview for the remainder.

Results distinguish `added`, `existing`, `excluded`, `failed`, `unknown` and `not_attempted`. An explicit validation/authentication rejection is a failure you can correct and retry. A timeout, server error or invalid success response after POST is **unknown**, since the target may have accepted the write. That stops the batch and is never automatically retried. The process blocks another write for that ID until it appears in the library or the bridge is restarted; check the target before restarting/retrying. This is not a transactional rollback: earlier successful adds remain in the library.

The existing bridge key now authorizes direct additions when an importer is configured. POST actions require its header; a feed URL's query key alone cannot submit an add. Use the bridge on your trusted network, as with the existing list builder.

## Local testing

The locally built image is `plextraktsync:visual-imports` (not `list-bridge` or `discovery-feeds`). To replace the prior test container while preserving saved feeds:

```bash
docker rm -f trakt-lists-test

docker run -d \
  --name trakt-lists-test \
  -p 127.0.0.1:8090:8090 \
  -e PYTHONPATH=/app:/helper \
  -e LIST_BRIDGE_SECRET="$BRIDGE_KEY" \
  -e TRAKT_BROWSER_TOKEN_FILE=/run/trakt/trakt.json \
  -v "$HOME/.plextraktsync-secrets:/run/trakt:ro" \
  -v trakt-lists-test-config:/app/config \
  --entrypoint python \
  plextraktsync:visual-imports \
  /helper/list_bridge.py
```

Ensure `BRIDGE_KEY` is set. This starts the bridge only, with no duplicate Plex sync worker. It enables poster previews and curation; add `RADARR_URL` / `RADARR_API_KEY` and/or Sonarr equivalents to the run command to enable direct additions. Refresh http://localhost:8090 and reconnect.

For a checkout, build with `docker build -f Dockerfile.all-in-one -t plextraktsync:visual-imports .` first. For TrueNAS, the published `all-in-one` tag includes this feature only after its PR is merged and the image workflow succeeds, followed by pulling/recreating the container.

## Validation

- Backend tests cover local filtering before pagination limits, unknown metadata, unsafe poster URLs, consistent previews/feeds, snapshot expiry/membership, header authorization, exact-ID target lookup, duplicate skips, invalid settings, explicit rejection and uncertain-write handling.
- Offline Chromium tests cover actual poster cards/links, unnamed search, local filters, exclusions, saved URL output, canceled review, individual movie adds and bulk series adds through fixture-backed importer HTTP routes. Both old and new browser workflows run in image CI.
- A read-only live Trakt check returned posters and links for five sampled movies and five sampled shows.
- No additions were made to the user's real Radarr/Sonarr instances during development. Direct-write behavior is tested with API fixtures and checked against upstream API v3 controllers; test with your target version/configuration before bulk use.

References: [Radarr add controller](https://github.com/Radarr/Radarr/blob/develop/src/Radarr.Api.V3/Movies/MovieController.cs), [Sonarr add controller](https://github.com/Sonarr/Sonarr/blob/develop/src/Sonarr.Api.V3/Series/SeriesController.cs), [Trakt image schema](https://github.com/trakt/trakt-api/blob/master/projects/api/src/contracts/_internal/response/imagesResponseSchema.ts).

## One-file local setup with TMDB

Use `.env.example` as the variable reference. Copy it to `.env` only if you do not already have a `.env`; fill in the private file and keep it out of Git. `.env` is also excluded from Docker builds. Docker Compose loads it automatically, so there is no need to `export` or `source` secrets.

- `LIST_BRIDGE_SECRET`: at least 24 characters; enter this in the bridge UI. Keep your existing value to preserve existing feed URLs.
- `TRAKT_TOKEN_DIR`: host directory containing `trakt.json`; defaults to your existing Mac browser-token directory.
- `TMDB_API_KEY`: your TMDB v3 API key. Alternatively set `TMDB_READ_ACCESS_TOKEN` to the API Read Access Token; it takes precedence. Neither is sent to the browser.
- `RADARR_URL`, `RADARR_API_KEY`, `SONARR_URL`, `SONARR_API_KEY`: optional direct-add connections. Use LAN addresses reachable from Docker.
- `BRIDGE_BIND_ADDRESS`, `BRIDGE_PORT`: default to `127.0.0.1` and `8090`.
- `TZ`, `LIST_BRIDGE_CACHE_SECONDS`: timezone and feed-cache settings.
- `SLACK_WEBHOOK_URL`: used by the full supervisor, not by this bridge-only test service.

```bash
cd ~/PlexTraktSync
# Edit private .env; do not paste keys into tracked templates.
nano .env
chmod 600 .env
# Creates the volume only if absent; preserves your existing saved test feeds.
docker volume create trakt-lists-test-config
# Stop the old test container if it is using port 8090.
docker stop trakt-lists-test
# Build the latest checkout and start the bridge with your variables.
docker compose -f compose.visual.yml up -d --build
```

Open http://localhost:8090 and enter `LIST_BRIDGE_SECRET`. Preview movies or shows. Trakt artwork is preferred; absent or failed Trakt images fall back to TMDB using the title's exact TMDB ID. Lookups happen as cards approach the viewport, cache for 24 hours (failures for five minutes), and do not change saved-feed contents. Titles lacking a TMDB ID retain the placeholder. Missing/invalid credentials or unavailable artwork do not break preview results.

After editing `.env`, run `docker compose -f compose.visual.yml up -d --force-recreate`. For logs use `docker compose -f compose.visual.yml logs --tail=100`. Avoid sharing `docker compose config` output: resolved output includes credentials. The test service uses your existing token read-only; your existing all-in-one container remains responsible for token renewal.

For TrueNAS, add `TMDB_API_KEY` or `TMDB_READ_ACCESS_TOKEN` alongside the four optional importer variables in the existing all-in-one service's `environment`. Do not replace its token path, browser volume or worker command with the Mac-only test configuration. Use an image built from this branch until the PR is merged and published.

TMDB authentication and image URL behavior follow [application authentication](https://developer.themoviedb.org/docs/authentication-application) and [image basics](https://developer.themoviedb.org/docs/image-basics). This product uses the TMDB API but is not endorsed or certified by TMDB.

## Hide watched or collected titles from any feed

Under **Local filters and exclusions**, enable **Hide watched titles from my Trakt account** and/or **Hide collected titles from my Trakt account**. Preview again, then save the feed. No new credentials or environment variables are needed. Both switches default off, so existing saved feeds keep their behavior.

These switches use the account signed in through the existing browser token, even when the source is another user's public list. They apply to every movie/TV source before the result limit, alongside language/year/genre and manual exclusions. Preview cards and recurring importer URLs use the same results. The bridge continues source pagination to fill the limit where possible, within its existing source-fetch budget.

For movies, watched means present in Trakt's watched-movies history and collected means present in its movie collection. For TV, **any watched episode excludes the whole show**, and **any collected episode excludes the whole show**. This is not a “fully watched series” filter, nor a direct scan of Plex/Radarr/Sonarr: those systems must sync their state into Trakt first.

Library snapshots and feed results use `LIST_BRIDGE_CACHE_SECONDS` (default one hour). A new feed does not extend the age of a reused library snapshot. Token changes invalidate the account-specific cache. Updates may therefore take up to the cache interval to appear after reaching Trakt.

Library loading follows Trakt pagination (250 items/page, at most 100 pages per snapshot), sharing the feed's 60-second fetch deadline. If authentication, rate limits, malformed data or incomplete pagination prevent a complete snapshot, the feed returns an error rather than silently returning unfiltered titles. An empty result is valid when every candidate is excluded.

The bridge only reads Trakt and removes matching titles from its output. It does not delete media or change watched/collection state. Any independent list-cleanup settings in Radarr/Sonarr remain controlled by those applications. Legacy source-specific `ignore_*` options remain available and independent; these new switches provide consistent account-based filtering across all sources.
