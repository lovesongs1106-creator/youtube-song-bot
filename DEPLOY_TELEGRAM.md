# Mobile-only Telegram Bot Setup

This makes the bot usable from mobile:

1. Send `/new` in Telegram.
2. Send a licensed/permitted YouTube song link.
3. Send your outro video.
4. Bot generates the thumbnail video, appends outro, and uploads to YouTube.

## What you still need one time

You do **not** need to manually edit videos on a computer, but the bot must run somewhere online. Free hosting can sleep or fail for long videos. Paid VPS/hosting is more reliable.

You need:

- Telegram bot token from `@BotFather`
- Google OAuth client JSON for YouTube upload
- A public hosting URL, for example Render/Railway/VPS

## Step 1: Create Telegram bot from mobile

1. Open Telegram.
2. Search `@BotFather`.
3. Send `/newbot`.
4. Choose name and username.
5. Copy the token like:

```text
1234567890:ABCDEF_your_token_here
```

This is `TELEGRAM_BOT_TOKEN`. Keep it private.

## Step 2: Get your Telegram user ID

After deploying, open your bot and send:

```text
/id
```

It replies with your numeric ID. Add it to hosting env as `AUTHORIZED_TELEGRAM_USER_ID` so only you can use the bot.

## Step 3: Google Cloud OAuth

Create a Google Cloud project, enable **YouTube Data API v3**, and create an OAuth client.

For the Telegram/mobile OAuth flow, use a **Web application** OAuth client and add this redirect URI:

```text
https://YOUR_HOSTING_URL/oauth2callback
```

Example:

```text
https://my-youtube-song-bot.onrender.com/oauth2callback
```

Download the JSON. You can either:

- upload it as `client_secrets.json`, or
- paste the full JSON into the hosting environment variable `GOOGLE_CLIENT_SECRETS_JSON`.

Do not share this JSON publicly.

## Step 4: Hosting environment variables

Set these variables on your hosting platform:

```text
TELEGRAM_BOT_TOKEN=your_botfather_token
BASE_URL=https://YOUR_HOSTING_URL
GOOGLE_CLIENT_SECRETS_JSON={full google oauth json here}
AUTHORIZED_TELEGRAM_USER_ID=your_numeric_telegram_id
DEFAULT_PRIVACY=private
```

Privacy options:

```text
private
unlisted
public
```

Recommended: start with `private`.

## Step 5: Start bot

The included `Procfile` runs:

```bash
python telegram_bot.py
```

The app also exposes:

```text
/
/oauth2callback
```

The `/oauth2callback` URL is required for Google login.

## Step 6: Connect YouTube from mobile

In Telegram bot:

```text
/auth
```

Open the link, approve YouTube upload access, then return to Telegram.

## Step 7: Make video from mobile

In Telegram bot:

```text
/new
```

Then send:

```text
https://www.youtube.com/watch?v=VIDEO_ID
```

Then send your outro video file.

The bot will:

- download/extract the permitted YouTube audio
- create thumbnail
- render full video
- append outro
- upload to your YouTube channel
- send you the uploaded video link

## Important notes

- Only use links where you own the content, have a license, or explicit permission to reupload.
- Free hosting may sleep, run out of disk, or time out on long videos.
- For reliability, use a small paid VPS or paid app hosting.
- Keep `TELEGRAM_BOT_TOKEN`, `GOOGLE_CLIENT_SECRETS_JSON`, and saved YouTube tokens secret.
