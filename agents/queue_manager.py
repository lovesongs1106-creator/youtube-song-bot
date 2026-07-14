#!/usr/bin/env python3
"""Manual Upload Queue System — SQLite-backed sequential job processor.

Tables:
  upload_queue    — pending/processing/completed/failed/cancelled jobs
  queue_history   — audit log of queue actions
  queue_state     — key/value store (paused flag)

Background processor:
  - Runs in bot's asyncio loop
  - Processes one item at a time
  - Dispatches to GitHub Actions worker
  - Resumes automatically after restart
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

DB_PATH = "storage/trends.db"


# ── Database ─────────────────────────────────────────────────────────────────

def init_queue_tables() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS upload_queue (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            chat_id INTEGER,
            song_name TEXT,
            youtube_url TEXT,
            status TEXT DEFAULT 'pending',
            video_id TEXT,
            error_message TEXT,
            created_at TEXT,
            completed_at TEXT
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS queue_history (
            id INTEGER PRIMARY KEY,
            queue_id INTEGER,
            action TEXT,
            detail TEXT,
            created_at TEXT
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS queue_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )"""
    )
    conn.commit()
    conn.close()


def _log_action(queue_id: int | None, action: str, detail: str = "") -> None:
    init_queue_tables()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO queue_history (queue_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
        (queue_id, action, detail, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


# ── Queue CRUD ───────────────────────────────────────────────────────────────

def add_queue_item(user_id: int, chat_id: int, song_name: str, youtube_url: str) -> int:
    init_queue_tables()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """INSERT INTO upload_queue (user_id, chat_id, song_name, youtube_url, status, created_at)
           VALUES (?, ?, ?, ?, 'pending', ?)""",
        (user_id, chat_id, song_name, youtube_url, datetime.now().isoformat()),
    )
    item_id = c.lastrowid
    conn.commit()
    conn.close()
    _log_action(item_id, "added", f"song={song_name}")
    return item_id


def add_multiple_items(user_id: int, chat_id: int, items: list[dict[str, str]]) -> list[int]:
    """Add multiple items. Returns list of IDs."""
    ids = []
    for item in items:
        ids.append(add_queue_item(user_id, chat_id, item["song_name"], item["youtube_url"]))
    return ids


def get_queue(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    init_queue_tables()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if status:
        c.execute(
            """SELECT * FROM upload_queue WHERE status = ? ORDER BY id LIMIT ?""",
            (status, limit),
        )
    else:
        c.execute("""SELECT * FROM upload_queue ORDER BY id DESC LIMIT ?""", (limit,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_next_pending() -> dict[str, Any] | None:
    """Get oldest pending item."""
    init_queue_tables()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        """SELECT * FROM upload_queue WHERE status = 'pending' ORDER BY id LIMIT 1"""
    )
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def update_status(item_id: int, status: str, video_id: str | None = None, error: str | None = None) -> None:
    init_queue_tables()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    completed_at = datetime.now().isoformat() if status in ("completed", "failed", "cancelled") else None
    c.execute(
        """UPDATE upload_queue
           SET status = ?, video_id = ?, error_message = ?, completed_at = ?
           WHERE id = ?""",
        (status, video_id, error, completed_at, item_id),
    )
    conn.commit()
    conn.close()
    _log_action(item_id, f"status_{status}", error or "")


def cancel_all_pending() -> int:
    """Cancel all pending items. Returns count cancelled."""
    init_queue_tables()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT id FROM upload_queue WHERE status = 'pending'""")
    ids = [r[0] for r in c.fetchall()]
    now = datetime.now().isoformat()
    c.execute(
        """UPDATE upload_queue SET status = 'cancelled', completed_at = ? WHERE status = 'pending'""",
        (now,),
    )
    conn.commit()
    conn.close()
    for item_id in ids:
        _log_action(item_id, "cancelled", "bulk_cancel")
    return len(ids)


def get_summary() -> dict[str, int]:
    """Return counts by status."""
    init_queue_tables()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT status, COUNT(*) FROM upload_queue GROUP BY status""")
    counts = {r[0]: r[1] for r in c.fetchall()}
    conn.close()
    return {
        "total": sum(counts.values()),
        "pending": counts.get("pending", 0),
        "processing": counts.get("processing", 0),
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0),
        "cancelled": counts.get("cancelled", 0),
    }


# ── Pause / Resume ───────────────────────────────────────────────────────────

def is_paused() -> bool:
    init_queue_tables()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM queue_state WHERE key = 'paused'")
    row = c.fetchone()
    conn.close()
    return row is not None and row[0] == "true"


def set_paused(paused: bool) -> None:
    init_queue_tables()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """INSERT INTO queue_state (key, value) VALUES ('paused', ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        ("true" if paused else "false",),
    )
    conn.commit()
    conn.close()
    _log_action(None, "paused" if paused else "resumed", "")


# ── Background Processor ─────────────────────────────────────────────────────

import asyncio

async def run_queue_processor(bot, default_privacy: str, dispatch_fn) -> None:
    """Background coroutine that processes the queue sequentially.

    Args:
        bot: python-telegram-bot Bot instance for sending messages.
        default_privacy: privacy status string.
        dispatch_fn: callable that accepts a payload dict and dispatches to GitHub worker.
    """
    from agents.outro_manager import select_outro, record_outro_usage
    from agents.viral_trend_engine import generate_seo_metadata
    import datetime as dt

    print("[QUEUE] Background processor started", flush=True)

    while True:
        if is_paused():
            await asyncio.sleep(5)
            continue

        item = get_next_pending()
        if not item:
            await asyncio.sleep(5)
            continue

        # Mark as processing
        update_status(item["id"], "processing")

        try:
            # Notify user
            await bot.send_message(
                chat_id=item["chat_id"],
                text=f"🎵 Processing queue item {item['id']}: {item['song_name']}",
            )

            # Select outro
            outro = select_outro()
            if not outro:
                update_status(item["id"], "failed", error="No outro available")
                await bot.send_message(
                    chat_id=item["chat_id"],
                    text=f"❌ Queue item {item['id']} failed: No outro available. Use /outro_add first.",
                )
                await asyncio.sleep(2)
                continue

            # Build metadata
            metadata = generate_seo_metadata(item["song_name"], None, item["youtube_url"])
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            job_id = f"queue-{item['id']}-{stamp}"

            payload = {
                "job_id": job_id,
                "chat_id": item["chat_id"],
                "user_id": item["user_id"],
                "song_name": item["song_name"],
                "artist": None,
                "source_type": "youtube_url",
                "youtube_url": item["youtube_url"],
                "privacy": default_privacy,
                "custom_title": metadata["title"],
                "custom_description": metadata["description"],
                "custom_tags": metadata["tags"],
                "outro_file_id": outro["file_id"],
                "outro_ext": outro["ext"],
            }

            # Dispatch to GitHub Actions
            await asyncio.to_thread(dispatch_fn, payload)
            record_outro_usage(outro["outro_id"], job_id)

            # Mark completed (GitHub worker handles actual upload)
            update_status(item["id"], "completed")

            await bot.send_message(
                chat_id=item["chat_id"],
                text=f"✅ Queue item {item['id']} dispatched to GitHub Actions.\nSong: {item['song_name']}",
            )

        except Exception as exc:
            error_msg = str(exc)[:200]
            update_status(item["id"], "failed", error=error_msg)
            try:
                await bot.send_message(
                    chat_id=item["chat_id"],
                    text=f"❌ Queue item {item['id']} failed:\n{error_msg}",
                )
            except Exception:
                pass

        await asyncio.sleep(2)
