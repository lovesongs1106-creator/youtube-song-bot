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
    prepare_exact_thumbnail,
    render_video,
)

ROOT = Path(__file__).parent.resolve()
JOB_DIR = ROOT / "job_workspace"
JOB_DIR.mkdir(exist_ok=True)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
YOUTUBE_TOKEN_JSON = os.environ.get("YOUTUBE_TOKEN_JSON", "").strip()
GOOGLE_CLIENT_SECRETS_JSON = os.environ.get("GOOGLE_CLIENT_SECRETS_JSON", "").strip()
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "").strip()

# Second YouTube channel (Facebook automation) — fully isolated from the
# first-channel OAuth above. Provided via GitHub Actions secrets/variables.
YOUTUBE_SECOND_CLIENT_ID = os.environ.get("YOUTUBE_SECOND_CLIENT_ID", "").strip()
YOUTUBE_SECOND_CLIENT_SECRET = os.environ.get("YOUTUBE_SECOND_CLIENT_SECRET", "").strip()
YOUTUBE_SECOND_REFRESH_TOKEN = os.environ.get("YOUTUBE_SECOND_REFRESH_TOKEN", "").strip()
YOUTUBE_TARGET_CHANNEL = os.environ.get("YOUTUBE_TARGET_CHANNEL", "second").strip().lower() or "second"
YOUTUBE_VISIBILITY = os.environ.get("YOUTUBE_VISIBILITY", "private").strip().lower() or "private"


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
    # New controller wraps all fields under `job` to satisfy GitHub's max 10 top-level
    # client_payload properties rule. Keep backward compatibility with old payloads.
    if isinstance(payload.get("job"), dict):
        payload = payload["job"]
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
            try:
                creds.refresh(GoogleRequest())
            except Exception as refresh_exc:
                # Catch invalid_grant and other refresh failures with a clear actionable message
                err_str = str(refresh_exc).lower()
                if "invalid_grant" in err_str:
                    raise RuntimeError(
                        "YouTube token EXPIRED/REVOKED (invalid_grant).\n\n"
                        "FIX karo ye steps follow karke:\n"
                        "1. Telegram bot me /auth bhejo\n"
                        "2. Google login approve karo\n"
                        "3. /export_youtube_token bhejo\n"
                        "4. Jo file mile uska content copy karo\n"
                        "5. GitHub repo > Settings > Secrets > YOUTUBE_TOKEN_JSON me paste karo\n\n"
                        f"Technical detail: {refresh_exc}"
                    )
                raise RuntimeError(f"YouTube token refresh failed: {refresh_exc}")
        else:
            raise RuntimeError("YouTube token invalid/expired. Re-run /auth and update YOUTUBE_TOKEN_JSON secret.")
    return build("youtube", "v3", credentials=creds)


def validate_youtube_token_early() -> tuple[bool, str]:
    """Validate YouTube token before doing any heavy work. Returns (ok, message)."""
    if not YOUTUBE_TOKEN_JSON:
        return False, "YOUTUBE_TOKEN_JSON secret missing in GitHub repo."
    try:
        get_youtube_service()
        return True, "YouTube token valid — upload ready."
    except RuntimeError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"YouTube token validation error: {exc}"


def get_second_channel_service():
    """Build the YouTube API client for the SECOND channel.

    Auth is a dedicated OAuth client (client_id + client_secret + refresh
    token from GitHub secrets). Never touches the first channel's
    YOUTUBE_TOKEN_JSON credentials.
    """
    missing = [
        name for name, val in (
            ("YOUTUBE_SECOND_CLIENT_ID", YOUTUBE_SECOND_CLIENT_ID),
            ("YOUTUBE_SECOND_CLIENT_SECRET", YOUTUBE_SECOND_CLIENT_SECRET),
            ("YOUTUBE_SECOND_REFRESH_TOKEN", YOUTUBE_SECOND_REFRESH_TOKEN),
        ) if not val
    ]
    if missing:
        raise RuntimeError(
            "Second-channel YouTube secrets missing in GitHub repo: "
            + ", ".join(missing) + ".\n\n"
            "Add them under Settings > Secrets and variables > Actions > Secrets. "
            "See FACEBOOK_SECOND_CHANNEL_SETUP.md for the one-time OAuth steps."
        )
    creds = Credentials(
        token=None,
        refresh_token=YOUTUBE_SECOND_REFRESH_TOKEN,
        client_id=YOUTUBE_SECOND_CLIENT_ID,
        client_secret=YOUTUBE_SECOND_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=YOUTUBE_UPLOAD_SCOPE,
    )
    try:
        creds.refresh(GoogleRequest())
    except Exception as refresh_exc:
        err_str = str(refresh_exc).lower()
        if "invalid_grant" in err_str:
            raise RuntimeError(
                "Second-channel YouTube refresh token EXPIRED/REVOKED (invalid_grant).\n\n"
                "FIX:\n"
                "1. Re-run the second-channel OAuth flow (FACEBOOK_SECOND_CHANNEL_SETUP.md)\n"
                "2. Update the YOUTUBE_SECOND_REFRESH_TOKEN repo secret\n"
                "3. Re-dispatch the job with /upload_force JOB-ID\n\n"
                f"Technical detail: {refresh_exc}"
            )
        raise RuntimeError(f"Second-channel token refresh failed: {refresh_exc}")
    return build("youtube", "v3", credentials=creds)


def validate_second_channel_early() -> tuple[bool, str]:
    """Validate second-channel credentials before heavy work. Returns (ok, message)."""
    try:
        service = get_second_channel_service()
        me = service.channels().list(part="snippet", mine=True).execute()
        items = me.get("items", [])
        if not items:
            return False, "Second-channel auth OK but no channel found on this Google account."
        title = items[0]["snippet"]["title"]
        return True, f"Second channel verified: '{title}' — upload ready."
    except RuntimeError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"Second-channel validation error: {exc}"


def upload_to_youtube(video_file: Path, thumbnail_file: Path, metadata: dict[str, Any], privacy: str, service=None) -> str:
    youtube = service if service is not None else get_youtube_service()
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


def verify_cookies() -> tuple[bool, str]:
    """Audit YOUTUBE_COOKIES env var and return (ok, diagnostic_message)."""
    cookies_text = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if not cookies_text:
        return False, "YOUTUBE_COOKIES audit: Secret exists: NO | Cookie file size: 0 bytes | Cookie format: MISSING"
    size = len(cookies_text.encode("utf-8"))
    first_line = cookies_text.splitlines()[0] if cookies_text else ""
    if first_line.startswith("# Netscape HTTP Cookie File"):
        return True, f"YOUTUBE_COOKIES audit: Secret exists: YES | Cookie file size: {size} bytes | Cookie format: VALID (Netscape header found)"
    return False, f"YOUTUBE_COOKIES audit: Secret exists: YES | Cookie file size: {size} bytes | Cookie format: INVALID (first line: {first_line[:60]})"


def report_fb_status(payload: dict[str, Any], status: str, stage: str = "",
                     error: str | None = None, video_id: str | None = None) -> None:
    """Best-effort status callback to the bot's /fb_job_status endpoint."""
    url = (payload.get("status_callback_url") or "").strip()
    if not url:
        return
    try:
        requests.post(url, json={
            "job_id": payload.get("job_id", ""),
            "status": status,
            "stage": stage,
            "error_message": (error or "")[:500],
            "youtube_video_id": video_id or "",
        }, timeout=30)
    except Exception as exc:
        log(f"[FB] Status callback failed (non-fatal): {exc}")


def cleanup_job_dir(job_dir: Path) -> None:
    import shutil
    try:
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
            log(f"[FB] Cleaned temp dir: {job_dir}")
    except Exception as exc:
        log(f"[FB] Temp cleanup warning: {exc}")


def run_second_channel_check(payload: dict[str, Any]) -> None:
    """Validate second-channel secrets end-to-end and report to Telegram."""
    from facebook_pipeline import resolve_outro_asset
    chat_id = payload["chat_id"]
    job_id = payload.get("job_id", "second-channel-check")
    send_message(chat_id, f"🔍 Second-channel check started.\nJob: {job_id}")

    log("=== Second Channel Check ===")
    ok, msg = validate_second_channel_early()
    log(f"[SECOND_CHECK] {msg}")

    outro = resolve_outro_asset()
    outro_msg = f"✅ Outro asset found: {outro}" if outro.exists() else (
        f"❌ Outro asset MISSING: {outro}\n"
        "Add assets/outro.mp4 to the repo (or set OUTRO_ASSET_PATH)."
    )
    log(f"[SECOND_CHECK] {outro_msg}")

    if ok and outro.exists():
        send_message(
            chat_id,
            "✅ Second channel check PASSED\n\n"
            f"{msg}\n"
            f"{outro_msg}\n\n"
            f"Target: {YOUTUBE_TARGET_CHANNEL} | Visibility: {YOUTUBE_VISIBILITY}\n"
            "Facebook jobs (/upload) are ready to run."
        )
    else:
        send_message(
            chat_id,
            "❌ Second channel check FAILED\n\n"
            f"{msg}\n"
            f"{outro_msg}\n\n"
            "Fix the items above, then run /second_channel_check again."
        )
        raise RuntimeError(f"Second-channel check failed: {msg} | {outro_msg}")


def run_facebook_job(payload: dict[str, Any]) -> None:
    """Full Facebook -> second-channel pipeline for one job."""
    from facebook_pipeline import (
        build_facebook_metadata,
        download_facebook_video,
        extract_frame,
        get_facebook_title,
        process_facebook_video,
        resolve_outro_asset,
        trim_video,
    )
    from bot import generate_reference_thumbnail, generate_thumbnail

    chat_id = payload["chat_id"]
    job_id = payload.get("job_id", str(int(time.time())))
    fb_url = payload.get("facebook_url", "")
    trim_start = payload.get("trim_start")
    trim_end = payload.get("trim_end")
    privacy = (payload.get("privacy") or YOUTUBE_VISIBILITY or "private").lower()
    if privacy not in ("private", "unlisted", "public"):
        privacy = "private"

    send_message(chat_id, f"🚀 Facebook worker started.\nJob: {job_id}\nPrivacy: {privacy}")

    log("=== Facebook Worker Environment Audit ===")
    log(f"yt-dlp version: {os.popen('yt-dlp --version').read().strip()}")
    log(f"python version: {sys.version.split()[0]}")
    log(f"ffmpeg version: {os.popen('ffmpeg -version').read().splitlines()[0]}")
    log(f"target_channel: {YOUTUBE_TARGET_CHANNEL} | visibility: {privacy}")
    log("=========================================")

    # EARLY second-channel validation — fail fast before heavy work.
    token_ok, token_msg = validate_second_channel_early()
    log(f"[FB_TOKEN_CHECK] {token_msg}")
    report_fb_status(payload, "dispatched", "token_check")
    if not token_ok:
        report_fb_status(payload, "failed", "token_check", error=token_msg)
        send_message(chat_id, f"❌ Second-channel auth INVALID.\n\n{token_msg}\n\nJob aborted early.")
        raise RuntimeError(token_msg)
    send_message(chat_id, f"🔑 {token_msg}")

    job_dir = JOB_DIR / job_id
    try:
        job_dir.mkdir(parents=True, exist_ok=True)

        outro_asset = resolve_outro_asset()
        if not outro_asset.exists():
            raise RuntimeError(
                f"Outro asset missing: {outro_asset}. "
                "Add assets/outro.mp4 to the repo (or set OUTRO_ASSET_PATH)."
            )

        # Title first (cheap metadata read, no download).
        send_message(chat_id, "🔎 Facebook video info read kar raha hoon...")
        report_fb_status(payload, "downloading", "probing")
        video_title = get_facebook_title(fb_url) or "Facebook Video"
        send_message(chat_id, f"🎬 Title: {video_title}")

        # Download (public/accessible content only — enforced in pipeline).
        send_message(chat_id, "⬇️ Facebook video download kar raha hoon...")
        try:
            source_video = download_facebook_video(fb_url, job_dir / "downloaded")
        except Exception as exc:
            report_fb_status(payload, "failed", "download", error=str(exc))
            send_message(chat_id, f"❌ Facebook download fail ho gaya.\n\n{exc}")
            raise

        # Optional exact trim.
        main_video = source_video
        if trim_start is not None and trim_end is not None:
            send_message(chat_id, f"✂️ Exact trim kar raha hoon: {trim_start:.0f}s-{trim_end:.0f}s...")
            report_fb_status(payload, "processing", "trim")
            main_video = trim_video(source_video, job_dir / "trimmed.mp4", trim_start, trim_end)

        # Normalize + append fixed outro.
        send_message(chat_id, "🎬 FFmpeg processing + outro append kar raha hoon...")
        report_fb_status(payload, "processing", "render")
        final_video = process_facebook_video(
            main_video, outro_asset, job_dir / "final_video.mp4", job_dir / "work"
        )

        # Thumbnail from a real video frame (existing reference-thumbnail style).
        thumb_path = job_dir / "thumbnail.jpg"
        try:
            frame = extract_frame(final_video, job_dir / "frame.jpg")
            generate_reference_thumbnail(video_title, None, frame, thumb_path)
        except Exception as exc:
            log(f"[FB] Frame thumbnail failed ({exc}) — using generated title card.")
            generate_thumbnail(video_title, None, thumb_path)

        # Metadata + upload to SECOND channel only.
        metadata = build_facebook_metadata(video_title, fb_url)
        send_message(
            chat_id,
            "✅ Metadata ready\n\n"
            f"Title: {metadata['title']}\n"
            f"Privacy: {privacy} (second channel)\n"
            "📤 YouTube upload start kar raha hoon...",
        )
        report_fb_status(payload, "uploading", "youtube_upload")
        service = get_second_channel_service()
        video_id = upload_to_youtube(final_video, thumb_path, metadata, privacy, service=service)

        report_fb_status(payload, "completed", "done", video_id=video_id)
        send_message(
            chat_id,
            f"✅ Done bhai! Second channel par upload ho gaya:\n"
            f"https://www.youtube.com/watch?v={video_id}\nJob: {job_id}"
        )
    except Exception as exc:
        log(f"[FB] ERROR: {exc}")
        report_fb_status(payload, "failed", "error", error=str(exc))
        try:
            send_message(chat_id, f"❌ Facebook job {job_id} failed:\n{exc}")
        except Exception:
            pass
        raise
    finally:
        cleanup_job_dir(job_dir)


def main() -> None:
    payload = read_payload()

    # Facebook -> second-channel path. Fully isolated: separate OAuth, separate
    # payload branch. First-channel flow below is unchanged.
    if (payload.get("mode") == "second_channel_check"
            or payload.get("target_channel") == "second"
            or payload.get("source_type") == "facebook_url"):
        if payload.get("mode") == "second_channel_check":
            run_second_channel_check(payload)
        else:
            run_facebook_job(payload)
        return

    chat_id = payload["chat_id"]
    job_id = payload.get("job_id", str(int(time.time())))
    song_name = payload["song_name"]
    artist = payload.get("artist") or None
    source_type = payload.get("source_type", "telegram_audio")
    privacy = payload.get("privacy", "private")
    youtube_url = payload.get("youtube_url")

    send_message(chat_id, f"🚀 GitHub worker started for: {song_name}\nJob: {job_id}")

    # Environment audit
    log("=== GitHub Worker Environment Audit ===")
    log(f"yt-dlp version: {os.popen('yt-dlp --version').read().strip()}")
    log(f"python version: {sys.version.split()[0]}")
    log(f"node version: {os.popen('node --version').read().strip()}")
    log(f"ffmpeg version: {os.popen('ffmpeg -version').read().splitlines()[0]}")
    cookies_ok, cookies_diag = verify_cookies()
    log(cookies_diag)
    log("=======================================")

    # EARLY TOKEN VALIDATION — fail fast before downloading/rendering
    token_ok, token_msg = validate_youtube_token_early()
    log(f"[TOKEN_CHECK] {token_msg}")
    if not token_ok:
        log(f"[TOKEN_CHECK] FAILED — aborting before heavy work.")
        send_message(
            chat_id,
            f"❌ YouTube upload token INVALID.\n\n"
            f"{token_msg}\n\n"
            f"Job aborted early — no time wasted on download/render."
        )
        raise RuntimeError(token_msg)
    send_message(chat_id, "🔑 YouTube token verified OK. Proceeding with download/render...")

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
            if not YOUTUBE_COOKIES:
                log("YOUTUBE_COOKIES secret missing")
            send_message(chat_id, "⬇️ YouTube audio download try kar raha hoon GitHub worker par...")
            try:
                audio_path = download_youtube_audio(youtube_url, job_dir / "downloaded_audio")
            except Exception as exc:
                send_message(
                    chat_id,
                    "⚠️ YouTube audio download fail ho gaya.\n\n"
                    "Cobalt API aur yt-dlp dono ne try kiya, par dono fail ho gaye.\n"
                    "Reasons: restricted video, age-gate, ya YouTube ne block kar diya.\n\n"
                    "Ab kya karna hai:\n"
                    "1. Telegram bot me /audio_retry bhejo\n"
                    "2. Sirf MP3/M4A audio file bhej do\n"
                    "3. Thumbnail, outro, title sab saved rahenge\n\n"
                    f"Technical error: {exc}"
                )
                raise

        # Download outro
        outro_ext = payload.get("outro_ext") or ".mp4"
        outro_path = job_dir / f"outro{outro_ext}"
        send_message(chat_id, "⬇️ Outro download kar raha hoon...")
        download_telegram_file(payload["outro_file_id"], outro_path)

        # Thumbnail
        thumb_path = job_dir / "thumbnail.jpg"
        thumb_file_id = payload.get("thumbnail_file_id")
        ref_file_id = payload.get("reference_file_id")
        if thumb_file_id:
            thumb_ext = payload.get("thumbnail_ext") or ".jpg"
            raw_thumb_path = job_dir / f"user_thumbnail{thumb_ext}"
            send_message(chat_id, "🖼️ Tumhara diya hua exact thumbnail prepare kar raha hoon...")
            download_telegram_file(thumb_file_id, raw_thumb_path)
            prepare_exact_thumbnail(raw_thumb_path, thumb_path)
        elif ref_file_id:
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
        metadata = generate_seo_metadata(
            song_name,
            artist,
            youtube_url,
            custom_title=payload.get("custom_title"),
            custom_description=payload.get("custom_description"),
            custom_tags=payload.get("custom_tags"),
        )
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
