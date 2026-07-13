#!/usr/bin/env python3
"""Real Trend Discovery Agent"""

import sqlite3
import subprocess
import json
import re
from datetime import datetime

DB_PATH = "storage/trends.db"

BAD_KEYWORDS = ["playlist", "mix", "jukebox", "top 10", "top 20", "best songs", "compilation", "audio jukebox"]

def is_valid_song(title):
    title_lower = title.lower()
    for kw in BAD_KEYWORDS:
        if kw in title_lower:
            return False
    return bool(re.search(r'\b(official|audio|song|music|full)\b', title_lower))

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS trends
                 (id INTEGER PRIMARY KEY, song_name TEXT, artist TEXT, youtube_url TEXT,
                  trend_score REAL, opportunity_score REAL, collected_at TEXT)''')
    conn.commit()
    conn.close()

def get_real_trends(limit=5):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT song_name, artist, youtube_url, opportunity_score 
                 FROM trends 
                 ORDER BY opportunity_score DESC LIMIT ?""", (limit,))
    results = c.fetchall()
    conn.close()
    return results

def generate_daily_report():
    opps = get_real_trends(5)
    if not opps:
        return "📈 No real trends found. Run /trend_debug to refresh."
    report = "📈 Today’s Best Upload Opportunities\n\n"
    for i, (song, artist, url, score) in enumerate(opps, 1):
        report += f"{i}. {song}\n   {artist}\n\n"
    report += "Buttons: [Approve Song] [Refresh]"
    return report

def collect_real_trends():
    """Collect real trending songs using yt-dlp"""
    queries = [
        "new hindi song this month official",
        "viral hindi song last 30 days official"
    ]
    results = []
    for query in queries:
        try:
            cmd = ["yt-dlp", "--dump-json", "--flat-playlist", "--playlist-end", "8", f"ytsearch15:{query}"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if not line.strip(): continue
                    try:
                        data = json.loads(line)
                        title = data.get("title", "")
                        if not is_valid_song(title): continue
                        url = data.get("webpage_url", "")
                        channel = data.get("channel", "")
                        if not url: continue
                        results.append({
                            "song_name": title[:100],
                            "artist": channel[:60],
                            "youtube_url": url,
                            "trend_score": 80,
                            "opportunity_score": 75,
                            "collected_at": datetime.now().isoformat()
                        })
                    except:
                        continue
        except:
            continue
    return results[:8]

def save_trends(trends):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for t in trends:
        c.execute("""INSERT INTO trends 
                     (song_name, artist, youtube_url, trend_score, opportunity_score, collected_at)
                     VALUES (?, ?, ?, ?, ?, ?)""",
                  (t["song_name"], t["artist"], t["youtube_url"], 
                   t["trend_score"], t["opportunity_score"], t["collected_at"]))
    conn.commit()
    conn.close()