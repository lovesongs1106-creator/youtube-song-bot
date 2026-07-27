#!/usr/bin/env python3
"""Downloader abstraction: Cobalt API → yt-dlp fallback.

Priority:
  1. Cobalt API (configurable via COBALT_API_URL)
  2. yt-dlp fallback

If both fail: raises RuntimeError with a clean message for Telegram.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import requests


def _run(cmd: list[str]) -> None:
    print("\n$ " + " ".join(map(str, cmd)))
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def _check_ffmpeg() -> None:
    for exe in ("ffmpeg", "ffprobe"):
        if not shutil.which(exe):
            raise SystemExit(f"ERROR: {exe} not found. Install FFmpeg.")


def _convert_to_mp3(src: Path, dest: Path) -> Path:
    _check_ffmpeg()
    _run([
        "ffmpeg", "-y",
        "-i", str(src),
        "-vn",
        "-c:a", "libmp3lame",
        "-q:a", "2",
        str(dest),
    ])
    return dest


def _download_file(url: str, dest: Path, timeout: int = 300) -> Path:
    """Download a file from a direct URL."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    return dest


def _download_with_cobalt(youtube_url: str, output_dir: Path) -> Optional[Path]:
    """Try downloading audio via Cobalt API. Returns MP3 Path or None on failure."""
    # Current Cobalt API uses POST / (root). The old /api/json endpoint was shut down in v7.
    # User should set COBALT_API_URL to their self-hosted instance, e.g. https://cobalt.example.com/
    cobalt_url = os.environ.get("COBALT_API_URL", "https://api.cobalt.tools/").rstrip("/") + "/"
    payload = {
        "url": youtube_url,
        "downloadMode": "audio",
        "audioFormat": "mp3",
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    print(f"[DOWNLOADER] Trying Cobalt API: {cobalt_url}")
    print(f"[DOWNLOADER] Request JSON: {json.dumps(payload, indent=2)}")
    print(f"[DOWNLOADER] Request headers: {json.dumps(headers, indent=2)}")

    try:
        r = requests.post(cobalt_url, json=payload, headers=headers, timeout=60)
        print(f"[DOWNLOADER] Response status: {r.status_code}")
        print(f"[DOWNLOADER] Response body: {r.text}")
        r.raise_for_status()
        data = r.json()
    except requests.HTTPError as exc:
        print(f"[DOWNLOADER] Cobalt API HTTP error: {exc}")
        return None
    except Exception as exc:
        print(f"[DOWNLOADER] Cobalt API request failed: {exc}")
        return None

    status = data.get("status", "")
    if status == "error":
        # New schema: {"status":"error","error":{"code":"..."}}
        # Old schema fallback: {"status":"error","text":"..."}
        error_detail = data.get("error", {})
        error_code = error_detail.get("code") if isinstance(error_detail, dict) else None
        error_text = data.get("text") or error_code or "unknown"
        print(f"[DOWNLOADER] Cobalt API returned error: {error_text}")
        return None

    download_url = data.get("url")
    if not download_url:
        print(f"[DOWNLOADER] Cobalt API response missing url. status={status}")
        return None

    print(f"[DOWNLOADER] Cobalt status={status}, downloading from returned URL...")
    raw_path = output_dir / "source_audio_cobalt.raw"
    try:
        _download_file(download_url, raw_path)
    except Exception as exc:
        print(f"[DOWNLOADER] Cobalt direct download failed: {exc}")
        return None

    # Convert whatever Cobalt gave us to MP3.
    mp3_path = output_dir / "source_audio.mp3"
    try:
        _convert_to_mp3(raw_path, mp3_path)
    finally:
        if raw_path.exists():
            raw_path.unlink()

    print(f"[DOWNLOADER] Cobalt success: {mp3_path}")
    return mp3_path


def _download_with_ytdlp(youtube_url: str, output_dir: Path) -> Optional[Path]:
    """Fallback yt-dlp download. Returns MP3 Path or None on failure."""
    _check_ffmpeg()
    outtmpl = output_dir / "source_audio.%(ext)s"

    cookies_text = os.environ.get("YOUTUBE_COOKIES", "").strip()
    cookies_path = output_dir / "cookies.txt"
    wrote_cookies = False

    if cookies_text:
        cookies_path.write_text(cookies_text, encoding="utf-8")
        wrote_cookies = True
    else:
        print("YOUTUBE_COOKIES secret missing")

    try:
        # Log available formats
        print(f"\n=== yt-dlp format list for {youtube_url} ===")
        if shutil.which("yt-dlp"):
            list_cmd = ["yt-dlp", "--no-playlist", "--list-formats", youtube_url]
            if wrote_cookies:
                list_cmd += ["--cookies", str(cookies_path)]
            subprocess.run(list_cmd, check=False)
        else:
            try:
                import yt_dlp
                list_opts = {"quiet": False, "noplaylist": True, "skip_download": True}
                if wrote_cookies:
                    list_opts["cookies"] = str(cookies_path)
                with yt_dlp.YoutubeDL(list_opts) as ydl:
                    info = ydl.extract_info(youtube_url, download=False)
                    if info and info.get("formats"):
                        print("Available formats:")
                        for f in info.get("formats", []):
                            print(f"  {f.get('format_id'):>4}  {f.get('ext'):<5}  {f.get('resolution','audio only'):<12}  {f.get('abr',''):>5}  {f.get('acodec',''):<8}  {f.get('vcodec',''):<8}")
            except Exception as list_exc:
                print(f"Could not list formats: {list_exc}")
        print("=== end format list ===\n")

        # Download best available audio
        if shutil.which("yt-dlp"):
            cmd = [
                "yt-dlp",
                "--no-playlist",
                "-f", "ba/b",
                "--output", str(outtmpl),
            ]
            if wrote_cookies:
                cmd += ["--cookies", str(cookies_path)]
            cmd.append(youtube_url)
            _run(cmd)
        else:
            try:
                import yt_dlp
            except ImportError as exc:
                raise SystemExit("Install yt-dlp first: pip install -r requirements.txt") from exc

            opts = {
                "format": "ba/b",
                "noplaylist": True,
                "outtmpl": str(outtmpl),
            }
            if wrote_cookies:
                opts["cookies"] = str(cookies_path)
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([youtube_url])

        candidates = sorted(output_dir.glob("source_audio.*"))
        if not candidates:
            raise SystemExit("ERROR: No playable audio formats found for this YouTube URL.")

        downloaded = candidates[0]
        print(f"Downloaded raw audio: {downloaded}")

        mp3_path = output_dir / "source_audio.mp3"
        print(f"Converting to MP3 with FFmpeg: {downloaded} -> {mp3_path}")
        _convert_to_mp3(downloaded, mp3_path)

        if downloaded != mp3_path:
            downloaded.unlink()
            print(f"Removed raw file: {downloaded}")

        print(f"Final MP3 audio: {mp3_path}")
        return mp3_path
    finally:
        if cookies_path.exists():
            cookies_path.unlink()


def download_audio(youtube_url: str, output_dir: Path) -> Path:
    """Download audio from YouTube URL using Cobalt API first, then yt-dlp fallback.

    Returns path to MP3 file.
    Raises RuntimeError with a clean message if both methods fail.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1) Try Cobalt
    result = _download_with_cobalt(youtube_url, output_dir)
    if result:
        return result

    # 2) Fallback to yt-dlp
    print("[DOWNLOADER] Cobalt failed. Falling back to yt-dlp...")
    result = _download_with_ytdlp(youtube_url, output_dir)
    if result:
        return result

    # 3) Both failed
    raise RuntimeError(
        "YouTube audio download failed.\n\n"
        "Both Cobalt API and yt-dlp were unable to download this video.\n"
        "Possible reasons:\n"
        "• Video is restricted, private, or age-gated\n"
        "• YouTube blocked the downloader IP\n"
        "• Video has no audio stream\n\n"
        "Try /audio_retry and upload the audio file manually."
    )
