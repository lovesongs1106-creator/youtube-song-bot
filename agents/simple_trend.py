#!/usr/bin/env python3
"""
Simple Trend Discovery Agent (Minimal Version)
"""

import sqlite3
from datetime import datetime

DB_PATH = "storage/trends.db"


def init_db():
    """Create trends table if it doesn't exist"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS trends
                 (id INTEGER PRIMARY KEY, source TEXT, collected_at TEXT,
                  song_name TEXT, artist TEXT, video_url TEXT,
                  views INTEGER, upload_age TEXT, days_since_upload INTEGER,
                  views_per_day REAL, freshness_score REAL,
                  trend_score REAL, competition_score REAL,
                  opportunity_score REAL, viral_potential_score REAL)''')
    conn.commit()
    conn.close()


def get_top_opportunities(limit=5):
    """Get top trending songs based on opportunity score"""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT song_name, artist, opportunity_score 
                 FROM trends 
                 ORDER BY opportunity_score DESC 
                 LIMIT ?""", (limit,))
    results = c.fetchall()
    conn.close()
    return results


def generate_daily_report():
    """Generate simple Telegram-friendly daily report"""
    opps = get_top_opportunities(5)
    
    if not opps:
        return "📈 Aaj koi trending songs nahi mile. Thoda wait karo."
    
    report = "📈 Today’s Best Upload Opportunities\n\n"
    
    for i, (song, artist, score) in enumerate(opps, 1):
        report += f"{i}. {song}\n"
        report += f"   {artist}\n"
        report += f"   Opportunity: {score}\n\n"
    
    report += "Buttons:\n[Approve Song] [Refresh]"
    
    return report


if __name__ == "__main__":
    print(generate_daily_report())
