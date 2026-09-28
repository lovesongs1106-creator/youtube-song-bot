# V2 Migration Plan

## Current State (Post-V1 Implementation)

### Existing Tables
```
viral_trends        — created by agents/viral_trend_engine.py
upload_queue        — created by agents/queue_manager.py
outros              — created by agents/outro_manager.py
outro_history       — created by agents/outro_manager.py
queue_history       — created by agents/queue_manager.py
queue_state         — created by agents/queue_manager.py
```

### Existing Files
```
telegram_bot.py     — has /trend_debug, /daily_report, /outro_*, /queue_*, /bulk_upload
github_worker.py    — handles outro_file_id, outro_ext
agents/viral_trend_engine.py
agents/outro_manager.py
agents/queue_manager.py
```

## Target State (V2 Production)

### New/Modified Tables
```
viral_trends        — ADD growth_score, competition_score
upload_queue        — ADD source column, ADD started_at
outros              — ADD file_unique_id
outro_history       — KEEP as-is
queue_history       — KEEP as-is
system_state        — RENAME from queue_state
uploads             — NEW: duplicate protection table
auto_mode_config    — NEW: auto upload settings
```

### New/Modified Files
```
agents/trend_engine.py      — RENAME from viral_trend_engine.py
agents/queue_engine.py      — RENAME from queue_manager.py + duplicate check
agents/outro_manager.py     — ADD file_unique_id, conversation flow support
agents/dashboard.py         — NEW
agents/health.py            — NEW
agents/auto_mode.py         — NEW: scheduler
telegram_bot.py             — ADD /agentreach, /dashboard, /system_health, Approve #1-5, outro conversation
.github/workflows/          — ADD error handling steps
.gitignore                  — NEW: exclude storage/
```

## Migration Steps

### Phase 1: Schema Migration (Zero Downtime)

```sql
-- Step 1.1: Add columns to viral_trends
ALTER TABLE viral_trends ADD COLUMN growth_score REAL DEFAULT 0;
ALTER TABLE viral_trends ADD COLUMN competition_score REAL DEFAULT 0;

-- Step 1.2: Add columns to upload_queue
ALTER TABLE upload_queue ADD COLUMN source TEXT DEFAULT 'manual';
ALTER TABLE upload_queue ADD COLUMN started_at TEXT;

-- Step 1.3: Add file_unique_id to outros
ALTER TABLE outros ADD COLUMN file_unique_id TEXT UNIQUE;

-- Step 1.4: Rename queue_state → system_state
CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT);
INSERT INTO system_state SELECT * FROM queue_state;
DROP TABLE queue_state;

-- Step 1.5: Create uploads table (MANDATORY duplicate protection)
CREATE TABLE uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id INTEGER,
    song_name TEXT NOT NULL,
    youtube_url TEXT NOT NULL UNIQUE,
    youtube_video_id TEXT NOT NULL,
    source TEXT DEFAULT 'manual',
    uploaded_at TEXT NOT NULL
);
CREATE INDEX idx_uploads_url ON uploads(youtube_url);
CREATE INDEX idx_uploads_time ON uploads(uploaded_at DESC);

-- Step 1.6: Create auto_mode_config table
CREATE TABLE auto_mode_config (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    enabled INTEGER DEFAULT 0,
    uploads_per_day INTEGER DEFAULT 3 CHECK(uploads_per_day >= 1 AND uploads_per_day <= 10),
    start_time TEXT DEFAULT '06:00',
    last_run TEXT
);
INSERT INTO auto_mode_config (id, enabled, uploads_per_day, start_time) VALUES (1, 0, 3, '06:00');

-- Step 1.7: Create indexes
CREATE INDEX idx_queue_source ON upload_queue(source);
CREATE INDEX idx_outros_active ON outros(is_active);
```

### Phase 2: Code Migration

| Step | Action | Risk | Verification |
|------|--------|------|-------------|
| 2.1 | Create `.gitignore` with `storage/` | Low | `git status` shows untracked |
| 2.2 | Create `agents/dashboard.py` | Low | `/dashboard` returns data |
| 2.3 | Create `agents/health.py` | Low | `/system_health` returns report |
| 2.4 | Create `agents/auto_mode.py` | Low | Config table accessible |
| 2.5 | Update `agents/outro_manager.py` | Medium | Conversation flow works |
| 2.6 | Update `agents/queue_engine.py` | Medium | Duplicate check works |
| 2.7 | Update `agents/trend_engine.py` | Low | New columns populated |
| 2.8 | Update `telegram_bot.py` | High | All commands registered |
| 2.9 | Update `github_worker.py` | Medium | Records in uploads table |

### Phase 3: Deployment

```
Step 3.1: Commit all changes to feature/v2-production branch
Step 3.2: Push to GitHub
Step 3.3: Render auto-deploys (verify /diag shows new commit)
Step 3.4: Run /system_health to verify all services
Step 3.5: Run /trend_debug to verify trend engine
Step 3.6: Run /dashboard to verify dashboard
Step 3.7: Test /agentreach with 2-3 songs
Step 3.8: Verify duplicate protection (try same URL twice)
Step 3.9: Test /outro_add conversation flow
Step 3.10: Test Approve #1-5 buttons
Step 3.11: Test /queue_pause and /queue_resume
Step 3.12: Verify queue survives restart
```

### Phase 4: Persistent Storage Setup

**CRITICAL:** Must complete before production use.

**Option A: Supabase PostgreSQL (Recommended)**
```bash
# 1. Create Supabase project (free tier)
# 2. Get connection string from dashboard
# 3. Add to Render environment variables:
DATABASE_URL="postgresql://user:pass@host:5432/db"

# 4. Run migration SQL in Supabase SQL Editor
# 5. Update code to use psycopg2 or asyncpg
# 6. Test connection with /system_health
```

**Option B: Render Persistent Disk**
```yaml
# render.yaml
services:
  - type: web
    name: youtube-song-bot
    env: docker
    disk:
      name: bot-data
      mountPath: /app/storage
      sizeGB: 1
```

### Phase 5: Rollback Plan

If critical failure:
```bash
# 1. Revert to last known good commit
git revert HEAD --no-edit
git push origin feature/v2-production

# 2. Render auto-deploys previous commit (~2 min)

# 3. Verify /diag shows previous commit hash

# 4. If database schema changed, restore from backup
```

Rollback time: ~2-3 minutes.

## Data Preservation

| Data | Preservation | Method |
|------|-------------|--------|
| Existing trends | ✅ Preserved | ALTER TABLE adds columns |
| Existing outros | ✅ Preserved | ALTER TABLE adds column |
| Existing queue | ✅ Preserved | ALTER TABLE adds columns |
| Existing outro history | ✅ Preserved | No schema changes |
| New uploads table | ✅ Empty initially | Created fresh |
| SQLite file | ⚠️ At risk | Must move to persistent storage |

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| SQLite lost on deploy | High | Critical | Add .gitignore, configure persistent storage BEFORE deploy |
| Import errors | Medium | High | Test `python3 -m py_compile` before push |
| Handler registration conflict | Medium | High | Check handler order, test all commands |
| GitHub worker incompatible | Low | High | Test dispatch payload format |
| Duplicate upload slips through | Medium | Critical | UNIQUE constraint on uploads.youtube_url |
| Auto mode runs unexpectedly | Low | Medium | Default disabled, must explicitly enable |
| Outro conversation breaks | Low | Medium | Test full flow: /outro_add → video → name → weight |

## Verification Checklist

- [ ] All 5 documents updated with 7 modifications
- [ ] Schema migration SQL tested locally
- [ ] `python3 -m py_compile` passes on all files
- [ ] `.gitignore` excludes `storage/`
- [ ] Persistent storage configured (Supabase or Render Disk)
- [ ] `/agentreach` command registered
- [ ] Approve #1-5 buttons use `approve_trend_<id>`
- [ ] Outro conversation flow implemented
- [ ] Uploads table has UNIQUE constraint on youtube_url
- [ ] Auto mode config table created
- [ ] Queue processor checks uploads table before dispatch
