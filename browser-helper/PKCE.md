# PKCE and browser/session authentication

Both authentication methods remain available. Browser/session login remains the
initial default. PKCE credentials are saved separately, and connecting PKCE does
not replace, delete, or revoke the browser token or browser profile.

## Local test at 192.168.1.100:8090

1. Add this exact redirect URI to your Trakt app in Trakt's app settings:
   `http://192.168.1.100:8090/oauth/callback`.
2. Build and deploy the updated all-in-one image, preserving the existing config
   and browser mounts. The `deploy/truenas-lists.yml` template includes:
   ```yaml
   TRAKT_PKCE_CLIENT_ID: "YOUR_TRAKT_APP_CLIENT_ID"
   TRAKT_PKCE_REDIRECT_URI: "http://192.168.1.100:8090/oauth/callback"
   TRAKT_PKCE_ALLOW_LOCAL_HTTP: "true"
   ```
3. Open `http://192.168.1.100:8090`, enter your existing bridge key, and Connect.
   Choose **Connect Trakt app with PKCE**. Approve your Trakt app in Trakt, then
   return to the builder and Connect again. PKCE is now selected.
4. Preview feeds and verify the sources you use. Different app credentials may
   have different endpoint access; the adapter does not silently retry with the
   browser session when PKCE is rejected.
5. Restart a running sync/watch process after changing authentication methods.
   Both the CLI and feeds read the shared selection from the config mount.

Trakt recommends HTTPS redirects. Local HTTP is an explicit testing option,
limited to localhost, loopback addresses, RFC1918 IPv4 addresses and IPv6 unique
local addresses. Trakt's acceptance of this HTTP redirect has not been verified.
If its app settings or authorization endpoint reject it, use an HTTPS callback
that routes to `/oauth/callback` on the bridge. That callback can remain local
with local DNS and a certificate trusted by the browser; the bridge itself can
continue listening on port 8090 behind a TLS reverse proxy. No public hosting is
required for the callback: the user's browser follows the redirect.

For HTTPS, set `TRAKT_PKCE_REDIRECT_URI` to the exact registered HTTPS callback and
remove `TRAKT_PKCE_ALLOW_LOCAL_HTTP` or set it to `false`.

## Switching back

Select **Browser / session token**, then **Use selected method**. The existing
browser credentials are used again, and saved PKCE credentials remain available.
Use the same Trakt account for both methods when keeping existing sync settings.
The browser helper and its session monitoring continue running.

## CLI

Without changing existing login behavior:

```sh
python -m plextraktsync trakt-login --method browser
python -m plextraktsync trakt-login --method pkce --redirect-uri https://YOUR_CALLBACK
```

The CLI prints an authorization URL and accepts the complete callback URL copied
from the browser. For this manual CLI flow, use a registered callback that does
not automatically exchange the code; `/oauth/callback` on the bridge exchanges it
itself, so use the bridge's Connect button for that callback.
Add `--allow-local-http` to explicitly enable a registered local HTTP callback.

## Token storage and refresh

`trakt-auth.json` stores the selected method. `trakt-pkce.json` stores PKCE access
and refresh tokens. `trakt-pkce-pending.json` stores a short-lived, single-use
state and verifier while login is pending. Files use mode 0600 and atomic
replacement. Per-file locks serialize refreshes across CLI and web processes.
Successful refreshes replace both tokens. Failed refreshes retain the selected
method and do not fall back to a different account or replay API writes.

PKCE login needs no client secret. Set `TRAKT_PKCE_CLIENT_ID` to your app’s Client ID; no personal app ID is bundled.
OAuth requests use `https://auth.trakt.tv`; API requests use
`https://api.trakt.tv`. API credentials are refused for any other host.

## Local Docker lab

The isolated Compose lab uses separate Docker volumes and runs `browser-only`;
it never starts a Plex sync or writes to your existing deployment.

```sh
# .env.pkce-local contains LIST_BRIDGE_SECRET and TRAKT_PKCE_CLIENT_ID.
docker compose --env-file .env.pkce-local -f browser-helper/compose.pkce-local.yml up -d
```

Open `http://localhost:18090` and use that bridge key. For a real PKCE login,
register `http://localhost:18090/oauth/callback` in your Trakt app settings.
For a fresh session login, open `http://localhost:16080/vnc.html?autoconnect=1&resize=scale` and sign in to Trakt in
the dedicated container browser. Existing production credentials remain separate.

Run the reproducible offline Docker checks:

```sh
bash browser-helper/test_pkce_docker.sh
```

The runner builds the production and test images, runs the full suite without
network access, checks CLI startup, tests both entrypoint startup paths, and runs
Chromium UI checks in a separate throwaway container. Image build dependency
downloads require network access; the tests themselves run with `--network none`.
It does not mount real tokens or alter the Compose lab's authentication state.

## Validation status (2026-10-11)

- Production Docker image built successfully.
- Full suite: **269 passed, 11 skipped, 77 subtests passed**, with network disabled.
- Every one of the 77 source/media pairs was saved, previewed and exported while
  switching browser → PKCE → browser, using mocked upstream responses.
- Browser token capture updates preserve PKCE credentials; inactive broken
  credentials cannot break the active method. Failed refresh/reconnect does not
  overwrite browser credentials or silently fall back.
- PKCE refresh serialized across eight readers and six separate processes.
- Actual pytrakt client reads and writes send the selected method's credentials.
  Existing device login still uses its original client ID/secret path.
- Container entrypoint tests preserve the original browser monitor validation
  gate, let PKCE start without browser login, and reject invalid active PKCE
  credentials despite an available browser readiness marker.
- CLI info and login help passed. Chromium UI checks passed for the selector,
  errors, PKCE link generation, callback state rejection and bridge key checks.
- your Trakt app Client ID returned HTTP 200 from the public API inside Docker.
- The previously saved local session credential returned HTTP 401 on a read-only
  account check. It was mounted read-only and was not modified.
- Live authenticated PKCE access, real refresh rotation, and local redirect
  acceptance remain pending user login and app redirect registration. The LAN
  deployment at 192.168.1.100 has not been changed.

The existing threading test previously flushed dummy operations to the live API
at shutdown. Its upstream calls are now mocked through queue shutdown, and it
asserts all four queued operations were flushed. No real writes are sent by the
regression suite.

References: https://developer.trakt.tv/docs/pkce and
https://developer.trakt.tv/docs/authentication-oauth.
