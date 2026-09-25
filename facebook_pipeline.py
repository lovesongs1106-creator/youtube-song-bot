#!/usr/bin/env python3
"""Facebook video pipeline for the second YouTube channel worker path.

Runs inside GitHub Actions (see github_worker.py `facebook_url` branch):
  1. Download Facebook video with yt-dlp (public/accessible content only).
  2. Optional exact trim with FFmpeg (re-encode for frame accuracy).
  3. Normalize + append the fixed outro asset (assets/outro.mp4).
  4. Extract a video frame for the thumbnail reference image.
  5. Build title/description/tags via the existing bot.py SEO conventions.

ACCESS POLICY — strictly enforced, no bypasses:
  * No cookies, no login, no credentials are ever passed to the downloader.
  * If Facebook/yt-dlp reports login-required, private, or DRM-protected
    content, the download is refused with a clear error. Only videos that
    are legitimately accessible to an unauthenticated downloader are fetched.
  * Only process videos you own, have licensed, or have explicit permission
    to reuse and re-upload.

Requires ffmpeg/ffprobe/yt-dlp on PATH (provided by the workflow).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

from bot import generate_seo_metadata, get_render_settings

OUTRO_ASSET_DEFAULT = "assets/outro.mp4"

# yt-dlp failure signatures that mean "not legitimately accessible".
ACCESS_DENIED_MARKERS = (
    "login required",
    "log in to watch",
    "private video",
    "this video is private",
    "only available to",
    "drm",
    "this video is protected",
    "cookies",
    "sign in to confirm",
)


def _check_tool(name: str) -> None:
    if not shutil.which(name):
        raise RuntimeError(f"ERROR: {name} not found on PATH.")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    print("\n$ " + " ".join(map(str, cmd)), flush=True)
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def get_facebook_title(fb_url: str) -> Optional[str]:
    """Read the Facebook video title without downloading media."""
    _check_tool("yt-dlp")
    proc = _run([
        "yt-dlp", "--no-playlist", "--skip-download",
        "--print", "%(title)s", "--no-warnings", fb_url,
    ])
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().splitlines()[-1][:150]
    return None


def download_facebook_video(fb_url: str, output_dir: Path) -> Path:
    """Download a Facebook video to MP4. Unauthenticated access only.

    Raises RuntimeError with a clear, user-safe message when the video is
    login-gated, private, DRM-protected, or otherwise unavailable.
    """
    _check_tool("yt-dlp")
    _check_tool("ffmpeg")
    output_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = output_dir / "fb_source.%(ext)s"

    # NOTE: deliberately no --cookies / --username / --password. Public-or-nothing.
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "--merge-output-format", "mp4",
        "--output", str(outtmpl),
        "--no-continue",
        fb_url,
    ]
    proc = _run(cmd)
    log_text = f"{proc.stdout}\n{proc.stderr}".lower()
    if proc.returncode != 0:
        if any(m in log_text for m in ACCESS_DENIED_MARKERS):
            raise RuntimeError(
                "Facebook video is not publicly accessible.\n\n"
                "This bot only downloads videos that are legitimately accessible "
                "without login. Login-gated, private or DRM-protected content is "
                "refused and never bypassed.\n\n"
                "If this is your own video, make its visibility Public (or "
                "unlisted-with-link if Facebook supports it for this post type) "
                "and try again."
            )
        raise RuntimeError(
            "Facebook download failed.\n\n"
            f"yt-dlp error: {(proc.stderr or proc.stdout).strip()[-500:]}"
        )

    candidates = sorted(output_dir.glob("fb_source.*"))
    if not candidates:
        raise RuntimeError("Facebook download produced no file.")
    video = candidates[0]
    if video.suffix.lower() != ".mp4":
        converted = output_dir / "fb_source.mp4"
        _run(["ffmpeg", "-y", "-i", str(video), "-c", "copy", str(converted)])
        video.unlink(missing_ok=True)
        video = converted
    print(f"Facebook video downloaded: {video}", flush=True)
    return video


def probe_duration(video: Path) -> Optional[float]:
    _check_tool("ffprobe")
    proc = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(video),
    ])
    try:
        return float(proc.stdout.strip())
    except (ValueError, TypeError):
        return None


def has_audio_stream(video: Path) -> bool:
    _check_tool("ffprobe")
    proc = _run([
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(video),
    ])
    return bool(proc.stdout.strip())


def trim_video(input_path: Path, output_path: Path, start: float, end: float) -> Path:
    """Trim exactly [start, end) seconds. Re-encodes for frame accuracy."""
    _check_tool("ffmpeg")
    _, _, _, preset = get_render_settings()
    duration = probe_duration(input_path)
    if duration and start >= duration:
        raise RuntimeError(
            f"Trim start {start:.0f}s is beyond the video duration ({duration:.0f}s)."
        )
    if duration and end > duration:
        print(f"[FB] Trim end {end:.0f}s exceeds duration {duration:.0f}s — clamping.", flush=True)
        end = duration
    output_path.parent.mkdir(parents=True, exist_ok=True)
    proc = _run([
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-ss", f"{start:.3f}",
        "-to", f"{end:.3f}",
        "-c:v", "libx264", "-preset", preset,
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
        str(output_path),
    ])
    if proc.returncode != 0 or not output_path.exists():
        raise RuntimeError(f"FFmpeg trim failed: {proc.stderr.strip()[-500:]}")
    print(f"Trimmed video saved: {output_path}", flush=True)
    return output_path


def extract_frame(video: Path, output: Path, at_seconds: Optional[float] = None) -> Path:
    """Extract one frame as JPG for the thumbnail reference image."""
    _check_tool("ffmpeg")
    if at_seconds is None:
        duration = probe_duration(video) or 10.0
        at_seconds = max(1.0, duration * 0.25)
    output.parent.mkdir(parents=True, exist_ok=True)
    proc = _run([
        "ffmpeg", "-y",
        "-ss", f"{at_seconds:.2f}",
        "-i", str(video),
        "-frames:v", "1",
        "-q:v", "3",
        str(output),
    ])
    if proc.returncode != 0 or not output.exists():
        raise RuntimeError(f"Frame extraction failed: {proc.stderr.strip()[-300:]}")
    return output


def _ensure_audio(video: Path, workdir: Path) -> Path:
    """Mux a silent stereo track when the source has no audio stream.

    The concat filter joins v+a, so both segments must carry audio.
    """
    if has_audio_stream(video):
        return video
    print("[FB] No audio stream — adding silent track.", flush=True)
    out = workdir / "with_silent_audio.mp4"
    proc = _run([
        "ffmpeg", "-y",
        "-i", str(video),
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-shortest",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
        str(out),
    ])
    if proc.returncode != 0:
        raise RuntimeError(f"Silent-audio mux failed: {proc.stderr.strip()[-300:]}")
    return out


def process_facebook_video(main_video: Path, outro_asset: Path,
                           output: Path, workdir: Path) -> Path:
    """Normalize the FB video + fixed outro to render settings and concat them.

    Same conventions as bot.render_video: pad/scale to VIDEO_WIDTHxHEIGHT,
    fps normalize, concat FILTER (not demuxer) for perfect A/V sync.
    """
    _check_tool("ffmpeg")
    if not outro_asset.exists():
        raise RuntimeError(
            f"Outro asset missing: {outro_asset}\n\n"
            "Add the fixed outro file at assets/outro.mp4 (or set the "
            "OUTRO_ASSET_PATH repo variable to its path) and re-run."
        )
    width, height, fps, preset = get_render_settings()
    workdir.mkdir(parents=True, exist_ok=True)

    main_with_audio = _ensure_audio(main_video, workdir)
    main_norm = workdir / "fb_main_normalized.mp4"
    outro_norm = workdir / "fb_outro_normalized.mp4"
    vf = (
        "setpts=PTS-STARTPTS,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}"
    )
    for src, dest in ((main_with_audio, main_norm), (outro_asset, outro_norm)):
        proc = _run([
            "ffmpeg", "-y",
            "-i", str(src),
            "-vf", vf,
            "-af", "asetpts=PTS-STARTPTS",
            "-c:v", "libx264", "-preset", preset, "-threads", "1",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
            str(dest),
        ])
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg normalize failed for {src.name}: {proc.stderr.strip()[-300:]}")

    output.parent.mkdir(parents=True, exist_ok=True)
    proc = _run([
        "ffmpeg", "-y",
        "-i", str(main_norm),
        "-i", str(outro_norm),
        "-filter_complex", "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]",
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", preset, "-threads", "1",
        "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
        str(output),
    ])
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg concat failed: {proc.stderr.strip()[-300:]}")
    print(f"Final Facebook video saved: {output}", flush=True)
    return output


def build_facebook_metadata(video_title: str, fb_url: str) -> dict:
    """Build YouTube metadata reusing the project's SEO conventions.

    Uses bot.generate_seo_metadata for description/tags structure, with the
    Facebook video title as the upload title (music-specific suffixes off).
    """
    clean = (video_title or "Facebook Video").strip()[:100]
    meta = generate_seo_metadata(clean, None, fb_url)
    meta["title"] = clean
    # Keep project tag style, drop music-only tags for a video re-upload.
    music_only = {"official audio", "new song", "music video", "full song"}
    meta["tags"] = [t for t in meta.get("tags", []) if t.lower() not in music_only]
    meta["tags"] += ["facebook video", "reupload"]
    return meta


def resolve_outro_asset() -> Path:
    """Resolve OUTRO_ASSET_PATH (repo-relative) to an absolute path."""
    import os
    rel = os.environ.get("OUTRO_ASSET_PATH", OUTRO_ASSET_DEFAULT).strip() or OUTRO_ASSET_DEFAULT
    p = Path(rel)
    if not p.is_absolute():
        p = Path(__file__).parent.resolve() / p
    return p
