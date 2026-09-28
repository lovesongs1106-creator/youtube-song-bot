#!/usr/bin/env python3
"""Migration script: SQLite -> PostgreSQL

Usage:
  1. Set DATABASE_URL env var to your PostgreSQL connection string.
  2. Run: python scripts/migrate_to_postgres.py

What it does:
  - Reads all data from local SQLite (storage/trends.db)
  - Connects to PostgreSQL via DATABASE_URL
  - Creates tables if they don't exist
  - Migrates data row-by-row (safe, idempotent)
  - Prints migration summary

Rollback:
  - Drop all tables in PostgreSQL and re-run from SQLite backup.
  - Or unset DATABASE_URL to fall back to SQLite.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

# Add parent to path so we can import agents.db
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.db import DATABASE_URL, is_using_postgres, get_connection, init_all_tables

SQLITE_PATH = Path("storage/trends.db")

TABLES = [
    "trends",
    "upload_queue",
    "uploads",
    "outros",
    "outro_history",
    "queue_history",
    "system_state",
    "auto_mode_config",
    "viral_trends",
]

# Columns for each table (must match schema order)
COLUMNS = {
    "trends": ["id", "song_name", "artist", "youtube_url", "source_platform", "viral_score", "growth_score", "competition_score", "opportunity_score", "created_at"],
    "upload_queue": ["id", "user_id", "chat_id", "song_name", "youtube_url", "status", "video_id", "error_message", "source", "created_at", "started_at", "completed_at"],
    "uploads": ["id", "queue_id", "song_name", "youtube_url", "youtube_video_id", "source", "uploaded_at"],
    "outros": ["id", "name", "file_id", "file_unique_id", "ext", "weight", "is_active", "added_at"],
    "outro_history": ["id", "outro_id", "upload_id", "used_at"],
    "queue_history": ["id", "queue_id", "action", "detail", "created_at"],
    "system_state": ["key", "value"],
    "auto_mode_config": ["id", "enabled", "uploads_per_day", "start_time", "last_run"],
    "viral_trends": ["id", "song_name", "artist", "platform", "source_url", "viral_score", "growth_rate", "competition_score", "opportunity_score", "discovered_at", "last_updated"],
}


def get_sqlite_data(table: str) -> list[dict]:
    """Fetch all rows from SQLite table as dicts."""
    if not SQLITE_PATH.exists():
        return []
    conn = sqlite3.connect(str(SQLITE_PATH))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(f"SELECT * FROM {table}")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def migrate_table(table: str, pg_conn) -> tuple[int, int]:
    """Migrate one table. Returns (rows_migrated, rows_skipped)."""
    rows = get_sqlite_data(table)
    if not rows:
        return 0, 0

    cols = COLUMNS[table]
    col_str = ", ".join(cols)
    placeholders = ", ".join(["%s"] * len(cols))

    # For tables with SERIAL PK, we need to handle id conflicts.
    # Use ON CONFLICT DO NOTHING for safety, or update.
    # For most tables, we want to preserve IDs exactly.
    c = pg_conn.cursor()
    migrated = 0
    skipped = 0

    for row in rows:
        values = [row.get(col) for col in cols]
        try:
            c.execute(
                f"INSERT INTO {table} ({col_str}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                values,
            )
            if c.rowcount > 0:
                migrated += 1
            else:
                skipped += 1
        except Exception as exc:
            print(f"  [WARN] Failed to migrate row in {table}: {exc}")
            skipped += 1

    pg_conn.commit()
    return migrated, skipped


def reset_postgres_sequences(pg_conn) -> None:
    """Reset SERIAL sequences to max(id)+1 for each table."""
    c = pg_conn.cursor()
    for table in TABLES:
        try:
            c.execute(f"SELECT MAX(id) FROM {table}")
            row = c.fetchone()
            max_id = row[0] if row and row[0] else 0
            seq_name = f"{table}_id_seq"
            c.execute(f"SELECT setval(%s, %s, true)", (seq_name, max_id + 1))
            pg_conn.commit()
            print(f"  [SEQ] {seq_name} set to {max_id + 1}")
        except Exception as exc:
            # Some tables might not have a sequence (e.g., system_state)
            pass


def main() -> None:
    if not DATABASE_URL:
        print("❌ DATABASE_URL env var not set. Nothing to migrate to.")
        print("   Set it to your PostgreSQL connection string and retry.")
        sys.exit(1)

    if not is_using_postgres():
        print(f"❌ DATABASE_URL does not look like a PostgreSQL URL: {DATABASE_URL[:20]}...")
        sys.exit(1)

    print("=" * 60)
    print("SQLite → PostgreSQL Migration")
    print("=" * 60)
    print(f"SQLite source: {SQLITE_PATH} (exists={SQLITE_PATH.exists()})")
    print(f"PostgreSQL target: {DATABASE_URL[:30]}...")
    print()

    # Ensure PostgreSQL tables exist
    print("[1/4] Ensuring PostgreSQL tables exist...")
    init_all_tables()
    print("      OK")

    # Connect to PostgreSQL directly for migration
    print("[2/4] Connecting to PostgreSQL...")
    pg_conn = get_connection()
    print("      OK")

    # Migrate each table
    print("[3/4] Migrating data...")
    total_migrated = 0
    total_skipped = 0
    for table in TABLES:
        migrated, skipped = migrate_table(table, pg_conn)
        total_migrated += migrated
        total_skipped += skipped
        print(f"      {table:20s} | migrated: {migrated:4d} | skipped: {skipped:4d}")

    # Reset sequences
    print("[4/4] Resetting sequences...")
    reset_postgres_sequences(pg_conn)

    pg_conn.close()

    print()
    print("=" * 60)
    print("Migration complete!")
    print(f"Total migrated: {total_migrated}")
    print(f"Total skipped:  {total_skipped}")
    print()
    print("Next steps:")
    print("  1. Verify data: SELECT * FROM upload_queue LIMIT 5;")
    print("  2. Deploy with DATABASE_URL set.")
    print("  3. Unset DATABASE_URL to fall back to SQLite.")
    print("=" * 60)


if __name__ == "__main__":
    main()
