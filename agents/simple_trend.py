#!/usr/bin/env python3
"""Real Trend Discovery Agent with full logging and diagnostics."""

import sqlite3
import subprocess
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

DB_PATH = "storage/trends.db"

BAD_KEYWORDS = [
    "playlist", "mix", "jukebox", "top 10", "top 20", "top 50",
    "best songs", "compilation", "audio jukebox", "nonstop",
    "back to back", "24/7", "radio", "hour loop", "mega mix",
    "full album", "greatest hits", "all songs", "bollywood hits",
    "weekend mix", "lofi mix", "study mix", "workout mix",
]


def is_valid_song(title):
    title_lower = title.lower()
    for kw in BAD_KEYWORDS:
        if kw in title_lower:
            return False
    return True


def init_db():
    try:
        db_file = Path(DB_PATH)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_file))
        c = conn.cursor()
        c.execute(
            """CREATE TABLE IF NOT EXISTS trends
               (id INTEGER PRIMARY KEY, song_name TEXT, artist TEXT, youtube_url TEXT,
                trend_score REAL, opportunity_score REAL, collected_at TEXT)"""
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[TREND] WARNING: trend DB init failed: {e}")


def get_real_trends(limit=5):
    try:
        db_file = Path(DB_PATH)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_file))
        c = conn.cursor()
        c.execute(
            """SELECT song_name, artist, youtube_url, opportunity_score
               FROM trends
               ORDER BY opportunity_score DESC LIMIT ?""",
            (limit,),
        )
        results = c.fetchall()
        conn.close()
        return results
    except Exception as e:
        print(f"[TREND] WARNING: get_real_trends failed: {e}")
        return []


def generate_daily_report():
    """Generate daily report. Auto-collects if DB is empty."""
    trends = get_real_trends(5)
    if not trends:
        success, logs = collect_and_save_trends()
        trends = get_real_trends(5)

    if not trends:
        return (
            "📈 No real trends found.\n\n"
            "Possible causes:\n"
            "• yt-dlp not installed or blocked\n"
            "• YouTube geo-restricting Render IP\n"
            "• All results filtered as playlists/mixes\n\n"
            "Run /trend_debug for full diagnostics."
        )

    report = "📈 Today's Best Upload Opportunities\n\n"
    for i, (song, artist, url, score) in enumerate(trends, 1):
        artist_str = artist or "Unknown Artist"
        # Truncate long URLs for display
        display_url = url if len(url) < 60 else url[:57] + "..."
        report += f"{i}. {song}\n   🎤 {artist_str}\n   🔗 {display_url}\n\n"
    report += "Tap 'Approve Song' to auto-upload the #1 opportunity."
    return report


def _yt_dlp_version():
    try:
        if shutil.which("yt-dlp"):
            result = subprocess.run(
                ["yt-dlp", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception as e:
        return f"error: {e}"
    return "not installed"


def collect_real_trends():
    """Collect real trending songs using yt-dlp with full diagnostics.

    Returns:
        tuple: (list of trend dicts, diagnostic logs string)
    """
    queries = [
        "new hindi song 2024 official",
        "viral hindi song 2024 official",
        "trending bollywood song official",
        "latest punjabi song 2024 official",
        "new english song 2024 official",
        "viral song 2024 official music video",
    ]
    results = []
    logs = []

    version = _yt_dlp_version()
    logs.append(f"yt-dlp version: {version}")

    if version in ("not installed", "unknown") or version.startswith("error"):
        logs.append("ERROR: yt-dlp not available or failed version check")
        return [], "\n".join(logs)

    for query in queries:
        try:
            cmd = [
                "yt-dlp",
                "--dump-json",
                "--playlist-end",
                "8",
                "--no-warnings",
                "--geo-bypass",
                f"ytsearch8:{query}",
            ]
            logs.append(f"Query: {query}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            logs.append(f"  Return code: {result.returncode}")

            if result.stderr:
                stderr_snippet = result.stderr[:400].replace("\n", " | ")
                logs.append(f"  Stderr: {stderr_snippet}")

            if result.returncode != 0:
                logs.append("  FAILED")
                continue

            lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
            logs.append(f"  Lines returned: {len(lines)}")

            found = 0
            for line in lines:
                try:
                    data = json.loads(line)
                    title = data.get("title", "")
                    if not is_valid_song(title):
                        continue

                    url = data.get("webpage_url") or data.get("url")
                    vid = data.get("id")
                    if not url and vid:
                        url = f"https://www.youtube.com/watch?v={vid}"
                    if not url:
                        continue

                    channel = (
                        data.get("channel")
                        or data.get("uploader")
                        or data.get("uploader_id")
                        or "Unknown"
                    )

                    results.append(
                        {
                            "song_name": title[:120],
                            "artist": channel[:80],
                            "youtube_url": url,
                            "trend_score": 80.0,
                            "opportunity_score": 75.0,
                            "collected_at": datetime.now().isoformat(),
                        }
                    )
                    found += 1
                except Exception as e:
                    logs.append(f"  Parse error: {str(e)[:100]}")
                    continue
            logs.append(f"  Valid results: {found}")
        except Exception as e:
            logs.append(f"  EXCEPTION: {str(e)[:200]}")
            continue

    # Deduplicate by URL
    seen = set()
    unique = []
    for r in results:
        if r["youtube_url"] not in seen:
            seen.add(r["youtube_url"])
            unique.append(r)

    logs.append(f"Total unique: {len(unique)}")
    return unique[:10], "\n".join(logs)


def collect_and_save_trends():
    """Collect trends and atomically replace DB contents."""
    trends, logs = collect_real_trends()
    if not trends:
        return False, f"No trends found.\n\n{logs}"

    try:
        db_file = Path(DB_PATH)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_file))
        c = conn.cursor()
        c.execute("DELETE FROM trends")
        for t in trends:
            c.execute(
                """INSERT INTO trends
                   (song_name, artist, youtube_url, trend_score, opportunity_score, collected_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    t["song_name"],
                    t["artist"],
                    t["youtube_url"],
                    t["trend_score"],
                    t["opportunity_score"],
                    t["collected_at"],
                ),
            )
        conn.commit()
        conn.close()
        return True, f"Saved {len(trends)} trends.\n\n{logs}"
    except Exception as e:
        return False, f"DB save failed: {e}\n\n{logs}"


def trend_debug_info():
    """Return full diagnostic string for /trend_debug command."""
    lines = []
    lines.append("🔧 TREND DEBUG REPORT")
    lines.append("=" * 40)
    lines.append(f"yt-dlp installed: {'YES' if shutil.which('yt-dlp') else 'NO'}")
    lines.append(f"yt-dlp version: {_yt_dlp_version()}")
    lines.append(f"DB path: {DB_PATH}")
    lines.append(f"DB exists: {Path(DB_PATH).exists()}")

    # Try collection
    lines.append("\n🚀 RUNNING LIVE COLLECTION TEST...")
    trends, logs = collect_real_trends()
    lines.append(logs)
    lines.append(f"\n📊 Results found: {len(trends)}")
    for i, t in enumerate(trends[:5], 1):
        lines.append(f"{i}. {t['song_name']}")
        lines.append(f"   Artist: {t['artist']}")
        lines.append(f"   URL: {t['youtube_url']}")

    # DB status
    db_trends = get_real_trends(5)
    lines.append(f"\n💾 DB trends count: {len(db_trends)}")
    for row in db_trends:
        lines.append(f"  - {row[0]} (score: {row[3]})")

    if not trends:
        lines.append("\n⚠️ No trends collected. Common causes:")
        lines.append("1. yt-dlp not in PATH (check Docker image)")
        lines.append("2. YouTube blocking requests from Render IP")
        lines.append("3. Network timeout (60s limit per query)")
        lines.append("4. All results filtered by BAD_KEYWORDS")
        lines.append("5. yt-dlp outdated (update requirements.txt)")

    return "\n".join(lines)
