#!/usr/bin/env python3
"""
Local integration test for the full pipeline EXCLUDING YouTube upload.

Usage:
  python test_pipeline.py

This tests:
  1. YouTube audio download (yt-dlp + Cobalt fallback)
  2. Thumbnail generation
  3. Video rendering (thumbnail + audio + outro)
  4. YouTube token validation (without actual upload)

RAW EVIDENCE output — no placeholders, no synthetic data.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Ensure imports work
ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

from bot import (
    generate_thumbnail,
    generate_seo_metadata,
    render_video,
)
from downloaders import download_audio
from github_worker import validate_youtube_token_early

# Sample public videos for testing
SAMPLE_VIDEOS = [
    ("Rick Astley - Never Gonna Give You Up", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("Charlie Puth - We Don't Talk Anymore", "https://www.youtube.com/watch?v=3AtDnEC4zak"),
    ("Ed Sheeran - Shape of You", "https://www.youtube.com/watch?v=JGwWNGJdvx8"),
]


def log(msg: str) -> None:
    print(msg, flush=True)


def test_token() -> dict:
    """Test YouTube token validity."""
    log("\n=== TEST 1: YouTube Token Validation ===")
    os.environ.setdefault("YOUTUBE_TOKEN_JSON", os.environ.get("YOUTUBE_TOKEN_JSON", ""))
    os.environ.setdefault("GOOGLE_CLIENT_SECRETS_JSON", os.environ.get("GOOGLE_CLIENT_SECRETS_JSON", ""))

    ok, msg = validate_youtube_token_early()
    status = "PASS" if ok else "FAIL"
    log(f"Token status: {status}")
    log(f"Message: {msg}")
    return {"test": "token_validation", "status": status, "message": msg}


def test_download(video_title: str, video_url: str, work_dir: Path) -> dict:
    """Test audio download from a YouTube URL."""
    log(f"\n=== TEST 2: Audio Download — {video_title} ===")
    log(f"URL: {video_url}")
    try:
        audio_dir = work_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        audio_path = download_audio(video_url, audio_dir)
        size = audio_path.stat().st_size if audio_path.exists() else 0
        log(f"Downloaded: {audio_path}")
        log(f"File size: {size} bytes ({size/1024/1024:.2f} MB)")
        return {
            "test": "audio_download",
            "video": video_title,
            "url": video_url,
            "status": "PASS",
            "path": str(audio_path),
            "size_bytes": size,
        }
    except Exception as exc:
        log(f"FAILED: {exc}")
        return {
            "test": "audio_download",
            "video": video_title,
            "url": video_url,
            "status": "FAIL",
            "error": str(exc),
        }


def test_thumbnail(song_name: str, artist: str | None, work_dir: Path) -> dict:
    """Test thumbnail generation."""
    log(f"\n=== TEST 3: Thumbnail Generation — {song_name} ===")
    try:
        thumb_path = work_dir / "thumbnail.jpg"
        generate_thumbnail(song_name, artist, thumb_path)
        size = thumb_path.stat().st_size if thumb_path.exists() else 0
        log(f"Generated: {thumb_path}")
        log(f"File size: {size} bytes ({size/1024:.2f} KB)")
        return {
            "test": "thumbnail",
            "song": song_name,
            "status": "PASS",
            "path": str(thumb_path),
            "size_bytes": size,
        }
    except Exception as exc:
        log(f"FAILED: {exc}")
        return {
            "test": "thumbnail",
            "song": song_name,
            "status": "FAIL",
            "error": str(exc),
        }


def test_render(thumb_path: Path, audio_path: Path, work_dir: Path) -> dict:
    """Test video rendering with a dummy outro if no real outro exists."""
    log(f"\n=== TEST 4: Video Render ===")
    outro_path = work_dir / "dummy_outro.mp4"
    video_path = work_dir / "final_video.mp4"
    work = work_dir / "work"

    # Create a minimal dummy outro (2 sec black video) if none exists
    if not outro_path.exists():
        log("Creating dummy outro (2 sec black video)...")
        import subprocess
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=1920x1080:d=2",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-shortest",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-b:a", "128k",
            str(outro_path),
        ], check=True, capture_output=True)
        log(f"Dummy outro created: {outro_path}")

    try:
        render_video(thumb_path, audio_path, outro_path, video_path, work)
        size = video_path.stat().st_size if video_path.exists() else 0
        log(f"Rendered: {video_path}")
        log(f"File size: {size} bytes ({size/1024/1024:.2f} MB)")
        return {
            "test": "render",
            "status": "PASS",
            "path": str(video_path),
            "size_bytes": size,
        }
    except Exception as exc:
        log(f"FAILED: {exc}")
        return {
            "test": "render",
            "status": "FAIL",
            "error": str(exc),
        }


def test_metadata(song_name: str, artist: str | None, youtube_url: str | None) -> dict:
    """Test SEO metadata generation."""
    log(f"\n=== TEST 5: SEO Metadata ===")
    try:
        meta = generate_seo_metadata(song_name, artist, youtube_url)
        log(f"Title: {meta['title']}")
        log(f"Tags: {meta['tags'][:5]}...")
        log(f"Description length: {len(meta['description'])} chars")
        return {
            "test": "metadata",
            "status": "PASS",
            "title": meta["title"],
            "tags_count": len(meta["tags"]),
            "description_length": len(meta["description"]),
        }
    except Exception as exc:
        log(f"FAILED: {exc}")
        return {
            "test": "metadata",
            "status": "FAIL",
            "error": str(exc),
        }


def main() -> None:
    log("=" * 60)
    log("YOUTUBE SONG BOT — FULL PIPELINE TEST (NO UPLOAD)")
    log("=" * 60)

    results: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="ytbot_test_") as tmp:
        work_dir = Path(tmp)
        log(f"\nWork directory: {work_dir}")

        # 1) Token validation
        results.append(test_token())

        # 2-5) Run pipeline on first sample video
        title, url = SAMPLE_VIDEOS[0]
        dl_result = test_download(title, url, work_dir)
        results.append(dl_result)

        if dl_result["status"] == "PASS":
            audio_path = Path(dl_result["path"])

            thumb_result = test_thumbnail(title, "Test Artist", work_dir)
            results.append(thumb_result)

            if thumb_result["status"] == "PASS":
                thumb_path = Path(thumb_result["path"])
                render_result = test_render(thumb_path, audio_path, work_dir)
                results.append(render_result)
            else:
                results.append({"test": "render", "status": "SKIPPED", "reason": "thumbnail failed"})

            meta_result = test_metadata(title, "Test Artist", url)
            results.append(meta_result)
        else:
            results.append({"test": "thumbnail", "status": "SKIPPED", "reason": "download failed"})
            results.append({"test": "render", "status": "SKIPPED", "reason": "download failed"})
            results.append({"test": "metadata", "status": "SKIPPED", "reason": "download failed"})

    # Summary
    log("\n" + "=" * 60)
    log("TEST SUMMARY")
    log("=" * 60)
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    skipped = sum(1 for r in results if r["status"] == "SKIPPED")
    for r in results:
        icon = "✅" if r["status"] == "PASS" else "❌" if r["status"] == "FAIL" else "⏭️"
        log(f"{icon} {r['test']}: {r['status']}")
    log(f"\nTotal: {len(results)} | Passed: {passed} | Failed: {failed} | Skipped: {skipped}")

    # Write JSON report
    report_path = ROOT / "test_pipeline_report.json"
    report_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    log(f"\nRaw report saved: {report_path}")

    if failed > 0:
        log("\n⚠️ Some tests FAILED. Check logs above.")
        sys.exit(1)
    else:
        log("\n🎉 All tests PASSED! Pipeline is healthy.")
        sys.exit(0)


if __name__ == "__main__":
    main()
