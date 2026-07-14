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
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

DB_PATH = "storage/trends.db"


# ── Data Model ───────────────────────────────────────────────────────────────

@dataclass
class Outro:
    id: int
    name: str
    file_id: str
    ext: str
    weight: int
    is_active: bool
    added_at: str


# ── Database ─────────────────────────────────────────────────────────────────

def init_outro_tables() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS outros (
            id INTEGER PRIMARY KEY,
            name TEXT,
            file_id TEXT UNIQUE,
            ext TEXT DEFAULT '.mp4',
            weight INTEGER DEFAULT 10,
            is_active INTEGER DEFAULT 1,
            added_at TEXT
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS outro_history (
            id INTEGER PRIMARY KEY,
            outro_id INTEGER,
            job_id TEXT,
            used_at TEXT,
            FOREIGN KEY (outro_id) REFERENCES outros(id)
        )"""
    )
    conn.commit()
    conn.close()


# ── CRUD Operations ──────────────────────────────────────────────────────────

def add_outro(name: str, file_id: str, ext: str = ".mp4", weight: int = 10) -> int:
    """Add a new outro. Returns the outro ID."""
    init_outro_tables()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """INSERT INTO outros (name, file_id, ext, weight, is_active, added_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(file_id) DO UPDATE SET
               name=excluded.name,
               ext=excluded.ext,
               weight=excluded.weight,
               is_active=1""",
        (name, file_id, ext, weight, 1, datetime.now().isoformat()),
    )
    outro_id = c.lastrowid
    conn.commit()
    conn.close()
    return outro_id


def list_outros() -> list[Outro]:
    """Return all active outros."""
    init_outro_tables()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """SELECT id, name, file_id, ext, weight, is_active, added_at
           FROM outros WHERE is_active = 1 ORDER BY added_at DESC"""
    )
    rows = c.fetchall()
    conn.close()
    return [Outro(*row) for row in rows]


def remove_outro(outro_id: int) -> bool:
    """Soft-delete an outro."""
    init_outro_tables()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE outros SET is_active = 0 WHERE id = ?", (outro_id,))
    changed = c.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def get_outro_by_id(outro_id: int) -> Outro | None:
    """Get a specific outro by ID."""
    init_outro_tables()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """SELECT id, name, file_id, ext, weight, is_active, added_at
           FROM outros WHERE id = ?""",
        (outro_id,),
    )
    row = c.fetchone()
    conn.close()
    return Outro(*row) if row else None


# ── Selection Logic ──────────────────────────────────────────────────────────

def get_last_used_outro_ids(limit: int = 3) -> list[int]:
    """Return IDs of the last N used outros."""
    init_outro_tables()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """SELECT outro_id FROM outro_history
           ORDER BY used_at DESC LIMIT ?""",
        (limit,),
    )
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]


def select_outro() -> dict[str, Any] | None:
    """Select an outro using weighted random with anti-repeat logic.

    Returns dict with keys: outro_id, name, file_id, ext
    Returns None if no active outros available.
    """
    init_outro_tables()
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
    init_outro_tables()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """INSERT INTO outro_history (outro_id, job_id, used_at)
           VALUES (?, ?, ?)""",
        (outro_id, job_id, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


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
