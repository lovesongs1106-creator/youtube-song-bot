#!/usr/bin/env python3
"""Outro Rotation System — weighted random selection with no-repeat history.

Tables:
  outros         — registered outro videos
  outro_history  — usage log for anti-repeat logic

Rules:
  - Never repeat same outro in last 3 uploads
  - Weighted random selection from remaining pool
  - Fallback to any active outro if all excluded
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agents.db import execute, fetchone, fetchall


# ── Data Model ───────────────────────────────────────────────────────────────

@dataclass
class Outro:
    id: int
    name: str
    file_id: str
    file_unique_id: str | None
    ext: str
    weight: int
    is_active: int
    added_at: str


# ── CRUD Operations ──────────────────────────────────────────────────────────

def add_outro(name: str, file_id: str, ext: str = ".mp4", weight: int = 10, file_unique_id: str | None = None) -> int:
    """Add a new outro. Returns the outro ID."""
    from agents.db import get_connection
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            """INSERT INTO outros (name, file_id, file_unique_id, ext, weight, is_active, added_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(file_id) DO UPDATE SET
                   name=excluded.name,
                   file_unique_id=excluded.file_unique_id,
                   ext=excluded.ext,
                   weight=excluded.weight,
                   is_active=1""",
            (name, file_id, file_unique_id, ext, weight, 1, datetime.now().isoformat()),
        )
        conn.commit()
        return c.lastrowid or 0
    finally:
        conn.close()


def list_outros() -> list[Outro]:
    """Return all active outros."""
    rows = fetchall(
        """SELECT id, name, file_id, file_unique_id, ext, weight, is_active, added_at
           FROM outros WHERE is_active = 1 ORDER BY added_at DESC"""
    )
    return [Outro(**r) for r in rows]


def remove_outro(outro_id: int) -> bool:
    """Soft-delete an outro."""
    execute("UPDATE outros SET is_active = 0 WHERE id = ?", (outro_id,))
    # Check if any row was updated
    result = fetchone("SELECT changes() as cnt")
    return result is not None and result.get("cnt", 0) > 0


def get_outro_by_id(outro_id: int) -> Outro | None:
    """Get a specific outro by ID."""
    row = fetchone(
        """SELECT id, name, file_id, file_unique_id, ext, weight, is_active, added_at
           FROM outros WHERE id = ?""",
        (outro_id,)
    )
    return Outro(**row) if row else None


# ── Selection Logic ──────────────────────────────────────────────────────────

def get_last_used_outro_ids(limit: int = 3) -> list[int]:
    """Return IDs of the last N used outros."""
    rows = fetchall(
        """SELECT outro_id FROM outro_history
           ORDER BY used_at DESC LIMIT ?""",
        (limit,)
    )
    return [r["outro_id"] for r in rows]


def select_outro() -> dict[str, Any] | None:
    """Select an outro using weighted random with anti-repeat logic.

    Returns dict with keys: outro_id, name, file_id, ext
    Returns None if no active outros available.
    """
    all_active = list_outros()
    if not all_active:
        return None

    last_used = get_last_used_outro_ids(3)
    eligible = [o for o in all_active if o.id not in last_used]

    # If all excluded (e.g., only 3 outros total), fallback to all active
    if not eligible:
        eligible = all_active

    # Weighted random selection
    weights = [o.weight for o in eligible]
    total = sum(weights)
    if total == 0:
        chosen = random.choice(eligible)
    else:
        r = random.uniform(0, total)
        cumulative = 0.0
        chosen = eligible[-1]
        for o in eligible:
            cumulative += o.weight
            if r <= cumulative:
                chosen = o
                break

    return {
        "outro_id": chosen.id,
        "name": chosen.name,
        "file_id": chosen.file_id,
        "ext": chosen.ext,
    }


def record_outro_usage(outro_id: int, job_id: str) -> None:
    """Log that an outro was used for a job."""
    execute(
        """INSERT INTO outro_history (outro_id, upload_id, used_at)
           VALUES (?, ?, ?)""",
        (outro_id, job_id, datetime.now().isoformat()),
    )


# ── Telegram Formatting ──────────────────────────────────────────────────────

def format_outro_list() -> str:
    """Format outro list for Telegram display."""
    outros = list_outros()
    if not outros:
        return "📭 No outros registered.\n\nUse /outro_add to upload an outro video."

    lines = ["🎬 Registered Outros\n"]
    last_used = get_last_used_outro_ids(3)
    for o in outros:
        marker = " 🔄" if o.id in last_used else ""
        lines.append(
            f"ID: {o.id} | {o.name}{marker}\n"
            f"   Weight: {o.weight} | Ext: {o.ext} | Added: {o.added_at[:10]}"
        )
    lines.append(f"\nTotal: {len(outros)} active outro(s)")
    lines.append("\nCommands:\n/outro_add — Upload new\n/outro_remove <id> — Remove\n/outro_test — Test selection")
    return "\n".join(lines)
