# V2 Database Schema

## Entity Relationship Diagram

```
┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐
│     trends      │       │  upload_queue   │       │     outros      │
├─────────────────┤       ├─────────────────┤       ├─────────────────┤
│ id (PK)         │       │ id (PK)         │       │ id (PK)         │
│ song_name       │       │ user_id         │       │ name            │
│ artist          │       │ chat_id         │       │ file_id         │
│ youtube_url     │       │ song_name       │       │ file_unique_id  │
│ source_platform │       │ youtube_url     │       │ ext             │
│ viral_score     │       │ status          │       │ weight          │
│ growth_score    │       │ video_id        │       │ is_active       │
│ competition_sc  │       │ error_message   │       │ added_at        │
│ opportunity_sc  │       │ source          │       └─────────────────┘
│ created_at      │       │ created_at      │              │
└─────────────────┘       │ started_at      │              │
                          │ completed_at    │              ▼
                          └─────────────────┘       ┌─────────────────┐
                                   │                │ outro_history   │
                                   │                ├─────────────────┤
                                   ▼                │ id (PK)         │
                          ┌─────────────────┐       │ outro_id (FK)   │
                          │  queue_history  │       │ upload_id       │
                          ├─────────────────┤       │ used_at         │
                          │ id (PK)         │       └─────────────────┘
                          │ queue_id (FK)   │
                          │ action          │
                          │ detail          │
                          │ created_at      │
                          └─────────────────┘

┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐
│    uploads      │       │  system_state   │       │ auto_mode_config│
├─────────────────┤       ├─────────────────┤       ├─────────────────┤
│ id (PK)         │       │ key (PK)        │       │ id (PK)         │
│ queue_id (FK)   │       │ value           │       │ enabled         │
│ song_name       │       └─────────────────┘       │ uploads_per_day │
│ youtube_url     │                                 │ start_time      │
│ youtube_video_id│                                 │ last_run        │
│ source          │                                 └─────────────────┘
│ uploaded_at     │
└─────────────────┘
```

## Table Definitions

### 1. trends

Stores discovered viral songs from all sources.

```sql
CREATE TABLE trends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    song_name TEXT NOT NULL,
    artist TEXT,
    youtube_url TEXT NOT NULL,
    source_platform TEXT NOT NULL,  -- 'TikTok', 'Instagram', 'YouTube Shorts', 'Spotify', 'YouTube Music', 'Agent Reach'
    viral_score REAL DEFAULT 0,     -- 0-100, base popularity
    growth_score REAL DEFAULT 0,    -- 0-100, estimated daily growth %
    competition_score REAL DEFAULT 0, -- 0-100, lower = less saturated
    opportunity_score REAL DEFAULT 0, -- weighted composite
    created_at TEXT NOT NULL
);

CREATE INDEX idx_trends_opportunity ON trends(opportunity_score DESC);
CREATE INDEX idx_trends_platform ON trends(source_platform);
```

### 2. upload_queue

Central queue for ALL upload jobs (auto trends + manual bulk + agent reach + approve).

```sql
CREATE TABLE upload_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    song_name TEXT NOT NULL,
    youtube_url TEXT NOT NULL,
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'processing', 'completed', 'failed', 'cancelled')),
    video_id TEXT,
    error_message TEXT,
    source TEXT DEFAULT 'manual',  -- 'manual', 'agentreach', 'trend_approve', 'auto', 'bulk_csv'
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE INDEX idx_queue_status ON upload_queue(status, id);
CREATE INDEX idx_queue_pending ON upload_queue(status) WHERE status = 'pending';
CREATE INDEX idx_queue_source ON upload_queue(source);
```

### 3. outros

Registered outro videos with weight for selection algorithm.

```sql
CREATE TABLE outros (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    file_id TEXT NOT NULL,                -- Telegram file_id for download
    file_unique_id TEXT UNIQUE,           -- Telegram file_unique_id for dedup
    ext TEXT DEFAULT '.mp4',
    weight INTEGER DEFAULT 10 CHECK(weight >= 1 AND weight <= 100),
    is_active INTEGER DEFAULT 1,
    added_at TEXT NOT NULL
);

CREATE INDEX idx_outros_active ON outros(is_active);
```

### 4. outro_history

Tracks which outro was used for which upload.

```sql
CREATE TABLE outro_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    outro_id INTEGER NOT NULL,
    upload_id TEXT NOT NULL,  -- job_id or queue_id
    used_at TEXT NOT NULL,
    FOREIGN KEY (outro_id) REFERENCES outros(id)
);

CREATE INDEX idx_outro_history_outro ON outro_history(outro_id);
CREATE INDEX idx_outro_history_time ON outro_history(used_at DESC);
```

### 5. queue_history

Audit log for queue actions.

```sql
CREATE TABLE queue_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id INTEGER,
    action TEXT NOT NULL,     -- 'added', 'started', 'completed', 'failed', 'cancelled', 'paused', 'resumed'
    detail TEXT,
    created_at TEXT NOT NULL
);
```

### 6. uploads — DUPLICATE PROTECTION TABLE

**CRITICAL:** Every successful upload is recorded here. Before adding ANY item to upload_queue, the system MUST check this table.

```sql
CREATE TABLE uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id INTEGER,
    song_name TEXT NOT NULL,
    youtube_url TEXT NOT NULL UNIQUE,  -- UNIQUE constraint prevents duplicates
    youtube_video_id TEXT NOT NULL,
    source TEXT DEFAULT 'manual',      -- 'manual', 'agentreach', 'trend_approve', 'auto'
    uploaded_at TEXT NOT NULL
);

CREATE INDEX idx_uploads_url ON uploads(youtube_url);
CREATE INDEX idx_uploads_time ON uploads(uploaded_at DESC);
```

**Duplicate Check Query:**
```sql
SELECT 1 FROM uploads WHERE youtube_url = ? LIMIT 1;
```

### 7. system_state

Key-value store for system flags.

```sql
CREATE TABLE system_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

### 8. auto_mode_config

Configuration for automatic upload mode.

```sql
CREATE TABLE auto_mode_config (
    id INTEGER PRIMARY KEY CHECK(id = 1),  -- Singleton table
    enabled INTEGER DEFAULT 0,
    uploads_per_day INTEGER DEFAULT 3 CHECK(uploads_per_day >= 1 AND uploads_per_day <= 10),
    start_time TEXT DEFAULT '06:00',        -- HH:MM format
    last_run TEXT                           -- ISO timestamp of last auto run
);

INSERT INTO auto_mode_config (id, enabled, uploads_per_day, start_time) VALUES (1, 0, 3, '06:00');
```

## Data Integrity Rules

1. **Upload Duplicate Protection**: `uploads.youtube_url` has UNIQUE constraint. Before inserting into `upload_queue`, system checks `uploads` table.
2. **Outro soft delete**: `is_active = 0` instead of DELETE to preserve history.
3. **Status transitions**:
   - `pending` → `processing` → `completed` | `failed`
   - `pending` → `cancelled`
   - `processing` → `failed` (on error)
4. **Queue item uniqueness**: Same `youtube_url` CAN be queued multiple times (user may retry with different metadata), but will be blocked at processing time by uploads table check.
5. **Auto mode singleton**: `auto_mode_config` has only one row (id=1).

## Agent Reach Import Flow

```
User sends /agentreach
    ↓
User pastes list:
    Song A | https://youtube.com/watch?v=abc
    Song B | https://youtube.com/watch?v=def
    ↓
System validates EACH row:
    1. Parse: song_name | youtube_url
    2. Validate URL format
    3. Check uploads table: SELECT 1 FROM uploads WHERE youtube_url = ?
    4. Check queue table: SELECT 1 FROM upload_queue WHERE youtube_url = ? AND status IN ('pending','processing')
    5. Check for duplicate within batch
    ↓
Categorize:
    valid[]      → ready to queue
    invalid[]    → malformed URL or missing name
    duplicate[]  → already in uploads or queue
    ↓
Show summary with counts
    [Add To Queue] [Cancel]
    ↓
On confirm:
    For each valid item:
        INSERT INTO upload_queue (...)
    Send confirmation: "N songs added to queue"
```

## Migration from Current State

### Current Tables (from previous commits)
```
viral_trends        — old schema
upload_queue        — old schema (no source, no started_at)
outros              — old schema (no file_unique_id)
outro_history       — old schema
queue_history       — old schema
queue_state         — old schema
```

### Migration SQL
```sql
-- Step 1: Add columns to viral_trends
ALTER TABLE viral_trends ADD COLUMN growth_score REAL DEFAULT 0;
ALTER TABLE viral_trends ADD COLUMN competition_score REAL DEFAULT 0;

-- Step 2: Add columns to upload_queue
ALTER TABLE upload_queue ADD COLUMN source TEXT DEFAULT 'manual';
ALTER TABLE upload_queue ADD COLUMN started_at TEXT;

-- Step 3: Add file_unique_id to outros
ALTER TABLE outros ADD COLUMN file_unique_id TEXT UNIQUE;

-- Step 4: Rename queue_state → system_state
CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT);
INSERT INTO system_state SELECT * FROM queue_state;
DROP TABLE queue_state;

-- Step 5: Create uploads table (duplicate protection)
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

-- Step 6: Create auto_mode_config table
CREATE TABLE auto_mode_config (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    enabled INTEGER DEFAULT 0,
    uploads_per_day INTEGER DEFAULT 3,
    start_time TEXT DEFAULT '06:00',
    last_run TEXT
);
INSERT INTO auto_mode_config (id, enabled, uploads_per_day, start_time) VALUES (1, 0, 3, '06:00');

-- Step 7: Create indexes
CREATE INDEX idx_queue_source ON upload_queue(source);
CREATE INDEX idx_outros_active ON outros(is_active);
```
