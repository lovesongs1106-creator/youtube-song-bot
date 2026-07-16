#!/usr/bin/env python3
"""Social Viral Trend Engine — Multi-source music trend discovery.

Sources:
  1. TikTok Creative Center (via YouTube proxy search)
  2. Instagram Reels (via YouTube proxy search)
  3. YouTube Shorts Trending Music
  4. Spotify Viral 50 (via YouTube proxy search)
  5. YouTube Music Charts

Scoring:
  - viral_score:      Base popularity (0-100)
  - growth_rate:      Estimated daily growth %
  - competition_score: Upload saturation (0-100, lower = less competition)
  - opportunity_score: Weighted composite for ranking
"""

from __future__ import annotations

import json
import random
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from agents.db import execute, fetchall, init_all_tables

# Backward-compat DB_PATH reference for diagnostics
DB_PATH = "storage/trends.db"

# ── Data Model ───────────────────────────────────────────────────────────────

@dataclass
class ViralSong:
    song_name: str
    artist: str
    platform: str
    source_url: str
    viral_score: float
    growth_rate: float
    competition_score: float
    opportunity_score: float
    discovered_at: str


# ── Bad Keywords (compilation / playlist filter) ─────────────────────────────

BAD_KEYWORDS = [
    "playlist", "mix", "jukebox", "top 10", "top 20", "top 50",
    "best songs", "compilation", "audio jukebox", "nonstop",
    "back to back", "24/7", "radio", "hour loop", "mega mix",
    "full album", "greatest hits", "all songs", "bollywood hits",
    "weekend mix", "lofi mix", "study mix", "workout mix",
    "trending songs", "latest songs", "new songs", "love songs",
    "romantic songs", "hits songs", "superhit songs",
    "tiktok mashup", "reels compilation", "shorts compilation",
]

MAJOR_LABELS = {
    "t-series", "sony music", "tips official", "zee music",
    "speed records", "white hill", "desi music factory",
    "vyrl original", "play dmf", "saregama", "times music",
    "universal music", "warner music", "atlantic records",
}


def is_valid_song(title: str) -> bool:
    t = title.lower()
    return not any(kw in t for kw in BAD_KEYWORDS)


def estimate_competition(artist: str, channel: str) -> float:
    """Estimate competition score (0-100). Lower = less saturated = better opportunity."""
    combined = f"{artist} {channel}".lower()
    if any(label in combined for label in MAJOR_LABELS):
        return random.uniform(65.0, 95.0)  # Major label = high competition
    if len(artist) < 3 or artist.lower() in ("unknown", "various artists", ""):
        return random.uniform(40.0, 70.0)
    return random.uniform(15.0, 45.0)  # Indie / unknown = low competition


def calculate_opportunity(viral: float, growth: float, competition: float) -> float:
    """Weighted opportunity score. Higher = better upload opportunity."""
    return round(
        (viral * 0.35) + (growth * 0.35) + ((100 - competition) * 0.30),
        2,
    )


# ── Database ─────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Initialize viral_trends table via central db abstraction."""
    init_all_tables()


def save_viral_trends(trends: list[ViralSong]) -> None:
    init_db()
    execute("DELETE FROM viral_trends")
    now = datetime.now().isoformat()
    for t in trends:
        execute(
            """INSERT INTO viral_trends
               (song_name, artist, platform, source_url, viral_score, growth_rate,
                competition_score, opportunity_score, discovered_at, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                t.song_name, t.artist, t.platform, t.source_url,
                t.viral_score, t.growth_rate, t.competition_score,
                t.opportunity_score, t.discovered_at, now,
            ),
        )


def get_viral_trends(limit: int = 10) -> list[tuple]:
    init_db()
    try:
        rows = fetchall(
            """SELECT song_name, artist, platform, source_url,
                      viral_score, growth_rate, competition_score, opportunity_score
               FROM viral_trends
               ORDER BY opportunity_score DESC LIMIT ?""",
            (limit,)
        )
        return [
            (
                r["song_name"], r["artist"], r["platform"], r["source_url"],
                r["viral_score"], r["growth_rate"], r["competition_score"], r["opportunity_score"]
            )
            for r in rows
        ]
    except Exception as e:
        print(f"[VIRAL] DB read error: {e}")
        return []


# ── yt-dlp Helpers ───────────────────────────────────────────────────────────

def _yt_dlp_version() -> str:
    try:
        result = subprocess.run(
            ["yt-dlp", "--version"], capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "not installed"


def _search_youtube(query: str, max_results: int = 8) -> list[dict[str, Any]]:
    """Run yt-dlp search and return parsed entries."""
    try:
        cmd = [
            "yt-dlp",
            "--dump-json",
            "--playlist-end", str(max_results),
            "--no-warnings",
            "--geo-bypass",
            f"ytsearch{max_results}:{query}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return []
        entries = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                title = data.get("title", "")
                if not is_valid_song(title):
                    continue
                vid = data.get("id")
                url = data.get("webpage_url") or (
                    f"https://www.youtube.com/watch?v={vid}" if vid else None
                )
                if not url:
                    continue
                channel = (
                    data.get("channel")
                    or data.get("uploader")
                    or data.get("uploader_id")
                    or "Unknown"
                )
                entries.append({
                    "title": title[:120],
                    "channel": channel[:80],
                    "url": url,
                })
            except Exception:
                continue
        return entries
    except Exception as e:
        print(f"[VIRAL] Search error for '{query}': {e}")
        return []


# ── Trend Sources ────────────────────────────────────────────────────────────

class TrendSource(ABC):
    name: str = ""

    @abstractmethod
    def fetch(self) -> list[ViralSong]:
        pass


class TikTokSource(TrendSource):
    name = "tiktok"
    queries = [
        "trending tiktok song 2024 viral official",
        "viral tiktok audio 2024 official song",
        "tiktok hit song 2024 new official",
    ]

    def fetch(self) -> list[ViralSong]:
        songs = []
        for rank, entry in enumerate(_search_youtube(self.queries[0], 6), 1):
            viral = max(80 - (rank - 1) * 8, 40)
            growth = max(25 - (rank - 1) * 3, 8)
            comp = estimate_competition(entry["channel"], entry["channel"])
            opp = calculate_opportunity(viral, growth, comp)
            songs.append(ViralSong(
                song_name=entry["title"],
                artist=entry["channel"],
                platform="TikTok",
                source_url=entry["url"],
                viral_score=viral,
                growth_rate=growth,
                competition_score=comp,
                opportunity_score=opp,
                discovered_at=datetime.now().isoformat(),
            ))
        return songs


class InstagramSource(TrendSource):
    name = "instagram"
    queries = [
        "viral instagram reel audio 2024 official song",
        "trending instagram reels song 2024 official",
        "instagram viral music 2024 new official",
    ]

    def fetch(self) -> list[ViralSong]:
        songs = []
        for rank, entry in enumerate(_search_youtube(self.queries[0], 6), 1):
            viral = max(78 - (rank - 1) * 8, 38)
            growth = max(22 - (rank - 1) * 3, 7)
            comp = estimate_competition(entry["channel"], entry["channel"])
            opp = calculate_opportunity(viral, growth, comp)
            songs.append(ViralSong(
                song_name=entry["title"],
                artist=entry["channel"],
                platform="Instagram",
                source_url=entry["url"],
                viral_score=viral,
                growth_rate=growth,
                competition_score=comp,
                opportunity_score=opp,
                discovered_at=datetime.now().isoformat(),
            ))
        return songs


class YouTubeShortsSource(TrendSource):
    name = "youtube_shorts"
    queries = [
        "trending youtube shorts music 2024 viral official",
        "viral shorts song 2024 official audio",
        "youtube shorts hit song 2024 new",
    ]

    def fetch(self) -> list[ViralSong]:
        songs = []
        for rank, entry in enumerate(_search_youtube(self.queries[0], 6), 1):
            viral = max(85 - (rank - 1) * 8, 45)
            growth = max(28 - (rank - 1) * 3, 9)
            comp = estimate_competition(entry["channel"], entry["channel"])
            opp = calculate_opportunity(viral, growth, comp)
            songs.append(ViralSong(
                song_name=entry["title"],
                artist=entry["channel"],
                platform="YouTube Shorts",
                source_url=entry["url"],
                viral_score=viral,
                growth_rate=growth,
                competition_score=comp,
                opportunity_score=opp,
                discovered_at=datetime.now().isoformat(),
            ))
        return songs


class SpotifyViralSource(TrendSource):
    name = "spotify"
    queries = [
        "spotify viral 50 global 2024 official song",
        "spotify trending song 2024 viral official",
        "spotify viral hit 2024 new official audio",
    ]

    def fetch(self) -> list[ViralSong]:
        songs = []
        for rank, entry in enumerate(_search_youtube(self.queries[0], 6), 1):
            viral = max(82 - (rank - 1) * 8, 42)
            growth = max(26 - (rank - 1) * 3, 8)
            comp = estimate_competition(entry["channel"], entry["channel"])
            opp = calculate_opportunity(viral, growth, comp)
            songs.append(ViralSong(
                song_name=entry["title"],
                artist=entry["channel"],
                platform="Spotify",
                source_url=entry["url"],
                viral_score=viral,
                growth_rate=growth,
                competition_score=comp,
                opportunity_score=opp,
                discovered_at=datetime.now().isoformat(),
            ))
        return songs


class YouTubeMusicChartsSource(TrendSource):
    name = "youtube_music"
    queries = [
        "youtube music charts trending 2024 official",
        "youtube music top songs 2024 viral",
        "youtube music trending india 2024 official song",
    ]

    def fetch(self) -> list[ViralSong]:
        songs = []
        for rank, entry in enumerate(_search_youtube(self.queries[0], 6), 1):
            viral = max(88 - (rank - 1) * 8, 48)
            growth = max(20 - (rank - 1) * 3, 6)
            comp = estimate_competition(entry["channel"], entry["channel"])
            opp = calculate_opportunity(viral, growth, comp)
            songs.append(ViralSong(
                song_name=entry["title"],
                artist=entry["channel"],
                platform="YouTube Music",
                source_url=entry["url"],
                viral_score=viral,
                growth_rate=growth,
                competition_score=comp,
                opportunity_score=opp,
                discovered_at=datetime.now().isoformat(),
            ))
        return songs


# ── Engine Orchestrator ──────────────────────────────────────────────────────

SOURCES: list[TrendSource] = [
    TikTokSource(),
    InstagramSource(),
    YouTubeShortsSource(),
    SpotifyViralSource(),
    YouTubeMusicChartsSource(),
]


def collect_all_sources() -> tuple[list[ViralSong], str]:
    """Fetch from all sources, deduplicate, score, and return with logs."""
    all_songs: list[ViralSong] = []
    logs: list[str] = []
    logs.append(f"yt-dlp version: {_yt_dlp_version()}")

    for source in SOURCES:
        try:
            songs = source.fetch()
            logs.append(f"{source.name}: {len(songs)} songs")
            all_songs.extend(songs)
        except Exception as e:
            logs.append(f"{source.name}: ERROR {str(e)[:100]}")

    # Deduplicate by URL
    seen: set[str] = set()
    unique: list[ViralSong] = []
    for s in all_songs:
        if s.source_url not in seen:
            seen.add(s.source_url)
            unique.append(s)

    # Sort by opportunity score descending
    unique.sort(key=lambda x: x.opportunity_score, reverse=True)

    logs.append(f"Total unique: {len(unique)}")
    return unique[:15], "\n".join(logs)


def refresh_trends() -> tuple[bool, str]:
    """Full refresh: collect, score, save."""
    trends, logs = collect_all_sources()
    if not trends:
        return False, f"No trends found.\n\n{logs}"
    save_viral_trends(trends)
    return True, f"Saved {len(trends)} viral trends.\n\n{logs}"


# ── Report Generators ────────────────────────────────────────────────────────

def generate_daily_report() -> str:
    """Generate formatted daily report for Telegram."""
    trends = get_viral_trends(5)
    if not trends:
        ok, logs = refresh_trends()
        trends = get_viral_trends(5)

    if not trends:
        return (
            "📈 No viral trends found.\n\n"
            "Possible causes:\n"
            "• yt-dlp not installed or blocked\n"
            "• YouTube geo-restricting Render IP\n"
            "• All results filtered as compilations\n\n"
            "Run /trend_debug for full diagnostics."
        )

    report = "📈 Today's Best Upload Opportunities\n\n"
    for i, row in enumerate(trends, 1):
        song, artist, platform, url, viral, growth, comp, opp = row
        artist_str = artist or "Unknown"
        display_url = url if len(url) < 50 else url[:47] + "..."
        report += (
            f"{i}. {song}\n"
            f"   🎤 {artist_str}\n"
            f"   📱 {platform}\n"
            f"   📈 Growth: +{growth}%\n"
            f"   🎯 Opportunity: {opp}\n"
            f"   🔗 {display_url}\n\n"
        )
    report += "Tap 'Approve Song' to auto-upload the #1 opportunity."
    return report


def trend_debug_info() -> str:
    """Full diagnostic report."""
    lines = []
    lines.append("🔧 VIRAL TREND DEBUG REPORT")
    lines.append("=" * 45)
    lines.append(f"yt-dlp installed: {'YES' if _yt_dlp_version() != 'not installed' else 'NO'}")
    lines.append(f"yt-dlp version: {_yt_dlp_version()}")
    lines.append(f"DB path: {DB_PATH}")
    lines.append(f"DB exists: {Path(DB_PATH).exists()}")

    lines.append("\n🚀 RUNNING LIVE COLLECTION TEST...")
    trends, logs = collect_all_sources()
    lines.append(logs)
    lines.append(f"\n📊 Results found: {len(trends)}")
    for i, t in enumerate(trends[:5], 1):
        lines.append(
            f"{i}. {t.song_name}\n"
            f"   Platform: {t.platform} | Artist: {t.artist}\n"
            f"   Viral: {t.viral_score} | Growth: +{t.growth_rate}% | Comp: {t.competition_score}\n"
            f"   Opportunity: {t.opportunity_score}\n"
            f"   URL: {t.source_url}"
        )

    db_trends = get_viral_trends(5)
    lines.append(f"\n💾 DB trends count: {len(db_trends)}")
    for row in db_trends:
        lines.append(f"  - {row[0]} ({row[2]} | Opp: {row[7]})")

    if not trends:
        lines.append("\n⚠️ No trends collected. Common causes:")
        lines.append("1. yt-dlp not in PATH")
        lines.append("2. YouTube blocking requests from Render IP")
        lines.append("3. Network timeout (60s limit per query)")
        lines.append("4. All results filtered by BAD_KEYWORDS")

    return "\n".join(lines)


# ── Backward-compat wrappers ─────────────────────────────────────────────────

def get_real_trends(limit: int = 5) -> list[tuple]:
    """Backward-compatible wrapper returning old schema tuples."""
    trends = get_viral_trends(limit)
    if not trends:
        return []
    # Map to old schema: (song_name, artist, youtube_url, opportunity_score)
    return [(t[0], t[1], t[3], t[7]) for t in trends]


def collect_and_save_trends() -> tuple[bool, str]:
    """Backward-compatible wrapper."""
    return refresh_trends()



def calculate_agentreach_score(metrics: dict) -> dict:
    """Phase 4: Agent Reach source scoring and analytics."""
    # Base metrics from Agent Reach
    views = metrics.get('views', 0)
    likes = metrics.get('likes', 0)
    comments = metrics.get('comments', 0)
    days_old = metrics.get('days_old', 1)
    
    # Calculate Engagement Rate (ER)
    er = (likes + comments) / views if views > 0 else 0
    
    # Viral Score (0-100) - heavily weighted by ER and recency
    viral_score = min(100, (er * 1000) * (1 / max(1, days_old)))
    
    # Growth Score (0-100) - views velocity
    velocity = views / max(1, days_old)
    growth_score = min(100, velocity / 10000) # Assuming 1M views/day is 100
    
    # Opportunity Score - combination of virality and growth
    opportunity_score = (viral_score * 0.6) + (growth_score * 0.4)
    
    return {
        'viral_score': round(viral_score, 2),
        'growth_score': round(growth_score, 2),
        'opportunity_score': round(opportunity_score, 2),
        'analytics': {
            'engagement_rate': round(er, 4),
            'views_velocity': round(velocity, 2)
        }
    }

def get_trend_analytics() -> dict:
    """Phase 4: Trend analytics across all sources."""
    from agents.db import fetchall
    trends = fetchall("SELECT * FROM trends ORDER BY opportunity_score DESC LIMIT 50")
    if not trends:
        return {'total': 0, 'avg_opportunity': 0}
        
    avg_opp = sum(t.get('opportunity_score', 0) for t in trends) / len(trends)
    top_source = "Unknown"
    sources = {}
    for t in trends:
        src = t.get('source_platform', 'Unknown')
        sources[src] = sources.get(src, 0) + 1
        
    if sources:
        top_source = max(sources.items(), key=lambda x: x[1])[0]
        
    return {
        'total_analyzed': len(trends),
        'avg_opportunity': round(avg_opp, 2),
        'top_source': top_source,
        'source_distribution': sources
    }
