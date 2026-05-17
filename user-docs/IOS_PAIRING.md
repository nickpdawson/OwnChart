# Pairing the OwnChart iOS app

> The native iOS companion app (in public TestFlight) pairs to your
> self-hosted OwnChart instance with **instance URL + email + password**.
> A server-issued, per-device bearer token is stored in your phone's
> Keychain and used for every subsequent request.

`OWNCHART_AUTO_EXPORT_TOKEN` (from [INSTALL.md](./INSTALL.md)) is a
**different path** — it's for the third-party Health Auto Export iOS
app's webhook, not the native OwnChart app. See
[Auto Export](#auto-export-separate-optional-lane) at the bottom of
this page if that's the lane you want. **Operators using the native
OwnChart app do not need to configure `OWNCHART_AUTO_EXPORT_TOKEN`.**

## Before you start

You need three things on the server side:

1. **A publicly reachable HTTPS URL** for your instance — the single
   host that serves both the UI and `/api/*`. Examples:
   - `https://ownchart.example.com`
   - `https://my-server.example/owncharts` (sub-path is fine)

   No `api.*` subdomain — OwnChart is single-origin. (See
   [NETWORK_ACCESS.md](./NETWORK_ACCESS.md) for exposure options
   including Cloudflare Tunnel and Tailscale Funnel if you don't have
   a static IP.)

   Confirm reachability from a browser:

   ```sh
   curl -sf https://your-instance.example.com/healthz && echo ok
   ```

   If that fails, the app will fail at the Server URL step. Fix the
   network posture first.

2. **An OwnChart account** on that instance. There's no separate
   "owner provisioning" step: the **first signup is automatically the
   owner**. Open `https://your-instance.example.com/` in any browser,
   click "First time? Create the owner account," set email and
   password. After that account exists, further calls to
   `/api/auth/register` return 403 — see
   [INSTALL.md → First user / admin](./INSTALL.md#5-first-user--admin)
   for the full account-lifecycle story in alpha.

3. **The TestFlight build of the OwnChart iOS app** on the phone you
   want to pair. Public TestFlight link:
   <https://testflight.apple.com/join/z8QemcTe>.

## In the app

1. **Install** the OwnChart app from TestFlight.
2. **Open** the app and tap **Get Started** on the welcome screen.
3. **Server URL** — paste your instance base URL exactly as you'd
   type it into Safari (scheme included). The app probes
   `GET <url>/healthz` to confirm reachability before letting you
   proceed.
4. **Sign in** — enter the email and password of your OwnChart
   account on that instance. The app sends them to
   `POST /api/auth/device/pair`, the server validates the password,
   mints a per-device bearer token, and returns it along with your
   user profile and the server's capabilities.
5. **HealthKit permissions** — iOS shows the standard permission
   prompt for the data categories OwnChart will read (heart, sleep,
   activity, workouts, body metrics, mindfulness, medications,
   clinical records, etc.). Approve what you want OwnChart to
   ingest. You can change these later in iOS Settings → Privacy &
   Security → Health → OwnChart.
6. **First sync** — the app's HealthKit sync engine runs the
   initial backfill in the background. You can use the rest of the
   app while it's working.

That's the entire pairing flow. No QR code, no token to copy by
hand, no out-of-band step.

## What goes wrong, and how to recover

| Symptom | Likely cause | Fix |
|---|---|---|
| Server URL step rejects with "we couldn't reach that server" | DNS / TLS / firewall / reverse proxy mis-set | From another network, run `curl -sfI https://<your-base>/healthz`. If that fails, the iOS app will too. Check the reverse proxy ([REVERSE_PROXY.md](./REVERSE_PROXY.md)) and DNS first. |
| Server URL step accepts but says "that server didn't respond like an OwnChart instance" | `/healthz` returned a non-200 or unexpected body | Check the api container is up: `docker compose -f infra/docker-compose.yml --env-file infra/.env logs api`. |
| Sign-in step rejects with "wrong email or password" | The account doesn't exist on that instance, or the password's wrong | First-time setup: visit `https://<your-base>/` in a browser, click "First time? Create the owner account." After that, only that one account exists; `/api/auth/register` returns 403 for new attempts in alpha. |
| Sign-in succeeds but the app immediately bounces back to the Server URL step | The server-issued token is being rejected on subsequent calls | Likely a clock-skew or cookie-secure mismatch between the phone and the server. Confirm the server is `OWNCHART_ENV=prod` and behind HTTPS; restart the iOS app. |
| App was paired and now shows "session ended" | An admin revoked this device's token in Settings → Security & Devices on the web (or another device) | Re-pair. The app will return to the Server URL step. |
| Reachability banner appears mid-session | Network dropped or the server became unreachable | The app retries automatically when reachability returns. Your auth state is preserved. |

## Diagnostics

The iOS app's **Settings → Diagnostics** shows:

- Instance URL the app is paired to.
- App build number.
- Auth state (paired / revoked / no-session).
- Last HealthKit sync result and timestamp.
- Most recent upload errors.

When asking for help (issue, email), include the Diagnostics
information. It's the fastest path to a useful answer.

## Multiple devices, multiple records

In 0.1 alpha:

- **Multiple devices for the same user** is supported. Each device
  pairs independently and gets its own bearer token. Revoke per
  device in Settings → Security & Devices.
- **Multiple person records under one user** (parent / household /
  caregiver) is **not shipped in alpha**. The Beta 1 plan covers it;
  see [SHIPPED_VS_ROADMAP.md](./SHIPPED_VS_ROADMAP.md).

## Revoking access

If a phone is lost or you want to take a device offline:

1. From the web at `https://<your-base>/settings` (or wherever your
   instance surfaces session management), go to **Security &
   Devices** and revoke the token for that device.
2. Next time the app tries to call the server, it gets a 401, drops
   its local token, and re-routes the user back to the Server URL
   step.

The token is server-side state — revoking it on the server is
sufficient. You don't need physical access to the phone.

## Auto Export (separate, optional lane)

If you use the third-party [Health Auto Export](https://www.healthyapps.dev/)
iOS app and want it to push HealthKit JSON to your OwnChart instance
(instead of, or alongside, the native OwnChart app), that's a
separate path:

1. **Generate a token** on the server side:

   ```sh
   tr -dc 'A-Za-z0-9_-' </dev/urandom | head -c 48
   ```

2. **Put it in `infra/.env`** as `OWNCHART_AUTO_EXPORT_TOKEN=<that>`.
   Restart the api container.

3. **In Health Auto Export**, configure a REST API automation:
   - URL: `https://<your-instance>/api/auto_export/healthkit`
   - Header: `Authorization: Bearer <that-token>`
   - Method: `POST`

4. **Verify**. Health Auto Export's next run posts a JSON batch;
   the api logs the ingest.

This is a coarse, instance-wide static token — there's no
per-device revocation. If you rotate the token, all configured Auto
Export clients have to be updated to the new value. The native
OwnChart iOS app does **not** use this token; it has its own
per-device bearer.
