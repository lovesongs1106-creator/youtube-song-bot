-- PostgreSQL Schema for youtube-song-bot
-- Compatible with: Supabase, Neon, AWS RDS, Render PostgreSQL
-- Run this first, then run migrate_to_postgres.py to move data from SQLite

-- 1. trends - viral trend discovery
CREATE TABLE IF NOT EXISTS trends (
    id SERIAL PRIMARY KEY,
    song_name TEXT NOT NULL,
    artist TEXT,
    youtube_url TEXT NOT NULL,
    source_platform TEXT NOT NULL,
    viral_score REAL DEFAULT 0,
    growth_score REAL DEFAULT 0,
    competition_score REAL DEFAULT 0,
    opportunity_score REAL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- 2. upload_queue - central upload queue
CREATE TABLE IF NOT EXISTS upload_queue (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    song_name TEXT NOT NULL,
    youtube_url TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    video_id TEXT,
    error_message TEXT,
    source TEXT DEFAULT 'manual',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

-- 3. uploads - DUPLICATE PROTECTION (MANDATORY)
CREATE TABLE IF NOT EXISTS uploads (
    id SERIAL PRIMARY KEY,
    queue_id INTEGER,
    song_name TEXT NOT NULL,
    youtube_url TEXT NOT NULL,
    youtube_video_id TEXT NOT NULL,
    source TEXT DEFAULT 'manual',
    uploaded_at TEXT NOT NULL
);

-- 4. outros - outro videos
CREATE TABLE IF NOT EXISTS outros (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    file_id TEXT NOT NULL UNIQUE,
    file_unique_id TEXT UNIQUE,
    ext TEXT DEFAULT '.mp4',
    weight INTEGER DEFAULT 10,
    is_active INTEGER DEFAULT 1,
    added_at TEXT NOT NULL
);

-- 5. outro_history - outro usage tracking
CREATE TABLE IF NOT EXISTS outro_history (
    id SERIAL PRIMARY KEY,
    outro_id INTEGER NOT NULL,
    upload_id TEXT NOT NULL,
    used_at TEXT NOT NULL
);

-- 6. queue_history - audit log
CREATE TABLE IF NOT EXISTS queue_history (
    id SERIAL PRIMARY KEY,
    queue_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

-- 7. system_state - key/value store
CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- 8. auto_mode_config - auto upload settings
CREATE TABLE IF NOT EXISTS auto_mode_config (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    enabled INTEGER DEFAULT 0,
    uploads_per_day INTEGER DEFAULT 3,
    start_time TEXT DEFAULT '06:00',
    last_run TEXT
);

-- 9. viral_trends - viral trend engine data
CREATE TABLE IF NOT EXISTS viral_trends (
    id SERIAL PRIMARY KEY,
    song_name TEXT,
    artist TEXT,
    platform TEXT,
    source_url TEXT,
    viral_score REAL,
    growth_rate REAL,
    competition_score REAL,
    opportunity_score REAL,
    discovered_at TEXT,
    last_updated TEXT
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_trends_opportunity ON trends(opportunity_score DESC);
CREATE INDEX IF NOT EXISTS idx_queue_status ON upload_queue(status, id);
CREATE INDEX IF NOT EXISTS idx_uploads_url ON uploads(youtube_url);
CREATE INDEX IF NOT EXISTS idx_outros_active ON outros(is_active);
CREATE INDEX IF NOT EXISTS idx_outro_history_time ON outro_history(used_at DESC);

-- Default auto_mode_config row
INSERT INTO auto_mode_config (id, enabled, uploads_per_day, start_time)
VALUES (1, 0, 3, '06:00')
ON CONFLICT(id) DO NOTHING;
