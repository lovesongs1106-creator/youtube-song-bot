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

    cookies_text = os.environ.get("YOUTUBE_COOKIES", "").strip()
    cookies_path = output_dir / "cookies.txt"
    wrote_cookies = False

    if cookies_text:
        cookies_path.write_text(cookies_text, encoding="utf-8")
        wrote_cookies = True
    else:
        print("YOUTUBE_COOKIES secret missing")

    try:
        # Prefer command-line yt-dlp because it is easier for users to update/debug.
        if shutil.which("yt-dlp"):
            cmd = [
                "yt-dlp",
                "--no-playlist",
                "--extract-audio",
                "--audio-format", "mp3",
                "--audio-quality", "0",
                "--output", str(outtmpl),
            ]
            if wrote_cookies:
                cmd += ["--cookies", str(cookies_path)]
            cmd.append(youtube_url)
            run(cmd)
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
            if wrote_cookies:
                opts["cookies"] = str(cookies_path)
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([youtube_url])
    finally:
        if cookies_path.exists():
            cookies_path.unlink()

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



def _cover_resize(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Resize/crop image to cover target size."""
    target_w, target_h = size
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_size = (int(src_w * scale), int(src_h * scale))
    img = img.resize(new_size, Image.Resampling.LANCZOS)
    left = (img.width - target_w) // 2
    top = (img.height - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))




def prepare_exact_thumbnail(input_image: Path, output: Path, size=(1280, 720)) -> None:
    """Prepare user-made thumbnail exactly as the video poster/YouTube thumbnail.

    The user's image is preserved as much as possible. If not 16:9, it is resized with
    padding/blurred background instead of adding text or changing the design.
    """
    width, height = size
    img = Image.open(input_image).convert("RGB")

    # If already 16:9-ish, cover resize to exact size. Otherwise fit with blurred background.
    src_ratio = img.width / img.height
    target_ratio = width / height
    if abs(src_ratio - target_ratio) < 0.08:
        out = _cover_resize(img, size).convert("RGB")
    else:
        bg = _cover_resize(img, size).convert("RGB").filter(ImageFilter.GaussianBlur(18))
        dark = Image.new("RGB", size, (0, 0, 0))
        bg = Image.blend(bg, dark, 0.25)
        scale = min(width / img.width, height / img.height)
        new_size = (int(img.width * scale), int(img.height * scale))
        fg = img.resize(new_size, Image.Resampling.LANCZOS)
        x = (width - fg.width) // 2
        y = (height - fg.height) // 2
        bg.paste(fg, (x, y))
        out = bg

    output.parent.mkdir(parents=True, exist_ok=True)
    out.save(output, quality=95)
    print(f"Exact thumbnail saved: {output}")

def generate_reference_thumbnail(
    song_name: str,
    artist: Optional[str],
    reference_image: Path,
    output: Path,
    size=(1280, 720),
) -> None:
    """Create a high-impact YouTube thumbnail using user's reference image.

    This is template-based, not AI-generated. It uses the provided image as a blurred/cropped
    background and creates bold YouTube-style text overlays.
    """
    width, height = size
    ref = Image.open(reference_image).convert("RGB")
    bg = _cover_resize(ref, size).convert("RGBA")
    bg = bg.filter(ImageFilter.GaussianBlur(10))

    # dark cinematic overlay
    overlay = Image.new("RGBA", size, (0, 0, 0, 80))
    bg.alpha_composite(overlay)

    draw = ImageDraw.Draw(bg)

    # Add warm spotlight/vignette
    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-180, -140, 820, 860), fill=(255, 190, 30, 45))
    gd.ellipse((520, -150, 1500, 830), fill=(255, 80, 20, 35))
    glow = glow.filter(ImageFilter.GaussianBlur(55))
    bg.alpha_composite(glow)

    # Right-side hero card with original reference image
    card_w, card_h = 500, 560
    card_x, card_y = width - card_w - 55, 80
    hero = _cover_resize(ref, (card_w, card_h)).convert("RGBA")
    hero = hero.filter(ImageFilter.UnsharpMask(radius=2, percent=135, threshold=3))
    mask = Image.new("L", (card_w, card_h), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle((0, 0, card_w, card_h), radius=36, fill=255)
    shadow = Image.new("RGBA", size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle((card_x + 14, card_y + 18, card_x + card_w + 14, card_y + card_h + 18), radius=36, fill=(0, 0, 0, 150))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    bg.alpha_composite(shadow)
    # Paste rounded hero image with mask. alpha_composite has no mask parameter.
    bg.paste(hero, (card_x, card_y), mask)
    draw = ImageDraw.Draw(bg)
    draw.rounded_rectangle((card_x, card_y, card_x + card_w, card_y + card_h), radius=36, outline=(255, 210, 60, 220), width=5)

    # Text panel on left
    panel = Image.new("RGBA", size, (0, 0, 0, 0))
    pd = ImageDraw.Draw(panel)
    pd.rounded_rectangle((45, 85, 760, 625), radius=35, fill=(0, 0, 0, 115), outline=(255, 220, 70, 150), width=3)
    bg.alpha_composite(panel)

    # Fonts
    title_font = find_font(94, bold=True)
    title_font_small = find_font(78, bold=True)
    artist_font = find_font(42, bold=True)
    badge_font = find_font(30, bold=True)

    title = song_name.upper().strip()
    max_width = 640
    lines = wrap_text(draw, title, title_font, max_width)
    if len(lines) > 3:
        title_font = title_font_small
        lines = wrap_text(draw, title, title_font, max_width)
    if len(lines) > 4:
        # hard trim for thumbnail readability
        words = title.split()
        title = " ".join(words[:8]) + "..."
        lines = wrap_text(draw, title, title_font_small, max_width)
        title_font = title_font_small

    # Badge
    badge = "TRENDING SONG!"
    bb = draw.textbbox((0, 0), badge, font=badge_font)
    draw.rounded_rectangle((72, 112, 72 + (bb[2]-bb[0]) + 34, 158), radius=22, fill=(255, 207, 48, 255))
    draw.text((89, 120), badge, font=badge_font, fill=(15, 15, 15, 255))

    # Title text with yellow/white alternating words/lines
    y = 190
    colors = [(255, 255, 255, 255), (255, 218, 46, 255)]
    for idx, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=title_font)
        x = 78
        # heavy shadow/stroke effect
        for dx, dy in [(6, 7), (3, 4), (-2, 3)]:
            draw.text((x + dx, y + dy), line, font=title_font, fill=(0, 0, 0, 210))
        draw.text((x, y), line, font=title_font, fill=colors[idx % 2], stroke_width=3, stroke_fill=(0, 0, 0, 230))
        y += (bbox[3] - bbox[1]) + 10

    # Artist/channel line
    if artist:
        artist_text = artist.upper()
    else:
        artist_text = "OFFICIAL AUDIO"
    y += 18
    draw.text((82, y + 3), artist_text, font=artist_font, fill=(0, 0, 0, 180))
    draw.text((80, y), artist_text, font=artist_font, fill=(255, 235, 160, 255))

    # Bottom strip
    strip = "FULL SONG • MUSIC VIDEO"
    sb = draw.textbbox((0, 0), strip, font=badge_font)
    draw.rounded_rectangle((72, 555, 72 + (sb[2]-sb[0]) + 40, 606), radius=25, fill=(180, 0, 0, 230), outline=(255, 255, 255, 120), width=2)
    draw.text((92, 565), strip, font=badge_font, fill=(255, 255, 255, 255))

    output.parent.mkdir(parents=True, exist_ok=True)
    bg.convert("RGB").save(output, quality=95)
    print(f"Reference thumbnail saved: {output}")


def generate_seo_metadata(
    song_name: str,
    artist: Optional[str] = None,
    source_url: Optional[str] = None,
    custom_title: Optional[str] = None,
    custom_description: Optional[str] = None,
    custom_tags: Optional[list[str]] = None,
) -> dict:
    """Generate professional YouTube SEO metadata for music uploads."""
    clean_title = song_name.strip()
    artist_clean = (artist or "").strip()
    if artist_clean:
        yt_title = f"{clean_title} - {artist_clean} | Official Audio"
    else:
        yt_title = f"{clean_title} | Official Audio"
    yt_title = yt_title[:100]

    hashtags = []
    for item in [clean_title, artist_clean, "OfficialAudio", "NewSong"]:
        if item:
            tag = re.sub(r"[^A-Za-z0-9]", "", item.title())
            if tag:
                hashtags.append("#" + tag)
    hashtags = list(dict.fromkeys(hashtags))[:5]

    desc_lines = [
        f"{clean_title} - Official Audio",
        "",
        "Enjoy this licensed music upload. Like, share, and subscribe for more songs.",
        "",
        f"Song: {clean_title}",
    ]
    if artist_clean:
        desc_lines.append(f"Artist/Channel: {artist_clean}")
    desc_lines += [
        "",
        "Listen with headphones for the best experience.",
        "",
    ]
    if source_url:
        desc_lines += [f"Source/Reference: {source_url}", ""]
    desc_lines += [
        "This upload is shared with permission/license from the rights holder.",
        "",
        " ".join(hashtags),
    ]
    description = "\n".join(desc_lines)

    base_tags = [
        clean_title,
        f"{clean_title} official audio",
        f"{clean_title} song",
        f"{clean_title} full song",
        "official audio",
        "new song",
        "music video",
        "full song",
        "latest song",
        "trending song",
        "viral song",
        "audio song",
        "love song",
        "music",
    ]
    if artist_clean:
        base_tags = [artist_clean, f"{artist_clean} songs", f"{clean_title} {artist_clean}"] + base_tags
    # YouTube tags total limit is 500 chars; keep concise
    tags = []
    total = 0
    for tag in base_tags:
        tag = tag.strip()
        if not tag or tag.lower() in [t.lower() for t in tags]:
            continue
        if total + len(tag) + 1 > 450:
            break
        tags.append(tag)
        total += len(tag) + 1

    if custom_title:
        yt_title = custom_title.strip()[:100]
    if custom_description:
        description = custom_description.strip()
    if custom_tags:
        tags = [t.strip() for t in custom_tags if t and t.strip()]

    return {"title": yt_title, "description": description, "tags": tags}



def get_render_settings() -> tuple[int, int, int, str]:
    """Low-memory render settings for small cloud instances.

    Defaults to 720p/24fps because 1080p x264 can exceed 512MB RAM on free hosts.
    Override with env vars:
      VIDEO_WIDTH=1280
      VIDEO_HEIGHT=720
      VIDEO_FPS=24
      FFMPEG_PRESET=ultrafast
    If Render free still OOMs, try VIDEO_WIDTH=854 VIDEO_HEIGHT=480.
    """
    width = int(os.environ.get("VIDEO_WIDTH", "1280"))
    height = int(os.environ.get("VIDEO_HEIGHT", "720"))
    fps = int(os.environ.get("VIDEO_FPS", "24"))
    preset = os.environ.get("FFMPEG_PRESET", "ultrafast")
    return width, height, fps, preset

def render_video(thumbnail: Path, audio: Path, outro: Path, output: Path, workdir: Path) -> None:
    check_ffmpeg()
    width, height, fps, preset = get_render_settings()
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
        "-preset", preset,
        "-tune", "stillimage",
        "-threads", "1",
        "-c:a", "aac",
        "-b:a", "160k",
        "-pix_fmt", "yuv420p",
        "-vf", f"scale={width}:{height},setsar=1,fps={fps}",
        "-shortest",
        str(main_video),
    ])

    # Normalize outro to match concat requirements.
    run([
        "ffmpeg", "-y",
        "-i", str(outro),
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}",
        "-c:v", "libx264",
        "-preset", preset,
        "-threads", "1",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "160k",
        "-ar", "44100",
        str(outro_norm),
    ])

    concat_file.write_text(
        f"file '{main_video.resolve().as_posix()}'\nfile '{outro_norm.resolve().as_posix()}'\n",
        encoding="utf-8",
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    # Final concat WITHOUT -c copy (ensures audio never mutes)
    run([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_file),
        "-c:v", "libx264",
        "-preset", preset,
        "-threads", "1",
        "-c:a", "aac",
        "-b:a", "160k",
        "-ar", "44100",
        "-ac", "2",
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
