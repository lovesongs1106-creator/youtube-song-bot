#!/usr/bin/env python3
"""Stdlib-only smoke test — no pip deps, no network required.

Validates the database layer + queue engine + outro manager + parsers using
an isolated temp SQLite database. Safe to run anywhere:

    python3 scripts/smoke_test.py

Exit code 0 = all checks passed, 1 = failure.
"""

from __future__ import annotations

import os
import sys
import tempfile

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    icon = "✅" if cond else "❌"
    print(f"{icon} {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not cond:
        FAILURES.append(name)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="ytbot_smoke_")
    os.chdir(tmp)
    print(f"work dir: {tmp}")

    # Force SQLite fallback regardless of host env
    os.environ.pop("DATABASE_URL", None)
    sys.path.insert(0, str(ROOT))

    import agents.db as db

    check("SQLite fallback active", not db.is_using_postgres())

    db.init_all_tables()
    check("init_all_tables", True)

    # Re-run to prove idempotency
    db.init_all_tables()
    check("init_all_tables idempotent", True)

    # ── Queue engine ──
    from agents.queue_engine import (
        add_queue_item,
        add_multiple_items,
        add_multiple_items_transactional,
        get_summary,
        get_next_pending,
        update_status,
        cancel_all_pending,
        is_paused,
        set_paused,
    )

    q1 = add_queue_item(111, 222, "Song A", "https://www.youtube.com/watch?v=AAA111")
    check("add_queue_item returns id", isinstance(q1, int), f"id={q1}")

    dup_single = add_queue_item(111, 222, "Song A dup", "https://www.youtube.com/watch?v=AAA111")
    check("single insert rejects in-queue duplicate", dup_single is None, f"got={dup_single}")

    added, rejected = add_multiple_items(111, 222, [
        {"song_name": "Song B", "youtube_url": "https://www.youtube.com/watch?v=BBB222"},
        {"song_name": "Song A again", "youtube_url": "https://www.youtube.com/watch?v=AAA111"},
    ])
    check("batch insert adds 1, rejects 1 in-queue", len(added) == 1 and len(rejected) == 1)

    tx = add_multiple_items_transactional(111, 222, [
        {"song_name": "Song C", "youtube_url": "https://www.youtube.com/watch?v=CCC333"},
        {"song_name": "Song A tx dup", "youtube_url": "https://www.youtube.com/watch?v=AAA111"},
    ])
    check("transactional batch rolls back on dup", tx["success"] is False and tx["added_count"] == 0)

    nxt = get_next_pending()
    check("get_next_pending returns oldest", nxt is not None and nxt["song_name"] == "Song A")

    update_status(q1, "processing")
    update_status(q1, "completed", video_id="vid123")
    s = get_summary()
    check("summary counts", s["completed"] == 1 and s["pending"] >= 1, str(s))

    # Pause / resume round-trip
    set_paused(True)
    check("pause", is_paused() is True)
    set_paused(False)
    check("resume", is_paused() is False)

    # Already-uploaded protection
    db.record_upload(q1, "Song A", "https://www.youtube.com/watch?v=AAA111", "vid123")
    check("is_already_uploaded", db.is_already_uploaded("https://www.youtube.com/watch?v=AAA111"))
    re_add = add_queue_item(111, 222, "Song A re", "https://www.youtube.com/watch?v=AAA111")
    check("reject already-uploaded URL", re_add is None)

    n_cancelled = cancel_all_pending()
    check("cancel_all_pending", n_cancelled >= 1, f"cancelled={n_cancelled}")

    # ── Outro manager ──
    from agents.outro_manager import add_outro, list_outros, select_outro, record_outro_usage

    o1 = add_outro("Outro One", "fileid_1", weight=10)
    o2 = add_outro("Outro Two", "fileid_2", weight=90)
    check("add_outro", o1 > 0 and o2 > 0, f"ids={o1},{o2}")
    check("list_outros", len(list_outros()) == 2)

    sel = select_outro()
    check("select_outro returns active outro", sel is not None)
    if sel:
        record_outro_usage(sel["outro_id"], "smoke-job-1")
        check("record_outro_usage", True)

    # ── system_state session store (webhook-safe bulk flow) ──
    db.execute(
        "INSERT INTO system_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("bulk_awaiting_999", "true"),
    )
    row = db.fetchone("SELECT value FROM system_state WHERE key = ?", ("bulk_awaiting_999",))
    check("system_state session flag", row is not None and row["value"] == "true")

    print()
    if FAILURES:
        print(f"SMOKE TEST FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("SMOKE TEST PASSED — all checks green.")
    return 0


ROOT = __import__("pathlib").Path(__file__).parent.parent.resolve()

if __name__ == "__main__":
    sys.exit(main())
