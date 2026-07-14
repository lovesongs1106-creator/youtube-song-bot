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

from feature_flags import (
    ENABLE_TREND_AGENT,
    ENABLE_RECOMMENDATIONS,
    ENABLE_APPROVE_WORKFLOW,
    ENABLE_AGENTREACH_IMPORT,
    ENABLE_OUTRO_ROTATION_V2,
    ENABLE_DASHBOARD,
    ENABLE_SYSTEM_HEALTH,
)

from agents.viral_trend_engine import generate_daily_report, trend_debug_info, collect_and_save_trends, get_real_trends
from agents.outro_manager import (
    add_outro,
    list_outros,
    remove_outro,
    select_outro,
    record_outro_usage,
    format_outro_list,
)
from agents.queue_engine import (
    add_multiple_items,
    add_multiple_items_transactional,
    get_summary,
    get_queue,
    cancel_all_pending,
    is_paused,
    set_paused,
    run_queue_processor,
    add_queue_item,
)
from agents.db import init_all_tables, is_already_uploaded, fetchone

WAITING_TITLE, WAITING_ARTIST, WAITING_REFERENCE, WAITING_LINK, WAITING_OUTRO, WAITING_RETRY_AUDIO, WAITING_OUTRO_VIDEO, WAITING_OUTRO_NAME, WAITING_OUTRO_WEIGHT, WAITING_AGENTREACH = range(10)

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
PENDING_RETRY_JOBS: dict[int, dict[str, Any]] = {}
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
    # GitHub repository_dispatch allows max 10 top-level client_payload properties.
    # Wrap everything inside one `job` object to avoid 422 errors.
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
        "/bulk_upload - Multiple songs queue karo\n"
        "/queue_status - Upload queue dekho\n"
        "/queue_pause - Queue roko\n"
        "/queue_resume - Queue chalao\n"
        "/queue_cancel - Pending items cancel karo\n"
        "/daily_report - Viral trends dekho\n"
        "/trend_debug - Trend engine diagnostics\n"
        "/outro_add - Outro video add karo\n"
        "/outro_list - Sab outro videos dekho\n"
        "/outro_remove <id> - Outro hatao\n"
        "/outro_test - Selection test karo\n"
        "/github_test - GitHub connection check\n"
        "/export_youtube_token - Token export karo\n"
        "/id - Apna Telegram user ID dekho\n"
        "/cancel - Current process cancel"
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.message:
        await update.message.reply_text(f"Your Telegram user ID: {update.effective_user.id}")


# ==================== OUTRO MANAGEMENT COMMANDS ====================

async def outro_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    if not update.message:
        return

    file_id = None
    ext = ".mp4"
    name = "outro"

    if update.message.video:
        file_id = update.message.video.file_id
        ext = ".mp4"
        name = update.message.video.file_name or "outro_video"
    elif update.message.document:
        file_id = update.message.document.file_id
        name = update.message.document.file_name or "outro"
        ext = Path(name).suffix or ".mp4"
        mime = update.message.document.mime_type or ""
        if not (ext in {".mp4", ".mov", ".mkv", ".webm"} or mime.startswith("video/")):
            await update.message.reply_text("Please send a video file (MP4/MOV/MKV/WEBM).")
            return
    else:
        await update.message.reply_text(
            "Send an outro video as a Telegram video or document.\n"
            "Optional: reply with /outro_add <name> <weight> to set metadata."
        )
        return

    # Parse optional name/weight from caption or command args
    weight = 10
    custom_name = None
    if context.args:
        custom_name = context.args[0]
        if len(context.args) > 1:
            try:
                weight = int(context.args[1])
            except ValueError:
                pass

    outro_name = custom_name or Path(name).stem or "outro"
    outro_id = add_outro(outro_name, file_id, ext, weight)
    await update.message.reply_text(
        f"✅ Outro added!\n\n"
        f"ID: {outro_id}\n"
        f"Name: {outro_name}\n"
        f"Weight: {weight}\n"
        f"Ext: {ext}\n\n"
        f"Total active outros: {len(list_outros())}"
    )


async def outro_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    if not update.message:
        return
    await update.message.reply_text(format_outro_list())


async def outro_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("Usage: /outro_remove <id>\n\nUse /outro_list to see IDs.")
        return
    try:
        outro_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID must be a number.")
        return

    if remove_outro(outro_id):
        await update.message.reply_text(f"✅ Outro {outro_id} removed.")
    else:
        await update.message.reply_text(f"❌ Outro {outro_id} not found.")


async def outro_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    if not update.message:
        return

    selected = select_outro()
    if not selected:
        await update.message.reply_text(
            "📭 No active outros available.\n\nUse /outro_add to upload outro videos first."
        )
        return

    await update.message.reply_text(
        f"🎲 Outro Selection Test\n\n"
        f"Selected: {selected['name']}\n"
        f"ID: {selected['outro_id']}\n"
        f"Ext: {selected['ext']}\n\n"
        f"This outro would be used for the next upload."
    )


# ==================== BULK UPLOAD QUEUE COMMANDS ====================

async def bulk_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    if not update.message:
        return
    context.user_data["awaiting_bulk"] = True
    context.user_data.pop("bulk_candidates", None)
    await update.message.reply_text(
        "📥 Bulk Upload Mode\n\n"
        "Send a list of songs in this format:\n\n"
        "Song Name 1 | https://youtube.com/watch?v=abc\n"
        "Song Name 2 | https://youtube.com/watch?v=def\n"
        "Song Name 3 | https://youtube.com/watch?v=ghi\n\n"
        "One song per line. Use | to separate name and URL."
    )


async def handle_bulk_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages when user is in bulk upload mode."""
    if not update.message or not update.message.text:
        return
    if not context.user_data.get("awaiting_bulk"):
        return

    text = update.message.text.strip()
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    valid = []
    invalid = []
    for line in lines:
        if "|" not in line:
            invalid.append((line, "Missing | separator"))
            continue
        parts = [p.strip() for p in line.split("|", 1)]
        song_name = parts[0]
        url = extract_youtube_url(parts[1]) if len(parts) > 1 else None
        if not song_name:
            invalid.append((line, "Missing song name"))
            continue
        if not url:
            invalid.append((line, "Invalid or missing YouTube URL"))
            continue
        valid.append({"song_name": song_name, "youtube_url": url})

    context.user_data["bulk_candidates"] = valid
    context.user_data["bulk_invalid"] = invalid
    context.user_data["awaiting_bulk"] = False

    summary = (
        f"📊 Bulk Upload Summary\n\n"
        f"Total lines: {len(lines)}\n"
        f"✅ Valid: {len(valid)}\n"
        f"❌ Invalid: {len(invalid)}\n\n"
    )
    if valid:
        summary += "Valid songs:\n"
        for i, item in enumerate(valid[:10], 1):
            summary += f"{i}. {item['song_name']}\n"
        if len(valid) > 10:
            summary += f"... and {len(valid) - 10} more\n"
    if invalid:
        summary += "\nInvalid entries:\n"
        for item, reason in invalid[:5]:
            summary += f"• {item[:40]}... ({reason})\n"

    keyboard = [
        [InlineKeyboardButton("✅ Confirm Upload", callback_data="bulk_confirm")],
        [InlineKeyboardButton("❌ Cancel", callback_data="bulk_cancel")],
    ]
    await update.message.reply_text(summary, reply_markup=InlineKeyboardMarkup(keyboard))


async def bulk_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    candidates = context.user_data.get("bulk_candidates", [])
    if not candidates:
        await query.edit_message_text("❌ No valid songs to upload. Send /bulk_upload again.")
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    ids = add_multiple_items(user_id, chat_id, candidates)

    context.user_data.pop("bulk_candidates", None)
    context.user_data.pop("bulk_invalid", None)

    await query.edit_message_text(
        f"✅ {len(ids)} songs added to upload queue!\n\n"
        f"Use /queue_status to check progress.\n"
        f"The queue processor will start automatically."
    )


async def bulk_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("bulk_candidates", None)
    context.user_data.pop("bulk_invalid", None)
    await query.edit_message_text("❌ Bulk upload cancelled.")


async def queue_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    if not update.message:
        return

    summary = get_summary()
    paused = is_paused()
    status_icon = "⏸️" if paused else "▶️"

    text = (
        f"{status_icon} Upload Queue Status\n\n"
        f"Total: {summary['total']}\n"
        f"⏳ Pending: {summary['pending']}\n"
        f"🔄 Processing: {summary['processing']}\n"
        f"✅ Completed: {summary['completed']}\n"
        f"❌ Failed: {summary['failed']}\n"
        f"🚫 Cancelled: {summary['cancelled']}\n\n"
    )

    if summary['pending'] > 0:
        items = get_queue(status="pending", limit=5)
        text += "Next up:\n"
        for item in items:
            text += f"  • {item['song_name'][:40]}\n"

    await update.message.reply_text(text)


async def queue_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    set_paused(True)
    await update.message.reply_text("⏸️ Queue paused. Current song will finish, then processing stops.")


async def queue_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    set_paused(False)
    await update.message.reply_text("▶️ Queue resumed. Processing will continue shortly.")


async def queue_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    count = cancel_all_pending()
    await update.message.reply_text(f"🚫 Cancelled {count} pending item(s) from the queue.")




# ==================== OUTRO CONVERSATION FLOW ====================

# ==================== OUTRO SINGLE-SHOT (WEBHOOK-SAFE) ====================

async def outro_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start /outro_add conversation. Ask for video file."""
    if await reject_if_unauthorized(update):
        return ConversationHandler.END
    if not update.message:
        return ConversationHandler.END
    await update.message.reply_text(
        "📤 Outro Upload\n\n"
        "Send your outro video as a Telegram video or document (MP4/MOV).\n"
        "Max size: 500MB"
    )
    return WAITING_OUTRO_VIDEO


async def receive_outro_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive outro video file."""
    if await reject_if_unauthorized(update):
        return ConversationHandler.END
    if not update.message:
        return ConversationHandler.END

    file_id = None
    file_unique_id = None
    ext = ".mp4"

    if update.message.video:
        file_id = update.message.video.file_id
        file_unique_id = update.message.video.file_unique_id
        ext = ".mp4"
    elif update.message.document:
        file_id = update.message.document.file_id
        file_unique_id = update.message.document.file_unique_id
        name = update.message.document.file_name or "outro.mp4"
        ext = Path(name).suffix or ".mp4"
        mime = update.message.document.mime_type or ""
        if not (ext in {".mp4", ".mov", ".mkv", ".webm"} or mime.startswith("video/")):
            await update.message.reply_text("Please send a video file (MP4/MOV/MKV/WEBM).")
            return WAITING_OUTRO_VIDEO
    else:
        await update.message.reply_text("Please send a video file.")
        return WAITING_OUTRO_VIDEO

    # Check file size (500MB max)
    file_size = 0
    if update.message.video:
        file_size = update.message.video.file_size or 0
    elif update.message.document:
        file_size = update.message.document.file_size or 0
    if file_size > 500 * 1024 * 1024:
        await update.message.reply_text("❌ File too large. Maximum 500MB allowed.")
        return ConversationHandler.END

    context.user_data["outro_file_id"] = file_id
    context.user_data["outro_file_unique_id"] = file_unique_id
    context.user_data["outro_ext"] = ext

    await update.message.reply_text(
        "✅ Video received.\n\n"
        "What name should I save this outro as?\n"
        "Example: Summer Outro"
    )
    return WAITING_OUTRO_NAME


async def receive_outro_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive outro name."""
    if await reject_if_unauthorized(update):
        return ConversationHandler.END
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("Please enter a name.")
        return WAITING_OUTRO_NAME

    context.user_data["outro_name"] = name
    await update.message.reply_text(
        f"Got it: '{name}'\n\n"
        "What weight for selection? (1-100)\n"
        "Higher = more frequently selected.\n"
        "Default: 10"
    )
    return WAITING_OUTRO_WEIGHT


async def receive_outro_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive outro weight and save to database."""
    if await reject_if_unauthorized(update):
        return ConversationHandler.END
    try:
        weight = int(update.message.text or "10")
        if weight < 1 or weight > 100:
            raise ValueError("Weight must be 1-100")
    except ValueError:
        await update.message.reply_text("Please enter a number between 1 and 100.")
        return WAITING_OUTRO_WEIGHT

    outro_id = add_outro(
        name=context.user_data["outro_name"],
        file_id=context.user_data["outro_file_id"],
        ext=context.user_data["outro_ext"],
        weight=weight,
        file_unique_id=context.user_data.get("outro_file_unique_id"),
    )

    await update.message.reply_text(
        f"✅ Outro added!\n\n"
        f"ID: {outro_id}\n"
        f"Name: {context.user_data['outro_name']}\n"
        f"Weight: {weight}\n"
        f"Ext: {context.user_data['outro_ext']}\n\n"
        f"Total active outros: {len(list_outros())}"
    )
    return ConversationHandler.END


async def outro_add_single_shot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Webhook-safe single-shot outro upload.
    
    Usage: Send a video/document with caption:
      /outro_add My Outro Name 10
    Or reply to a video with:
      /outro_add My Outro Name 10
    """
    if await reject_if_unauthorized(update):
        return
    if not update.message:
        return

    # Try to get video from message or replied message
    target_msg = update.message
    if update.message.reply_to_message:
        target_msg = update.message.reply_to_message

    file_id = None
    file_unique_id = None
    ext = ".mp4"
    file_size = 0

    if target_msg.video:
        file_id = target_msg.video.file_id
        file_unique_id = target_msg.video.file_unique_id
        ext = ".mp4"
        file_size = target_msg.video.file_size or 0
    elif target_msg.document:
        file_id = target_msg.document.file_id
        file_unique_id = target_msg.document.file_unique_id
        name = target_msg.document.file_name or "outro.mp4"
        ext = Path(name).suffix or ".mp4"
        mime = target_msg.document.mime_type or ""
        file_size = target_msg.document.file_size or 0
        if not (ext in {".mp4", ".mov", ".mkv", ".webm"} or mime.startswith("video/")):
            await update.message.reply_text("❌ Replied message is not a video file.")
            return
    else:
        await update.message.reply_text(
            "📤 Outro Upload (Webhook Mode)\n\n"
            "Send a video with caption:\n"
            "`/outro_add Outro Name 10`\n\n"
            "Or reply to a video with:\n"
            "`/outro_add Outro Name 10`"
        )
        return

    if file_size > 500 * 1024 * 1024:
        await update.message.reply_text("❌ File too large. Maximum 500MB allowed.")
        return

    # Parse name and weight from args
    args = context.args or []
    weight = 10
    name = "Outro"
    if args:
        # Last arg might be weight
        if len(args) > 1 and args[-1].isdigit():
            weight = int(args[-1])
            name = " ".join(args[:-1])
        else:
            name = " ".join(args)
    else:
        name = Path(target_msg.document.file_name if target_msg.document else "outro").stem or "Outro"

    outro_id = add_outro(name=name, file_id=file_id, ext=ext, weight=weight, file_unique_id=file_unique_id)
    await update.message.reply_text(
        f"✅ Outro added!\n\n"
        f"ID: {outro_id}\n"
        f"Name: {name}\n"
        f"Weight: {weight}\n"
        f"File ID: `{file_id}`\n"
        f"Ext: {ext}\n\n"
        f"Total active outros: {len(list_outros())}"
    )


# ==================== AGENT REACH IMPORT (PHASE 2) ====================

def _store_agentreach_session(user_id: int, candidates: list, invalid: list, duplicate: list, source: str = "text") -> str:
    """Store agentreach session in DB for webhook-safe retrieval."""
    import json, secrets
    from agents.db import execute
    session_id = secrets.token_urlsafe(16)
    execute(
        """INSERT INTO system_state (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (f"agentreach_{user_id}_{session_id}", json.dumps({
            "candidates": candidates,
            "invalid": invalid,
            "duplicate": duplicate,
            "source": source,
            "created_at": dt.datetime.now().isoformat(),
        }))
    )
    return session_id


def _load_agentreach_session(user_id: int, session_id: str) -> dict | None:
    import json
    from agents.db import fetchone, execute
    row = fetchone("SELECT value FROM system_state WHERE key = ?", (f"agentreach_{user_id}_{session_id}",))
    if not row:
        return None
    execute("DELETE FROM system_state WHERE key = ?", (f"agentreach_{user_id}_{session_id}",))
    return json.loads(row["value"])


def _parse_agentreach_lines(lines: list[str]) -> tuple[list[dict], list[dict], list[dict]]:
    """Parse and validate Agent Reach lines.
    
    Returns: (valid_items, invalid_items, duplicate_items)
    """
    valid = []
    invalid = []
    duplicate = []
    seen_urls = set()
    seen_names = set()

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if "|" not in line:
            invalid.append({"line": line, "reason": "Missing | separator"})
            continue
        parts = [p.strip() for p in line.split("|", 1)]
        song_name = parts[0]
        url = extract_youtube_url(parts[1]) if len(parts) > 1 else None

        if not song_name:
            invalid.append({"line": line, "reason": "Missing song name"})
            continue
        if not url:
            invalid.append({"line": line, "reason": "Invalid or missing YouTube URL"})
            continue
        if url in seen_urls:
            duplicate.append({"song_name": song_name, "youtube_url": url, "reason": "Duplicate URL in batch"})
            continue
        if song_name.lower() in seen_names:
            duplicate.append({"song_name": song_name, "youtube_url": url, "reason": "Duplicate song name in batch"})
            continue
        if is_already_uploaded(url):
            duplicate.append({"song_name": song_name, "youtube_url": url, "reason": "Already uploaded"})
            continue
        # Check if already in queue (pending/processing)
        existing = fetchone(
            "SELECT id FROM upload_queue WHERE youtube_url = ? AND status IN ('pending', 'processing') LIMIT 1",
            (url,)
        )
        if existing:
            duplicate.append({"song_name": song_name, "youtube_url": url, "reason": "Already in queue"})
            continue

        seen_urls.add(url)
        seen_names.add(song_name.lower())
        valid.append({"song_name": song_name, "youtube_url": url})

    return valid, invalid, duplicate


def _format_agentreach_summary(valid: list, invalid: list, duplicate: list, total_lines: int) -> str:
    """Format the validation summary message."""
    summary = (
        f"📊 Agent Reach Summary\n\n"
        f"📥 Total Songs: {total_lines}\n"
        f"✅ Valid: {len(valid)}\n"
        f"❌ Invalid: {len(invalid)}\n"
        f"🔄 Duplicates: {len(duplicate)}\n\n"
    )
    if valid:
        summary += "Valid songs:\n"
        for i, item in enumerate(valid[:10], 1):
            summary += f"{i}. {item['song_name']}\n"
        if len(valid) > 10:
            summary += f"... and {len(valid) - 10} more\n"
    if invalid:
        summary += "\n❌ Invalid entries:\n"
        for item in invalid[:5]:
            summary += f"• {item['line'][:40]}... ({item['reason']})\n"
    if duplicate:
        summary += "\n🔄 Duplicates:\n"
        for item in duplicate[:5]:
            summary += f"• {item['song_name'][:30]}... ({item['reason']})\n"
    return summary


async def agentreach_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start /agentreach command (Conversation mode for polling)."""
    if await reject_if_unauthorized(update):
        return ConversationHandler.END
    if not update.message:
        return ConversationHandler.END
    await update.message.reply_text(
        "📥 Agent Reach Import\n\n"
        "Paste your song list in this format:\n\n"
        "Song Name | https://youtube.com/watch?v=xxx\n"
        "Song Name 2 | https://youtube.com/watch?v=yyy\n\n"
        "One song per line. Use | to separate name and URL.\n"
        "Maximum 100 songs.\n\n"
        "Or send a CSV file with columns: song_name,youtube_url"
    )
    return WAITING_AGENTREACH


async def handle_agentreach_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Parse Agent Reach pasted list (Conversation mode)."""
    if not update.message or not update.message.text:
        return ConversationHandler.END

    text = update.message.text.strip()
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    if len(lines) > 100:
        await update.message.reply_text("❌ Too many songs. Maximum 100 allowed.")
        return ConversationHandler.END

    valid, invalid, duplicate = _parse_agentreach_lines(lines)

    context.user_data["agentreach_candidates"] = valid
    context.user_data["agentreach_invalid"] = invalid
    context.user_data["agentreach_duplicate"] = duplicate

    summary = _format_agentreach_summary(valid, invalid, duplicate, len(lines))
    keyboard = [
        [InlineKeyboardButton("✅ Add To Queue", callback_data="agentreach_confirm")],
        [InlineKeyboardButton("❌ Cancel", callback_data="agentreach_cancel")],
    ]
    await update.message.reply_text(summary, reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END


async def agentreach_single_shot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Webhook-safe /agentreach that parses text in one shot and stores session in DB.
    
    Supports:
      /agentreach (shows instructions)
      /agentreach Song 1 | https://...\nSong 2 | https://...
      Plain text list (when used as general message handler)
    """
    try:
        if await reject_if_unauthorized(update):
            return
        if not update.message or not update.message.text:
            return

        text = update.message.text.strip()
        
        # Strip /agentreach command prefix if present
        if text.startswith("/agentreach"):
            text = text[len("/agentreach"):].strip()
        
        if not text:
            await update.message.reply_text(
                "📥 Agent Reach Import\n\n"
                "Send your song list in this format (same message as /agentreach):\n\n"
                "/agentreach\n"
                "Song Name 1 | https://youtube.com/watch?v=xxx\n"
                "Song Name 2 | https://youtube.com/watch?v=yyy\n\n"
                "Or paste the list directly after the command.\n"
                "Maximum 100 songs.\n\n"
                "You can also send a CSV file with columns: song_name,youtube_url"
            )
            return

        lines = [l.strip() for l in text.split("\n") if l.strip()]

        if len(lines) > 100:
            await update.message.reply_text("❌ Too many songs. Maximum 100 allowed.")
            return

        valid, invalid, duplicate = _parse_agentreach_lines(lines)
        user_id = update.effective_user.id
        session_id = _store_agentreach_session(user_id, valid, invalid, duplicate, source="text")

        summary = _format_agentreach_summary(valid, invalid, duplicate, len(lines))
        keyboard = [
            [InlineKeyboardButton("✅ Add To Queue", callback_data=f"ar_confirm_{session_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"ar_cancel_{session_id}")],
        ]
        await update.message.reply_text(summary, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as exc:
        import traceback
        error_msg = f"❌ Agent Reach error:\n{exc}\n\n{traceback.format_exc()[:500]}"
        if update.message:
            await update.message.reply_text(error_msg)
        else:
            print(error_msg, flush=True)


async def agentreach_csv_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle CSV document upload for Agent Reach."""
    if await reject_if_unauthorized(update):
        return
    if not update.message or not update.message.document:
        return

    doc = update.message.document
    name = doc.file_name or ""
    if not name.lower().endswith(".csv"):
        await update.message.reply_text("❌ Please send a CSV file (.csv extension).")
        return

    # Download CSV
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        csv_path = TELEGRAM_DOWNLOADS_DIR / f"agentreach_{doc.file_id}.csv"
        await tg_file.download_to_drive(custom_path=str(csv_path))
    except Exception as exc:
        await update.message.reply_text(f"❌ Failed to download CSV: {exc}")
        return

    # Parse CSV
    import csv
    lines = []
    try:
        with csv_path.open("r", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) >= 2:
                    song_name = row[0].strip()
                    url = row[1].strip()
                    lines.append(f"{song_name} | {url}")
                elif len(row) == 1 and row[0].strip():
                    # Maybe tab-separated or malformed
                    lines.append(row[0].strip())
    except Exception as exc:
        await update.message.reply_text(f"❌ Failed to parse CSV: {exc}")
        return
    finally:
        csv_path.unlink(missing_ok=True)

    if len(lines) > 100:
        await update.message.reply_text("❌ Too many songs in CSV. Maximum 100 allowed.")
        return

    valid, invalid, duplicate = _parse_agentreach_lines(lines)
    user_id = update.effective_user.id
    session_id = _store_agentreach_session(user_id, valid, invalid, duplicate, source="csv")

    summary = _format_agentreach_summary(valid, invalid, duplicate, len(lines))
    keyboard = [
        [InlineKeyboardButton("✅ Add To Queue", callback_data=f"ar_confirm_{session_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"ar_cancel_{session_id}")],
    ]
    await update.message.reply_text(summary, reply_markup=InlineKeyboardMarkup(keyboard))


# ── Callbacks ──

async def agentreach_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Legacy polling-mode confirm."""
    query = update.callback_query
    await query.answer()

    candidates = context.user_data.get("agentreach_candidates", [])
    if not candidates:
        await query.edit_message_text("❌ No valid songs to add. Send /agentreach again.")
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    result = add_multiple_items_transactional(user_id, chat_id, candidates, source="agentreach")

    context.user_data.pop("agentreach_candidates", None)
    context.user_data.pop("agentreach_invalid", None)
    context.user_data.pop("agentreach_duplicate", None)

    if result["success"]:
        msg = (
            f"✅ {result['added_count']} songs added to upload queue!\n\n"
            f"⏳ Pending Count: {result['pending_total']}\n"
            f"📦 Queue IDs: {', '.join(str(i) for i in result['added_ids'][:5])}"
            f"{'...' if len(result['added_ids']) > 5 else ''}\n\n"
            f"Use /queue_status to check progress."
        )
    else:
        msg = f"❌ Batch failed:\n{result.get('error', 'Unknown error')}\n\nNo items were added."
    await query.edit_message_text(msg)


async def agentreach_db_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Webhook-safe confirm using DB session + transactional insert."""
    try:
        query = update.callback_query
        await query.answer()

        callback_data = query.data or ""
        session_id = callback_data.replace("ar_confirm_", "")
        user_id = update.effective_user.id
        session = _load_agentreach_session(user_id, session_id)
        if not session:
            await query.edit_message_text("❌ Session expired. Send /agentreach again.")
            return

        candidates = session.get("candidates", [])
        if not candidates:
            await query.edit_message_text("❌ No valid songs to add.")
            return

        chat_id = update.effective_chat.id
        result = add_multiple_items_transactional(user_id, chat_id, candidates, source="agentreach")

        if result["success"]:
            msg = (
                f"✅ {result['added_count']} songs added to upload queue!\n\n"
                f"⏳ Pending Count: {result['pending_total']}\n"
                f"📦 Queue IDs: {', '.join(str(i) for i in result['added_ids'][:5])}"
                f"{'...' if len(result['added_ids']) > 5 else ''}\n\n"
                f"Use /queue_status to check progress."
            )
        else:
            msg = f"❌ Batch failed:\n{result.get('error', 'Unknown error')}\n\nNo items were added."
        await query.edit_message_text(msg)
    except Exception as exc:
        import traceback
        error_msg = f"❌ Confirm error:\n{exc}\n\n{traceback.format_exc()[:500]}"
        if update.callback_query and update.callback_query.message:
            await update.callback_query.message.reply_text(error_msg)
        print(error_msg, flush=True)


async def agentreach_db_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Webhook-safe cancel using DB session."""
    query = update.callback_query
    await query.answer()
    callback_data = query.data or ""
    session_id = callback_data.replace("ar_cancel_", "")
    user_id = update.effective_user.id
    _load_agentreach_session(user_id, session_id)
    await query.edit_message_text("❌ Agent Reach import cancelled.")


async def agentreach_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Legacy polling-mode cancel."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("agentreach_candidates", None)
    context.user_data.pop("agentreach_invalid", None)
    context.user_data.pop("agentreach_duplicate", None)
    await query.edit_message_text("❌ Agent Reach import cancelled.")


# ==================== QUEUE COMMANDS ====================

async def queue_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    if not update.message:
        return

    summary = get_summary()
    paused = is_paused()
    status_icon = "⏸️" if paused else "▶️"

    text = (
        f"{status_icon} Upload Queue Status\n\n"
        f"Total: {summary['total']}\n"
        f"⏳ Pending: {summary['pending']}\n"
        f"🔄 Processing: {summary['processing']}\n"
        f"✅ Completed: {summary['completed']}\n"
        f"❌ Failed: {summary['failed']}\n"
        f"🚫 Cancelled: {summary['cancelled']}\n\n"
    )

    if summary['pending'] > 0:
        items = get_queue(status="pending", limit=5)
        text += "Next up:\n"
        for item in items:
            text += f"  • {item['song_name'][:40]}\n"

    await update.message.reply_text(text)


async def queue_pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    set_paused(True)
    await update.message.reply_text("⏸️ Queue paused. Current song will finish, then processing stops.")


async def queue_resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    set_paused(False)
    await update.message.reply_text("▶️ Queue resumed. Processing will continue shortly.")


async def queue_cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    count = cancel_all_pending()
    await update.message.reply_text(f"🚫 Cancelled {count} pending item(s) from the queue.")


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


def _get_commit_hash() -> str:
    import subprocess, os
    env_hash = os.environ.get("RENDER_GIT_COMMIT", "").strip()
    if env_hash:
        return env_hash[:7]
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


@flask_app.route("/diag")
def diag():
    """Production diagnostic endpoint. Returns trend engine, outro, and queue status."""
    from agents.viral_trend_engine import _yt_dlp_version, get_viral_trends, DB_PATH
    from agents.outro_manager import list_outros, get_last_used_outro_ids
    from agents.queue_manager import get_summary, is_paused
    from pathlib import Path
    db_exists = Path(DB_PATH).exists()
    trends = get_viral_trends(5)
    outros = list_outros()
    last_used = get_last_used_outro_ids(3)
    queue_summary = get_summary()
    return {
        "status": "ok",
        "commit": _get_commit_hash(),
        "yt_dlp_version": _yt_dlp_version(),
        "db_path": DB_PATH,
        "db_exists": db_exists,
        "db_trend_count": len(trends),
        "trends": [
            {"song": t[0], "artist": t[1], "platform": t[2], "url": t[3],
             "viral": t[4], "growth": t[5], "competition": t[6], "opportunity": t[7]}
            for t in trends
        ],
        "outros": {
            "count": len(outros),
            "last_used_ids": last_used,
            "items": [{"id": o.id, "name": o.name, "weight": o.weight} for o in outros],
        },
        "queue": {
            "paused": is_paused(),
            **queue_summary,
        },
        "feature_flags": {
            "ENABLE_TREND_AGENT": ENABLE_TREND_AGENT,
            "ENABLE_RECOMMENDATIONS": ENABLE_RECOMMENDATIONS,
            "ENABLE_APPROVE_WORKFLOW": ENABLE_APPROVE_WORKFLOW,
            "ENABLE_AGENTREACH_IMPORT": ENABLE_AGENTREACH_IMPORT,
            "ENABLE_OUTRO_ROTATION_V2": ENABLE_OUTRO_ROTATION_V2,
            "ENABLE_DASHBOARD": ENABLE_DASHBOARD,
            "ENABLE_SYSTEM_HEALTH": ENABLE_SYSTEM_HEALTH,
        },
        "github_worker": USE_GITHUB_WORKER,
        "github_repo": normalize_github_repo(GITHUB_REPO) if GITHUB_REPO else None,
    }


@flask_app.route("/raw")
def raw():
    """Temporary raw data dump for audit."""
    import sqlite3
    from pathlib import Path
    db = Path("storage/trends.db")
    if not db.exists():
        return {"error": "DB not found"}
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, name, file_id, weight, is_active, added_at FROM outros WHERE is_active = 1 ORDER BY id")
    outros = [dict(r) for r in c.fetchall()]
    c.execute("SELECT id, outro_id, job_id, used_at FROM outro_history ORDER BY used_at DESC LIMIT 10")
    history = [dict(r) for r in c.fetchall()]
    c.execute("SELECT song_name, artist, platform, source_url, opportunity_score FROM viral_trends ORDER BY opportunity_score DESC LIMIT 3")
    trends = [dict(r) for r in c.fetchall()]
    conn.close()
    return {"outros": outros, "history": history, "trends": trends}


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




def parse_tags_text(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    tags = [t.strip() for t in raw.replace("#", "").split(",") if t.strip()]
    return tags or None


def parse_quick_details(text: str) -> dict[str, Any]:
    """Parse optional one-message metadata.

    Supported formats:
      Title: My Song
      Artist: Artist Name
      YouTube: https://...
      YT Title: Custom upload title
      Description: custom desc
      Tags: tag1, tag2

    Also supports one-line: My Song | Artist Name | https://youtu.be/...
    """
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
        # Treat plain text as title if no URL.
        if not url:
            data["song_name"] = raw
    if data.get("custom_tags_raw"):
        data["custom_tags"] = parse_tags_text(data.pop("custom_tags_raw"))
    return data


async def maybe_dispatch_if_ready(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """If all required inputs are present, dispatch GitHub worker."""
    data = context.user_data
    has_source = data.get("source_type") == "youtube_url" or bool(data.get("audio_file_id"))

    # Auto-select outro if not provided
    if not data.get("outro_file_id"):
        outro = select_outro()
        if outro:
            data["outro_file_id"] = outro["file_id"]
            data["outro_ext"] = outro["ext"]

    if not (data.get("song_name") and has_source and data.get("outro_file_id")):
        missing = []
        if not data.get("song_name"):
            missing.append("song title")
        if not has_source:
            missing.append("audio file ya YouTube link")
        if not data.get("outro_file_id"):
            missing.append("outro video (use /outro_add first)")
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
    # Save a fallback job so /audio_retry can reuse all details if YouTube link fails.
    PENDING_RETRY_JOBS[user_id] = payload.copy()
    if USE_GITHUB_WORKER:
        await asyncio.to_thread(dispatch_github_worker, payload)
        await update.message.reply_text("🚀 Job GitHub Actions worker ko bhej diya. Status yahin aayega.")
    else:
        await update.message.reply_text("Local render mode me quick dispatch supported nahi. USE_GITHUB_WORKER=true recommended.")
    context.user_data.clear()
    return True

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
        "Tum sab details ek message me bhej sakte ho, example:\n\n"
        "Title: Pieces of You\n"
        "Artist: Vedansh Jain\n"
        "YouTube: https://youtu.be/...   optional\n"
        "YT Title: custom upload title   optional\n"
        "Description: custom description optional\n"
        "Tags: tag1, tag2, tag3 optional\n\n"
        "Ya simple song title bhejo. Ab files bhi kisi bhi order me bhej sakte ho — thumbnail/audio/outro pehle bhi chalega. Jo missing hoga bot bata dega."
    )
    return WAITING_TITLE


async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await reject_if_unauthorized(update):
        return ConversationHandler.END
    details = parse_quick_details(update.message.text or "")
    context.user_data.update(details)
    if details.get("youtube_url"):
        context.user_data["source_type"] = "youtube_url"

    if not context.user_data.get("song_name"):
        await update.message.reply_text("Proper song title bhejo.")
        return WAITING_TITLE

    await update.message.reply_text(
        "Details saved ✅\n\n"
        "Ab tum ye files kisi bhi order me bhej sakte ho:\n"
        "1. Exact thumbnail image jo video/poster me use hogi\n"
        "2. Audio file MP3/M4A agar YouTube link nahi diya\n"
        "3. Outro video\n\n"
        "Jab required cheezein mil jayengi, bot khud GitHub worker start kar dega.\n"
        "Agar artist add/replace karna ho: Artist: name bhej do."
    )
    return WAITING_LINK

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


async def collect_asset_or_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await reject_if_unauthorized(update):
        return ConversationHandler.END
    msg = update.message

    if msg.text:
        details = parse_quick_details(msg.text)
        if details:
            context.user_data.update(details)
            if details.get("youtube_url"):
                context.user_data["source_type"] = "youtube_url"
            await update.message.reply_text("Text details/link saved ✅")
        await maybe_dispatch_if_ready(update, context)
        return WAITING_LINK

    # Photo/image = exact thumbnail now (not reference template)
    if msg.photo:
        context.user_data["thumbnail_file_id"] = msg.photo[-1].file_id
        context.user_data["thumbnail_ext"] = ".jpg"
        await update.message.reply_text("Exact thumbnail saved ✅ Yehi poster/video thumbnail use hoga.")
        await maybe_dispatch_if_ready(update, context)
        return WAITING_LINK

    if msg.audio:
        context.user_data["source_type"] = "telegram_audio"
        context.user_data["audio_file_id"] = msg.audio.file_id
        context.user_data["audio_ext"] = Path(msg.audio.file_name or "song.mp3").suffix or ".mp3"
        if not context.user_data.get("song_name"):
            context.user_data["song_name"] = msg.audio.title or Path(msg.audio.file_name or "Song").stem
        await update.message.reply_text("Audio file saved ✅")
        await maybe_dispatch_if_ready(update, context)
        return WAITING_LINK

    if msg.video:
        context.user_data["outro_file_id"] = msg.video.file_id
        context.user_data["outro_ext"] = ".mp4"
        await update.message.reply_text("Outro video saved ✅")
        await maybe_dispatch_if_ready(update, context)
        return WAITING_LINK

    if msg.document:
        name = msg.document.file_name or "file"
        ext = Path(name).suffix.lower()
        mime = msg.document.mime_type or ""
        if mime.startswith("image/") or ext in {".jpg", ".jpeg", ".png", ".webp"}:
            context.user_data["thumbnail_file_id"] = msg.document.file_id
            context.user_data["thumbnail_ext"] = ext or ".jpg"
            await update.message.reply_text("Exact thumbnail image saved ✅")
        elif ext in AUDIO_EXTS:
            context.user_data["source_type"] = "telegram_audio"
            context.user_data["audio_file_id"] = msg.document.file_id
            context.user_data["audio_ext"] = ext or ".mp3"
            if not context.user_data.get("song_name"):
                context.user_data["song_name"] = Path(name).stem
            await update.message.reply_text("Audio file saved ✅")
        elif ext in {".mp4", ".mov", ".mkv", ".webm"} or mime.startswith("video/"):
            context.user_data["outro_file_id"] = msg.document.file_id
            context.user_data["outro_ext"] = ext or ".mp4"
            await update.message.reply_text("Outro video saved ✅")
        else:
            await update.message.reply_text("File type samajh nahi aaya. Image/audio/outro video bhejo.")
            return WAITING_LINK
        await maybe_dispatch_if_ready(update, context)
        return WAITING_LINK

    await update.message.reply_text("Please text details, thumbnail image, audio file, ya outro video bhejo.")
    return WAITING_LINK

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
                "thumbnail_file_id": context.user_data.get("thumbnail_file_id"),
                "thumbnail_ext": context.user_data.get("thumbnail_ext", ".jpg"),
                "reference_file_id": context.user_data.get("reference_file_id"),
                "reference_ext": context.user_data.get("reference_ext", ".jpg"),
                "custom_title": context.user_data.get("custom_title"),
                "custom_description": context.user_data.get("custom_description"),
                "custom_tags": context.user_data.get("custom_tags"),
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



async def audio_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await reject_if_unauthorized(update):
        return ConversationHandler.END
    if not update.effective_user or not update.message:
        return ConversationHandler.END
    payload = PENDING_RETRY_JOBS.get(update.effective_user.id)
    if not payload:
        await update.message.reply_text("Koi pending failed YouTube-link job nahi mila. /new se start karo.")
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["retry_payload"] = payload
    await update.message.reply_text("Same title/thumbnail/outro saved hai ✅ Ab sirf MP3/M4A audio file bhejo.")
    return WAITING_RETRY_AUDIO


async def receive_retry_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await reject_if_unauthorized(update):
        return ConversationHandler.END
    payload = context.user_data.get("retry_payload")
    if not payload:
        await update.message.reply_text("Retry data missing. /new se start karo.")
        return ConversationHandler.END
    msg = update.message
    file_id = None
    ext = ".mp3"
    if msg.audio:
        file_id = msg.audio.file_id
        ext = Path(msg.audio.file_name or "song.mp3").suffix or ".mp3"
    elif msg.document:
        name = msg.document.file_name or "song.mp3"
        ext = Path(name).suffix.lower() or ".mp3"
        if ext not in AUDIO_EXTS:
            await update.message.reply_text("Please MP3/M4A/AAC/WAV audio file bhejo.")
            return WAITING_RETRY_AUDIO
        file_id = msg.document.file_id
    else:
        await update.message.reply_text("Please audio file bhejo.")
        return WAITING_RETRY_AUDIO
    payload["source_type"] = "telegram_audio"
    payload["audio_file_id"] = file_id
    payload["audio_ext"] = ext
    payload["youtube_url"] = payload.get("youtube_url")
    payload["job_id"] = f"{update.effective_user.id}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-retry"
    await asyncio.to_thread(dispatch_github_worker, payload)
    await update.message.reply_text("🚀 Retry job GitHub worker ko bhej diya. Ab audio file se render/upload hoga.")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.message:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ==================== DAILY TREND REPORT + APPROVE WORKFLOW ====================

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

async def trend_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    if not update.message:
        return
    await update.message.reply_text("🔍 Running trend diagnostics... this may take 30–60s.")
    try:
        report = await asyncio.to_thread(trend_debug_info)
        # Telegram max message length ~4096; split safely
        for i in range(0, len(report), 3900):
            chunk = report[i : i + 3900]
            await update.message.reply_text(f"```\n{chunk}\n```", parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"❌ trend_debug error:\n{exc}")


async def daily_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return
    if not ENABLE_RECOMMENDATIONS:
        await update.message.reply_text("Trend recommendations are currently disabled.")
        return

    await update.message.reply_text("📡 Fetching trends... this may take a moment.")
    
    # Get trends with IDs for approve buttons
    from agents.db import fetchall
    trends = fetchall("SELECT * FROM trends ORDER BY opportunity_score DESC LIMIT 5")
    
    if not trends:
        report_text = (
            "📈 No trends found.\n\n"
            "Run /trend_debug to diagnose."
        )
        await update.message.reply_text(report_text)
        return

    report_lines = ["📈 Today's Best Upload Opportunities\n"]
    keyboard = []
    
    for i, trend in enumerate(trends, 1):
        song = trend["song_name"][:50]
        artist = trend.get("artist", "Unknown")[:30]
        platform = trend.get("source_platform", "Unknown")
        growth = trend.get("growth_score", 0)
        opp = trend.get("opportunity_score", 0)
        trend_id = trend["id"]
        
        report_lines.append(
            f"{i}. {song}\n"
            f"   🎤 {artist} | 📱 {platform}\n"
            f"   📈 Growth: +{growth}% | 🎯 Opp: {opp}\n"
        )
        keyboard.append([InlineKeyboardButton(f"Approve #{i}", callback_data=f"approve_trend_{trend_id}")])
    
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh_report")])
    
    report_text = "\n".join(report_lines)
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(report_text, reply_markup=reply_markup)


async def approve_song_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not ENABLE_APPROVE_WORKFLOW:
        try:
            await query.edit_message_text("Approve workflow is currently disabled.")
        except Exception as e:
            if "Message is not modified" not in str(e):
                raise
        return

    # Parse trend_id from callback_data: "approve_trend_123"
    callback_data = query.data or ""
    try:
        trend_id = int(callback_data.split("_")[-1])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid trend selection.")
        return

    # Load SPECIFIC trend by ID from database
    from agents.db import fetchone
    trend = fetchone("SELECT * FROM trends WHERE id = ?", (trend_id,))
    if not trend:
        await query.edit_message_text("❌ Trend not found. Run /daily_report to refresh.")
        return

    song_name = trend["song_name"]
    artist = trend.get("artist", "")
    youtube_url = trend["youtube_url"]

    # DUPLICATE PROTECTION: Check uploads table
    if is_already_uploaded(youtube_url):
        await query.edit_message_text(f"❌ Already uploaded: {song_name}\n{youtube_url}")
        return

    metadata = generate_seo_metadata(song_name, artist, youtube_url)

    # Auto-select outro
    outro = select_outro()
    if not outro:
        await query.edit_message_text(
            "❌ No outro videos available.\n"
            "Use /outro_add to upload at least one outro video first."
        )
        return

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    job_id = f"trend-{stamp}"
    payload = {
        "job_id": job_id,
        "chat_id": update.effective_chat.id,
        "user_id": update.effective_user.id,
        "song_name": song_name,
        "artist": artist,
        "source_type": "youtube_url",
        "youtube_url": youtube_url,
        "privacy": DEFAULT_PRIVACY,
        "custom_title": metadata["title"],
        "custom_description": metadata["description"],
        "custom_tags": metadata["tags"],
        "outro_file_id": outro["file_id"],
        "outro_ext": outro["ext"],
    }

    await asyncio.to_thread(dispatch_github_worker, payload)
    record_outro_usage(outro["outro_id"], job_id)

    try:
        await query.edit_message_text(
            f"✅ Approved #{trend_id}: {song_name}\n\n"
            f"Title: {metadata['title']}\n"
            f"Privacy: {DEFAULT_PRIVACY}\n\n"
            "🚀 Job sent to GitHub Actions."
        )
    except Exception as e:
        if "Message is not modified" not in str(e):
            raise


async def refresh_report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not ENABLE_RECOMMENDATIONS:
        await query.edit_message_text("Trend recommendations are currently disabled.")
        return

    report_text = await asyncio.to_thread(generate_daily_report)
    keyboard = [
        [InlineKeyboardButton("Approve Song", callback_data="approve_song")],
        [InlineKeyboardButton("Refresh", callback_data="refresh_report")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(report_text, reply_markup=reply_markup)


    return {
        "outros": outros,
        "history": history,
        "trends": trends,
    }


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
    app.add_handler(CommandHandler("audio_retry", audio_retry))
    app.add_handler(CommandHandler("trend_debug", trend_debug))
    app.add_handler(CommandHandler("outro_list", outro_list))
    app.add_handler(CommandHandler("outro_remove", outro_remove))
    app.add_handler(CommandHandler("outro_test", outro_test))
    app.add_handler(CommandHandler("queue_status", queue_status_cmd))
    app.add_handler(CommandHandler("queue_pause", queue_pause_cmd))
    app.add_handler(CommandHandler("queue_resume", queue_resume_cmd))
    app.add_handler(CommandHandler("queue_cancel", queue_cancel_cmd))

    # Outro add conversation (polling mode)
    outro_conv = ConversationHandler(
        entry_points=[CommandHandler("outro_add", outro_add_start)],
        states={
            WAITING_OUTRO_VIDEO: [MessageHandler((filters.VIDEO | filters.Document.VIDEO | filters.Document.ALL) & ~filters.COMMAND, receive_outro_video)],
            WAITING_OUTRO_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_outro_name)],
            WAITING_OUTRO_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_outro_weight)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(outro_conv)

    # Webhook-safe single-shot outro handler (higher priority for video+caption)
    app.add_handler(MessageHandler(
        (filters.VIDEO | filters.Document.VIDEO | filters.Document.ALL) & filters.CaptionRegex(r"^/outro_add") & ~filters.COMMAND,
        outro_add_single_shot,
    ))

    # Agent Reach — webhook-safe single-shot (primary handler)
    if ENABLE_AGENTREACH_IMPORT:
        app.add_handler(CommandHandler("agentreach", agentreach_single_shot))
        # CSV upload handler for Agent Reach
        app.add_handler(MessageHandler(
            filters.Document.FileExtension("csv") & ~filters.COMMAND,
            agentreach_csv_handler,
        ))

    conv = ConversationHandler(
        entry_points=[CommandHandler("new", new_video)],
        states={
            # After /new accept title/details OR files in any order.
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

    # General text handler (lowest priority, catches remaining text)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bulk_text))

    print(f"ENABLE_RECOMMENDATIONS value: {ENABLE_RECOMMENDATIONS}")

    if ENABLE_RECOMMENDATIONS:
        print("REGISTERING DAILY REPORT HANDLER")
        app.add_handler(CommandHandler("daily_report", daily_report))
        print("DAILY REPORT HANDLER REGISTERED")

    if ENABLE_APPROVE_WORKFLOW:
        from telegram.ext import CallbackQueryHandler
        app.add_handler(CallbackQueryHandler(approve_song_callback, pattern=r"^approve_trend_\d+$"))
        app.add_handler(CallbackQueryHandler(refresh_report_callback, pattern="^refresh_report$"))

    if ENABLE_AGENTREACH_IMPORT:
        from telegram.ext import CallbackQueryHandler
        # Legacy polling-mode callbacks
        app.add_handler(CallbackQueryHandler(agentreach_confirm_callback, pattern="^agentreach_confirm$"))
        app.add_handler(CallbackQueryHandler(agentreach_cancel_callback, pattern="^agentreach_cancel$"))
        # Webhook-safe DB session callbacks
        app.add_handler(CallbackQueryHandler(agentreach_db_confirm_callback, pattern=r"^ar_confirm_"))
        app.add_handler(CallbackQueryHandler(agentreach_db_cancel_callback, pattern=r"^ar_cancel_"))

    return app


@flask_app.route("/verify")
def verify():
    """Production verification endpoint. Returns raw DB rows."""
    import sqlite3
    from pathlib import Path
    from agents.db import fetchall
    
    db_path = Path("storage/trends.db")
    if not db_path.exists():
        return {"error": "DB not found"}
    
    result = {}
    
    # uploads table
    result["uploads"] = fetchall("SELECT id, song_name, youtube_url, youtube_video_id, source, uploaded_at FROM uploads ORDER BY id DESC LIMIT 10")
    
    # upload_queue
    result["queue"] = fetchall("SELECT id, song_name, youtube_url, status, source, created_at FROM upload_queue ORDER BY id DESC LIMIT 10")
    
    # outros
    result["outros"] = fetchall("SELECT id, name, file_id, file_unique_id, ext, weight, is_active, added_at FROM outros ORDER BY id")
    
    # outro_history
    result["outro_history"] = fetchall("SELECT id, outro_id, upload_id, used_at FROM outro_history ORDER BY used_at DESC LIMIT 10")
    
    # trends
    result["trends"] = fetchall("SELECT id, song_name, artist, youtube_url, source_platform, opportunity_score FROM trends ORDER BY opportunity_score DESC LIMIT 10")
    
    # queue_history
    result["queue_history"] = fetchall("SELECT id, queue_id, action, detail, created_at FROM queue_history ORDER BY id DESC LIMIT 10")
    
    # system_state
    result["system_state"] = fetchall("SELECT key, value FROM system_state")
    
    return result


@flask_app.route("/debug_agentreach", methods=["POST"])
def debug_agentreach():
    """Debug endpoint to test Agent Reach parsing without Telegram."""
    try:
        text = request.json.get("text", "") if request.json else ""
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if text.startswith("/agentreach"):
            lines = [l.strip() for l in text[len("/agentreach"):].split("\n") if l.strip()]
        valid, invalid, duplicate = _parse_agentreach_lines(lines)
        return {
            "total": len(lines),
            "valid": valid,
            "invalid": invalid,
            "duplicate": duplicate,
        }
    except Exception as exc:
        import traceback
        return {"error": str(exc), "trace": traceback.format_exc()}, 500


@flask_app.route("/debug_callback", methods=["POST"])
def debug_callback():
    """Debug endpoint to test Agent Reach confirm callback without Telegram."""
    try:
        session_id = request.json.get("session_id", "")
        user_id = request.json.get("user_id", 1768510980)
        chat_id = request.json.get("chat_id", 1768510980)
        
        session = _load_agentreach_session(user_id, session_id)
        if not session:
            return {"error": "Session not found"}, 404
        
        candidates = session.get("candidates", [])
        if not candidates:
            return {"error": "No candidates"}, 400
        
        result = add_multiple_items_transactional(user_id, chat_id, candidates, source="agentreach")
        return result
    except Exception as exc:
        import traceback
        return {"error": str(exc), "trace": traceback.format_exc()}, 500


@flask_app.route("/seed_trends", methods=["POST"])
def seed_trends():
    """Seed test trends for Phase 1 verification. Protected by webhook secret."""
    secret = request.headers.get("X-Webhook-Secret", "")
    if secret != WEBHOOK_SECRET:
        return {"error": "Unauthorized"}, 401
    
    from agents.db import execute
    from datetime import datetime
    
    test_trends = [
        ("Test Song Alpha", "Artist A", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube", 85.5, 120.0, 30.0, 95.0),
        ("Test Song Beta", "Artist B", "https://www.youtube.com/watch?v=9bZkp7q19f0", "youtube", 72.0, 95.0, 45.0, 80.0),
        ("Test Song Gamma", "Artist C", "https://www.youtube.com/watch?v=kfVsfOSbJY0", "youtube", 60.0, 80.0, 20.0, 75.0),
    ]
    
    for song, artist, url, platform, viral, growth, competition, opp in test_trends:
        execute(
            """INSERT INTO trends (song_name, artist, youtube_url, source_platform, viral_score, growth_score, competition_score, opportunity_score, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (song, artist, url, platform, viral, growth, competition, opp, dt.datetime.now().isoformat())
        )
    
    return {"status": "ok", "seeded": len(test_trends)}


@flask_app.route("/test_dispatch", methods=["POST"])
def test_dispatch():
    """Trigger a test GitHub Actions dispatch for verification."""
    secret = request.headers.get("X-Webhook-Secret", "")
    if secret != WEBHOOK_SECRET:
        return {"error": "Unauthorized"}, 401
    
    from agents.outro_manager import select_outro
    outro = select_outro()
    if not outro:
        return {"error": "No outro available"}, 400
    
    payload = {
        "job_id": "verify-test-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"),
        "chat_id": 1768510980,
        "user_id": 1768510980,
        "song_name": "Verification Test Song",
        "artist": "Test Artist",
        "source_type": "youtube_url",
        "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "privacy": "private",
        "custom_title": "Verification Test Upload",
        "custom_description": "This is a test dispatch for Phase 1 verification.",
        "custom_tags": ["test", "verification"],
        "outro_file_id": outro["file_id"],
        "outro_ext": outro["ext"],
    }
    
    try:
        dispatch_github_worker(payload)
        return {"status": "dispatched", "job_id": payload["job_id"], "outro_file_id": outro["file_id"]}
    except Exception as exc:
        return {"error": str(exc)}, 500

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
            # Initialize database tables first
            init_all_tables()
            print("[DB] All tables initialized", flush=True)
            
            loop = asyncio.new_event_loop()
            BOT_LOOP = loop
            asyncio.set_event_loop(loop)
            loop.run_until_complete(telegram_app.initialize())
            loop.run_until_complete(telegram_app.bot.delete_webhook(drop_pending_updates=True))
            loop.run_until_complete(telegram_app.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES))
            loop.run_until_complete(telegram_app.start())
            print(f"Telegram webhook set: {webhook_url}", flush=True)
            
            # Start background queue processor
            asyncio.run_coroutine_threadsafe(
                run_queue_processor(telegram_app.bot, DEFAULT_PRIVACY, dispatch_github_worker),
                loop,
            )
            print("[QUEUE] Background processor scheduled", flush=True)
            loop.run_forever()

        thread = threading.Thread(target=bot_loop_thread, daemon=True)
        thread.start()
        run_flask()
    else:
        thread = threading.Thread(target=run_flask, daemon=True)
        thread.start()
        telegram_app.run_polling(allowed_updates=Update.ALL_TYPES)




@flask_app.route("/db_status")
def db_status():
    """Show current database backend and persistence status."""
    from agents.db import USE_POSTGRES, DATABASE_URL, DB_PATH
    from pathlib import Path
    return {
        "backend": "postgresql" if USE_POSTGRES else "sqlite",
        "database_url_set": bool(DATABASE_URL),
        "sqlite_path": DB_PATH,
        "sqlite_exists": Path(DB_PATH).exists(),
        "persistence_warning": "SQLite is ephemeral on Render. Set DATABASE_URL env var to use PostgreSQL (Supabase/etc) for persistent storage.",
    }


@flask_app.route("/debug_agentreach", methods=["POST"])
def debug_agentreach():
    """Debug endpoint to test Agent Reach parsing without Telegram."""
    try:
        text = request.json.get("text", "") if request.json else ""
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if text.startswith("/agentreach"):
            lines = [l.strip() for l in text[len("/agentreach"):].split("\n") if l.strip()]
        valid, invalid, duplicate = _parse_agentreach_lines(lines)
        return {
            "total": len(lines),
            "valid": valid,
            "invalid": invalid,
            "duplicate": duplicate,
        }
    except Exception as exc:
        import traceback
        return {"error": str(exc), "trace": traceback.format_exc()}, 500


@flask_app.route("/debug_callback", methods=["POST"])
def debug_callback():
    """Debug endpoint to test Agent Reach confirm callback without Telegram."""
    try:
        session_id = request.json.get("session_id", "")
        user_id = request.json.get("user_id", 1768510980)
        chat_id = request.json.get("chat_id", 1768510980)
        session = _load_agentreach_session(user_id, session_id)
        if not session:
            return {"error": "Session not found"}, 404
        candidates = session.get("candidates", [])
        if not candidates:
            return {"error": "No candidates"}, 400
        result = add_multiple_items_transactional(user_id, chat_id, candidates, source="agentreach")
        return result
    except Exception as exc:
        import traceback
        return {"error": str(exc), "trace": traceback.format_exc()}, 500

if __name__ == "__main__":
    main()
