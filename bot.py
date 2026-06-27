#!/usr/bin/env python3
"""
YouTube Song Automation Bot

Creates a music video from:
  1) a generated thumbnail/title card,
  2) a user-provided audio file,
  3) a user-provided outro video,
and optionally uploads the final video + thumbnail to YouTube.

IMPORTANT: Use only audio you own, licensed, or have permission to upload.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# YouTube imports are optional until upload is requested.
YOUTUBE_UPLOAD_SCOPE = ["https://www.googleapis.com/auth/youtube.upload"]


SAFE_CHARS_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def run(cmd: list[str]) -> None:
    print("\n$ " + " ".join(map(str, cmd)))
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def check_ffmpeg() -> None:
    for exe in ("ffmpeg", "ffprobe"):
        if not shutil.which(exe):
            raise SystemExit(
                f"ERROR: {exe} not found. Install FFmpeg and ensure it is in your PATH."
            )


def slugify(text: str, max_len: int = 80) -> str:
    text = SAFE_CHARS_RE.sub("-", text.strip()).strip("-").lower()
    return (text[:max_len].strip("-") or "video")


def find_font(preferred_size: int, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        if path and Path(path).exists():
            return ImageFont.truetype(path, preferred_size)
    return ImageFont.load_default()


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = current + " " + word
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def make_gradient(width: int, height: int, palette: list[tuple[int, int, int]]) -> Image.Image:
    img = Image.new("RGB", (width, height))
    pix = img.load()
    c1, c2, c3 = palette
    for y in range(height):
        for x in range(width):
            # diagonal + radial-ish blend
            t = (x / width * 0.55) + (y / height * 0.45)
            if t < 0.5:
                k = t / 0.5
                c = tuple(int(c1[i] * (1 - k) + c2[i] * k) for i in range(3))
            else:
                k = (t - 0.5) / 0.5
                c = tuple(int(c2[i] * (1 - k) + c3[i] * k) for i in range(3))
            pix[x, y] = c
    return img


def generate_thumbnail(song_name: str, artist: Optional[str], output: Path, size=(1280, 720)) -> None:
    width, height = size
    random.seed(song_name + (artist or ""))
    palettes = [
        [(15, 12, 41), (78, 31, 162), (255, 79, 129)],
        [(4, 21, 31), (0, 116, 144), (0, 255, 209)],
        [(28, 6, 50), (111, 23, 152), (255, 186, 73)],
        [(9, 10, 15), (50, 88, 160), (167, 224, 255)],
        [(34, 8, 20), (190, 49, 68), (255, 200, 87)],
    ]
    img = make_gradient(width, height, random.choice(palettes)).convert("RGBA")

    # Soft bokeh circles
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for _ in range(36):
        r = random.randint(25, 120)
        x = random.randint(-r, width)
        y = random.randint(-r, height)
        color = random.choice([(255, 255, 255, 28), (255, 220, 130, 25), (120, 220, 255, 30), (255, 100, 180, 30)])
        od.ellipse((x, y, x + r, y + r), fill=color)
    overlay = overlay.filter(ImageFilter.GaussianBlur(10))
    img.alpha_composite(overlay)

    # Dark vignette for readable text
    vignette = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    vd = ImageDraw.Draw(vignette)
    vd.rectangle((0, 0, width, height), fill=(0, 0, 0, 45))
    img.alpha_composite(vignette)

    draw = ImageDraw.Draw(img)
    title_font = find_font(88, bold=True)
    artist_font = find_font(42, bold=False)
    small_font = find_font(28, bold=True)

    max_text_width = int(width * 0.82)
    title_lines = wrap_text(draw, song_name.upper(), title_font, max_text_width)
    if len(title_lines) > 3:
        title_font = find_font(72, bold=True)
        title_lines = wrap_text(draw, song_name.upper(), title_font, max_text_width)

    line_heights = []
    for line in title_lines:
        b = draw.textbbox((0, 0), line, font=title_font)
        line_heights.append(b[3] - b[1])
    title_block_h = sum(line_heights) + (len(title_lines) - 1) * 14
    artist_text = artist.upper() if artist else "MUSIC VIDEO"
    ab = draw.textbbox((0, 0), artist_text, font=artist_font)
    total_h = title_block_h + 34 + (ab[3] - ab[1])
    y = (height - total_h) // 2

    # Decorative label
    label = "OFFICIAL AUDIO"
    lb = draw.textbbox((0, 0), label, font=small_font)
    lx = (width - (lb[2] - lb[0] + 48)) // 2
    ly = y - 72
    draw.rounded_rectangle((lx, ly, lx + lb[2] - lb[0] + 48, ly + 46), radius=23, fill=(255, 255, 255, 42), outline=(255, 255, 255, 120), width=2)
    draw.text((lx + 24, ly + 8), label, font=small_font, fill=(255, 255, 255, 230))

    # Title with shadow
    for line, lh in zip(title_lines, line_heights):
        b = draw.textbbox((0, 0), line, font=title_font)
        x = (width - (b[2] - b[0])) // 2
        for dx, dy, alpha in [(5, 7, 120), (2, 3, 180)]:
            draw.text((x + dx, y + dy), line, font=title_font, fill=(0, 0, 0, alpha))
        draw.text((x, y), line, font=title_font, fill=(255, 255, 255, 255))
        y += lh + 14

    # Artist/subtitle
    y += 20
    ab = draw.textbbox((0, 0), artist_text, font=artist_font)
    ax = (width - (ab[2] - ab[0])) // 2
    draw.text((ax + 2, y + 3), artist_text, font=artist_font, fill=(0, 0, 0, 140))
    draw.text((ax, y), artist_text, font=artist_font, fill=(255, 232, 190, 245))

    # Minimal waveform bars
    bar_y = height - 95
    cx = width // 2
    for i in range(90):
        h = random.randint(8, 62)
        x = cx - 450 + i * 10
        draw.rounded_rectangle((x, bar_y - h // 2, x + 4, bar_y + h // 2), radius=2, fill=(255, 255, 255, random.randint(80, 180)))

    output.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(output, quality=95)
    print(f"Thumbnail saved: {output}")



def download_youtube_audio(youtube_url: str, output_dir: Path) -> Path:
    """Download/extract audio from a YouTube URL using yt-dlp.

    Only use this for content you own, have licensed, or have explicit permission to reuse.
    This function does not bypass DRM; it relies on yt-dlp/FFmpeg for normal extraction.
    """
    check_ffmpeg()
    output_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = output_dir / "source_audio.%(ext)s"

    # Prefer command-line yt-dlp because it is easier for users to update/debug.
    if shutil.which("yt-dlp"):
        run([
            "yt-dlp",
            "--no-playlist",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--output", str(outtmpl),
            youtube_url,
        ])
    else:
        try:
            import yt_dlp
        except ImportError as exc:
            raise SystemExit("Install yt-dlp first: pip install -r requirements.txt") from exc

        opts = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "outtmpl": str(outtmpl),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "0",
            }],
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([youtube_url])

    candidates = sorted(output_dir.glob("source_audio.*"))
    mp3s = [c for c in candidates if c.suffix.lower() == ".mp3"]
    if mp3s:
        print(f"Downloaded audio: {mp3s[0]}")
        return mp3s[0]
    if candidates:
        print(f"Downloaded audio: {candidates[0]}")
        return candidates[0]
    raise SystemExit("Could not find downloaded YouTube audio output.")


def get_youtube_title(youtube_url: str) -> Optional[str]:
    """Try to read the YouTube title without downloading media."""
    try:
        if shutil.which("yt-dlp"):
            proc = subprocess.run(
                ["yt-dlp", "--no-playlist", "--print", "%(title)s", "--skip-download", youtube_url],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip().splitlines()[-1]
        else:
            import yt_dlp
            with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True, "skip_download": True}) as ydl:
                info = ydl.extract_info(youtube_url, download=False)
                title = info.get("title") if info else None
                return title
    except Exception:
        return None
    return None

def render_video(thumbnail: Path, audio: Path, outro: Path, output: Path, workdir: Path) -> None:
    check_ffmpeg()
    workdir.mkdir(parents=True, exist_ok=True)
    main_video = workdir / "main_song_video.mp4"
    outro_norm = workdir / "outro_normalized.mp4"
    concat_file = workdir / "concat.txt"

    # Main video: still thumbnail for whole audio duration.
    run([
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", str(thumbnail),
        "-i", str(audio),
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-c:a", "aac",
        "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-vf", "scale=1920:1080,setsar=1,fps=30",
        "-shortest",
        str(main_video),
    ])

    # Normalize outro to match concat requirements.
    run([
        "ffmpeg", "-y",
        "-i", str(outro),
        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        str(outro_norm),
    ])

    concat_file.write_text(
        f"file '{main_video.resolve().as_posix()}'\nfile '{outro_norm.resolve().as_posix()}'\n",
        encoding="utf-8",
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_file),
        "-c", "copy",
        str(output),
    ])
    print(f"Final video saved: {output}")


def get_authenticated_youtube(client_secrets: Path, token_file: Path):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise SystemExit("Install dependencies first: pip install -r requirements.txt") from exc

    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), YOUTUBE_UPLOAD_SCOPE)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not client_secrets.exists():
                raise SystemExit(
                    f"Missing {client_secrets}. Download OAuth client JSON from Google Cloud "
                    "and save it there. See README.md."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), YOUTUBE_UPLOAD_SCOPE)
            creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json(), encoding="utf-8")

    return build("youtube", "v3", credentials=creds)


def upload_to_youtube(
    video_file: Path,
    thumbnail_file: Path,
    title: str,
    description: str,
    tags: list[str],
    category_id: str,
    privacy_status: str,
    made_for_kids: bool,
    client_secrets: Path,
    token_file: Path,
) -> str:
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise SystemExit("Install dependencies first: pip install -r requirements.txt") from exc

    youtube = get_authenticated_youtube(client_secrets, token_file)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": made_for_kids,
        },
    }

    insert_request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=MediaFileUpload(str(video_file), chunksize=-1, resumable=True),
    )

    print("Uploading video to YouTube...")
    response = None
    while response is None:
        status, response = insert_request.next_chunk()
        if status:
            print(f"Uploaded {int(status.progress() * 100)}%")

    video_id = response["id"]
    print(f"Video uploaded: https://www.youtube.com/watch?v={video_id}")

    if thumbnail_file.exists():
        print("Uploading thumbnail...")
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumbnail_file)),
        ).execute()
        print("Thumbnail uploaded.")

    return video_id


def parse_tags(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]


def create_upload(args: argparse.Namespace) -> None:
    if not args.audio_file and not args.youtube_url:
        raise SystemExit("Provide either --audio-file or --youtube-url")
    if args.audio_file and args.youtube_url:
        raise SystemExit("Use only one source: either --audio-file or --youtube-url, not both")

    song_name = args.song_name
    if not song_name and args.youtube_url:
        song_name = get_youtube_title(args.youtube_url) or "YouTube Song"
    if not song_name:
        raise SystemExit("Provide --song-name, or use --youtube-url so the title can be detected")

    outro = Path(args.outro_file).expanduser().resolve()
    if not outro.exists():
        raise SystemExit(f"Outro file not found: {outro}")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = slugify(song_name)
    out_dir = Path(args.output_dir).expanduser().resolve() / f"{base}-{stamp}"
    thumb = out_dir / "thumbnail.jpg"
    video = out_dir / f"{base}.mp4"
    workdir = out_dir / "work"

    if args.youtube_url:
        print("Using YouTube URL as the audio source. Make sure you have reuse/upload rights for this content.")
        audio = download_youtube_audio(args.youtube_url, out_dir / "downloaded_audio")
    else:
        audio = Path(args.audio_file).expanduser().resolve()
        if not audio.exists():
            raise SystemExit(f"Audio file not found: {audio}")

    generate_thumbnail(song_name, args.artist, thumb)
    render_video(thumb, audio, outro, video, workdir)

    metadata = {
        "song_name": song_name,
        "artist": args.artist,
        "source_youtube_url": args.youtube_url,
        "title": args.title or song_name,
        "description": args.description,
        "tags": parse_tags(args.tags),
        "privacy": args.privacy,
        "video": str(video),
        "thumbnail": str(thumb),
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    if args.no_upload:
        print("\nDone. Upload skipped because --no-upload was used.")
        print(f"Video: {video}")
        print(f"Thumbnail: {thumb}")
        return

    video_id = upload_to_youtube(
        video_file=video,
        thumbnail_file=thumb,
        title=args.title or song_name,
        description=args.description,
        tags=parse_tags(args.tags),
        category_id=args.category_id,
        privacy_status=args.privacy,
        made_for_kids=args.made_for_kids,
        client_secrets=Path(args.client_secrets).expanduser().resolve(),
        token_file=Path(args.token_file).expanduser().resolve(),
    )
    print(f"\nDone: https://www.youtube.com/watch?v={video_id}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a thumbnail music video, append outro, and upload to YouTube.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-upload", help="Create video and optionally upload it")
    p.add_argument("--song-name", default=None, help="Song name shown on thumbnail and used as default YouTube title. Optional if --youtube-url is used; the bot will try to detect the YouTube title.")
    p.add_argument("--artist", default=None, help="Optional artist/channel name shown on thumbnail")
    p.add_argument("--audio-file", default=None, help="Path to licensed/local audio file, e.g. song.mp3")
    p.add_argument("--youtube-url", default=None, help="YouTube video URL to use as the audio source. Use only when you have permission/license to reuse and upload it.")
    p.add_argument("--outro-file", required=True, help="Path to outro video file, e.g. outro.mp4")
    p.add_argument("--output-dir", default="outputs", help="Folder for generated files")

    p.add_argument("--title", default=None, help="YouTube title. Defaults to song name")
    p.add_argument("--description", default="", help="YouTube description")
    p.add_argument("--tags", default="music,official audio", help="Comma-separated YouTube tags")
    p.add_argument("--category-id", default="10", help="YouTube category ID. 10 = Music")
    p.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    p.add_argument("--made-for-kids", action="store_true", help="Mark as made for kids")

    p.add_argument("--client-secrets", default="client_secrets.json", help="Google OAuth client secrets JSON")
    p.add_argument("--token-file", default="token.json", help="Where OAuth token is stored after first login")
    p.add_argument("--no-upload", action="store_true", help="Generate files but do not upload")
    p.set_defaults(func=create_upload)
    return parser


def main(argv: Optional[list[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
