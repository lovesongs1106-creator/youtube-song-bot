#!/usr/bin/env python3
"""Simple Trend Discovery Agent"""

import sqlite3

DB_PATH = "storage/trends.db"

def get_top_opportunities(limit=5):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT song_name, artist FROM trends 
                 ORDER BY opportunity_score DESC LIMIT ?""", (limit,))
    results = c.fetchall()
    conn.close()
    return results

def generate_daily_report():
    opps = get_top_opportunities(5)
    report = "📈 Today’s Best Upload Opportunities\n\n"
    for i, (song, artist) in enumerate(opps, 1):
        report += f"{i}. {song}\n   {artist}\n\n"
    report += "Buttons: [Approve Song] [Refresh]"
    return report
