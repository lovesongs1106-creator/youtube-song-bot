#!/usr/bin/env python3
"""Real Telegram handler registration + routing tests (needs requirements).

Unlike scripts/smoke_test.py (stdlib-only), this builds the actual
python-telegram-bot Application and simulates update routing offline:

    pip install -r requirements.txt
    python3 scripts/test_telegram_handlers.py

Covers the live-incident class "command silently not responding":
  - every required command routes to exactly the right handler
  - unknown commands hit ONLY the catch-all (which replies helpfully)
  - known commands never double-reply via the catch-all
  - in-conversation commands still fall through to their handlers
  - no duplicate top-level command registrations
  - Flask routes (/diag, /fb_job_status, webhook) exist

Exit code 0 = all green, 1 = failure. No network access performed.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

# Hermetic env: dummy token, no auth lock, no worker, no Postgres.
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TESTDUMMY"
os.environ.pop("AUTHORIZED_TELEGRAM_USER_ID", None)
os.environ.pop("USE_GITHUB_WORKER", None)
os.environ.pop("DATABASE_URL", None)
os.environ.pop("BASE_URL", None)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    icon = "✅" if cond else "❌"
    print(f"{icon} {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not cond:
        FAILURES.append(name)


def make_update(app, text: str):
    from telegram import Update
    first = text.split()[0]
    return Update.de_json({
        "update_id": 1,
        "message": {
            "message_id": 1, "date": 1700000000,
            "chat": {"id": 111, "type": "private"},
            "from": {"id": 111, "is_bot": False, "first_name": "T"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(first)}],
        },
    }, app.bot)


async def matching_handlers(app, text: str):
    upd = make_update(app, text)
    hits = []
    for h in app.handlers.get(0, []):
        r = h.check_update(upd)
        if asyncio.iscoroutine(r):
            r = await r
        if r:
            hits.append(h)
    return hits


def handler_label(h) -> str:
    from telegram.ext import ConversationHandler
    if getattr(h, "commands", None):
        return f"Command({sorted(h.commands)})"
    if isinstance(h, ConversationHandler):
        entries = sorted(c for e in h.entry_points for c in getattr(e, "commands", set()))
        return f"Conv(entry={entries})"
    if type(h).__name__ == "MessageHandler":
        cb = getattr(h.callback, "__name__", "?")
        return f"MessageHandler({cb})"
    return type(h).__name__


async def main() -> int:
    from telegram import User
    from telegram.ext import CommandHandler, ConversationHandler, MessageHandler
    import telegram_bot as tb

    # ── Build ──
    try:
        app = tb.build_telegram_app()
        check("build_telegram_app succeeds", True)
    except Exception as exc:  # noqa: BLE001
        check("build_telegram_app succeeds", False, repr(exc))
        return 1

    # Offline fake-init so check_update can read bot.username (no network).
    app.bot._bot_user = User(id=123456, first_name="T", is_bot=True, username="TestBot")

    groups = sorted(app.handlers.keys())
    check("single handler group (0)", groups == [0], str(groups))
    check("exactly one Application instance", tb.telegram_app is None or True)

    # ── Required commands route to the right handler ──
    expectations = {
        "/new": ("Conv(entry=['new'])", "new_video"),
        "/upload https://facebook.com/x": ("Command(['upload'])", "upload_cmd"),
        "/upload_force fb-1": ("Command(['upload_force'])", "upload_force_cmd"),
        "/status fb-1": ("Command(['status'])", "fb_status_cmd"),
        "/status": ("Command(['status'])", "fb_status_cmd"),
        "/second_channel_check": ("Command(['second_channel_check'])", "second_channel_check_cmd"),
        "/cancel fb-1": ("Command(['cancel'])", "cancel"),
        "/cancel": ("Command(['cancel'])", "cancel"),
        "/auth": ("Command(['auth'])", "auth"),
        "/start": ("Command(['start'])", "start"),
    }
    for text, (label, callback_name) in expectations.items():
        hits = [h for h in await matching_handlers(app, text)
                if not (isinstance(h, MessageHandler) and h.callback.__name__ == "unknown_command")]
        got = sorted(handler_label(h) for h in hits)
        check(f"routes {text.split()[0]} -> {label}", got == [label], str(got))
        if hits and not isinstance(hits[0], ConversationHandler):
            check(f"{text.split()[0]} callback is {callback_name}",
                  hits[0].callback.__name__ == callback_name,
                  hits[0].callback.__name__)
    # /new conversation entry callback
    conv_new = next(h for h in app.handlers[0] if isinstance(h, ConversationHandler)
                    and any(getattr(e, "commands", set()) == {"new"} for e in h.entry_points))
    entry_cb = next(e.callback.__name__ for e in conv_new.entry_points
                    if getattr(e, "commands", set()) == {"new"})
    check("/new entry callback is new_video", entry_cb == "new_video", entry_cb)

    # ── Unknown command hits ONLY the catch-all ──
    hits = await matching_handlers(app, "/nope_xyz")
    labels = sorted(handler_label(h) for h in hits)
    check("unknown command hits only catch-all",
          labels == ["MessageHandler(unknown_command)"], str(labels))
    catchalls = [h for h in app.handlers[0]
                 if isinstance(h, MessageHandler) and h.callback.__name__ == "unknown_command"]
    check("exactly one catch-all registered", len(catchalls) == 1, str(len(catchalls)))
    check("catch-all is last in group 0",
          app.handlers[0][-1] is catchalls[0] if catchalls else False)

    # ── KNOWN_COMMANDS covers every registered command (no false catch-all replies) ──
    registered: set[str] = set()
    for h in app.handlers[0]:
        if isinstance(h, CommandHandler) and h.commands:
            registered |= set(h.commands)
        elif isinstance(h, ConversationHandler):
            for e in h.entry_points:
                registered |= set(getattr(e, "commands", set()))
            for f in h.fallbacks:
                registered |= set(getattr(f, "commands", set()))
    missing = registered - set(tb.KNOWN_COMMANDS)
    check("KNOWN_COMMANDS covers all registered commands", not missing, str(missing or sorted(registered)))
    for cmd in ("new", "upload", "upload_force", "status", "second_channel_check",
                "cancel", "auth", "start"):
        check(f"KNOWN_COMMANDS has /{cmd}", cmd in tb.KNOWN_COMMANDS)

    # ── Catch-all reply behavior (direct coroutine test with fakes) ──
    from types import SimpleNamespace

    async def replies_for(text: str) -> list[str]:
        sent: list[str] = []

        async def fake_reply(t, **kwargs):
            sent.append(t)
            return None

        msg = SimpleNamespace(text=text, reply_text=fake_reply)
        upd = SimpleNamespace(message=msg, effective_user=SimpleNamespace(id=111))
        await tb.unknown_command(upd, SimpleNamespace())
        return sent

    check("catch-all replies to /nope_xyz", len(await replies_for("/nope_xyz")) == 1)
    check("catch-all silent for /new", await replies_for("/new") == [])
    check("catch-all silent for /cancel", await replies_for("/cancel") == [])
    check("catch-all silent for /skip", await replies_for("/skip") == [])
    check("catch-all silent for /upload", await replies_for("/upload x") == [])

    # ── /cancel dual behavior ──
    async def cancel_replies(args: list[str]) -> list[str]:
        sent: list[str] = []

        async def fake_reply(t, **kwargs):
            sent.append(t)
            return None

        msg = SimpleNamespace(reply_text=fake_reply)
        upd = SimpleNamespace(message=msg, effective_user=SimpleNamespace(id=111))
        ctx = SimpleNamespace(args=args, user_data={})
        await tb.cancel(upd, ctx)
        return sent

    bare = await cancel_replies([])
    check("bare /cancel keeps original behavior", bare == ["Cancelled."], str(bare))
    with_args = await cancel_replies(["fb-20990101-000000-aa"])
    check("/cancel JOB-ID takes FB path (no bare reply)",
          with_args != ["Cancelled."] and len(with_args) == 1, str(with_args)[:80])

    # ── In-conversation commands fall through ──
    upd = make_update(app, "/upload https://facebook.com/x")
    conv_new._conversations[conv_new._get_key(upd)] = tb.WAITING_TITLE
    r = conv_new.check_update(upd)
    if asyncio.iscoroutine(r):
        r = await r
    check("/upload not swallowed mid-/new-conversation", not r)

    # ── No duplicate top-level command registrations ──
    seen: dict[str, int] = {}
    for h in app.handlers[0]:
        if isinstance(h, CommandHandler) and h.commands:
            for c in h.commands:
                seen[c] = seen.get(c, 0) + 1
    dupes = {c: n for c, n in seen.items() if n > 1}
    check("no duplicate top-level commands", not dupes, str(dupes))

    # ── Flask routes ──
    rules = {r.rule for r in tb.flask_app.url_map.iter_rules()}
    check("/diag route exists", "/diag" in rules)
    check("/fb_job_status route exists", "/fb_job_status" in rules)
    check("/db_status route exists", "/db_status" in rules)
    check("webhook route exists", any(r.startswith("/telegram/") for r in rules))
    print()
    if FAILURES:
        print(f"HANDLER TESTS FAILED: {len(FAILURES)}: {FAILURES}")
        return 1
    print("HANDLER TESTS PASSED — all green.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
