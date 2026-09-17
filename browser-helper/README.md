# PlexTraktSync all-in-one for TrueNAS

One image/container runs PlexTraktSync, a persistent Chromium browser, the
email/PIN login interface and optional Slack notifications. No GPU is required.
The separate browser container used during investigation is not the deployment
model. This image is distinct from the upstream lightweight Alpine image.

## Build and launch

From the repository root:

```sh
docker build -f Dockerfile.all-in-one -t ghcr.io/ac1dburnz/plextraktsync:all-in-one .
export PTS_CONFIG_PATH=/mnt/YOUR_POOL/apps/plextraktsync/config
export TRAKT_BROWSER_STATE=/mnt/YOUR_POOL/apps/plextraktsync/browser
docker compose -f browser-helper/compose.yml up -d
```

Replace paths with your actual datasets. For TrueNAS Custom App YAML, use
`compose.yml`, replace its variable-based mounts with dataset paths, and remove
`build` once the published image is available. The GitHub workflow publishes
`ghcr.io/ac1dburnz/plextraktsync:all-in-one` on main; it must run successfully
before that tag is available. Publishing currently targets AMD64.

Preserve existing PlexTraktSync configuration. The default command is `watch`;
change it to your desired supported command. Browser mode writes
`/app/config/browser-token.json` and sets `TRAKT_BROWSER_TOKEN_FILE` accordingly.
The container waits for successful token validation before starting PlexTraktSync.
Startup updates only `TRAKT_BROWSER_TOKEN_FILE` in the mounted `.env` so an old
external-token path cannot override the managed path. Configure Plex
interactively if this is a new installation. Do not run two sync instances on
the same configuration directory during migration.

## First login and headless operation

Keep port 6080 bound to localhost. From your Mac:

```sh
ssh -N -L 16080:127.0.0.1:6080 ac1dburn@192.168.2.19
```

Open http://localhost:16080/vnc.html?autoconnect=1&resize=scale.
Sign in to Trakt using your email and emailed PIN. The browser profile persists
under `/browser/profile`. Do not expose this unauthenticated browser interface
to the LAN or internet.

After successful login, set `TRAKT_BROWSER_HEADLESS=true` and recreate the
container with the same datasets. This disables the login display/interface.
If another PIN is needed later, set it back to `false` and recreate the container.
No ongoing login or automatic renewal is guaranteed: it depends on Trakt keeping
this browser session valid. Capture of a token is not proof of renewal.

The image currently runs as root inside the container. Its private token/profile
files are mode-restricted; PUID/PGID switching from the Alpine image is not
implemented in this image.

## Slack notifications

Set a TrueNAS environment variable:

```text
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/PRIVATE/WEBHOOK
```

Omit it or leave it empty to disable alerts. Use a Slack Incoming Webhook, not a
WebSocket URL: https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/
Never commit the webhook URL. It is a secret.

Every five minutes the browser helper checks the saved token using the read-only
Trakt users/settings endpoint. It alerts for missing/invalid credentials, a known
expiry timestamp, or HTTP 401. Network failures, rate limiting, 403, and server
errors are logged as validation failures, not incorrectly called token expiry.
A 403 may still require manual investigation.

One successful Slack alert is sent per failure episode. That state persists in
`/browser/slack-alert-state.json`; successful token validation resets it. Failed
Slack deliveries retry after a 15-minute cooldown. No tokens, account identifiers,
webhook URLs or email PINs are included in messages or exception logs. Slack
delivery failure does not terminate token monitoring. No recovery message is sent.

Tests use mocked Slack delivery; real delivery needs your configured webhook.

## Diagnostics

`/browser/auth-status.json` reports `healthy`, `login_required`, or `unavailable`
with the check timestamp. It contains no credentials. `unavailable` indicates
an API/network problem, not confirmed token expiry. A new token is checked
against the public API before replacing the previous token.

The `browser-only` command is available for isolated validation or observation;
it runs the same image and token monitoring but does not start sync. The normal
default remains `watch`. See [VALIDATION.md](VALIDATION.md) for actual test results.

## Ready-to-edit TrueNAS template

Use [deploy/truenas.yml](../deploy/truenas.yml). It uses the AppyHoe dataset
layout and one container. Set `PLEX_BASEURL`, `PLEX_TOKEN`, and optionally
`SLACK_WEBHOOK_URL` privately in your deployment environment. If the TrueNAS YAML
editor does not interpolate variables, replace those placeholders in the editor,
not in Git. SIMKL and `PLEXYTRACK_*` settings belong to a different application
and are not used here.

Plex environment credentials initialize a new configuration. Existing
`servers.yml` remains authoritative; dotenv overrides still apply to Plex
credentials. Use your working Plex URL with a valid certificate hostname for
HTTPS; a literal LAN IP may not match the certificate.

Keep `TRAKT_BROWSER_HEADLESS: "false"` for first login. Tunnel host port 6080 to
your Mac as described above. After signing in, change it to `"true"` and redeploy
with the same two datasets. Stop the old sync container before launching this
one to avoid duplicate sync workers. The image tag must be built/published
before deploying the template.
