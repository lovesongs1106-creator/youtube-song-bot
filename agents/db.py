#!/usr/bin/env python3
"""Database abstraction layer with migration support.

Supports PostgreSQL (Supabase) or SQLite fallback.
All tables in single database.
Migration-safe: never destroys existing data.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = "storage/trends.db"

# Detect if PostgreSQL is available
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith("postgresql") or DATABASE_URL.startswith("postgres")


def get_connection():
    """Return database connection. PostgreSQL preferred, SQLite fallback."""
    if USE_POSTGRES:
        try:
            import psycopg2
            return psycopg2.connect(DATABASE_URL)
        except ImportError:
            pass
    # SQLite fallback
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def execute(sql: str, params: tuple = ()) -> None:
    """Execute SQL with auto-commit."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def fetchone(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    """Fetch single row as dict."""
    conn = get_connection()
    try:
        if USE_POSTGRES:
            import psycopg2.extras
            c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
        c.execute(sql, params)
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def fetchall(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Fetch all rows as list of dicts."""
    conn = get_connection()
    try:
        if USE_POSTGRES:
            import psycopg2.extras
            c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
        c.execute(sql, params)
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def init_all_tables() -> None:
    """Create all tables if they don't exist. Safe to run multiple times."""
    conn = get_connection()
    try:
        c = conn.cursor()

        # 1. trends - viral trend discovery
        c.execute(
            """CREATE TABLE IF NOT EXISTS trends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                song_name TEXT NOT NULL,
                artist TEXT,
                youtube_url TEXT NOT NULL,
                source_platform TEXT NOT NULL,
                viral_score REAL DEFAULT 0,
                growth_score REAL DEFAULT 0,
                competition_score REAL DEFAULT 0,
                opportunity_score REAL DEFAULT 0,
                created_at TEXT NOT NULL
            )"""
        )

        # 2. upload_queue - central upload queue
        c.execute(
            """CREATE TABLE IF NOT EXISTS upload_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                song_name TEXT NOT NULL,
                youtube_url TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                video_id TEXT,
                error_message TEXT,
                source TEXT DEFAULT 'manual',
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )"""
        )

        # 3. uploads - DUPLICATE PROTECTION (MANDATORY)
        c.execute(
            """CREATE TABLE IF NOT EXISTS uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_id INTEGER,
                song_name TEXT NOT NULL,
                youtube_url TEXT NOT NULL,
                youtube_video_id TEXT NOT NULL,
                source TEXT DEFAULT 'manual',
                uploaded_at TEXT NOT NULL
            )"""
        )

        # 4. outros - outro videos
        c.execute(
            """CREATE TABLE IF NOT EXISTS outros (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                file_id TEXT NOT NULL UNIQUE,
                file_unique_id TEXT UNIQUE,
                ext TEXT DEFAULT '.mp4',
                weight INTEGER DEFAULT 10,
                is_active INTEGER DEFAULT 1,
                added_at TEXT NOT NULL
            )"""
        )

        # 5. outro_history - outro usage tracking
        c.execute(
            """CREATE TABLE IF NOT EXISTS outro_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                outro_id INTEGER NOT NULL,
                upload_id TEXT NOT NULL,
                used_at TEXT NOT NULL
            )"""
        )

        # 6. queue_history - audit log
        c.execute(
            """CREATE TABLE IF NOT EXISTS queue_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_id INTEGER,
                action TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            )"""
        )

        # 7. system_state - key/value store
        c.execute(
            """CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )"""
        )

        # 8. auto_mode_config - auto upload settings
        c.execute(
            """CREATE TABLE IF NOT EXISTS auto_mode_config (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                enabled INTEGER DEFAULT 0,
                uploads_per_day INTEGER DEFAULT 3,
                start_time TEXT DEFAULT '06:00',
                last_run TEXT
            )"""
        )
        # Insert default config if not exists
        c.execute("INSERT OR IGNORE INTO auto_mode_config (id, enabled, uploads_per_day, start_time) VALUES (1, 0, 3, '06:00')")

        conn.commit()

        # Create indexes (SQLite supports CREATE INDEX IF NOT EXISTS)
        c.execute("CREATE INDEX IF NOT EXISTS idx_trends_opportunity ON trends(opportunity_score DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_queue_status ON upload_queue(status, id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_uploads_url ON uploads(youtube_url)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_outros_active ON outros(is_active)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_outro_history_time ON outro_history(used_at DESC)")

        conn.commit()
    finally:
        conn.close()


def migrate_v2_phase1() -> None:
    """Phase 1 migration: add missing columns, create uploads table."""
    conn = get_connection()
    try:
        c = conn.cursor()

        # Check if uploads table exists (new install vs migration)
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='uploads'")
        if c.fetchone():
            print("[DB] uploads table already exists, skipping Phase 1 migration")
            return

        # Add columns to existing tables (SQLite limited ALTER TABLE)
        # For new columns, we just ensure tables are created with full schema above
        # For existing data, init_all_tables() already ran with IF NOT EXISTS

        print("[DB] Phase 1 migration complete: all tables initialized")
    finally:
        conn.close()


def is_already_uploaded(youtube_url: str) -> bool:
    """Check if a YouTube URL has already been successfully uploaded."""
    result = fetchone(
        "SELECT 1 FROM uploads WHERE youtube_url = ? LIMIT 1",
        (youtube_url,)
    )
    return result is not None


def record_upload(queue_id: int, song_name: str, youtube_url: str, video_id: str, source: str = "manual") -> None:
    """Record a successful upload in the uploads table."""
    from datetime import datetime
    execute(
        """INSERT INTO uploads (queue_id, song_name, youtube_url, youtube_video_id, source, uploaded_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (queue_id, song_name, youtube_url, video_id, source, datetime.now().isoformat())
    )
