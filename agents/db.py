#!/usr/bin/env python3
"""Database abstraction layer with migration support.

Supports PostgreSQL (Supabase/Neon) or SQLite fallback.
All tables in single database.
Migration-safe: never destroys existing data.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = "storage/trends.db"

# Detect if PostgreSQL is available
# DEFAULT_URL is Sydney Pooler for ap-southeast-2
DEFAULT_URL = "postgresql://postgres.vckbjanbeovtqszsmfte:buzZug-mattym-jymby5@aws-0-ap-southeast-2.pooler.supabase.com:6543/postgres"
DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_URL).strip()
_FORCE_SQLITE = False

def is_using_postgres():
    """Check if we are actually using PostgreSQL."""
    return (DATABASE_URL.startswith("postgresql") or DATABASE_URL.startswith("postgres")) and not _FORCE_SQLITE

def _adapt_sql(sql: str) -> str:
    """Translate SQLite SQL to PostgreSQL compatible syntax."""
    if not is_using_postgres():
        return sql
    # Replace ? placeholders with %s for psycopg2
    sql = sql.replace("?", "%s")
    sql = sql.replace("INSERT OR IGNORE", "INSERT")
    return sql

def get_connection():
    """Return database connection. PostgreSQL preferred, SQLite fallback."""
    global _FORCE_SQLITE
    if (DATABASE_URL.startswith("postgresql") or DATABASE_URL.startswith("postgres")) and not _FORCE_SQLITE:
        try:
            import psycopg2
            # Add a 5 second timeout to avoid hanging startup
            return psycopg2.connect(DATABASE_URL, connect_timeout=5)
        except ImportError:
            print("[DB] psycopg2 not installed, falling back to SQLite", flush=True)
            _FORCE_SQLITE = True
        except Exception as e:
            print(f"[DB] PostgreSQL connection failed: {e}, falling back to SQLite", flush=True)
            # If it's a Supabase IPv6 error, give a helpful hint
            if "Network is unreachable" in str(e) or "could not translate host name" in str(e):
                print("[DB] HINT: This looks like a Supabase IPv6-only issue on an IPv4-only host (like Render).", flush=True)
                print("[DB] HINT: Use the Supabase IPv4 Pooler URL instead.", flush=True)
            _FORCE_SQLITE = True
            
    # SQLite fallback
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)

def execute(sql: str, params: tuple = ()) -> None:
    """Execute SQL with auto-commit."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(_adapt_sql(sql), params)
        conn.commit()
    finally:
        conn.close()

def fetchone(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    """Fetch single row as dict."""
    conn = get_connection()
    try:
        if is_using_postgres():
            import psycopg2.extras
            c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
        c.execute(_adapt_sql(sql), params)
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def fetchall(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Fetch all rows as list of dicts."""
    conn = get_connection()
    try:
        if is_using_postgres():
            import psycopg2.extras
            c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
        c.execute(_adapt_sql(sql), params)
        return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()

def insert_and_get_id(sql: str, params: tuple = ()) -> int | None:
    """Execute INSERT and return generated primary key.
    
    Works for both SQLite (lastrowid) and PostgreSQL (RETURNING id).
    """
    conn = get_connection()
    try:
        c = conn.cursor()
        if is_using_postgres():
            # Append RETURNING id if not already present
            if "RETURNING" not in sql.upper():
                sql = sql.strip().rstrip(";") + " RETURNING id"
            c.execute(_adapt_sql(sql), params)
            row = c.fetchone()
            conn.commit()
            return row[0] if row else None
        else:
            c.execute(sql, params)
            conn.commit()
            return c.lastrowid
    finally:
        conn.close()

def get_changes(cursor) -> int:
    """Get number of rows affected by last operation.
    
    For SQLite, executes SELECT changes().
    For PostgreSQL, uses cursor.rowcount.
    """
    if is_using_postgres():
        return cursor.rowcount
    cursor.execute("SELECT changes() as cnt")
    row = cursor.fetchone()
    return row[0] if row else 0


def _pg_ddl(table_name: str) -> str:
    """Return PostgreSQL-compatible CREATE TABLE DDL."""
    ddls = {
        "trends": """
            CREATE TABLE IF NOT EXISTS trends (
                id SERIAL PRIMARY KEY,
                song_name TEXT NOT NULL,
                artist TEXT,
                youtube_url TEXT NOT NULL,
                source_platform TEXT NOT NULL,
                viral_score REAL DEFAULT 0,
                growth_score REAL DEFAULT 0,
                competition_score REAL DEFAULT 0,
                opportunity_score REAL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """,
        "upload_queue": """
            CREATE TABLE IF NOT EXISTS upload_queue (
                id SERIAL PRIMARY KEY,
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
            )
        """,
        "uploads": """
            CREATE TABLE IF NOT EXISTS uploads (
                id SERIAL PRIMARY KEY,
                queue_id INTEGER,
                song_name TEXT NOT NULL,
                youtube_url TEXT NOT NULL,
                youtube_video_id TEXT NOT NULL,
                source TEXT DEFAULT 'manual',
                uploaded_at TEXT NOT NULL
            )
        """,
        "outros": """
            CREATE TABLE IF NOT EXISTS outros (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                file_id TEXT NOT NULL UNIQUE,
                file_unique_id TEXT UNIQUE,
                ext TEXT DEFAULT '.mp4',
                weight INTEGER DEFAULT 10,
                is_active INTEGER DEFAULT 1,
                added_at TEXT NOT NULL
            )
        """,
        "outro_history": """
            CREATE TABLE IF NOT EXISTS outro_history (
                id SERIAL PRIMARY KEY,
                outro_id INTEGER NOT NULL,
                upload_id TEXT NOT NULL,
                used_at TEXT NOT NULL
            )
        """,
        "queue_history": """
            CREATE TABLE IF NOT EXISTS queue_history (
                id SERIAL PRIMARY KEY,
                queue_id INTEGER,
                action TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            )
        """,
        "system_state": """
            CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """,
        "auto_mode_config": """
            CREATE TABLE IF NOT EXISTS auto_mode_config (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                enabled INTEGER DEFAULT 0,
                uploads_per_day INTEGER DEFAULT 3,
                start_time TEXT DEFAULT '06:00',
                last_run TEXT
            )
        """,
        "viral_trends": """
            CREATE TABLE IF NOT EXISTS viral_trends (
                id SERIAL PRIMARY KEY,
                song_name TEXT,
                artist TEXT,
                platform TEXT,
                source_url TEXT,
                viral_score REAL,
                growth_rate REAL,
                competition_score REAL,
                opportunity_score REAL,
                discovered_at TEXT,
                last_updated TEXT
            )
        """,
        "github_jobs": """
            CREATE TABLE IF NOT EXISTS github_jobs (
                id SERIAL PRIMARY KEY,
                job_id TEXT NOT NULL UNIQUE,
                song_name TEXT NOT NULL,
                status TEXT DEFAULT 'dispatched',
                stage TEXT DEFAULT 'queued',
                error_message TEXT,
                dispatched_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """,
    }
    return ddls.get(table_name, "")


def _sqlite_ddl(table_name: str) -> str:
    """Return SQLite CREATE TABLE DDL."""
    ddls = {
        "trends": """
            CREATE TABLE IF NOT EXISTS trends (
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
            )
        """,
        "upload_queue": """
            CREATE TABLE IF NOT EXISTS upload_queue (
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
            )
        """,
        "uploads": """
            CREATE TABLE IF NOT EXISTS uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_id INTEGER,
                song_name TEXT NOT NULL,
                youtube_url TEXT NOT NULL,
                youtube_video_id TEXT NOT NULL,
                source TEXT DEFAULT 'manual',
                uploaded_at TEXT NOT NULL
            )
        """,
        "outros": """
            CREATE TABLE IF NOT EXISTS outros (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                file_id TEXT NOT NULL UNIQUE,
                file_unique_id TEXT UNIQUE,
                ext TEXT DEFAULT '.mp4',
                weight INTEGER DEFAULT 10,
                is_active INTEGER DEFAULT 1,
                added_at TEXT NOT NULL
            )
        """,
        "outro_history": """
            CREATE TABLE IF NOT EXISTS outro_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                outro_id INTEGER NOT NULL,
                upload_id TEXT NOT NULL,
                used_at TEXT NOT NULL
            )
        """,
        "queue_history": """
            CREATE TABLE IF NOT EXISTS queue_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_id INTEGER,
                action TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            )
        """,
        "system_state": """
            CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """,
        "auto_mode_config": """
            CREATE TABLE IF NOT EXISTS auto_mode_config (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                enabled INTEGER DEFAULT 0,
                uploads_per_day INTEGER DEFAULT 3,
                start_time TEXT DEFAULT '06:00',
                last_run TEXT
            )
        """,
        "viral_trends": """
            CREATE TABLE IF NOT EXISTS viral_trends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                song_name TEXT,
                artist TEXT,
                platform TEXT,
                source_url TEXT,
                viral_score REAL,
                growth_rate REAL,
                competition_score REAL,
                opportunity_score REAL,
                discovered_at TEXT,
                last_updated TEXT
            )
        """,
        "github_jobs": """
            CREATE TABLE IF NOT EXISTS github_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL UNIQUE,
                song_name TEXT NOT NULL,
                status TEXT DEFAULT 'dispatched',
                stage TEXT DEFAULT 'queued',
                error_message TEXT,
                dispatched_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """,
    }
    return ddls.get(table_name, "")


def init_all_tables() -> None:
    """Create all tables if they don't exist. Safe to run multiple times."""
    conn = get_connection()
    try:
        c = conn.cursor()
        tables = [
            "trends", "upload_queue", "uploads", "outros",
            "outro_history", "queue_history", "system_state",
            "auto_mode_config", "viral_trends", "github_jobs",
        ]
        for table in tables:
            ddl = _pg_ddl(table) if is_using_postgres() else _sqlite_ddl(table)
            if ddl:
                c.execute(ddl)

        conn.commit()

        # Insert default auto_mode_config if not exists
        if is_using_postgres():
            c.execute(
                """INSERT INTO auto_mode_config (id, enabled, uploads_per_day, start_time)
                   VALUES (1, 0, 3, '06:00')
                   ON CONFLICT(id) DO NOTHING"""
            )
        else:
            c.execute(
                """INSERT OR IGNORE INTO auto_mode_config (id, enabled, uploads_per_day, start_time)
                   VALUES (1, 0, 3, '06:00')"""
            )

        conn.commit()

        # Create indexes
        index_sql = [
            "CREATE INDEX IF NOT EXISTS idx_trends_opportunity ON trends(opportunity_score DESC)",
            "CREATE INDEX IF NOT EXISTS idx_queue_status ON upload_queue(status, id)",
            "CREATE INDEX IF NOT EXISTS idx_uploads_url ON uploads(youtube_url)",
            "CREATE INDEX IF NOT EXISTS idx_outros_active ON outros(is_active)",
            "CREATE INDEX IF NOT EXISTS idx_outro_history_time ON outro_history(used_at DESC)",
        ]
        for sql in index_sql:
            c.execute(sql)

        conn.commit()
    finally:
        conn.close()


def migrate_v2_phase1() -> None:
    """Phase 1 migration: add missing columns, create uploads table."""
    conn = get_connection()
    try:
        c = conn.cursor()
        if is_using_postgres():
            c.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'uploads'
            """)
        else:
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='uploads'")
        if c.fetchone():
            print("[DB] uploads table already exists, skipping Phase 1 migration")
            return
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
