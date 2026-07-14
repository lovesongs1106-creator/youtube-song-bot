# YouTube Song Upload Automation Platform — V2 Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              TELEGRAM BOT                                    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │  /start     │  │ /dashboard  │  │/daily_report│  │   /agentreach       │ │
│  │  /auth      │  │/queue_status│  │ /trend_debug│  │   (Agent Reach      │ │
│  │  /new       │  │/queue_pause │  │/system_health│  │    import)          │ │
│  │             │  │/queue_resume│  │             │  │                     │ │
│  │             │  │/queue_cancel│  │             │  │                     │ │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────────────┘ │
│                              │                                               │
│                              ▼                                               │
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │                    FLASK WEBHOOK SERVER (Render)                        ││
│  │  Routes: /  /diag  /raw  /telegram/<secret>  /oauth2callback           ││
│  └─────────────────────────────────────────────────────────────────────────┘│
│                              │                                               │
│         ┌────────────────────┼────────────────────┐                         │
│         ▼                    ▼                    ▼                         │
│  ┌─────────────┐     ┌─────────────┐     ┌─────────────────────┐           │
│  │   Trend     │     │   Queue     │     │      Outro          │           │
│  │   Engine    │     │   Engine    │     │     Manager         │           │
│  │             │     │             │     │                     │           │
│  │ • YT Music  │     │ • SQLite    │     │ • Conversation Flow │           │
│  │ • YT Trend  │     │ • Sequential│     │ • Weighted Random   │           │
│  │ • YT Shorts │     │ • Resume    │     │ • Anti-Repeat       │           │
│  │ • Spotify   │     │ • 100+ jobs │     │ • History Tracking  │           │
│  │ • AgentReach│     │ • Duplicate │     │ • Fallback Logic    │           │
│  └──────┬──────┘     │   Protection│     └──────────┬──────────┘           │
│         │            └──────┬──────┘                │                       │
│         │                   │                       │                       │
│         └───────────────────┼───────────────────────┘                       │
│                             ▼                                               │
│              ┌──────────────────────────────┐                               │
│              │   PERSISTENT STORAGE         │                               │
│              │  (Supabase PG or Render Disk)│                               │
│              │  trends | queue | outros     │                               │
│              │  history | uploads | state   │                               │
│              └──────────────┬───────────────┘                               │
│                             │                                               │
│                             ▼                                               │
│              ┌──────────────────────────────┐                               │
│              │   Background Async Processor │                               │
│              │   (runs in bot event loop)   │                               │
│              └──────────────┬───────────────┘                               │
│                             │                                               │
│              ┌──────────────┴───────────────┐                               │
│              ▼                              ▼                               │
│  ┌─────────────────────┐      ┌─────────────────────┐                      │
│  │   Auto Mode         │      │   Manual Mode       │                      │
│  │   (scheduled)       │      │   (on demand)       │                      │
│  │                     │      │                     │                      │
│  │ • collect trends    │      │ • /agentreach       │                      │
│  │ • score trends      │      │ • /bulk_upload      │                      │
│  │ • queue best N      │      │ • Approve #1-5      │                      │
│  │ • auto upload       │      │ • /new              │                      │
│  └─────────────────────┘      └─────────────────────┘                      │
│                             │                                               │
│                             ▼                                               │
│              ┌──────────────────────────────┐                               │
│              │  GitHub Actions Dispatch     │                               │
│              │  repository_dispatch event   │                               │
│              └──────────────┬───────────────┘                               │
└─────────────────────────────┼───────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         GITHUB ACTIONS WORKER                                │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │Download     │  │  Generate   │  │   Select    │  │    Render Video     │ │
│  │YouTube Audio│  │  Thumbnail  │  │   Outro     │  │   (ffmpeg concat)   │ │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────────────┘ │
│                              │                                               │
│                              ▼                                               │
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │                    Upload to YouTube (private)                          ││
│  │                    Send result to Telegram                              ││
│  │                    Record in uploads table                              ││
│  └─────────────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────────────┘
```

## Two Operating Modes

### MODE 1: Automatic Trend Discovery + Auto Upload
```
Scheduled Trigger (cron)
    ↓
trend_engine.collect() → trends table
    ↓
Score all trends
    ↓
Select top N by opportunity_score
    ↓
Check uploads table (duplicate protection)
    ↓
Add to upload_queue (status='pending', source='auto')
    ↓
Background processor → GitHub Actions
    ↓
On completion → record in uploads table
```

### MODE 2: Manual Upload (Agent Reach / Bulk / Approve)
```
SOURCE A: /agentreach
    User pastes: Song Name | YouTube URL
    Max 100 songs
    Validation: duplicate URL, duplicate song, already uploaded, malformed URL
    Summary → [Add To Queue] [Cancel]

SOURCE B: /bulk_upload (CSV)
    User uploads CSV: song_name,youtube_url
    Parse → Validate → Summary → Confirm

SOURCE C: Approve #1-5
    /daily_report shows top 5 trends
    Each button: approve_trend_<trend_id>
    Loads SPECIFIC trend from DB (not always #1)
    Checks uploads table for duplicate protection
    Adds to queue

All sources → upload_queue → Background processor → GitHub Actions
```

## Component Responsibilities

| Component | File | Responsibility |
|-----------|------|----------------|
| Telegram Controller | `telegram_bot.py` | Webhook server, all command handlers, OAuth, background thread startup |
| Trend Engine | `agents/trend_engine.py` | Multi-source discovery, scoring, deduplication |
| Queue Engine | `agents/queue_engine.py` | SQLite queue CRUD, background processor, resume logic, duplicate protection |
| Outro Manager | `agents/outro_manager.py` | Conversation flow registration, weighted selection, anti-repeat history |
| Dashboard | `agents/dashboard.py` | Stats aggregation for /dashboard command |
| Health Monitor | `agents/health.py` | Service health checks for /system_health |
| GitHub Worker | `github_worker.py` | Download, render, upload pipeline, record completion |
| Video Utils | `bot.py` | Thumbnail generation, ffmpeg rendering |

## Data Flow

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   SOURCE A  │     │   SOURCE B  │     │   SOURCE C  │
│ Auto Trends │     │ Agent Reach │     │  Approve #N │
│ (scheduled) │     │ /agentreach │     │  (buttons)  │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │
       └───────────────────┼───────────────────┘
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
    ┌─────────────────┐      ┌─────────────────┐
    │ Validate URLs   │      │ Check uploads   │
    │ Check duplicates│      │ table (reject   │
    │ within batch    │      │ if exists)      │
    └────────┬────────┘      └────────┬────────┘
             │                        │
             └──────────┬─────────────┘
                        ▼
              ┌─────────────────────┐
              │   upload_queue      │
              │   (SQLite)          │
              │   status=pending    │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │ Background Processor│
              │ (async loop)        │
              └──────────┬──────────┘
                         │
       ┌─────────────────┼─────────────────┐
       ▼                 ▼                 ▼
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│   outro     │  │  metadata   │  │   GitHub    │
│  selection  │  │ generation  │  │  dispatch   │
└─────────────┘  └─────────────┘  └──────┬──────┘
                                         │
                              ┌──────────▼──────────┐
                              │  GitHub Actions     │
                              │  Worker Execution   │
                              └──────────┬──────────┘
                                         │
                              ┌──────────▼──────────┐
                              │   Update queue      │
                              │   status=completed  │
                              │   video_id=xxx      │
                              │   Record in uploads │
                              └──────────┬──────────┘
                                         │
                              ┌──────────▼──────────┐
                              │  Telegram Message   │
                              │  "Upload complete"  │
                              └─────────────────────┘
```

## Fault Tolerance Design

| Failure Scenario | Handling |
|------------------|----------|
| Render restart | Queue table persists in **Supabase PostgreSQL** or **Render Persistent Disk**. Background processor auto-starts on boot. |
| Bot crash | Database is ACID (PostgreSQL) or SQLite on persistent disk. Pending items remain. Processor resumes on restart. |
| GitHub Actions failure | Item marked "failed". Error logged. Next item processed. |
| YouTube download failure | Item marked "failed". Telegram notification sent. Next item proceeds. |
| Missing outro | Item fails immediately. User notified. Queue continues. |
| Deployment (new code) | Database on persistent storage. Queue resumes. |
| yt-dlp blocked | Trend collection returns 0. Daily report shows diagnostic message. |
| Duplicate upload attempt | Blocked by uploads table check. User shown: "Already uploaded: {url}" |
| Telegram API down | Webhook fails. Messages queue in memory briefly. |

## Persistent Storage Requirements

**MANDATORY:** Queue, Trends, Outros, Upload History MUST survive:
- Render restart
- Deployment
- Crash

**Preferred Options:**
1. **Supabase PostgreSQL** (free tier: 500MB) — Recommended for production
2. **Render Persistent Disk** ($0.25/GB/month) — Alternative

**NOT ACCEPTABLE:** Render ephemeral filesystem alone.

**Implementation:**
- Connection string from env var `DATABASE_URL`
- Fallback to SQLite on persistent path if PostgreSQL unavailable
- All tables in single database
- Connection pooling for concurrent access

## Scalability Limits

| Resource | Limit | Mitigation |
|----------|-------|------------|
| Database size | 500MB (Supabase free) | Archive old records monthly |
| Concurrent queue items | 1 (sequential by design) | GitHub Actions handles parallelism per job |
| Telegram message rate | 30 msg/sec | Queue processor sleeps between items |
| Render free tier RAM | 512MB | 720p render, ultrafast preset |
| GitHub Actions timeout | 120 min | Sufficient for single video |
| yt-dlp queries | 6 queries × 8 results = 48 trends | Deduplicated to ~15 unique |
| Agent Reach batch | 100 songs max | Enforced in parser |

## Approve Button Architecture

```
/daily_report shows:

1. Song A | Artist A | Opp: 95
   [Approve #1]

2. Song B | Artist B | Opp: 88
   [Approve #2]

3. Song C | Artist C | Opp: 82
   [Approve #3]

4. Song D | Artist D | Opp: 76
   [Approve #4]

5. Song E | Artist E | Opp: 71
   [Approve #5]

Each button callback_data: "approve_trend_<trend_id>"

On click:
1. Parse trend_id from callback_data
2. SELECT * FROM trends WHERE id = ?
3. Check uploads table: SELECT 1 FROM uploads WHERE youtube_url = ?
4. If exists → reject with "Already uploaded"
5. If new → add to queue → dispatch
```

## Auto Mode Architecture

```
Environment Variables:
  AUTO_UPLOAD_ENABLED=true
  AUTO_UPLOADS_PER_DAY=3
  AUTO_UPLOAD_START_TIME="06:00"

Scheduled Task (asyncio or APScheduler):
  Every day at 06:00:
    1. trend_engine.collect_all_sources()
    2. Score all trends
    3. Select top N by opportunity_score
    4. For each selected trend:
         a. Check uploads table for duplicate
         b. If new → add to queue (source='auto')
    5. Background processor handles the rest
```

## Outro Conversation Flow

```
User: /outro_add
Bot: "📤 Send your outro video as MP4 or MOV."

User: [uploads video file]
Bot: "✅ Video received.\n\nWhat name should I save this as?"

User: "Summer Outro"
Bot: "Got it. What weight for selection? (1-100, higher = more frequent)"

User: "10"
Bot: "✅ Outro added!\nName: Summer Outro\nWeight: 10\nID: 4"
```
