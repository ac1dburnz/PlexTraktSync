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
