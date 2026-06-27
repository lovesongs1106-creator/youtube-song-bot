#!/usr/bin/env python3
"""GitHub Actions worker for rendering + uploading YouTube music videos.

Triggered by repository_dispatch from telegram_bot.py.
Downloads Telegram files, generates thumbnail/video, uploads to YouTube, then messages user.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from bot import (
    YOUTUBE_UPLOAD_SCOPE,
    download_youtube_audio,
    generate_reference_thumbnail,
    generate_seo_metadata,
    generate_thumbnail,
    render_video,
)

ROOT = Path(__file__).parent.resolve()
JOB_DIR = ROOT / "job_workspace"
JOB_DIR.mkdir(exist_ok=True)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
YOUTUBE_TOKEN_JSON = os.environ.get("YOUTUBE_TOKEN_JSON", "").strip()
GOOGLE_CLIENT_SECRETS_JSON = os.environ.get("GOOGLE_CLIENT_SECRETS_JSON", "").strip()


def log(msg: str) -> None:
    print(msg, flush=True)


def read_payload() -> dict[str, Any]:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not Path(event_path).exists():
        raise RuntimeError("GITHUB_EVENT_PATH missing. This worker must run inside GitHub Actions.")
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    payload = event.get("client_payload") or {}
    if not payload:
        raise RuntimeError("repository_dispatch client_payload missing.")
    return payload


def telegram_api(method: str, **data: Any) -> dict[str, Any]:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN secret missing in GitHub repo.")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    r = requests.post(url, data=data, timeout=60)
    try:
        js = r.json()
    except Exception:
        raise RuntimeError(f"Telegram API non-json response: {r.status_code} {r.text[:500]}")
    if not js.get("ok"):
        raise RuntimeError(f"Telegram API error {method}: {js}")
    return js


def send_message(chat_id: int | str, text: str) -> None:
    # Telegram max 4096 chars; split safely.
    for i in range(0, len(text), 3900):
        telegram_api("sendMessage", chat_id=chat_id, text=text[i : i + 3900])


def download_telegram_file(file_id: str, dest: Path) -> Path:
    info = telegram_api("getFile", file_id=file_id)["result"]
    file_path = info["file_path"]
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    return dest


def get_youtube_service():
    if not YOUTUBE_TOKEN_JSON:
        raise RuntimeError("YOUTUBE_TOKEN_JSON secret missing in GitHub repo.")

    token_info = json.loads(YOUTUBE_TOKEN_JSON)

    # If exported token lacks client_id/client_secret, fill from Google client JSON.
    if GOOGLE_CLIENT_SECRETS_JSON and (not token_info.get("client_id") or not token_info.get("client_secret")):
        google_info = json.loads(GOOGLE_CLIENT_SECRETS_JSON)
        cfg = google_info.get("web") or google_info.get("installed") or {}
        token_info.setdefault("client_id", cfg.get("client_id"))
        token_info.setdefault("client_secret", cfg.get("client_secret"))
        token_info.setdefault("token_uri", cfg.get("token_uri", "https://oauth2.googleapis.com/token"))

    creds = Credentials.from_authorized_user_info(token_info, YOUTUBE_UPLOAD_SCOPE)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
        else:
            raise RuntimeError("YouTube token invalid/expired. Re-run /auth and update YOUTUBE_TOKEN_JSON secret.")
    return build("youtube", "v3", credentials=creds)


def upload_to_youtube(video_file: Path, thumbnail_file: Path, metadata: dict[str, Any], privacy: str) -> str:
    youtube = get_youtube_service()
    body = {
        "snippet": {
            "title": metadata["title"][:100],
            "description": metadata["description"],
            "tags": metadata["tags"],
            "categoryId": "10",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    request_upload = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=MediaFileUpload(str(video_file), chunksize=8 * 1024 * 1024, resumable=True),
    )
    response = None
    while response is None:
        status, response = request_upload.next_chunk()
        if status:
            log(f"YouTube upload progress: {int(status.progress() * 100)}%")
    video_id = response["id"]

    if thumbnail_file.exists():
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumbnail_file)),
        ).execute()
    return video_id


def main() -> None:
    payload = read_payload()
    chat_id = payload["chat_id"]
    job_id = payload.get("job_id", str(int(time.time())))
    song_name = payload["song_name"]
    artist = payload.get("artist") or None
    source_type = payload.get("source_type", "telegram_audio")
    privacy = payload.get("privacy", "private")
    youtube_url = payload.get("youtube_url")

    send_message(chat_id, f"🚀 GitHub worker started for: {song_name}\nJob: {job_id}")

    try:
        job_dir = JOB_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        # Download/generate audio
        if source_type == "telegram_audio":
            audio_ext = payload.get("audio_ext") or ".mp3"
            audio_path = job_dir / f"source_audio{audio_ext}"
            send_message(chat_id, "⬇️ Audio file download kar raha hoon GitHub worker par...")
            download_telegram_file(payload["audio_file_id"], audio_path)
        else:
            if not youtube_url:
                raise RuntimeError("youtube_url missing for source_type=youtube_url")
            send_message(chat_id, "⬇️ YouTube audio download try kar raha hoon GitHub worker par...")
            audio_path = download_youtube_audio(youtube_url, job_dir / "downloaded_audio")

        # Download outro
        outro_ext = payload.get("outro_ext") or ".mp4"
        outro_path = job_dir / f"outro{outro_ext}"
        send_message(chat_id, "⬇️ Outro download kar raha hoon...")
        download_telegram_file(payload["outro_file_id"], outro_path)

        # Thumbnail
        thumb_path = job_dir / "thumbnail.jpg"
        ref_file_id = payload.get("reference_file_id")
        if ref_file_id:
            ref_ext = payload.get("reference_ext") or ".jpg"
            ref_path = job_dir / f"reference{ref_ext}"
            send_message(chat_id, "🎨 Reference thumbnail generate kar raha hoon...")
            download_telegram_file(ref_file_id, ref_path)
            generate_reference_thumbnail(song_name, artist, ref_path, thumb_path)
        else:
            send_message(chat_id, "🎨 Thumbnail generate kar raha hoon...")
            generate_thumbnail(song_name, artist, thumb_path)

        # Render
        video_path = job_dir / "final_video.mp4"
        send_message(chat_id, "🎬 1080p/720p video render kar raha hoon. Ye kuch minutes le sakta hai...")
        render_video(thumb_path, audio_path, outro_path, video_path, job_dir / "work")

        # Metadata + upload
        metadata = generate_seo_metadata(song_name, artist, youtube_url)
        send_message(
            chat_id,
            "✅ SEO metadata ready\n\n"
            f"Title: {metadata['title']}\n\n"
            f"Privacy: {privacy}\n"
            "📤 YouTube upload start kar raha hoon...",
        )
        video_id = upload_to_youtube(video_path, thumb_path, metadata, privacy)
        send_message(chat_id, f"✅ Done bhai! Video upload ho gaya:\nhttps://www.youtube.com/watch?v={video_id}")

    except Exception as exc:
        log(f"ERROR: {exc}")
        send_message(chat_id, f"❌ GitHub worker error:\n{exc}")
        raise


if __name__ == "__main__":
    main()
