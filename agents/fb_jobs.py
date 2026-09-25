#!/usr/bin/env python3
"""Facebook -> second YouTube channel job manager.

Owns the `fb_jobs` table: job IDs, duplicate URL/job protection, retry
accounting, cancellation and status reporting.

Job lifecycle:
  queued -> dispatched -> downloading -> processing -> uploading -> completed
  Any non-terminal state may transition to failed or cancelled.

Only Facebook videos that are legitimately accessible to an unauthenticated
downloader are processed. Login-gated, private and DRM-protected content is
refused by the worker (see facebook_pipeline.py) — never bypassed.

This module is stdlib-only (+ agents.db) so it runs on Render and in tests
without heavy dependencies.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from typing import Any
from urllib.parse import urlparse, parse_qsl, urlencode

from agents.db import execute, fetchone, fetchall

# Statuses that block a duplicate URL from being queued again.
ACTIVE_STATUSES = ("queued", "dispatched", "downloading", "processing", "uploading")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
RETRYABLE_STATUSES = ("failed", "cancelled")

DEFAULT_MAX_ATTEMPTS = 3

FB_URL_RE = re.compile(
    r"https?://(?:www\.|m\.|web\.)?(?:facebook\.com|fb\.watch)/[^\s\"'<>]*",
    re.IGNORECASE,
)

# Tracking / session params stripped during normalization so the same video
# shared via different links maps to one canonical URL.
_TRACKING_PARAMS = {
    "__cft__", "__tn__", "fbclid", "si", "s", "lsrc", "ref", "refsrc",
    "__eep__", "_rdr", "locale", "paipv",
}

# Single segment: MM:SS or HH:MM:SS (hours may exceed 23).
_TS_RE = re.compile(r"^(?:(\d+):)?([0-5]?\d):([0-5]\d)$")


# ── URL helpers ──────────────────────────────────────────────────────────────

def extract_facebook_url(text: str) -> str | None:
    """Return the first Facebook video URL in free text, or None."""
    match = FB_URL_RE.search(text or "")
    return match.group(0).rstrip(").,;:!?") if match else None


def normalize_facebook_url(url: str) -> str:
    """Canonicalize a Facebook URL for duplicate detection.

    Lowercases scheme/host, strips tracking query params, drops fragments.
    """
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "https").lower()
    host = parsed.hostname.lower() if parsed.hostname else ""
    if host.startswith("web."):
        host = host[4:]
    path = parsed.path or "/"
    kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k not in _TRACKING_PARAMS]
    query = f"?{urlencode(kept)}" if kept else ""
    return f"{scheme}://{host}{path}{query}"


# ── Trim helpers ─────────────────────────────────────────────────────────────

def parse_timestamp(raw: str) -> float:
    """Parse MM:SS or HH:MM:SS into seconds. Raises ValueError on bad input."""
    m = _TS_RE.match((raw or "").strip())
    if not m:
        raise ValueError(f"Bad timestamp '{raw}'. Use MM:SS or HH:MM:SS (e.g. 02:15).")
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2))
    seconds = int(m.group(3))
    return float(hours * 3600 + minutes * 60 + seconds)


def parse_trim_spec(text: str) -> tuple[float, float] | None:
    """Parse an optional `trim START-END` clause from /upload text.

    Returns (start_seconds, end_seconds), or None when no trim requested.
    Raises ValueError on malformed ranges (bad format, start >= end).
    """
    m = re.search(r"\btrim\s+(\S+)", text or "", re.IGNORECASE)
    if not m:
        return None
    spec = m.group(1)
    if "-" not in spec:
        raise ValueError(
            f"Bad trim '{spec}'. Use: trim START-END (e.g. trim 02:15-38:42)."
        )
    start_raw, end_raw = spec.split("-", 1)
    start = parse_timestamp(start_raw)
    end = parse_timestamp(end_raw)
    if not end > start:
        raise ValueError(
            f"Bad trim '{spec}'. End ({end_raw}) must be after start ({start_raw})."
        )
    return start, end


def format_timestamp(seconds: float) -> str:
    """Format seconds as HH:MM:SS for ffmpeg / display."""
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


# ── Job IDs ──────────────────────────────────────────────────────────────────

def new_job_id() -> str:
    """Generate a unique, human-readable job ID: fb-YYYYMMDD-HHMMSS-xxxx."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"fb-{stamp}-{secrets.token_hex(2)}"


# ── CRUD ─────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().isoformat()


def find_duplicate(normalized_url: str) -> dict[str, Any] | None:
    """Return the newest active/completed job for a URL, or None.

    A URL counts as duplicate while a job for it is active OR already
    completed (completed videos must not be re-uploaded accidentally).
    """
    return fetchone(
        """SELECT * FROM fb_jobs
           WHERE normalized_url = ?
             AND status IN ('queued', 'dispatched', 'downloading', 'processing',
                            'uploading', 'completed')
           ORDER BY id DESC LIMIT 1""",
        (normalized_url,),
    )


def create_job(user_id: int, chat_id: int, facebook_url: str,
               trim: tuple[float, float] | None = None,
               force: bool = False) -> dict[str, Any]:
    """Create a queued job. Returns {"job": row} or {"duplicate": row}.

    Unless force=True, refuses when the same video already has an
    active or completed job (duplicate URL protection).
    """
    normalized = normalize_facebook_url(facebook_url)
    if not force:
        dup = find_duplicate(normalized)
        if dup:
            return {"duplicate": dup}
    job_id = new_job_id()
    now = _now()
    execute(
        """INSERT INTO fb_jobs
           (job_id, user_id, chat_id, facebook_url, normalized_url,
            trim_start, trim_end, status, stage, attempts, max_attempts,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', 1, ?, ?, ?)""",
        (job_id, user_id, chat_id, facebook_url, normalized,
         trim[0] if trim else None, trim[1] if trim else None,
         DEFAULT_MAX_ATTEMPTS, now, now),
    )
    return {"job": get_job(job_id)}


def get_job(job_id: str) -> dict[str, Any] | None:
    return fetchone("SELECT * FROM fb_jobs WHERE job_id = ?", (job_id.strip(),))


def set_status(job_id: str, status: str, stage: str = "",
               error: str | None = None,
               youtube_video_id: str | None = None) -> None:
    """Update job status. Terminal updates on cancelled jobs are ignored
    unless they carry the final YouTube video id (recorded for audit)."""
    job = get_job(job_id)
    if not job:
        return
    if job["status"] == "cancelled" and status in ("completed", "failed"):
        if youtube_video_id and not job.get("youtube_video_id"):
            execute(
                """UPDATE fb_jobs SET youtube_video_id = ?, stage = ?,
                                      updated_at = ? WHERE job_id = ?""",
                (youtube_video_id,
                 "finished_after_cancel_request", _now(), job_id),
            )
        return
    execute(
        """UPDATE fb_jobs SET status = ?, stage = ?,
                              error_message = COALESCE(?, error_message),
                              youtube_video_id = COALESCE(?, youtube_video_id),
                              updated_at = ? WHERE job_id = ?""",
        (status, stage, error, youtube_video_id, _now(), job_id),
    )


def retry_job(job_id: str) -> dict[str, Any]:
    """Re-queue a failed/cancelled job. Returns {"job": row} or {"error": msg}."""
    job = get_job(job_id)
    if not job:
        return {"error": f"Job {job_id} not found."}
    if job["status"] not in RETRYABLE_STATUSES:
        return {"error": f"Job {job_id} is {job['status']} — only failed/cancelled jobs can be retried."}
    if job["attempts"] >= job["max_attempts"]:
        return {"error": f"Job {job_id} already used {job['attempts']}/{job['max_attempts']} attempts."}
    execute(
        """UPDATE fb_jobs SET status = 'queued', stage = 'requeued',
                              attempts = attempts + 1, error_message = NULL,
                              cancel_requested = 0, updated_at = ?
           WHERE job_id = ?""",
        (_now(), job_id),
    )
    return {"job": get_job(job_id)}


def cancel_job(job_id: str) -> dict[str, Any]:
    """Cancel a job. Returns {"job": row, "note": ...} or {"error": msg}.

    Queued jobs are cancelled immediately. Jobs already dispatched to GitHub
    Actions cannot be recalled remotely, so they are flagged cancel_requested:
    the runner may still finish, but completion is recorded as cancelled.
    """
    job = get_job(job_id)
    if not job:
        return {"error": f"Job {job_id} not found."}
    if job["status"] in TERMINAL_STATUSES:
        return {"error": f"Job {job_id} is already {job['status']}."}
    if job["status"] == "queued":
        execute(
            """UPDATE fb_jobs SET status = 'cancelled', stage = 'cancelled_before_dispatch',
                                  updated_at = ? WHERE job_id = ?""",
            (_now(), job_id),
        )
        return {"job": get_job(job_id),
                "note": "Cancelled before dispatch. Nothing was uploaded."}
    execute(
        """UPDATE fb_jobs SET status = 'cancelled', stage = 'cancel_requested_after_dispatch',
                              cancel_requested = 1, updated_at = ?
           WHERE job_id = ?""",
        (_now(), job_id),
    )
    return {"job": get_job(job_id),
            "note": ("Cancel requested. The GitHub Actions run may still finish, "
                     "but its result will be recorded as cancelled.")}


def list_recent_jobs(limit: int = 10) -> list[dict[str, Any]]:
    return fetchall(
        "SELECT * FROM fb_jobs ORDER BY id DESC LIMIT ?",
        (limit,),
    )


# ── Display ──────────────────────────────────────────────────────────────────

STATUS_ICONS = {
    "queued": "🕓",
    "dispatched": "📤",
    "downloading": "⬇️",
    "processing": "🎬",
    "uploading": "📤",
    "completed": "✅",
    "failed": "❌",
    "cancelled": "🚫",
}


def format_job_status(job: dict[str, Any]) -> str:
    icon = STATUS_ICONS.get(job["status"], "❓")
    lines = [
        f"{icon} Job {job['job_id']}",
        f"Status: {job['status']}",
    ]
    if job.get("stage"):
        lines.append(f"Stage: {job['stage']}")
    lines.append(f"URL: {job['facebook_url'][:80]}")
    if job.get("trim_start") is not None and job.get("trim_end") is not None:
        lines.append(
            f"✂️ Trim: {format_timestamp(job['trim_start'])}-"
            f"{format_timestamp(job['trim_end'])}"
        )
    lines.append(f"Attempts: {job['attempts']}/{job['max_attempts']}")
    if job.get("youtube_video_id"):
        lines.append(f"🎬 https://www.youtube.com/watch?v={job['youtube_video_id']}")
    if job.get("error_message"):
        lines.append(f"Error: {job['error_message'][:200]}")
    lines.append(f"Updated: {(job.get('updated_at') or '?')[:16]}")
    return "\n".join(lines)
