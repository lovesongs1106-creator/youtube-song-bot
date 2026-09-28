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

    # ── Stabilization regression checks (source-level, stdlib-only) ──
    import ast

    engine_src = (ROOT / "agents" / "queue_engine.py").read_text(encoding="utf-8")
    check(
        "queue_engine imports SEO metadata from bot.py",
        "from bot import generate_seo_metadata" in engine_src
        and "viral_trend_engine import generate_seo_metadata" not in engine_src,
    )

    bot_src = (ROOT / "telegram_bot.py").read_text(encoding="utf-8")
    tree = ast.parse(bot_src)
    fn_names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    check("_build_daily_report_view helper exists", "_build_daily_report_view" in fn_names)
    refresh_fn = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "refresh_report_callback"),
        None,
    )
    refresh_src = ast.get_source_segment(bot_src, refresh_fn) if refresh_fn else ""
    check(
        "refresh handler exists and uses shared report view",
        refresh_fn is not None and "_build_daily_report_view" in refresh_src,
    )
    check(
        "refresh handler has no orphaned return block",
        refresh_fn is not None and '"outros": outros' not in refresh_src,
    )
    check(
        "no dead approve_song callback button",
        'callback_data="approve_song"' not in bot_src and "callback_data='approve_song'" not in bot_src,
    )
    check(
        "/diag uses queue_engine (not legacy queue_manager)",
        "from agents.queue_engine import get_summary, is_paused" in bot_src,
    )

    # Legacy shim still importable and delegates to queue_engine
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import agents.queue_manager as qm
        import agents.queue_engine as qe
    check(
        "queue_manager shim delegates to queue_engine",
        qm.get_summary is qe.get_summary and qm.is_paused is qe.is_paused,
    )

    # ── Facebook job manager ──
    from agents import fb_jobs

    fb_urls = [
        "https://www.facebook.com/watch?v=123456789",
        "https://fb.watch/abcXYZ123/",
        "https://www.facebook.com/SomePage/videos/987654321",
        "https://www.facebook.com/reel/555666777?__cft__=x&fbclid=y",
    ]
    check(
        "extract_facebook_url finds FB links",
        all(fb_jobs.extract_facebook_url(u) for u in fb_urls),
    )
    check(
        "extract_facebook_url rejects non-FB links",
        fb_jobs.extract_facebook_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is None,
    )
    check(
        "normalize strips tracking params",
        fb_jobs.normalize_facebook_url(fb_urls[3])
        == "https://www.facebook.com/reel/555666777",
    )

    trim = fb_jobs.parse_trim_spec(f"/upload {fb_urls[0]} trim 02:15-38:42")
    check("parse_trim_spec MM:SS range", trim == (135.0, 2322.0), str(trim))
    trim_h = fb_jobs.parse_trim_spec("trim 1:02:03-2:00:00")
    check("parse_trim_spec HH:MM:SS range", trim_h == (3723.0, 7200.0), str(trim_h))
    check("parse_trim_spec absent -> None", fb_jobs.parse_trim_spec(fb_urls[0]) is None)
    for bad in ("trim 38:42-02:15", "trim abc", "trim 01:00"):
        try:
            fb_jobs.parse_trim_spec(bad)
            check(f"parse_trim_spec rejects '{bad}'", False)
        except ValueError:
            check(f"parse_trim_spec rejects '{bad}'", True)

    r1 = fb_jobs.create_job(111, 222, fb_urls[0], trim)
    check("fb create_job queued", "job" in r1 and r1["job"]["status"] == "queued")
    job_a = r1["job"]["job_id"]
    check("fb job_id format", job_a.startswith("fb-"), job_a)

    dup = fb_jobs.create_job(111, 222, fb_urls[0])
    check("fb duplicate URL blocked", "duplicate" in dup)

    rf = fb_jobs.create_job(111, 222, fb_urls[0], None, True)
    check("fb force bypasses duplicate", "job" in rf)
    job_b = rf["job"]["job_id"]

    fb_jobs.set_status(job_a, "completed", "done", youtube_video_id="vidABC")
    still_dup = fb_jobs.create_job(111, 222, fb_urls[0])
    check("fb completed URL still protected", "duplicate" in still_dup)

    c = fb_jobs.cancel_job(job_b)
    check("fb cancel queued job", "job" in c and c["job"]["status"] == "cancelled")

    rt = fb_jobs.retry_job(job_b)
    check("fb retry cancelled job", "job" in rt and rt["job"]["attempts"] == 2)
    fb_jobs.set_status(job_b, "dispatched", "queued_on_github")
    cd = fb_jobs.cancel_job(job_b)
    check(
        "fb cancel dispatched job flags request",
        "job" in cd and cd["job"]["cancel_requested"] == 1,
    )
    fb_jobs.set_status(job_b, "failed", "error", error="boom")
    rt2 = fb_jobs.retry_job(job_b)
    check("fb retry failed job", "job" in rt2 and rt2["job"]["attempts"] == 3)
    rt3 = fb_jobs.retry_job(job_b)
    check("fb retry respects max attempts", "error" in rt3)

    done_job = fb_jobs.get_job(job_a)
    text = fb_jobs.format_job_status(done_job)
    check("fb format_job_status", job_a in text and "vidABC" in text)

    # ── Facebook wiring (source-level: worker/bot need heavy deps) ──
    import ast as _ast

    gw_src = (ROOT / "github_worker.py").read_text(encoding="utf-8")
    gw_tree = _ast.parse(gw_src)
    gw_fns = {n.name for n in _ast.walk(gw_tree)
              if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))}
    for fn in ("run_facebook_job", "run_second_channel_check",
               "get_second_channel_service", "validate_second_channel_early",
               "report_fb_status", "cleanup_job_dir"):
        check(f"github_worker has {fn}", fn in gw_fns)
    check(
        "worker branches on second-channel payload",
        'target_channel") == "second"' in gw_src and 'facebook_url"' in gw_src,
    )
    check(
        "upload_to_youtube keeps default first-channel behavior",
        "service=None" in gw_src and "service if service is not None else get_youtube_service()" in gw_src,
    )
    check(
        "worker never mixes channel credentials",
        "YOUTUBE_TOKEN_JSON" in gw_src and "YOUTUBE_SECOND_REFRESH_TOKEN" in gw_src,
    )

    tb_src = (ROOT / "telegram_bot.py").read_text(encoding="utf-8")
    tb_tree = _ast.parse(tb_src)
    tb_fns = {n.name for n in _ast.walk(tb_tree)
              if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))}
    for fn in ("upload_cmd", "upload_force_cmd", "fb_status_cmd",
               "fb_cancel_cmd", "second_channel_check_cmd"):
        check(f"telegram_bot has {fn}", fn in tb_fns)
    check("telegram_bot has /fb_job_status route", '"/fb_job_status"' in tb_src)
    for cmd in ('CommandHandler("upload"', 'CommandHandler("upload_force"',
                'CommandHandler("status"', 'CommandHandler("second_channel_check"'):
        check(f"telegram_bot registers {cmd[15:-1]}", cmd in tb_src)

    import feature_flags

    check("ENABLE_FACEBOOK_SECOND_CHANNEL on",
          getattr(feature_flags, "ENABLE_FACEBOOK_SECOND_CHANNEL", False) is True)

    # Access-policy compliance: pipeline must not contain auth bypasses.
    # (Scan code string literals, not comments/docstrings that document the ban.)
    fp_src = (ROOT / "facebook_pipeline.py").read_text(encoding="utf-8")
    fp_tree = _ast.parse(fp_src)
    fp_strings = [
        n.value for n in _ast.walk(fp_tree)
        if isinstance(n, _ast.Constant) and isinstance(n.value, str)
    ]
    check(
        "fb pipeline passes no cookies/credentials",
        not any(s in ("--cookies", "--username", "--password",
                      "--cookiefile", "--netrc")
                for s in fp_strings),
    )
    check(
        "fb pipeline refuses gated content",
        "ACCESS_DENIED_MARKERS" in fp_src and "login required" in fp_src,
    )
    check(
        "fb pipeline honors OUTRO_ASSET_PATH",
        'OUTRO_ASSET_PATH' in fp_src and 'assets/outro.mp4' in fp_src,
    )
    resolve_fn = next(
        (n for n in _ast.walk(fp_tree) if isinstance(n, _ast.FunctionDef)
         and n.name == "resolve_outro_asset"),
        None,
    )
    resolve_src = _ast.get_source_segment(fp_src, resolve_fn) if resolve_fn else ""
    check(
        "resolve_outro_asset reads env with default + repo-root join",
        resolve_fn is not None
        and 'os.environ.get("OUTRO_ASSET_PATH"' in resolve_src
        and "OUTRO_ASSET_DEFAULT" in resolve_src
        and "__file__" in resolve_src,
    )
    outro_asset = ROOT / "assets" / "outro.mp4"
    check(
        "assets/outro.mp4 exists and non-empty",
        outro_asset.is_file() and outro_asset.stat().st_size > 0,
        f"{outro_asset.stat().st_size} bytes" if outro_asset.is_file() else "missing",
    )
    check(
        "no stray root outro.mp4",
        not (ROOT / "outro.mp4").exists(),
    )

    wf_src = (ROOT / ".github" / "workflows" / "render-upload.yml").read_text(encoding="utf-8")
    for ref in ("secrets.YOUTUBE_SECOND_CLIENT_ID",
                "secrets.YOUTUBE_SECOND_CLIENT_SECRET",
                "secrets.YOUTUBE_SECOND_REFRESH_TOKEN",
                "vars.YOUTUBE_TARGET_CHANNEL",
                "vars.YOUTUBE_VISIBILITY",
                "vars.OUTRO_ASSET_PATH"):
        check(f"workflow references {ref}", ref in wf_src)

    print()
    if FAILURES:
        print(f"SMOKE TEST FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("SMOKE TEST PASSED — all checks green.")
    return 0


ROOT = __import__("pathlib").Path(__file__).parent.parent.resolve()

if __name__ == "__main__":
    sys.exit(main())
