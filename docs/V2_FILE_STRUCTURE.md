# V2 File Structure

```
youtube-song-bot/
│
├── .github/
│   └── workflows/
│       └── render-upload.yml          # GitHub Actions workflow
│
├── agents/                             # Business logic modules
│   ├── __init__.py
│   ├── trend_engine.py                 # Multi-source viral trend discovery
│   │   ├── TrendSource (ABC)
│   │   ├── TikTokSource
│   │   ├── InstagramSource
│   │   ├── YouTubeShortsSource
│   │   ├── SpotifyViralSource
│   │   ├── YouTubeMusicSource
│   │   ├── AgentReachSource
│   │   ├── collect_all_sources()
│   │   ├── refresh_trends()
│   │   ├── generate_daily_report()
│   │   └── trend_debug_info()
│   │
│   ├── queue_engine.py                 # Upload queue + background processor
│   │   ├── init_queue_tables()
│   │   ├── add_queue_item()
│   │   ├── add_multiple_items()
│   │   ├── get_next_pending()
│   │   ├── update_status()
│   │   ├── cancel_all_pending()
│   │   ├── get_summary()
│   │   ├── is_paused() / set_paused()
│   │   ├── check_duplicate_upload()    # NEW: checks uploads table
│   │   ├── run_queue_processor()       # Background async loop
│   │   └── format_queue_status()
│   │
│   ├── outro_manager.py                # Outro rotation system
│   │   ├── init_outro_tables()
│   │   ├── add_outro()                 # Now accepts name, file_id, ext, weight
│   │   ├── list_outros()
│   │   ├── remove_outro()
│   │   ├── select_outro()              # Weighted random + anti-repeat
│   │   ├── record_outro_usage()
│   │   └── format_outro_list()
│   │
│   ├── dashboard.py                    # Admin dashboard data aggregation
│   │   ├── get_dashboard_data()
│   │   ├── get_recent_uploads()
│   │   └── get_stats_by_period()
│   │
│   ├── health.py                       # System health checks
│   │   ├── check_github()
│   │   ├── check_render()
│   │   ├── check_database()
│   │   ├── check_youtube()
│   │   ├── check_telegram()
│   │   ├── check_queue()
│   │   └── format_health_report()
│   │
│   └── auto_mode.py                    # NEW: Automatic upload scheduler
│       ├── init_auto_config()
│       ├── get_auto_config()
│       ├── set_auto_config()
│       ├── should_run_now()
│       └── run_auto_upload()
│
├── storage/                            # Database (persistent storage required)
│   └── trends.db                       # SQLite (fallback only)
│
├── docs/                               # Documentation
│   ├── V2_ARCHITECTURE.md
│   ├── V2_DATABASE_SCHEMA.md
│   ├── V2_FILE_STRUCTURE.md
│   ├── V2_MIGRATION_PLAN.md
│   └── V2_QUEUE_FLOW.md
│
├── telegram_bot.py                     # Main entry point
│   ├── Flask app (webhook server)
│   ├── OAuth callback handler
│   ├── Command handlers:
│   │   ├── /start
│   │   ├── /auth
│   │   ├── /new (conversation)
│   │   ├── /agentreach              # NEW: Agent Reach import
│   │   ├── /bulk_upload             # CSV import
│   │   ├── /queue_status
│   │   ├── /queue_pause
│   │   ├── /queue_resume
│   │   ├── /queue_cancel
│   │   ├── /daily_report
│   │   ├── /trend_debug
│   │   ├── /dashboard               # NEW: Admin dashboard
│   │   ├── /system_health           # NEW: Health check
│   │   ├── /outro_add (conversation)# UPDATED: Conversation flow
│   │   ├── /outro_list
│   │   ├── /outro_remove
│   │   ├── /outro_test
│   │   ├── /github_test
│   │   ├── /export_youtube_token
│   │   └── /id
│   ├── Callback handlers:
│   │   ├── approve_trend_<id>       # UPDATED: Approve #1-5 with trend_id
│   │   ├── refresh_report_callback
│   │   ├── bulk_confirm_callback
│   │   ├── bulk_cancel_callback
│   │   └── agentreach_confirm_callback  # NEW
│   ├── Background thread:
│   │   └── bot_loop_thread() → starts queue processor + auto mode scheduler
│   └── HTTP endpoints:
│       ├── GET /
│       ├── GET /diag
│       ├── GET /raw
│       ├── POST /telegram/<secret>
│       └── GET /oauth2callback
│
├── github_worker.py                    # GitHub Actions worker
│   ├── read_payload()
│   ├── download_telegram_file()
│   ├── download_youtube_audio()
│   ├── get_youtube_service()
│   ├── upload_to_youtube()
│   └── main():
│       ├── Download audio
│       ├── Download outro
│       ├── Generate/prepare thumbnail
│       ├── Render video (ffmpeg)
│       ├── Upload to YouTube
│       ├── Record in uploads table    # NEW
│       └── Send Telegram message
│
├── bot.py                              # Video rendering utilities
│   ├── generate_thumbnail()
│   ├── generate_reference_thumbnail()
│   ├── prepare_exact_thumbnail()
│   ├── generate_seo_metadata()
│   ├── render_video()
│   ├── download_youtube_audio()
│   └── get_youtube_title()
│
├── feature_flags.py                    # Feature toggles
│   ├── ENABLE_TREND_AGENT
│   ├── ENABLE_RECOMMENDATIONS
│   ├── ENABLE_APPROVE_WORKFLOW
│   ├── ENABLE_BULK_UPLOAD
│   └── ENABLE_AUTO_MODE               # NEW
│
├── requirements.txt                    # Python dependencies
├── Dockerfile                          # Render deployment container
├── render.yaml                         # Render service config
└── .gitignore                          # Exclude storage/ from git
```

## Module Dependencies

```
telegram_bot.py
    ├── bot.py
    ├── feature_flags.py
    ├── agents/trend_engine.py
    ├── agents/queue_engine.py
    ├── agents/outro_manager.py
    ├── agents/dashboard.py
    ├── agents/health.py
    └── agents/auto_mode.py         # NEW

github_worker.py
    └── bot.py
    └── agents/queue_engine.py      # NEW: for recording uploads

agents/trend_engine.py
    └── (standalone, uses yt-dlp subprocess)

agents/queue_engine.py
    └── agents/outro_manager.py
    └── agents/trend_engine.py (for metadata)

agents/outro_manager.py
    └── (standalone)

agents/dashboard.py
    └── agents/queue_engine.py
    └── agents/outro_manager.py

agents/health.py
    └── (standalone, uses requests)

agents/auto_mode.py                   # NEW
    └── agents/trend_engine.py
    └── agents/queue_engine.py
```

## Entry Points

| Entry Point | Command | Purpose |
|-------------|---------|---------|
| Render Web | `python telegram_bot.py` | Production webhook server |
| GitHub Actions | `python github_worker.py` | Video render + upload worker |
| Local CLI | `python bot.py create-upload` | Manual video creation |

## Conversation Flows

### /outro_add Conversation
```
State: WAITING_OUTRO_VIDEO
  → User sends video file
  → Bot saves file_id, transitions to WAITING_OUTRO_NAME

State: WAITING_OUTRO_NAME
  → User sends name text
  → Bot saves name, transitions to WAITING_OUTRO_WEIGHT

State: WAITING_OUTRO_WEIGHT
  → User sends weight (1-100)
  → Bot validates, saves to DB, confirms
```

### /agentreach Flow
```
State: None (single message handler)
  → User sends /agentreach
  → Bot sets awaiting_agentreach=True
  → User pastes list
  → Bot parses, validates, checks duplicates
  → Shows summary with [Add To Queue] [Cancel]
```

## Approve Button Callbacks

```python
# Daily report inline keyboard:
keyboard = [
    [InlineKeyboardButton("Approve #1", callback_data="approve_trend_1")],
    [InlineKeyboardButton("Approve #2", callback_data="approve_trend_2")],
    [InlineKeyboardButton("Approve #3", callback_data="approve_trend_3")],
    [InlineKeyboardButton("Approve #4", callback_data="approve_trend_4")],
    [InlineKeyboardButton("Approve #5", callback_data="approve_trend_5")],
]

# Handler registration:
app.add_handler(CallbackQueryHandler(approve_trend_callback, pattern=r"^approve_trend_\d+$"))

# Callback processing:
async def approve_trend_callback(update, context):
    trend_id = int(update.callback_query.data.split("_")[-1])
    trend = get_trend_by_id(trend_id)
    # Check uploads table for duplicate
    if is_already_uploaded(trend.youtube_url):
        await query.edit_message_text("❌ Already uploaded: {url}")
        return
    # Add to queue
    add_queue_item(...)
```
