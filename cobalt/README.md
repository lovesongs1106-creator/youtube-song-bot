# Self-Hosted Cobalt API

This folder contains deployment configs for a self-hosted Cobalt instance so the bot can download YouTube audio without hitting the public API's JWT/auth blocks.

## Quick Start (Docker)

```bash
cd cobalt
docker compose up -d
```

Cobalt will be available at `http://localhost:9000/`.

## Railway Deployment (One-Click)

Use the official community template:

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/cobalt-youtube-downloader)

After deploy:
1. Copy your Railway domain (e.g. `https://cobalt-xxxx.up.railway.app/`)
2. Add it to the bot's Render environment as `COBALT_API_URL`

## Health Check

```bash
curl http://localhost:9000/
```

Expected response:
```json
{
  "cobalt": {
    "version": "...",
    "url": "...",
    "services": ["youtube", ...]
  }
}
```

## Bot Integration

Set this environment variable on your **Render** service:

```
COBALT_API_URL=https://your-cobalt-instance.up.railway.app/
```

The bot will:
1. Test the connection on startup
2. Use Cobalt as the first downloader priority
3. Fall back to yt-dlp if Cobalt is unavailable
4. Ask the user to upload MP3 if both fail
