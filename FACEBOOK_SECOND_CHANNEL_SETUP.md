# Facebook → Second YouTube Channel — Setup Guide

Isolated automation: Facebook videos are downloaded on GitHub Actions,
trimmed with FFmpeg, appended with `assets/outro.mp4`, and uploaded to a
**separate second YouTube channel** as **private** by default.

First-channel OAuth (`/auth`, `/new`, `YOUTUBE_TOKEN_JSON`) is untouched.

---

## 1. One-time OAuth for the second channel (manual step)

The worker authenticates with an OAuth **refresh token** belonging to the
Google account that owns the second YouTube channel.

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → create
   (or reuse) a project → enable **YouTube Data API v3**.
2. Create OAuth credentials → **Desktop app** (simplest for a refresh token).
   Note the **Client ID** and **Client Secret**.
3. Get a refresh token with the upload scope. Easiest: use
   [Google OAuth Playground](https://developers.google.com/oauthplayground/):
   - Click the gear icon → check "Use your own OAuth credentials" → paste
     Client ID + Client Secret.
   - In the scopes list, enter `https://www.googleapis.com/auth/youtube.upload`
     → Authorize APIs → log in with the **second channel's Google account** →
     Exchange authorization code for tokens.
   - Copy the **Refresh token** (starts with `1//...`).
4. Add three repository secrets (repo → Settings → Secrets and variables →
   Actions → Secrets → New repository secret):

   | Secret | Value |
   |--------|-------|
   | `YOUTUBE_SECOND_CLIENT_ID` | OAuth Client ID |
   | `YOUTUBE_SECOND_CLIENT_SECRET` | OAuth Client secret |
   | `YOUTUBE_SECOND_REFRESH_TOKEN` | Refresh token from step 3 |

## 2. Repository variables

Same page → **Variables** tab → New repository variable:

| Variable | Value |
|----------|-------|
| `YOUTUBE_TARGET_CHANNEL` | `second` |
| `YOUTUBE_VISIBILITY` | `private` |
| `OUTRO_ASSET_PATH` | `assets/outro.mp4` |

## 3. Outro asset

Place your outro video at `assets/outro.mp4` in the repo and push.
See `assets/README.md`.

## 4. Render (bot side)

No new Render env vars are needed. Required (already documented in
`.env.example`): `TELEGRAM_BOT_TOKEN`, `BASE_URL`, `GITHUB_REPO`,
`GITHUB_TOKEN`, `USE_GITHUB_WORKER=true`.

## 5. Verify

In Telegram:

```
/second_channel_check
```

The worker replies with the verified channel name + outro status.
Then test end-to-end:

```
/upload https://www.facebook.com/.../videos/... trim 00:10-00:40
/status fb-YYYYMMDD-HHMMSS-xxxx
```

## 6. Commands

| Command | Purpose |
|---------|---------|
| `/upload <URL> [trim START-END]` | Queue + dispatch an FB job (private upload) |
| `/upload_force <URL...>` | Re-run bypassing duplicate protection |
| `/upload_force <JOB-ID>` | Retry failed/cancelled job (max 3 attempts) |
| `/status <JOB-ID>` (or bare) | Job detail / recent jobs |
| `/cancel <JOB-ID>` | Cancel queued job, or request cancel of dispatched job |
| `/second_channel_check` | Verify secrets + channel + outro asset |

Timestamps accept `MM:SS` and `HH:MM:SS` (e.g. `trim 02:15-38:42`).

## 7. Access & compliance policy

- Only Facebook videos accessible **without login** are downloaded.
  Login-gated, private, and DRM-protected content is **refused, never bypassed**
  (no cookies, no credentials are ever sent to the downloader).
- Only process videos you own, have licensed, or have explicit permission to
  reuse and re-upload. You are responsible for the content you upload.
- Every job gets an ID, duplicate URLs are blocked (unless forced), temp
  files are cleaned after success/failure, and status is queryable via
  `/status` at every stage.
