#!/usr/bin/env python3
"""Upload Queue Engine — SQLite-backed sequential job processor with resume support.

Features:
  - Duplicate protection via uploads table
  - Resume after restart/crash/deploy
  - Pause/resume controls
  - 100+ song capacity
  - Audit logging
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from agents.db import execute, fetchone, fetchall, is_already_uploaded, record_upload, insert_and_get_id


# ── Queue CRUD ───────────────────────────────────────────────────────────────

def add_queue_item(user_id: int, chat_id: int, song_name: str, youtube_url: str, source: str = "manual") -> int | None:
    """Add a single item to queue. Returns item ID or None if duplicate/already uploaded."""
    # DUPLICATE PROTECTION: Check uploads table
    if is_already_uploaded(youtube_url):
        return None

    # DUPLICATE PROTECTION: Check if already in queue (pending or processing).
    # Batch paths (add_multiple_items / transactional) already do this check;
    # single-item inserts must behave the same so /new and approve flows
    # can't enqueue the same URL twice.
    existing = fetchone(
        "SELECT id FROM upload_queue WHERE youtube_url = ? AND status IN ('pending', 'processing') LIMIT 1",
        (youtube_url,)
    )
    if existing:
        return None

    item_id = insert_and_get_id(
        """INSERT INTO upload_queue (user_id, chat_id, song_name, youtube_url, status, source, created_at)
           VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
        (user_id, chat_id, song_name, youtube_url, source, datetime.now().isoformat()),
    )
    if item_id:
        _log_action(item_id, "added", f"source={source}, song={song_name}")
    return item_id


def add_multiple_items(user_id: int, chat_id: int, items: list[dict[str, str]], source: str = "manual") -> tuple[list[int], list[dict]]:
    """Add multiple items. Returns (added_ids, rejected_items).
    
    Each item dict must have: song_name, youtube_url
    Rejected items include reason: 'already_uploaded' or 'in_queue'
    """
    added = []
    rejected = []

    for item in items:
        url = item.get("youtube_url", "")
        name = item.get("song_name", "")

        # Check uploads table
        if is_already_uploaded(url):
            rejected.append({**item, "reason": "already_uploaded"})
            continue

        # Check if already in queue (pending or processing)
        existing = fetchone(
            "SELECT id FROM upload_queue WHERE youtube_url = ? AND status IN ('pending', 'processing') LIMIT 1",
            (url,)
        )
        if existing:
            rejected.append({**item, "reason": "in_queue"})
            continue

        item_id = add_queue_item(user_id, chat_id, name, url, source)
        if item_id:
            added.append(item_id)
        else:
            rejected.append({**item, "reason": "unknown"})

    return added, rejected


def add_multiple_items_transactional(user_id: int, chat_id: int, items: list[dict[str, str]], source: str = "agentreach") -> dict[str, Any]:
    """Transactional batch insert. ALL or NOTHING.
    
    Pre-flight checks each item for duplicates/already-uploaded using SAME connection.
    If any item fails validation, NOTHING is inserted.
    If DB error occurs mid-insert, full rollback.
    
    Returns:
        {
            "success": bool,
            "added_ids": list[int],
            "added_count": int,
            "pending_total": int,
            "rejected": list[dict],  # pre-flight rejections
            "error": str | None,
        }
    """
    from agents.db import get_connection, is_using_postgres
    
    result = {
        "success": False,
        "added_ids": [],
        "added_count": 0,
        "pending_total": 0,
        "rejected": [],
        "error": None,
    }
    
    if not items:
        result["error"] = "No items to insert"
        return result
    
    if len(items) > 100:
        result["error"] = "Maximum 100 songs per batch"
        return result
    
    conn = get_connection()
    try:
        c = conn.cursor()
        
        # Pre-flight validation using SAME connection
        seen_urls = set()
        for item in items:
            url = item.get("youtube_url", "").strip()
            name = item.get("song_name", "").strip()
            
            if not name or not url:
                result["rejected"].append({**item, "reason": "missing_name_or_url"})
                continue
            
            if url in seen_urls:
                result["rejected"].append({**item, "reason": "duplicate_in_batch"})
                continue
            seen_urls.add(url)
            
            # Check uploads table
            if is_using_postgres():
                c.execute("SELECT 1 FROM uploads WHERE youtube_url = %s LIMIT 1", (url,))
            else:
                c.execute("SELECT 1 FROM uploads WHERE youtube_url = ? LIMIT 1", (url,))
            if c.fetchone():
                result["rejected"].append({**item, "reason": "already_uploaded"})
                continue
            
            # Check queue
            if is_using_postgres():
                c.execute(
                    "SELECT id FROM upload_queue WHERE youtube_url = %s AND status IN ('pending', 'processing') LIMIT 1",
                    (url,)
                )
            else:
                c.execute(
                    "SELECT id FROM upload_queue WHERE youtube_url = ? AND status IN ('pending', 'processing') LIMIT 1",
                    (url,)
                )
            if c.fetchone():
                result["rejected"].append({**item, "reason": "in_queue"})
                continue
        
        # If any pre-flight rejections, abort entire batch
        if result["rejected"]:
            result["error"] = f"Pre-flight validation failed for {len(result['rejected'])} item(s). Entire batch rolled back."
            conn.rollback()
            return result
        
        # Transactional insert
        now = datetime.now().isoformat()
        added_ids = []
        
        for item in items:
            url = item["youtube_url"].strip()
            name = item["song_name"].strip()
            
            if is_using_postgres():
                c.execute(
                    """INSERT INTO upload_queue (user_id, chat_id, song_name, youtube_url, status, source, created_at)
                       VALUES (%s, %s, %s, %s, 'pending', %s, %s)
                       RETURNING id""",
                    (user_id, chat_id, name, url, source, now),
                )
                row = c.fetchone()
                item_id = row[0] if row else None
            else:
                c.execute(
                    """INSERT INTO upload_queue (user_id, chat_id, song_name, youtube_url, status, source, created_at)
                       VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                    (user_id, chat_id, name, url, source, now),
                )
                item_id = c.lastrowid
            
            if item_id:
                added_ids.append(item_id)
        
        conn.commit()
        
        # Log actions after successful commit (best effort)
        for item_id in added_ids:
            _log_action(item_id, "added", f"source={source}")
        
        result["success"] = True
        result["added_ids"] = added_ids
        result["added_count"] = len(added_ids)
        
        # Get updated pending count
        if is_using_postgres():
            c.execute("SELECT COUNT(*) as cnt FROM upload_queue WHERE status = 'pending'")
        else:
            c.execute("SELECT COUNT(*) as cnt FROM upload_queue WHERE status = 'pending'")
        row = c.fetchone()
        result["pending_total"] = row[0] if row else 0
        
    except Exception as exc:
        conn.rollback()
        result["error"] = f"Transaction rolled back: {exc}"
    finally:
        conn.close()
    
    return result


def get_next_pending() -> dict[str, Any] | None:
    """Get oldest pending item for processing."""
    return fetchone(
        "SELECT * FROM upload_queue WHERE status = 'pending' ORDER BY id LIMIT 1"
    )


def get_queue(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Get queue items."""
    if status:
        return fetchall(
            "SELECT * FROM upload_queue WHERE status = ? ORDER BY id LIMIT ?",
            (status, limit)
        )
    return fetchall(
        "SELECT * FROM upload_queue ORDER BY id DESC LIMIT ?",
        (limit,)
    )


def update_status(item_id: int, status: str, video_id: str | None = None, error: str | None = None) -> None:
    """Update queue item status."""
    now = datetime.now().isoformat()
    if status == "processing":
        execute(
            """UPDATE upload_queue SET status = ?, started_at = ? WHERE id = ?""",
            (status, now, item_id)
        )
    elif status in ("completed", "failed", "cancelled"):
        execute(
            """UPDATE upload_queue SET status = ?, video_id = ?, error_message = ?, completed_at = ? WHERE id = ?""",
            (status, video_id, error, now, item_id)
        )
    else:
        execute(
            "UPDATE upload_queue SET status = ? WHERE id = ?",
            (status, item_id)
        )
    _log_action(item_id, f"status_{status}", error or "")


def cancel_all_pending() -> int:
    """Cancel all pending items. Returns count."""
    items = fetchall("SELECT id FROM upload_queue WHERE status = 'pending'")
    now = datetime.now().isoformat()
    execute(
        "UPDATE upload_queue SET status = 'cancelled', completed_at = ? WHERE status = 'pending'",
        (now,)
    )
    for item in items:
        _log_action(item["id"], "cancelled", "bulk_cancel")
    return len(items)


def get_summary() -> dict[str, int]:
    """Return queue counts by status."""
    rows = fetchall("SELECT status, COUNT(*) as cnt FROM upload_queue GROUP BY status")
    counts = {r["status"]: r["cnt"] for r in rows}
    return {
        "total": sum(counts.values()),
        "pending": counts.get("pending", 0),
        "processing": counts.get("processing", 0),
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0),
        "cancelled": counts.get("cancelled", 0),
    }


def _log_action(queue_id: int | None, action: str, detail: str = "") -> None:
    execute(
        "INSERT INTO queue_history (queue_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
        (queue_id, action, detail, datetime.now().isoformat())
    )


# ── Pause / Resume ───────────────────────────────────────────────────────────

def is_paused() -> bool:
    result = fetchone("SELECT value FROM system_state WHERE key = 'paused'")
    return result is not None and result.get("value") == "true"


def set_paused(paused: bool) -> None:
    execute(
        """INSERT INTO system_state (key, value) VALUES ('paused', ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        ("true" if paused else "false",)
    )
    _log_action(None, "paused" if paused else "resumed", "")


# ── Background Processor ─────────────────────────────────────────────────────

async def run_queue_processor(bot, default_privacy: str, dispatch_fn) -> None:
    """Background coroutine that processes queue sequentially.
    
    Args:
        bot: python-telegram-bot Bot instance
        default_privacy: privacy status string
        dispatch_fn: callable that accepts payload dict and dispatches to GitHub worker
    """
    from agents.outro_manager import select_outro, record_outro_usage
    from bot import generate_seo_metadata
    import datetime as dt

    print("[QUEUE] Background processor started", flush=True)

    while True:
        try:
            if is_paused():
                await asyncio.sleep(5)
                continue

            item = get_next_pending()
            if not item:
                await asyncio.sleep(5)
                continue

            # Mark as processing
            update_status(item["id"], "processing")

            # Notify user
            try:
                await bot.send_message(
                    chat_id=item["chat_id"],
                    text=f"🎵 Processing queue item {item['id']}: {item['song_name']}",
                )
            except Exception as e:
                print(f"[QUEUE] Telegram notify error: {e}", flush=True)

            # Select outro
            outro = select_outro()
            if not outro:
                update_status(item["id"], "failed", error="No outro available")
                try:
                    await bot.send_message(
                        chat_id=item["chat_id"],
                        text=f"❌ Queue item {item['id']} failed: No outro available. Use /outro_add first.",
                    )
                except Exception:
                    pass
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

            # Mark completed
            update_status(item["id"], "completed")

            try:
                await bot.send_message(
                    chat_id=item["chat_id"],
                    text=f"✅ Queue item {item['id']} dispatched to GitHub Actions.\nSong: {item['song_name']}",
                )
            except Exception as e:
                print(f"[QUEUE] Telegram completion error: {e}", flush=True)

        except Exception as exc:
            error_msg = str(exc)[:200]
            if item:
                update_status(item["id"], "failed", error=error_msg)
            print(f"[QUEUE] Processor error: {error_msg}", flush=True)
            try:
                await bot.send_message(
                    chat_id=item["chat_id"],
                    text=f"❌ Queue item {item['id']} failed:\n{error_msg}",
                )
            except Exception:
                pass

        await asyncio.sleep(2)
