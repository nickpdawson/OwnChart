# Google Calendar Setup

> Beta 1 feature. Google Calendar support requires an operator-owned
> Google Cloud OAuth client. Users connect their own Google accounts
> through the web UI after the operator setup below is complete.

OwnChart can read Google Calendar events with read-only access and use
them as life context in Calendar Settings, Chat, Ask, Timeline, Events,
and Dossiers as those surfaces land.

This setup has two separate pieces:

- **Operator setup:** the person running the OwnChart instance creates
  a Google Cloud project and stores the OAuth client id / secret in
  `infra/.env`.
- **User setup:** each OwnChart user opens Settings -> Calendar,
  clicks **Connect Google account**, consents in Google, and chooses
  which calendars under that Google account to import.

The user should never enter the OAuth client secret. The secret belongs
to the OwnChart instance, not to an individual calendar user.

## What You Need

- A running OwnChart instance with a public HTTPS URL.
- Access to edit `infra/.env` on the OwnChart host.
- A Google account for Google Cloud Console.
- A privacy-policy URL and terms URL for the OAuth consent screen.

You do **not** need a Google Calendar API key. You need an **OAuth 2.0
Client ID** of type **Web application**.

## 1. Create A Google Cloud Project

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Create or select a project for this OwnChart deployment.
3. Go to **APIs & Services -> Library**.
4. Enable **Google Calendar API**.

Do not enable unrelated APIs unless you know you need them.

## 2. Configure The OAuth Consent Screen

Go to **APIs & Services -> OAuth consent screen**.

Use **External** unless every OwnChart user is inside the same Google
Workspace organization. For a personal, household, or beta deployment,
External is usually correct.

Fill in:

| Field | Value |
|---|---|
| App name | `OwnChart` or your fork/instance name |
| User support email | An address you actually read |
| Application home page | `https://your-instance.example.com` |
| Privacy policy | `https://your-instance.example.com/privacy` |
| Terms of service | `https://your-instance.example.com/tos` |
| Authorized domain | The domain that hosts OwnChart |
| Developer contact | Your operator email |

Add only these scopes:

```text
https://www.googleapis.com/auth/calendar.readonly
https://www.googleapis.com/auth/calendar.events.readonly
https://www.googleapis.com/auth/userinfo.email
```

Why these scopes:

- `calendar.readonly` lets OwnChart list calendars for the picker.
- `calendar.events.readonly` lets OwnChart read events from selected
  calendars.
- `userinfo.email` lets OwnChart show the connected Google account and
  de-duplicate re-consent for the same account.

Do **not** add write scopes such as:

```text
https://www.googleapis.com/auth/calendar
https://www.googleapis.com/auth/calendar.events
```

OwnChart's Google Calendar adapter is read-only.

If the consent screen is still in Testing mode, add every beta tester's
Google account under **Test users**. Otherwise Google will block them
before OwnChart receives a callback.

## 3. Create OAuth Web Client Credentials

Go to **APIs & Services -> Credentials**.

1. Click **Create credentials -> OAuth client ID**.
2. Application type: **Web application**.
3. Name: `OwnChart on <your host>` or similar.
4. Authorized redirect URI:

```text
https://your-instance.example.com/settings/calendar/google/callback
```

This is the canonical Beta 1 redirect URI.

Important details:

- Use the **web callback path** above, not
  `/api/calendar/google/callback`.
- The scheme, host, port, path, and trailing slash must match exactly.
- If you run staging and production on different hostnames, add both
  redirect URIs to the Google client and set the matching env var in
  each deployment.

After creation, Google offers a JSON download named like:

```text
client_secret_<numbers>-<client>.apps.googleusercontent.com.json
```

That file is useful, but it is secret. Do not commit it, copy it into
the repo, paste it into chat, or leave it in a public downloads folder.

The values OwnChart needs are:

```text
web.client_id
web.client_secret
```

If `jq` is available, you can inspect the file locally without printing
anything into application logs:

```sh
jq -r '.web.client_id' ~/client_secret_*.json
jq -r '.web.client_secret' ~/client_secret_*.json
```

## 4. Set OwnChart Environment Variables

Edit `infra/.env` on the OwnChart host:

```env
OWNCHART_GOOGLE_CALENDAR_CLIENT_ID=<web.client_id from Google JSON>
OWNCHART_GOOGLE_CALENDAR_CLIENT_SECRET=<web.client_secret from Google JSON>
OWNCHART_GOOGLE_CALENDAR_REDIRECT_URI=https://your-instance.example.com/settings/calendar/google/callback
```

These values are operator secrets:

- Keep them in `infra/.env`.
- Never commit `infra/.env`.
- Never put the secret in `config.yaml`.
- Never paste the secret into support tickets or chat logs.

The long-term OwnChart goal is a friendlier instance-admin credential
screen. Beta 1 uses env vars.

Restart the app so the API reads the new env:

```sh
docker compose -f infra/docker-compose.yml --env-file infra/.env up -d --build api worker web
```

Or use your deployment script if your instance has one.

## 5. Connect Calendars In The Web UI

After restart:

1. Sign in to OwnChart.
2. Open **Settings -> Calendar**.
3. Click **Connect Google account**.
4. Complete Google's consent flow.
5. OwnChart returns to:

```text
/settings/calendar/google/callback
```

6. Pick one or more calendars from that Google account.
7. Choose privacy mode, AI exposure, and history window.
8. Bind the selected calendars.

OwnChart intentionally distinguishes:

- **Connected account:** the Google account that granted access.
- **Selected calendars:** the calendars under that account that
  OwnChart imports as separate calendar sources.

One Google account can contain multiple calendars. Work, family,
personal, subscribed, and shared calendars may all be separate choices.

## Privacy Modes

Each selected calendar source has its own privacy settings:

| Mode | Stored |
|---|---|
| Busy only | Time blocks only |
| Title and time | Title + start/end time |
| Full details | Full event details allowed by adapter |

AI exposure is a separate toggle. If AI exposure is off, Chat/Ask only
sees the consent-floor projection for that source, even if richer
details are stored.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Settings says "Google Calendar is not configured by this OwnChart operator" | One or more `OWNCHART_GOOGLE_CALENDAR_*` env vars missing or the API has not restarted | Set all three env vars, then restart `api`, `worker`, and `web` |
| Google says `redirect_uri_mismatch` | The URI in Google Cloud does not exactly match `OWNCHART_GOOGLE_CALENDAR_REDIRECT_URI` | Use `https://your-instance.example.com/settings/calendar/google/callback` exactly |
| Consent screen blocks the user | OAuth app is in Testing mode and the Google account is not listed as a test user | Add the account under Test users or publish/verify the app |
| OwnChart rejects the callback after consent | State expired, user changed records, or callback params were missing | Start the connect flow again from Settings -> Calendar |
| No calendars appear in the picker | Wrong scopes or Google account has no readable calendars | Confirm the three read-only scopes and test with the Google Calendar web app |
| A work calendar is missing | It may be on another Google account, not selected in the picker, or unavailable to the OAuth client | Connect the correct account and bind that specific calendar |
| Events sync but Chat does not mention them | Source history window, privacy mode, or AI exposure may hide the details | Check Settings -> Calendar for the source status and history window |

## Verification Checklist

For a beta operator, consider the feature configured when:

- `/settings/calendar` shows **Connect Google account** instead of
  "not configured by operator."
- Clicking Connect opens a Google OAuth consent screen.
- Returning from Google shows a calendar picker.
- Binding a calendar creates a Google Calendar source.
- The source appears with last sync, event count, privacy mode, AI
  exposure, and history window controls.
- Chat can answer a date-scoped question using calendar context,
  subject to the source's privacy settings.

