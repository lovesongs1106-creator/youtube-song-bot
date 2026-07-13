#!/usr/bin/env python3
"""
Telegram front-end for YouTube Song Automation Bot.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import secrets
import threading
import requests
from pathlib import Path
from typing import Any

from flask import Flask, request
from werkzeug.middleware.proxy_fix import ProxyFix
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)

from bot import (
    YOUTUBE_UPLOAD_SCOPE,
    download_youtube_audio,
    generate_thumbnail,
    generate_reference_thumbnail,
    generate_seo_metadata,
    get_youtube_title,
    render_video,
    slugify,
)

from feature_flags import (
    ENABLE_TREND_AGENT,
    ENABLE_RECOMMENDATIONS,
    ENABLE_APPROVE_WORKFLOW,
)

from agents.simple_trend import generate_daily_report

WAITING_TITLE, WAITING_ARTIST, WAITING_REFERENCE, WAITING_LINK, WAITING_OUTRO, WAITING_RETRY_AUDIO = range(6)

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

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".webm"}

USE_WEBHOOK = os.environ.get("USE_WEBHOOK", "true").strip().lower() in {"1", "true", "yes", "on"}
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me-secret").strip()

USE_GITHUB_WORKER = os.environ.get("USE_GITHUB_WORKER", "false").strip().lower() in {"1", "true", "yes", "on"}
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_EVENT_TYPE = os.environ.get("GITHUB_EVENT_TYPE", "render_video").strip()

GOOGLE_CLIENT_SECRETS_PATH = ROOT / "client_secrets.json"
if os.environ.get("GOOGLE_CLIENT_SECRETS_JSON") and not GOOGLE_CLIENT_SECRETS_PATH.exists():
    GOOGLE_CLIENT_SECRETS_PATH.write_text(os.environ["GOOGLE_CLIENT_SECRETS_JSON"], encoding="utf-8")

OAUTH_STATES: dict[str, dict[str, Any]] = {}
PENDING_RETRY_JOBS: dict[int, dict[str, Any]] = {}
telegram_app: Application | None = None
BOT_LOOP: asyncio.AbstractEventLoop | None = None
flask_app = Flask(__name__)
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


def make_flow(state: str | None = None, code_verifier: str | None = None) -> Flow:
    if not BASE_URL:
        raise RuntimeError("BASE_URL env var is required for mobile OAuth.")
    kwargs = {}
    if code_verifier:
        kwargs["code_verifier"] = code_verifier
    flow = Flow.from_client_config(
        load_client_config(),
        scopes=YOUTUBE_UPLOAD_SCOPE,
        state=state,
        redirect_uri=f"{BASE_URL}/oauth2callback",
        **kwargs,
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


def normalize_github_repo(raw: str) -> str:
    repo = (raw or "").strip()
    repo = repo.replace("https://github.com/", "").replace("http://github.com/", "")
    repo = repo.replace("github.com/", "")
    repo = repo.strip().strip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    parts = [x for x in repo.split("/") if x]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return repo


def github_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_repo_diagnostics() -> str:
    if not GITHUB_REPO:
        return "❌ GITHUB_REPO missing. Format: username/repo-name"
    if not GITHUB_TOKEN:
        return "❌ GITHUB_TOKEN missing. Add GitHub PAT in Render env."
    repo = normalize_github_repo(GITHUB_REPO)
    url = f"https://api.github.com/repos/{repo}"
    r = requests.get(url, headers=github_headers(), timeout=60)
    if r.status_code == 200:
        data = r.json()
        return (
            "✅ GitHub repo access OK\n"
            f"Repo: {data.get('full_name')}\n"
            f"Private: {data.get('private')}\n"
            "Dispatch should work if workflow file is on default branch."
        )
    if r.status_code == 404:
        return (
            "❌ GitHub repo access failed: 404 Not Found\n\n"
            f"GITHUB_REPO currently: {GITHUB_REPO}\n"
            f"Parsed as: {repo}\n\n"
            "Fix checklist:\n"
            "1. GITHUB_REPO must be exactly: username/repo-name\n"
            "2. Do NOT use full URL unless latest code parses it.\n"
            "3. Repo must exist.\n"
            "4. If repo is private, PAT must have access to that repo.\n"
            "5. Fine-grained PAT: Repository access = selected repo, Permissions > Contents = Read and Write.\n"
            "6. Classic PAT: scope repo enabled."
        )
    return f"❌ GitHub repo check failed: {r.status_code}\n{r.text[:1000]}"


def dispatch_github_worker(payload: dict[str, Any]) -> None:
    if not GITHUB_REPO:
        raise RuntimeError("GITHUB_REPO env var missing. Example: yourname/youtube-song-bot")
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN env var missing. Add GitHub PAT in Render env.")
    repo = normalize_github_repo(GITHUB_REPO)
    url = f"https://api.github.com/repos/{repo}/dispatches"
    body = {"event_type": GITHUB_EVENT_TYPE, "client_payload": {"job": payload}}
    r = requests.post(url, headers=github_headers(), json=body, timeout=60)
    if r.status_code not in (200, 201, 202, 204):
        if r.status_code == 404:
            diag = github_repo_diagnostics()
            raise RuntimeError(
                "GitHub dispatch failed: 404 Not Found\n\n"
                + diag
                + "\n\nRender env expected:\n"
                "GITHUB_REPO=username/repo-name\n"
                "GITHUB_TOKEN=PAT with repo access"
            )
        raise RuntimeError(f"GitHub dispatch failed: {r.status_code} {r.text[:1000]}")


# ==================== DAILY REPORT + APPROVE WORKFLOW ====================

async def daily_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    if not ENABLE_RECOMMENDATIONS:
        await update.message.reply_text("Trend recommendations are currently disabled.")
        return

    report_text = generate_daily_report()

    keyboard = [
        [InlineKeyboardButton("Approve Song", callback_data="approve_song")],
        [InlineKeyboardButton("Refresh", callback_data="refresh_report")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(report_text, reply_markup=reply_markup)


async def approve_song_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not ENABLE_APPROVE_WORKFLOW:
        await query.edit_message_text("Approve workflow is currently disabled.")
        return

    song_name = "Sample Trending Song"
    artist = "Sample Artist"

    metadata = generate_seo_metadata(song_name, artist, None)

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = {
        "job_id": f"trend-{stamp}",
        "chat_id": update.effective_chat.id,
        "user_id": update.effective_user.id,
        "song_name": song_name,
        "artist": artist,
        "source_type": "telegram_audio",
        "privacy": DEFAULT_PRIVACY,
        "custom_title": metadata["title"],
        "custom_description": metadata["description"],
        "custom_tags": metadata["tags"],
    }

    await asyncio.to_thread(dispatch_github_worker, payload)

    await query.edit_message_text(
        f"✅ Approved!\n\n"
        f"Title: {metadata['title']}\n"
        f"Privacy: {DEFAULT_PRIVACY}\n\n"
        "🚀 Job sent to GitHub Actions. You will receive the private YouTube link here."
    )


async def refresh_report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not ENABLE_RECOMMENDATIONS:
        await query.edit_message_text("Trend recommendations are currently disabled.")
        return

    report_text = generate_daily_report()
    keyboard = [
        [InlineKeyboardButton("Approve Song", callback_data="approve_song")],
        [InlineKeyboardButton("Refresh", callback_data="refresh_report")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(report_text, reply_markup=reply_markup)


# ==================== OLD FUNCTIONS (KEEPING ALL) ====================

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
        "/export_youtube_token - GitHub Actions secret ke liye token export karo\n"
        "/github_test - GitHub repo/token connection check karo\n"
        "/new - Naya video banao aur upload karo\n"
        "/id - Apna Telegram user ID dekho\n"
        "/cancel - Current process cancel"
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.message:
        await update.message.reply_text(f"Your Telegram user ID: {update.effective_user.id}")


async def github_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    if not update.message:
        return
    await update.message.reply_text("GitHub connection test kar raha hoon...")
    try:
        result = await asyncio.to_thread(github_repo_diagnostics)
        await update.message.reply_text(result)
    except Exception as exc:
        await update.message.reply_text(f"❌ GitHub test error:\n{exc}")


async def export_youtube_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    if not update.effective_user or not update.message:
        return
    token_path = token_file_for_user(update.effective_user.id)
    if not token_path.exists():
        await update.message.reply_text("YouTube token nahi mila. Pehle /auth complete karo.")
        return
    await update.message.reply_text(
        "⚠️ Sensitive token export. Is content ko sirf GitHub repo Secret me paste karna:\n"
        "Secret name: YOUTUBE_TOKEN_JSON\n\n"
        "Isko kisi ke saath share mat karna."
    )
    await update.message.reply_document(
        document=token_path.open("rb"),
        filename="YOUTUBE_TOKEN_JSON.txt",
        caption="GitHub Secret me value ke andar is file ka full content paste karo."
    )


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
    code_verifier = secrets.token_urlsafe(64)
    OAUTH_STATES[state] = {"user_id": update.effective_user.id, "code_verifier": code_verifier}
    flow = make_flow(state=state, code_verifier=code_verifier)
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
        if BOT_LOOP is None:
            return "Bot loop not ready", 503
        update = Update.de_json(request.get_json(force=True), telegram_app.bot)
        asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), BOT_LOOP)
        return "OK"
    except Exception as exc:
        return f"Webhook error: {exc}", 500


@flask_app.route("/oauth2callback")
def oauth2callback():
    state = request.args.get("state", "")
    state_data = OAUTH_STATES.get(state)
    if not state_data:
        return "Invalid/expired OAuth state. Go back to Telegram and send /auth again.", 400
    user_id = int(state_data["user_id"])
    code_verifier = state_data.get("code_verifier")
    try:
        flow = make_flow(state=state, code_verifier=code_verifier)
        authorization_response = f"{BASE_URL}/oauth2callback"
        if request.query_string:
            authorization_response += "?" + request.query_string.decode("utf-8")
        flow.fetch_token(authorization_response=authorization_response)
        creds = flow.credentials
        token_file_for_user(user_id).write_text(creds.to_json(), encoding="utf-8")
        OAUTH_STATES.pop(state, None)

        if telegram_app and BOT_LOOP:
            asyncio.run_coroutine_threadsafe(
                telegram_app.bot.send_message(
                    chat_id=user_id,
                    text="✅ YouTube channel connected. Ab /new bhejo aur video banao.",
                ),
                BOT_LOOP,
            )
        return "Success! YouTube connected. You can close this page and go back to Telegram."
    except Exception as exc:
        return f"OAuth failed: {exc}", 500


def parse_tags_text(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    tags = [t.strip() for t in raw.replace("#", "").split(",") if t.strip()]
    return tags or None


def parse_quick_details(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    raw = (text or "").strip()
    if not raw:
        return data
    keymap = {
        "title": "song_name",
        "song": "song_name",
        "song title": "song_name",
        "artist": "artist",
        "channel": "artist",
        "show": "artist",
        "youtube": "youtube_url",
        "yt": "youtube_url",
        "link": "youtube_url",
        "url": "youtube_url",
        "yt title": "custom_title",
        "youtube title": "custom_title",
        "upload title": "custom_title",
        "description": "custom_description",
        "desc": "custom_description",
        "tags": "custom_tags_raw",
    }
    for line in raw.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            key = keymap.get(k.strip().lower())
            if key and v.strip():
                data[key] = v.strip()
    url = extract_youtube_url(raw)
    if url:
        data["youtube_url"] = url
    if not data and "|" in raw:
        parts = [x.strip() for x in raw.split("|") if x.strip()]
        if parts:
            data["song_name"] = parts[0]
        if len(parts) > 1:
            data["artist"] = parts[1]
        if len(parts) > 2 and extract_youtube_url(parts[2]):
            data["youtube_url"] = extract_youtube_url(parts[2])
    elif not data:
        if not url:
            data["song_name"] = raw
    if data.get("custom_tags_raw"):
        data["custom_tags"] = parse_tags_text(data.pop("custom_tags_raw"))
    return data


async def maybe_dispatch_if_ready(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    data = context.user_data
    has_source = data.get("source_type") == "youtube_url" or bool(data.get("audio_file_id"))
    if not (data.get("song_name") and has_source and data.get("outro_file_id")):
        missing = []
        if not data.get("song_name"):
            missing.append("song title")
        if not has_source:
            missing.append("audio file ya YouTube link")
        if not data.get("outro_file_id"):
            missing.append("outro video")
        await update.message.reply_text("Abhi missing hai: " + ", ".join(missing))
        return False

    user_id = update.effective_user.id
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = {
        "job_id": f"{user_id}-{stamp}",
        "chat_id": update.effective_chat.id,
        "user_id": user_id,
        "song_name": data["song_name"],
        "artist": data.get("artist"),
        "source_type": data.get("source_type", "telegram_audio"),
        "youtube_url": data.get("youtube_url"),
        "audio_file_id": data.get("audio_file_id"),
        "audio_ext": data.get("audio_ext", ".mp3"),
        "thumbnail_file_id": data.get("thumbnail_file_id"),
        "thumbnail_ext": data.get("thumbnail_ext", ".jpg"),
        "reference_file_id": data.get("reference_file_id"),
        "reference_ext": data.get("reference_ext", ".jpg"),
        "outro_file_id": data.get("outro_file_id"),
        "outro_ext": data.get("outro_ext", ".mp4"),
        "privacy": DEFAULT_PRIVACY,
        "custom_title": data.get("custom_title"),
        "custom_description": data.get("custom_description"),
        "custom_tags": data.get("custom_tags"),
    }
    PENDING_RETRY_JOBS[user_id] = payload.copy()
    if USE_GITHUB_WORKER:
        await asyncio.to_thread(dispatch_github_worker, payload)
        await update.message.reply_text("🚀 Job GitHub Actions worker ko bhej diya. Status yahin aayega.")
    else:
        await update.message.reply_text("Local render mode me quick dispatch supported nahi. USE_GITHUB_WORKER=true recommended.")
    context.user_data.clear()
    return True


# ==================== build_telegram_app ====================

def build_telegram_app() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("auth", auth))
    app.add_handler(CommandHandler("export_youtube_token", export_youtube_token))
    app.add_handler(CommandHandler("github_test", github_test))
    app.add_handler(CommandHandler("audio_retry", audio_retry))

    conv = ConversationHandler(
        entry_points=[CommandHandler("new", new_video)],
        states={
            WAITING_TITLE: [MessageHandler((filters.TEXT | filters.PHOTO | filters.AUDIO | filters.VIDEO | filters.Document.ALL) & ~filters.COMMAND, collect_asset_or_text)],
            WAITING_ARTIST: [
                CommandHandler("skip", skip_artist),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_artist),
            ],
            WAITING_REFERENCE: [
                CommandHandler("skip", skip_reference),
                MessageHandler((filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND, receive_reference),
            ],
            WAITING_LINK: [MessageHandler((filters.TEXT | filters.PHOTO | filters.AUDIO | filters.VIDEO | filters.Document.ALL) & ~filters.COMMAND, collect_asset_or_text)],
            WAITING_OUTRO: [MessageHandler((filters.VIDEO | filters.Document.VIDEO | filters.Document.ALL) & ~filters.COMMAND, receive_outro)],
            WAITING_RETRY_AUDIO: [MessageHandler((filters.AUDIO | filters.Document.ALL) & ~filters.COMMAND, receive_retry_audio)],
        },
        fallbacks=[CommandHandler("skip", skip_reference), CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("cancel", cancel))

    if ENABLE_RECOMMENDATIONS:
        app.add_handler(CommandHandler("daily_report", daily_report))

    if ENABLE_APPROVE_WORKFLOW:
        app.add_handler(CallbackQueryHandler(approve_song_callback, pattern="^approve_song$"))
        app.add_handler(CallbackQueryHandler(refresh_report_callback, pattern="^refresh_report$"))

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

        def bot_loop_thread() -> None:
            global BOT_LOOP
            loop = asyncio.new_event_loop()
            BOT_LOOP = loop
            asyncio.set_event_loop(loop)
            loop.run_until_complete(telegram_app.initialize())
            loop.run_until_complete(telegram_app.bot.delete_webhook(drop_pending_updates=True))
            loop.run_until_complete(telegram_app.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES))
            loop.run_until_complete(telegram_app.start())
            print(f"Telegram webhook set: {webhook_url}", flush=True)
            loop.run_forever()

        thread = threading.Thread(target=bot_loop_thread, daemon=True)
        thread.start()
        run_flask()
    else:
        thread = threading.Thread(target=run_flask, daemon=True)
        thread.start()
        telegram_app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
