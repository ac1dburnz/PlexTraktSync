# Optional Trakt WireGuard transport

VPN support is disabled unless `TRAKT_VPN_ENABLED=true`. It works with the
browser/session method, PKCE, and legacy device authentication without changing
or copying their tokens. Only Trakt HTTPS requests and Trakt pages in the helper
browser use the selected route. Plex, Sonarr/Radarr, and local bridge access stay
on their normal routes, including `http://192.168.1.100:8090`.

In `auto` mode the app starts direct. Three consecutive connection failures,
timeouts, or HTTP 502/503/504 responses start the first VPN profile. Continued
failures rotate sorted `.conf` profiles, with a minimum cooldown between changes.
A successful API response resets the consecutive failure count. A failed tunnel
health check also rotates/restarts profiles. Once a VPN is active, failure never
silently falls back to direct: a missing interface has a terminal kernel route
and existing Trakt proxy connections are closed when routes change. Every
15 minutes a separate direct connectivity check can return the app to direct.
`always` mode requires a healthy VPN and never automatically returns to direct.

With `VPN_RATE_LIMIT_FAILOVER=true` (the default when VPN support is enabled),
three HTTP 429 responses schedule VPN takeover or profile rotation. Successful
reads do not erase this separate denial count. The gateway waits for the full
shared `Retry-After` or backoff period before switching or probing, and the app
keeps its shared rate limiter across route changes. No request is replayed by
the adapter. Set this option to `false` to keep transport-only failover.

Direct recovery probes returning 401/403/412/420/429 do not turn the VPN off.
HTTP 401/403/420 do **not** trigger rotation. Changing IP cannot resolve bad
credentials, app/account restrictions, or authenticated per-user API quotas.
Existing rate-limit retry/backoff behavior applies. The connectivity probe checks
TLS and network reachability, not whether the selected account is authenticated.
No write request is automatically replayed by this integration.

## Local Docker setup

From the repository root, with Docker running:

```sh
export WIREGUARD_CONFIG_PATH=/path/to/wireguard/provider.conf
docker compose -f vpn-helper/compose.yml up -d --build
docker build -f Dockerfile.all-in-one -t plextraktsync:pkce-vpn-local .
docker compose --env-file .env.pkce-local \
  -f browser-helper/compose.pkce-local.yml \
  -f browser-helper/compose.vpn-local.yml up -d
```

The local UI stays at `http://localhost:18090`. Interactive browser:
`http://localhost:16080/vnc.html?autoconnect=1&resize=scale`.
Use your existing bridge key and choose either authentication method as before.
VPN settings do not resolve the Trakt connected-app limit for your Trakt app.
The bridge-key-protected `GET /api/vpn` reports the gateway status.

The gateway's ports are internal Docker endpoints; no host port is published.
Inspect it without displaying WireGuard keys:

```sh
docker compose -f browser-helper/compose.pkce-local.yml -f browser-helper/compose.vpn-local.yml exec pkce python -c \
 'import urllib.request; print(urllib.request.urlopen("http://trakt-vpn:9090/status").read().decode())'
```

To disable VPN support, recreate the app with only `compose.pkce-local.yml`,
then stop the gateway. Authentication files remain in the same lab volumes.

## Multiple profiles and deployment

For multiple providers/exits, mount a directory containing sorted `.conf` files
at `/wireguard:ro` instead of a single file. Keep the originals outside the
repository and images. Each profile must contain one `[Interface]`, one `[Peer]`,
and IPv4 `AllowedIPs = 0.0.0.0/0`. Executable `wg-quick` hooks are rejected.
Generated container configs are mode 0600 and removed when the tunnel stops.

Build/load `plextraktsync:wireguard-gateway` on your server. The optional
`deploy/truenas-vpn.yml` overlay adds the gateway to `deploy/truenas-lists.yml`.
Set `WIREGUARD_CONFIG_DIR` to the server directory of profiles. The app image
must include these changes; they are not yet published to GHCR.
The WireGuard Linux kernel module and Docker `NET_ADMIN` capability are required.
No host networking, Docker socket, or privileged container is required.

| Variable | Default | Meaning |
| --- | --- | --- |
| `TRAKT_VPN_ENABLED` | `false` | App transport opt-in |
| `TRAKT_VPN_PROXY_URL` | `http://trakt-vpn:8080` | Internal HTTPS CONNECT proxy |
| `TRAKT_VPN_CONTROL_URL` | `http://trakt-vpn:9090` | Internal status/report API |
| `VPN_RATE_LIMIT_FAILOVER` | `true` | Take over after repeated 429 responses, after the shared pause |
| `VPN_MODE` | `auto` | `auto` or `always` |
| `VPN_FAILURE_THRESHOLD` | `3` | Consecutive transport/health failures |
| `VPN_SWITCH_COOLDOWN` | `120` | Minimum seconds between profile attempts |
| `VPN_RECOVERY_INTERVAL` | `900` | Seconds between direct probes; minimum 30 |
| `VPN_HEALTH_INTERVAL` | `30` | Seconds between VPN health checks |

This is app transport failover, not a whole-host VPN or a DNS privacy solution.
DNS uses Docker's existing resolver; profile DNS settings are ignored. Upstream
connections use IPv4. Keep the proxy/control network limited to trusted app
containers: the control API has no account credentials or authentication, and
must not be published to the LAN or Internet. HTTPS remains encrypted end to end.

## Validation

A supplied provider profile tested in an isolated Docker gateway: different VPN public exit,
unchanged direct exit, public Trakt API HTTP 200, automatic activation after two
simulated transport failures, forced interface crash blocked direct fallback,
healthy tunnel restart, and automatic stop after direct recovery. The accelerated
failure/cooldown/recovery settings were used only for that crash test.

Unit tests cover two-profile rotation, cooldowns, failed startup, crash recovery,
rate/auth denial exclusions, existing connection shutdown, private profile
permissions, hook rejection, and browser/PKCE transport isolation. Only one real
provider profile was supplied, so distinct live provider rotation remains untested.
Authenticated account calls still require valid session credentials or completing
PKCE consent; public API success is not proof of authenticated account access.

Repeat the live lifecycle check using a separate disposable gateway:

```sh
WIREGUARD_CONFIG_PATH=/path/to/wireguard/provider.conf bash vpn-helper/test_docker.sh
```

The full offline regression suite currently passes **342 tests**, with 11 skipped
and 77 subtests passed. Run `bash browser-helper/test_pkce_docker.sh` to rebuild
and repeat it with startup and Chromium UI checks.
