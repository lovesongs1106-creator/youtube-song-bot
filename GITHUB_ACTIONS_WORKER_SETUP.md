# GitHub Actions Worker Architecture

This version fixes Render Free 512MB memory crashes by moving heavy rendering/upload work to GitHub Actions.

## Architecture

```text
Telegram bot on Render = collects title/reference/audio/outro only
GitHub Actions worker = downloads files, renders 720p/1080p video, uploads to YouTube
Telegram = receives progress + final YouTube link
```

## One-time setup

### 1. Upload updated code to GitHub

Make sure these files are in your repo root:

```text
bot.py
telegram_bot.py
github_worker.py
requirements.txt
.github/workflows/render-upload.yml
```

### 2. Enable GitHub Actions

Open your GitHub repo:

```text
Actions > I understand my workflows, go ahead and enable them
```

### 3. Create GitHub repo secrets

Open:

```text
GitHub repo > Settings > Secrets and variables > Actions > Secrets > New repository secret
```

Add:

#### Secret 1

```text
Name: TELEGRAM_BOT_TOKEN
Value: your BotFather token
```

#### Secret 2

First, in Telegram bot after YouTube auth, send:

```text
/export_youtube_token
```

Bot sends a file named `YOUTUBE_TOKEN_JSON.txt`. Copy the full content of that file.

```text
Name: YOUTUBE_TOKEN_JSON
Value: full content of YOUTUBE_TOKEN_JSON.txt
```

#### Optional Secret 3

If YouTube token refresh fails, also add your Google OAuth JSON:

```text
Name: GOOGLE_CLIENT_SECRETS_JSON
Value: full Google OAuth client JSON
```

### 4. Create GitHub Personal Access Token for Render

Render bot needs permission to trigger GitHub Actions.

GitHub mobile/browser:

```text
GitHub > Settings > Developer settings > Personal access tokens
```

Use classic token or fine-grained token.

For classic token, select:

```text
repo
```

For fine-grained token, grant repository access and:

```text
Contents: Read and Write
Actions: Read
Metadata: Read
```

Copy token. Keep it private.

### 5. Add Render environment variables

In Render service:

```text
Environment
```

Add/update:

```text
USE_GITHUB_WORKER=true
GITHUB_REPO=your_github_username/your_repo_name
GITHUB_TOKEN=your_github_personal_access_token
GITHUB_EVENT_TYPE=render_video
```

Keep existing variables:

```text
TELEGRAM_BOT_TOKEN
BASE_URL
GOOGLE_CLIENT_SECRETS_JSON
AUTHORIZED_TELEGRAM_USER_ID
DEFAULT_PRIVACY
USE_WEBHOOK=true
WEBHOOK_SECRET=...
```

### 6. Set GitHub render quality

Open:

```text
GitHub repo > Settings > Secrets and variables > Actions > Variables
```

Add repository variables:

For 1080p:

```text
VIDEO_WIDTH=1920
VIDEO_HEIGHT=1080
VIDEO_FPS=30
FFMPEG_PRESET=veryfast
```

For faster 720p:

```text
VIDEO_WIDTH=1280
VIDEO_HEIGHT=720
VIDEO_FPS=30
FFMPEG_PRESET=veryfast
```

### 7. Redeploy Render

```text
Render > Manual Deploy > Deploy latest commit
```

## Mobile usage

In Telegram:

```text
/new
```

Then follow prompts:

1. Song title
2. Artist/channel/show name or `/skip`
3. Reference image or `/skip`
4. MP3/M4A audio file recommended
5. Outro video

Bot will reply:

```text
🚀 Job GitHub Actions worker ko bhej diya
```

Then GitHub worker will send progress and final YouTube link.

## Limits

- YouTube API default quota: about 5–6 uploads/day.
- GitHub private repo free minutes: about 2000 min/month.
- Use one video job at a time.
- Audio/outro file size should be Telegram-bot-download friendly; keep files small when possible.

## If job does not start

Check:

```text
GitHub repo > Actions
```

If no workflow starts, Render env `GITHUB_REPO` or `GITHUB_TOKEN` is wrong.

## If upload fails

Check:

```text
GitHub repo > Actions > failed run logs
```

Common issues:

- Missing `YOUTUBE_TOKEN_JSON` secret
- YouTube token expired: run `/auth` then `/export_youtube_token` and update GitHub secret
- Telegram file too large or expired
- YouTube API quota exceeded
