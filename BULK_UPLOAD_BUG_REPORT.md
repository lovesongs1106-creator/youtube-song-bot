# Bulk Upload Production Bug Report
**Date:** 2026-07-14  
**Branch:** `feature/agent-reach-automation-v1`  
**Commit:** `2ded092` (LIVE)  
**Previous failed:** `14af24f` (update_failed)

---

## 1. Bug Report Summary

| # | Bug | Severity | Status |
|---|-----|----------|--------|
| 1 | Missing CommandHandler for `/bulk_upload` | **CRITICAL** | Fixed |
| 2 | Missing CallbackQueryHandler for bulk buttons | **CRITICAL** | Fixed |
| 3 | Webhook mode: `context.user_data` doesn't persist | **CRITICAL** | Fixed |
| 4 | `InlineKeyboardButton/InlineKeyboardMarkup` imported too late | **MEDIUM** | Fixed |
| 5 | `ENABLE_BULK_UPLOAD` not imported → deploy crash | **CRITICAL** | Fixed |

---

## 2. Root Cause Analysis

### Bug 1: Missing CommandHandler Registration
**Evidence:**
```bash
$ grep -n 'bulk_upload' telegram_bot.py | grep CommandHandler
# NO OUTPUT — handler was NEVER registered
```

The `bulk_upload()` function existed but `CommandHandler("bulk_upload", bulk_upload)` was never added to `build_telegram_app()`. So `/bulk_upload` command was silently ignored by the bot.

### Bug 2: Missing CallbackQueryHandler Registration
**Evidence:**
```bash
$ grep -n 'bulk_confirm\|bulk_cancel' telegram_bot.py | grep CallbackQueryHandler
# NO OUTPUT — buttons had no handlers
```

`bulk_confirm_callback` and `bulk_cancel_callback` existed but were never registered as `CallbackQueryHandler`s. So even if the user saw buttons, clicking them would do nothing.

### Bug 3: Webhook Mode — context.user_data Not Persistent
**Evidence:**
```python
# OLD CODE (broken in webhook):
async def bulk_upload(update, context):
    context.user_data["awaiting_bulk"] = True  # Stored in memory

async def handle_bulk_text(update, context):
    if not context.user_data.get("awaiting_bulk"):  # NEW request = EMPTY dict
        return  # Silently exits
```

In webhook mode, each Telegram message is a separate HTTP POST. Each POST creates a fresh `ContextTypes.DEFAULT_TYPE` with an empty `user_data` dict. So `awaiting_bulk` was always `False` when the song list arrived.

**Fix:** Store awaiting flag in `system_state` DB table:
```python
execute("INSERT INTO system_state (key, value) VALUES (?, ?)",
        (f"bulk_awaiting_{user_id}", "true"))
```

### Bug 4: InlineKeyboard Imports at Wrong Location
**Evidence:**
```python
# Line 1965 (AFTER bulk_upload code at line 458):
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
```

While Python resolves this at runtime (function bodies aren't executed at import), it's fragile. Moved to top-level import block.

### Bug 5: Missing ENABLE_BULK_UPLOAD Import → Deploy Crash
**Evidence:**
```python
# build_telegram_app() line 2289:
if ENABLE_BULK_UPLOAD:  # NameError! Not imported.
```

`ENABLE_BULK_UPLOAD` was used in `build_telegram_app()` but never imported from `feature_flags.py`. This caused:
```
NameError: name 'ENABLE_BULK_UPLOAD' is not defined
→ Process exited with code 1
→ Render deploy status: update_failed
```

**Render deploy evidence:**
```json
{
  "status": "update_failed",
  "details": {
    "reason": {
      "failure": {
        "nonZeroExit": 1
      }
    }
  }
}
```

---

## 3. Fixes Applied

### File: `telegram_bot.py`

**Change 1:** Add missing imports at top
```python
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ...,
    CallbackQueryHandler,  # Added
)
from feature_flags import (
    ...,
    ENABLE_BULK_UPLOAD,  # Added — fixes deploy crash
)
```

**Change 2:** Add DB-backed session storage
```python
def _store_bulk_session(user_id, candidates, invalid) -> str:
    # Stores in system_state table (webhook-safe)

def _load_bulk_session(user_id, session_id) -> dict | None:
    # Retrieves + deletes from system_state

def _parse_bulk_lines(lines) -> tuple[list, list]:
    # Pure parser, no side effects
```

**Change 3:** Rewrite `bulk_upload()` — webhook-safe
- Sets `bulk_awaiting_{user_id}` flag in DB
- Logs: `[BULK_UPLOAD_START]`

**Change 4:** Rewrite `handle_bulk_text()` — webhook-safe
- Reads `bulk_awaiting_{user_id}` from DB
- Parses lines, stores session in DB
- Logs: `[BULK_UPLOAD_RECEIVED]`, `[BULK_UPLOAD_PARSED]`

**Change 5:** Rewrite `bulk_confirm_callback()` — webhook-safe
- Loads session from DB via `_load_bulk_session()`
- Calls `add_multiple_items()` to insert into queue
- Logs: `[BULK_UPLOAD_QUEUE_INSERT]`

**Change 6:** Rewrite `bulk_cancel_callback()` — webhook-safe
- Loads + deletes session from DB
- Logs: `[BULK_UPLOAD_CANCEL]`

**Change 7:** Register all missing handlers in `build_telegram_app()`
```python
app.add_handler(CommandHandler("bulk_upload", bulk_upload))
if ENABLE_BULK_UPLOAD:
    app.add_handler(CallbackQueryHandler(bulk_confirm_callback, pattern=r"^bulk_confirm_"))
    app.add_handler(CallbackQueryHandler(bulk_cancel_callback, pattern=r"^bulk_cancel_"))
```

---

## 4. Deploy History

| Commit | Status | Notes |
|--------|--------|-------|
| `8e60b8f` | deactivated | Baseline |
| `c5fdfc5` | deactivated | PostgreSQL migration code |
| `ea61a07` | deactivated | SQLite cleanup in /verify |
| `14af24f` | **update_failed** | Bulk fix — missing ENABLE_BULK_UPLOAD import |
| `2ded092` | **live** ✅ | Added missing import, deploy successful |

**Live verification:**
```bash
$ curl -s https://youtube-song-bot.onrender.com/diag
{
  "commit": "2ded092",
  "status": "ok"
}
```

---

## 5. Local Test Evidence

### Parser Test
```
Parsed: valid=3, invalid=0
  - Song A -> https://www.youtube.com/watch?v=abc123
  - Song B -> https://www.youtube.com/watch?v=def456
  - Song C -> https://www.youtube.com/watch?v=ghi789
```

### DB Session Test
```
Session stored: lgVYg0u6X0kYLwLkh8CLng
Session loaded: candidates=3
Awaiting flag: {'value': 'true'}
All tests passed
```

### App Build Test
```
Module loaded
App built OK
```

---

## 6. Next Step: Production Telegram Test

Ab tum Telegram pe test kar sakte ho:

1. `/bulk_upload` bhejo
2. Bot reply karega: "📥 Bulk Upload Mode..."
3. Song list bhejo:
   ```
   Song A | https://www.youtube.com/watch?v=abc123
   Song B | https://www.youtube.com/watch?v=def456
   ```
4. Bot reply karega: "📊 Bulk Upload Summary" with Confirm/Cancel buttons
5. ✅ Confirm pe click karo
6. `/queue_status` bhejo → Pending count should be > 0

Agar koi problem aaye toh Render logs mein ye search karna:
```
[BULK_UPLOAD_START]
[BULK_UPLOAD_RECEIVED]
[BULK_UPLOAD_PARSED]
[BULK_UPLOAD_QUEUE_INSERT]
```

---

**Deploy:** `2ded092` is LIVE on Render  
**Service:** https://youtube-song-bot.onrender.com  
**Status:** `ok`
