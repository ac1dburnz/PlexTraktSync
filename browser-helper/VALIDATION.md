# All-in-one validation — 2026-09-17

Branch: `feat/all-in-one-validation`.
Server: `192.168.2.19` (AMD64 Docker); local build smoke tests also ran on ARM64.
All server tests used isolated copies of the browser profile/configuration.
The existing `plextraktsync` container was not replaced or stopped.

## Verified

- Combined image builds on the server. Python dependencies use a virtual
  environment to avoid clashes with Ubuntu packages.
- Application self-test and offline headless Chromium launch pass.
- Saved Trakt email/PIN login works headlessly without another PIN.
- Trakt public users/settings returns HTTP 200 for the recovered token; account
  matches the initial browser-login baseline and copied app configuration.
- Actual `factory.trakt_api.me` authentication succeeds.
- Actual combined entrypoint running `trakt-login` exits 0 and saves the username.
- Read-only connection to the configured Plex server succeeds.
- Browser login interface serves HTTP 200 when headed mode is enabled.
- Restart, container replacement, headless/headed transitions, and abrupt kill
  recovery retain the saved login. Stale display and Chromium locks are handled.
- Existing `.env` token path migration is tested. Startup writes only the managed
  token-path key before the application reads dotenv configuration.
- Real Slack delivery returns HTTP 200 and `ok`.
- Actual token-monitor -> Slack alert path tested with an expired synthetic token,
  labelled as a test. Repeating the failure with a new alert object does not send
  another message. Real credentials were not expired or revoked.
- Python tests: 54 passed, 11 skipped. Changed Python files passed Ruff.
  The existing suite emits a background TraktBatchWorker 403 after completion;
  this is recorded separately from its successful exit status. No real account
  credentials were supplied to the test-suite container.
- Mocked checks cover 401, 403, rate limiting, server errors, redirects, malformed
  responses, network failure, expiry, Slack failure cooldown, and recovery.

## Not yet proven

- Natural browser-token renewal: final comparison still matches the original
  token. Reopening a valid session is not proof of token renewal.
- GitHub workflow execution/publishing: source prepared, not run/published.
- A new production sync run with the combined image: deliberately not started
  in parallel with the working sync process. Connectivity/authentication checks
  and the Trakt login command were read-only against the accounts.

## Current observation

`pts-integration` runs the combined image in `browser-only`, headless mode with
restart-unless-stopped and Slack enabled. It uses private copies under
`/home/ac1dburn/pts-integration`; the working application uses its original paths.
It does not sync Plex/Trakt data. The Slack label identifies this test monitor.
The webhook is stored in a private server env file outside the repository and
build context. It is not included in this report or the image.

Read status without exposing credentials:

```sh
sudo docker exec pts-integration cat /browser/auth-status.json
sudo docker ps --filter name=pts-integration
```

The test can be stopped with `sudo docker stop pts-integration`. A future check
must compare the observed token with the private initial baseline to establish
whether a real token change occurred, then validate it against the same account.
