#!/usr/bin/env python3
"""
Telegram front-end for YouTube Song Automation Bot.

Mobile flow:
  /auth -> connect YouTube channel once
  /new  -> send YouTube song link -> send outro video -> bot generates + uploads

Environment variables:
  TELEGRAM_BOT_TOKEN              Required. From @BotFather.
  BASE_URL                        Required for OAuth. Example: https://your-app.onrender.com
  GOOGLE_CLIENT_SECRETS_JSON      Optional JSON string. If not set, uses client_secrets.json file.
  AUTHORIZED_TELEGRAM_USER_ID     Optional. Restrict bot to one Telegram numeric user ID.
  DEFAULT_PRIVACY                 Optional: private/unlisted/public. Default: private.
  PORT                            Optional. Hosting provider usually sets this.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any

from flask import Flask, request
from werkzeug.middleware.proxy_fix import ProxyFix
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot import (
    YOUTUBE_UPLOAD_SCOPE,
    download_youtube_audio,
    generate_thumbnail,
    get_youtube_title,
    render_video,
    slugify,
)

WAITING_LINK, WAITING_OUTRO = range(2)

ROOT = Path(__file__).parent.resolve()
TOKENS_DIR = ROOT / "tokens"
TELEGRAM_DOWNLOADS_DIR = ROOT / "telegram_downloads"
OUTPUTS_DIR = ROOT / "outputs"
TOKENS_DIR.mkdir(exist_ok=True)
TELEGRAM_DOWNLOADS_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
BASE_URL = os.environ.get("BASE_URL", "").strip().rstrip("/")
AUTHORIZED_TELEGRAM_USER_ID = os.environ.get("AUTHORIZED_TELEGRAM_USER_ID", "").strip()
DEFAULT_PRIVACY = os.environ.get("DEFAULT_PRIVACY", "private").strip().lower()
if DEFAULT_PRIVACY not in {"private", "unlisted", "public"}:
    DEFAULT_PRIVACY = "private"

# Webhook mode is better for free hosting because incoming Telegram messages wake the app.
USE_WEBHOOK = os.environ.get("USE_WEBHOOK", "true").strip().lower() in {"1", "true", "yes", "on"}
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me-secret").strip()

GOOGLE_CLIENT_SECRETS_PATH = ROOT / "client_secrets.json"
if os.environ.get("GOOGLE_CLIENT_SECRETS_JSON") and not GOOGLE_CLIENT_SECRETS_PATH.exists():
    GOOGLE_CLIENT_SECRETS_PATH.write_text(os.environ["GOOGLE_CLIENT_SECRETS_JSON"], encoding="utf-8")

# In-memory OAuth state -> Telegram user ID mapping.
OAUTH_STATES: dict[str, int] = {}
telegram_app: Application | None = None
flask_app = Flask(__name__)
# Render/other hosts terminate HTTPS at a proxy. This makes Flask respect X-Forwarded-Proto=https.
flask_app.wsgi_app = ProxyFix(flask_app.wsgi_app, x_proto=1, x_host=1)


def allowed(update: Update) -> bool:
    if not AUTHORIZED_TELEGRAM_USER_ID:
        return True
    user = update.effective_user
    return bool(user and str(user.id) == AUTHORIZED_TELEGRAM_USER_ID)


async def reject_if_unauthorized(update: Update) -> bool:
    if allowed(update):
        return False
    if update.message:
        await update.message.reply_text("Unauthorized user. This bot is locked to its owner.")
    return True


def token_file_for_user(user_id: int) -> Path:
    return TOKENS_DIR / f"youtube_token_{user_id}.json"


def load_client_config() -> dict[str, Any]:
    if not GOOGLE_CLIENT_SECRETS_PATH.exists():
        raise RuntimeError("client_secrets.json missing or GOOGLE_CLIENT_SECRETS_JSON env var not set.")
    return json.loads(GOOGLE_CLIENT_SECRETS_PATH.read_text(encoding="utf-8"))


def make_flow(state: str | None = None) -> Flow:
    if not BASE_URL:
        raise RuntimeError("BASE_URL env var is required for mobile OAuth.")
    flow = Flow.from_client_config(
        load_client_config(),
        scopes=YOUTUBE_UPLOAD_SCOPE,
        state=state,
        redirect_uri=f"{BASE_URL}/oauth2callback",
    )
    return flow


def youtube_authed_for_user(user_id: int):
    token_path = token_file_for_user(user_id)
    if not token_path.exists():
        raise RuntimeError("YouTube is not connected. Send /auth first.")

    creds = Credentials.from_authorized_user_file(str(token_path), YOUTUBE_UPLOAD_SCOPE)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError("YouTube token expired. Send /auth again.")
    return build("youtube", "v3", credentials=creds)


def upload_for_user(
    user_id: int,
    video_file: Path,
    thumbnail_file: Path,
    title: str,
    description: str,
    tags: list[str],
    privacy_status: str,
) -> str:
    youtube = youtube_authed_for_user(user_id)
    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": tags,
            "categoryId": "10",
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }
    request_upload = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=MediaFileUpload(str(video_file), chunksize=-1, resumable=True),
    )
    response = None
    while response is None:
        _, response = request_upload.next_chunk()
    video_id = response["id"]

    if thumbnail_file.exists():
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumbnail_file)),
        ).execute()
    return video_id


def extract_youtube_url(text: str) -> str | None:
    match = re.search(r"https?://(?:www\.)?(?:youtube\.com/watch\?v=[\w-]+[^\s]*|youtu\.be/[\w-]+[^\s]*)", text)
    return match.group(0) if match else None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    user = update.effective_user
    auth_status = "connected" if user and token_file_for_user(user.id).exists() else "not connected"
    await update.message.reply_text(
        "Bhai bot ready hai.\n\n"
        f"YouTube status: {auth_status}\n"
        f"Default privacy: {DEFAULT_PRIVACY}\n\n"
        "Commands:\n"
        "/auth - YouTube channel connect karo\n"
        "/new - Naya video banao aur upload karo\n"
        "/id - Apna Telegram user ID dekho\n"
        "/cancel - Current process cancel"
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.message:
        await update.message.reply_text(f"Your Telegram user ID: {update.effective_user.id}")


async def auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    if not update.effective_user or not update.message:
        return
    if not GOOGLE_CLIENT_SECRETS_PATH.exists():
        await update.message.reply_text(
            "Google OAuth config missing. Hosting env me GOOGLE_CLIENT_SECRETS_JSON add karo "
            "ya client_secrets.json file deploy karo."
        )
        return
    if not BASE_URL:
        await update.message.reply_text("BASE_URL missing hai. Hosting URL env var me set karo.")
        return

    state = secrets.token_urlsafe(24)
    OAUTH_STATES[state] = update.effective_user.id
    flow = make_flow(state=state)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    await update.message.reply_text(
        "YouTube connect karne ke liye ye link open karo, Google login approve karo:\n\n"
        f"{auth_url}\n\n"
        "Approve ke baad browser me success message aayega. Phir Telegram par /new bhejna."
    )


@flask_app.route("/")
def home():
    return "YouTube Song Telegram Bot is running. Open Telegram and use /start."


@flask_app.route(f"/telegram/{WEBHOOK_SECRET}", methods=["POST"])
def telegram_webhook():
    if telegram_app is None:
        return "Bot not ready", 503
    try:
        update = Update.de_json(request.get_json(force=True), telegram_app.bot)
        asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), telegram_app.loop)
        return "OK"
    except Exception as exc:
        return f"Webhook error: {exc}", 500


@flask_app.route("/oauth2callback")
def oauth2callback():
    state = request.args.get("state", "")
    user_id = OAUTH_STATES.get(state)
    if not user_id:
        return "Invalid/expired OAuth state. Go back to Telegram and send /auth again.", 400
    try:
        flow = make_flow(state=state)
        # Some hosts pass the callback to Flask as http internally even though the public URL is https.
        # OAuth requires https, so rebuild the callback from BASE_URL.
        authorization_response = f"{BASE_URL}/oauth2callback"
        if request.query_string:
            authorization_response += "?" + request.query_string.decode("utf-8")
        flow.fetch_token(authorization_response=authorization_response)
        creds = flow.credentials
        token_file_for_user(user_id).write_text(creds.to_json(), encoding="utf-8")
        OAUTH_STATES.pop(state, None)

        if telegram_app:
            asyncio.run_coroutine_threadsafe(
                telegram_app.bot.send_message(
                    chat_id=user_id,
                    text="✅ YouTube channel connected. Ab /new bhejo aur video banao.",
                ),
                telegram_app.loop,
            )
        return "Success! YouTube connected. You can close this page and go back to Telegram."
    except Exception as exc:
        return f"OAuth failed: {exc}", 500


async def new_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await reject_if_unauthorized(update):
        return ConversationHandler.END
    if not update.effective_user or not update.message:
        return ConversationHandler.END
    if not token_file_for_user(update.effective_user.id).exists():
        await update.message.reply_text("Pehle /auth bhej ke YouTube channel connect karo.")
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text(
        "YouTube song link bhejo. Example:\n"
        "https://www.youtube.com/watch?v=VIDEO_ID\n\n"
        "Note: sirf wahi link use karo jiska reupload/license permission tumhare paas hai."
    )
    return WAITING_LINK


async def receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await reject_if_unauthorized(update):
        return ConversationHandler.END
    text = update.message.text or ""
    url = extract_youtube_url(text)
    if not url:
        await update.message.reply_text("Valid YouTube link nahi mila. Dobara link bhejo.")
        return WAITING_LINK

    title = get_youtube_title(url) or "YouTube Song"
    context.user_data["youtube_url"] = url
    context.user_data["song_name"] = title
    await update.message.reply_text(
        f"Song detect hua: {title}\n\n"
        "Ab apna 30–40 sec outro video bhejo as Telegram video/document."
    )
    return WAITING_OUTRO


async def receive_outro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await reject_if_unauthorized(update):
        return ConversationHandler.END
    if not update.effective_user or not update.message:
        return ConversationHandler.END

    tg_file_id = None
    ext = ".mp4"
    if update.message.video:
        tg_file_id = update.message.video.file_id
        ext = ".mp4"
    elif update.message.document:
        tg_file_id = update.message.document.file_id
        name = update.message.document.file_name or "outro.mp4"
        ext = Path(name).suffix or ".mp4"
    else:
        await update.message.reply_text("Please outro video file bhejo, text/photo nahi.")
        return WAITING_OUTRO

    await update.message.reply_text("Outro mil gaya. Ab video generate + upload start kar raha hoon. Thoda time lagega.")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO)

    user_id = update.effective_user.id
    youtube_url = context.user_data["youtube_url"]
    song_name = context.user_data["song_name"]

    try:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        base = slugify(song_name)
        job_dir = OUTPUTS_DIR / f"telegram-{user_id}-{base}-{stamp}"
        job_dir.mkdir(parents=True, exist_ok=True)
        outro_path = job_dir / f"outro{ext}"
        thumb_path = job_dir / "thumbnail.jpg"
        video_path = job_dir / f"{base}.mp4"
        workdir = job_dir / "work"

        tg_file = await context.bot.get_file(tg_file_id)
        await tg_file.download_to_drive(custom_path=str(outro_path))

        await update.message.reply_text("Audio download kar raha hoon...")
        audio_path = await asyncio.to_thread(download_youtube_audio, youtube_url, job_dir / "downloaded_audio")

        await update.message.reply_text("Thumbnail aur video render kar raha hoon...")
        await asyncio.to_thread(generate_thumbnail, song_name, None, thumb_path)
        await asyncio.to_thread(render_video, thumb_path, audio_path, outro_path, video_path, workdir)

        await update.message.reply_text(f"YouTube par upload kar raha hoon as {DEFAULT_PRIVACY}...")
        description = (
            "Licensed/permission-based upload.\n\n"
            f"Source link: {youtube_url}\n"
        )
        tags = ["music", "official audio"]
        video_id = await asyncio.to_thread(
            upload_for_user,
            user_id,
            video_path,
            thumb_path,
            song_name,
            description,
            tags,
            DEFAULT_PRIVACY,
        )
        await update.message.reply_text(
            "✅ Done bhai! Video upload ho gaya:\n"
            f"https://www.youtube.com/watch?v={video_id}"
        )
    except Exception as exc:
        await update.message.reply_text(
            "❌ Error aa gaya:\n"
            f"{exc}\n\n"
            "Agar YouTube download issue hai to yt-dlp update karna padega, ya hosting storage/time limit ho sakti hai."
        )
    finally:
        context.user_data.clear()

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.message:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def run_flask() -> None:
    port = int(os.environ.get("PORT", "8080"))
    flask_app.run(host="0.0.0.0", port=port)


def build_telegram_app() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("auth", auth))

    conv = ConversationHandler(
        entry_points=[CommandHandler("new", new_video)],
        states={
            WAITING_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_link)],
            WAITING_OUTRO: [MessageHandler((filters.VIDEO | filters.Document.VIDEO | filters.Document.ALL) & ~filters.COMMAND, receive_outro)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("cancel", cancel))
    return app


def main() -> None:
    global telegram_app
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN env var missing.")

    telegram_app = build_telegram_app()

    if USE_WEBHOOK:
        if not BASE_URL:
            raise SystemExit("BASE_URL env var missing. Required for webhook mode.")
        webhook_url = f"{BASE_URL}/telegram/{WEBHOOK_SECRET}"

        async def runner() -> None:
            await telegram_app.initialize()
            await telegram_app.bot.delete_webhook(drop_pending_updates=True)
            await telegram_app.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
            await telegram_app.start()
            print(f"Telegram webhook set: {webhook_url}")
            run_flask()

        asyncio.run(runner())
    else:
        thread = threading.Thread(target=run_flask, daemon=True)
        thread.start()
        telegram_app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
