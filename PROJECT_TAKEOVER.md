# 🎛️ Project Takeover — YouTube Song Bot

**Taken over:** 2026-09-25 · **Branch:** `arena/01a0da9a-youtube-song-bot`
**Base:** `feature/agent-reach-automation-v1` @ `3836bd3`
**Takeover commit health:** `python3 scripts/smoke_test.py` → 19/19 ✅ · `py_compile` all files ✅

---

## 1. What this project is

A Telegram-controlled **YouTube music-video factory**:

1. User sends a song (YouTube link or audio file) + outro video via Telegram.
2. Bot downloads audio → generates a 1280×720 thumbnail → renders a 1080p
   still-image video → appends the outro → uploads to YouTube (Music category).
3. Heavy work (download/render/upload) is **offloaded to GitHub Actions** so the
   Render free-tier bot (512 MB) never runs out of memory.

Two operating modes: **manual** (`/new`, `/bulk_upload`, `/agentreach`,
approve-buttons) and **automatic** (trend discovery → scored queue → auto upload).

---

## 2. Architecture (as built)

```
Telegram ──webhook──▶ Flask (telegram_bot.py on Render)
                          ├─ Trend Engine      agents/viral_trend_engine.py
                          ├─ Queue Engine      agents/queue_engine.py  ← LIVE path
                          ├─ Queue Manager     agents/queue_manager.py ← legacy, see §6
                          ├─ Outro Manager     agents/outro_manager.py
                          ├─ DB layer          agents/db.py (PostgreSQL ⇄ SQLite)
                          └─ Background processor ──repository_dispatch──▶ GitHub Actions
                                                                                │
                                                 github_worker.py ◀─────────────┘
                                                  ├─ downloaders.py (Cobalt API → yt-dlp)
                                                  ├─ bot.py (thumbnail / ffmpeg render / SEO)
                                                  └─ YouTube Data API v3 upload + Telegram notify
```

**Persistence:** PostgreSQL (Supabase pooler, IPv4 `:6543`) when `DATABASE_URL`
is set, else ephemeral SQLite (`storage/trends.db`, wiped on Render redeploy).
10 tables — see `scripts/schema_postgres.sql` + `docs/V2_DATABASE_SCHEMA.md`.

**Audio chain:** Cobalt API → yt-dlp fallback (`YOUTUBE_COOKIES` Netscape format).
Worker runs its own local Cobalt Docker container in CI.

---

## 3. File map

| Path | Lines | Role |
|------|-------|------|
| `telegram_bot.py` | ~3,250 | Flask + all Telegram handlers, OAuth, dashboard, scheduler |
| `bot.py` | ~750 | Thumbnail gen, ffmpeg render, SEO metadata, CLI `create-upload` |
| `github_worker.py` | ~320 | CI worker: download → render → upload → notify |
| `downloaders.py` | ~320 | Cobalt + yt-dlp abstraction |
| `agents/db.py` | ~460 | PG/SQLite abstraction, DDL, migrations helpers |
| `agents/queue_engine.py` | ~410 | Queue CRUD, transactional batch, background processor |
| `agents/queue_manager.py` | ~230 | ⚠️ Legacy duplicate of queue engine (only `/diag` uses it) |
| `agents/outro_manager.py` | ~170 | Weighted, no-repeat outro rotation |
| `agents/viral_trend_engine.py` | ~550 | Multi-source trend discovery + scoring |
| `agents/simple_trend*.py` | — | ⚠️ Dead code (no imports anywhere) |
| `scripts/smoke_test.py` | — | **NEW:** stdlib-only health check, no deps/network |
| `scripts/migrate_to_postgres.py` | — | One-shot SQLite → PG migration |
| `test_pipeline.py` | — | Full pipeline test (needs deps + ffmpeg + network) |
| `.env.example` | — | **NEW:** documented env template |
| `.github/workflows/render-upload.yml` | — | CI: Cobalt container + `github_worker.py` |

---

## 4. Telegram commands & HTTP endpoints

**Commands:** `/start /id /auth /export_youtube_token /new /audio_retry /cancel`
`/bulk_upload /import_trends /agentreach /daily_report /trend_debug`
`/outro_add /outro_list /outro_remove /outro_test`
`/queue_status /queue_pause /queue_resume /queue_cancel`
`/worker_status /github_test /auto_mode`

**Endpoints:** `/ /diag /raw /verify /db_status /system_health /health`
`/dashboard /oauth2callback /toggle_queue /seed_trends /seed_outro`
`/test_dispatch /github_status /debug_* /create_session`
`/telegram/<WEBHOOK_SECRET>` (webhook receiver)

**Feature flags** (`feature_flags.py`): `TREND_AGENT, RECOMMENDATIONS,
APPROVE_WORKFLOW, BULK_UPLOAD, AGENTREACH_IMPORT, AUTO_MODE,
OUTRO_ROTATION_V2, DASHBOARD, SYSTEM_HEALTH` — all currently `True`.

---

## 5. Run it

```bash
cp .env.example .env        # fill in secrets, never commit
pip install -r requirements.txt
python3 scripts/smoke_test.py      # stdlib-only, always works
python3 telegram_bot.py            # needs TELEGRAM_BOT_TOKEN + BASE_URL
```

Full pipeline (needs ffmpeg + network + tokens): `python3 test_pipeline.py`

---

## 6. Known issues & tech debt (triaged)

| # | Issue | Severity | Status |
|---|-------|----------|--------|
| 1 | **Live secrets committed in `CHAT_TRANSFER_SUMMARY.md`** (PAT, Render token, bot token, DB password) | 🔴 CRITICAL | Redacted in tree; **rotation still required** (git history still has them) |
| 2 | `client_secrets.json` / `token.json` not in `.gitignore` | 🟠 High | ✅ Fixed (takeover) |
| 3 | `add_queue_item()` allowed duplicate in-queue URLs (batch paths rejected them) | 🟡 Medium | ✅ Fixed + smoke-tested (takeover) |
| 4 | `queue_manager.py` vs `queue_engine.py` duplication; `/diag` used legacy module | 🟡 Medium | ✅ Fixed (stabilize): `/diag` → `queue_engine`, legacy module is now a deprecated shim |
| 5 | `agents/simple_trend*.py` dead code | 🟢 Low | ✅ Fixed (stabilize): deleted, zero refs |
| 6 | `/diag` read only `viral_trends`, importers write `trends` → count mismatch (from QA report) | 🟡 Medium | ✅ Fixed (stabilize): `/diag` reports **both** tables (`trends` + `trends_imported`) |
| 7 | `DATABASE_URL` read at import time (hard to reconfigure in tests/long-running) | 🟢 Low | Open |
| 8 | Live Render reachability unverified this session (sandbox has no egress) | 🟡 Medium | Open — run §10 checklist from your network |
| 9 | Queue processor imported `generate_seo_metadata` from `viral_trend_engine` (doesn't exist there) → **ImportError on first real queue item** | 🔴 CRITICAL | ✅ Fixed (stabilize): imports from `bot.py`; regression check in smoke test |
| 10 | Orphaned `return {"outros": ...}` block inside `refresh_report_callback` → **NameError on every 🔄 Refresh click** | 🔴 CRITICAL | ✅ Fixed (stabilize): deleted; refresh shares `_build_daily_report_view()` with `/daily_report` |
| 11 | Refresh rebuilt message with dead `approve_song` button (no handler registered) + different table than `/daily_report` | 🟡 Medium | ✅ Fixed (stabilize): unified view, per-trend `approve_trend_<id>` buttons |
| 12 | No CI gate — broken imports/NameErrors reached production undetected | 🟡 Medium | ✅ Fixed (stabilize): `.github/workflows/lint.yml` (`py_compile` + smoke test on every push) |

---

## 7. 🔴 Security — action required

Secrets were committed to git history. Redaction in the working tree is **not
enough** if the branch was ever pushed. Rotate now, in this order:

1. **Telegram:** @BotFather → `/revoke` → update `TELEGRAM_BOT_TOKEN`
   (Render env + GitHub secret) → redeploy.
2. **GitHub PAT** in `GITHUB_TOKEN`: regenerate → update Render env.
3. **Render API token:** regenerate in dashboard.
4. **Supabase:** reset DB password → update `DATABASE_URL` (Render env).
   Keep using the **pooler** URL (`aws-*-pooler.supabase.com:6543`, IPv4).
5. **Webhook secret:** change `WEBHOOK_SECRET` → redeploy → bot re-registers webhook.
6. **YouTube OAuth:** if `YOUTUBE_TOKEN_JSON` was ever pasted anywhere public,
   re-run `/auth` + `/export_youtube_token` and update the GitHub secret.
7. Optional hardening: purge the file from history (`git filter-repo`) or
   accept rotation as sufficient (recommended: rotation is enough once done).

---

## 8. Roadmap options (pick a direction)

- **A. Stabilize & verify:** live Render check, `/diag` table fix, queue-module
  consolidation, dead-code removal, CI lint (`py_compile` gate).
- **B. Ship next features:** dashboard polish, approve-flow UX, auto-mode
  scheduler limits, trend-engine scoring upgrades.
- **C. Harden pipeline:** Cobalt self-host runbook, yt-dlp cookie refresh flow,
  token-expiry early warnings, upload-quota tracking.

Suggested default: **A first** (1 session), then B/C.

---

## 9. Session conventions (from repo history)

- Hindi-English mix is fine; user wants **raw evidence** (logs, rows, statuses),
  never placeholders or synthetic data.
- New features behind **feature flags**; never destroy production data.
- One working branch: `arena/01a0da9a-youtube-song-bot`.
