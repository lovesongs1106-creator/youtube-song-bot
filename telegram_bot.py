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
    generate_reference_thumbnail,
    generate_seo_metadata,
    get_youtube_title,
    render_video,
    slugify,
)

WAITING_TITLE, WAITING_ARTIST, WAITING_REFERENCE, WAITING_LINK, WAITING_OUTRO = range(5)

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

# Webhook mode is better for free hosting because incoming Telegram messages wake the app.
USE_WEBHOOK = os.environ.get("USE_WEBHOOK", "true").strip().lower() in {"1", "true", "yes", "on"}
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me-secret").strip()

# If true, Render only controls Telegram and dispatches heavy video work to GitHub Actions.
USE_GITHUB_WORKER = os.environ.get("USE_GITHUB_WORKER", "false").strip().lower() in {"1", "true", "yes", "on"}
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()  # example: username/youtube-song-bot
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()  # PAT with repo dispatch permission
GITHUB_EVENT_TYPE = os.environ.get("GITHUB_EVENT_TYPE", "render_video").strip()

GOOGLE_CLIENT_SECRETS_PATH = ROOT / "client_secrets.json"
if os.environ.get("GOOGLE_CLIENT_SECRETS_JSON") and not GOOGLE_CLIENT_SECRETS_PATH.exists():
    GOOGLE_CLIENT_SECRETS_PATH.write_text(os.environ["GOOGLE_CLIENT_SECRETS_JSON"], encoding="utf-8")

# In-memory OAuth state -> Telegram user ID + PKCE verifier mapping.
OAUTH_STATES: dict[str, dict[str, Any]] = {}
telegram_app: Application | None = None
BOT_LOOP: asyncio.AbstractEventLoop | None = None
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
    """Accept owner/repo OR GitHub URL and return owner/repo."""
    repo = (raw or "").strip()
    repo = repo.replace("https://github.com/", "").replace("http://github.com/", "")
    repo = repo.replace("github.com/", "")
    repo = repo.strip().strip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    # If user pasted URL with extra path, keep only owner/repo
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
    body = {"event_type": GITHUB_EVENT_TYPE, "client_payload": payload}
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
    """Send the user's YouTube OAuth token JSON for adding to GitHub Actions secrets.

    Owner-only if AUTHORIZED_TELEGRAM_USER_ID is set. Treat this as highly sensitive.
    """
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
    # PKCE needs the same code_verifier during callback. If we don't keep it,
    # Google returns: invalid_grant Missing code verifier.
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
        # Some hosts pass the callback to Flask as http internally even though the public URL is https.
        # OAuth requires https, so rebuild the callback from BASE_URL.
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
        "Naya video start ✅\n\n"
        "Pehle full song title bhejo. Example:\n"
        "Oye Hoye Kya Scene Hai"
    )
    return WAITING_TITLE


async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await reject_if_unauthorized(update):
        return ConversationHandler.END
    title = (update.message.text or "").strip()
    if len(title) < 2:
        await update.message.reply_text("Proper song title bhejo.")
        return WAITING_TITLE
    context.user_data["song_name"] = title
    await update.message.reply_text(
        "Artist/channel/show name bhejo. Example:\n"
        "India's Got Latent Season 2\n\n"
        "Agar nahi chahiye to /skip bhejo."
    )
    return WAITING_ARTIST


async def receive_artist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await reject_if_unauthorized(update):
        return ConversationHandler.END
    artist = (update.message.text or "").strip()
    context.user_data["artist"] = artist
    await update.message.reply_text(
        "Ab thumbnail ke liye reference image/photo bhejo.\n\n"
        "Ye image poster me use hogi aur uske upar attractive text design banega.\n"
        "Agar reference nahi dena to /skip bhejo."
    )
    return WAITING_REFERENCE


async def skip_artist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await reject_if_unauthorized(update):
        return ConversationHandler.END
    context.user_data["artist"] = None
    await update.message.reply_text(
        "Ab thumbnail ke liye reference image/photo bhejo. Agar reference nahi dena to /skip bhejo."
    )
    return WAITING_REFERENCE


async def receive_reference(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await reject_if_unauthorized(update):
        return ConversationHandler.END
    if not update.effective_user or not update.message:
        return ConversationHandler.END

    ref_file_id = None
    ext = ".jpg"
    if update.message.photo:
        ref_file_id = update.message.photo[-1].file_id
        ext = ".jpg"
    elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
        ref_file_id = update.message.document.file_id
        ext = Path(update.message.document.file_name or "reference.jpg").suffix or ".jpg"
    else:
        await update.message.reply_text("Please image/photo bhejo, ya /skip bhejo.")
        return WAITING_REFERENCE

    context.user_data["reference_file_id"] = ref_file_id
    context.user_data["reference_ext"] = ext
    await ask_audio_source(update)
    return WAITING_LINK


async def skip_reference(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await reject_if_unauthorized(update):
        return ConversationHandler.END
    context.user_data["reference_file_id"] = None
    await ask_audio_source(update)
    return WAITING_LINK


async def ask_audio_source(update: Update) -> None:
    await update.message.reply_text(
        "Ab song audio source bhejo:\n\n"
        "Option 1: Licensed MP3/M4A audio file bhejo — recommended.\n"
        "Option 2: YouTube link bhejo — kabhi-kabhi cloud par block ho sakta hai.\n\n"
        "Example link:\nhttps://www.youtube.com/watch?v=VIDEO_ID"
    )

async def receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await reject_if_unauthorized(update):
        return ConversationHandler.END

    # Source can be a YouTube link OR an uploaded audio file.
    if update.message.text:
        text = update.message.text or ""
        url = extract_youtube_url(text)
        if not url:
            await update.message.reply_text(
                "Valid YouTube link nahi mila. Ya to proper YouTube link bhejo, ya licensed MP3/M4A audio file bhejo."
            )
            return WAITING_LINK

        detected_title = get_youtube_title(url) or context.user_data.get("song_name") or "YouTube Song"
        context.user_data["source_type"] = "youtube_url"
        context.user_data["youtube_url"] = url
        context.user_data.setdefault("song_name", detected_title)
        await update.message.reply_text(
            f"Audio source set ho gaya. Title: {context.user_data['song_name']}\n\n"
            "Ab apna 30–40 sec outro video bhejo as Telegram video/document."
        )
        return WAITING_OUTRO

    tg_file_id = None
    ext = ".mp3"
    title = "Telegram Audio"

    if update.message.audio:
        tg_file_id = update.message.audio.file_id
        title = update.message.audio.title or update.message.audio.file_name or "Telegram Audio"
        ext = Path(update.message.audio.file_name or "song.mp3").suffix or ".mp3"
    elif update.message.document:
        name = update.message.document.file_name or "song.mp3"
        ext = Path(name).suffix.lower() or ".mp3"
        if ext not in AUDIO_EXTS:
            await update.message.reply_text(
                "Ye audio file nahi lag rahi. Please MP3/M4A/WAV/AAC/FLAC/OGG file bhejo, ya YouTube link bhejo."
            )
            return WAITING_LINK
        tg_file_id = update.message.document.file_id
        title = Path(name).stem or "Telegram Audio"
    else:
        await update.message.reply_text("Please YouTube link ya song audio file bhejo.")
        return WAITING_LINK

    context.user_data["source_type"] = "telegram_audio"
    context.user_data["audio_file_id"] = tg_file_id
    context.user_data["audio_ext"] = ext
    context.user_data.setdefault("song_name", title)
    await update.message.reply_text(
        f"Audio mil gaya. Title: {context.user_data['song_name']}\n\n"
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
    source_type = context.user_data.get("source_type", "youtube_url")
    youtube_url = context.user_data.get("youtube_url")
    song_name = context.user_data["song_name"]
    artist = context.user_data.get("artist")

    try:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        base = slugify(song_name)
        job_dir = OUTPUTS_DIR / f"telegram-{user_id}-{base}-{stamp}"
        job_dir.mkdir(parents=True, exist_ok=True)
        outro_path = job_dir / f"outro{ext}"
        thumb_path = job_dir / "thumbnail.jpg"
        video_path = job_dir / f"{base}.mp4"
        workdir = job_dir / "work"

        if USE_GITHUB_WORKER:
            job_payload = {
                "job_id": f"{user_id}-{stamp}",
                "chat_id": update.effective_chat.id,
                "user_id": user_id,
                "song_name": song_name,
                "artist": artist,
                "source_type": source_type,
                "youtube_url": youtube_url,
                "audio_file_id": context.user_data.get("audio_file_id"),
                "audio_ext": context.user_data.get("audio_ext", ".mp3"),
                "reference_file_id": context.user_data.get("reference_file_id"),
                "reference_ext": context.user_data.get("reference_ext", ".jpg"),
                "outro_file_id": tg_file_id,
                "outro_ext": ext,
                "privacy": DEFAULT_PRIVACY,
            }
            await asyncio.to_thread(dispatch_github_worker, job_payload)
            await update.message.reply_text(
                "🚀 Job GitHub Actions worker ko bhej diya.\n"
                "Ab heavy 1080p/720p render GitHub par hoga. Status yahin Telegram par aayega."
            )
            context.user_data.clear()
            return ConversationHandler.END

        tg_file = await context.bot.get_file(tg_file_id)
        await tg_file.download_to_drive(custom_path=str(outro_path))

        if source_type == "youtube_url":
            await update.message.reply_text("Audio download kar raha hoon...")
            audio_path = await asyncio.to_thread(download_youtube_audio, youtube_url, job_dir / "downloaded_audio")
        else:
            await update.message.reply_text("Audio file download kar raha hoon...")
            audio_ext = context.user_data.get("audio_ext", ".mp3")
            audio_path = job_dir / f"source_audio{audio_ext}"
            audio_tg_file = await context.bot.get_file(context.user_data["audio_file_id"])
            await audio_tg_file.download_to_drive(custom_path=str(audio_path))

        await update.message.reply_text("Thumbnail aur video render kar raha hoon...")
        ref_file_id = context.user_data.get("reference_file_id")
        if ref_file_id:
            ref_ext = context.user_data.get("reference_ext", ".jpg")
            ref_path = job_dir / f"reference{ref_ext}"
            ref_tg_file = await context.bot.get_file(ref_file_id)
            await ref_tg_file.download_to_drive(custom_path=str(ref_path))
            await asyncio.to_thread(generate_reference_thumbnail, song_name, artist, ref_path, thumb_path)
        else:
            await asyncio.to_thread(generate_thumbnail, song_name, artist, thumb_path)

        await asyncio.to_thread(render_video, thumb_path, audio_path, outro_path, video_path, workdir)

        metadata = generate_seo_metadata(song_name, artist, youtube_url)
        await update.message.reply_text(
            "SEO metadata ready ✅\n\n"
            f"Title: {metadata['title']}\n\n"
            f"Tags: {', '.join(metadata['tags'][:8])}..."
        )
        await update.message.reply_text(f"YouTube par upload kar raha hoon as {DEFAULT_PRIVACY}...")
        video_id = await asyncio.to_thread(
            upload_for_user,
            user_id,
            video_path,
            thumb_path,
            metadata["title"],
            metadata["description"],
            metadata["tags"],
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
    app.add_handler(CommandHandler("export_youtube_token", export_youtube_token))
    app.add_handler(CommandHandler("github_test", github_test))

    conv = ConversationHandler(
        entry_points=[CommandHandler("new", new_video)],
        states={
            WAITING_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
            WAITING_ARTIST: [
                CommandHandler("skip", skip_artist),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_artist),
            ],
            WAITING_REFERENCE: [
                CommandHandler("skip", skip_reference),
                MessageHandler((filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND, receive_reference),
            ],
            WAITING_LINK: [MessageHandler((filters.TEXT | filters.AUDIO | filters.Document.ALL) & ~filters.COMMAND, receive_link)],
            WAITING_OUTRO: [MessageHandler((filters.VIDEO | filters.Document.VIDEO | filters.Document.ALL) & ~filters.COMMAND, receive_outro)],
        },
        fallbacks=[CommandHandler("skip", skip_reference), CommandHandler("cancel", cancel)],
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
