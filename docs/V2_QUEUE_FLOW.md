# V2 Queue Flow Diagram

## Complete Upload Pipeline

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            INPUT SOURCES                                     │
│                                                                              │
│  ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────────────┐  │
│  │  AUTO MODE      │    │  AGENT REACH    │    │   DAILY REPORT          │  │
│  │  (scheduled)    │    │  /agentreach    │    │   Approve #1-5          │  │
│  │                 │    │                 │    │                         │  │
│  │ • collect       │    │ • Paste list    │    │ • Load trend by ID      │  │
│  │ • score         │    │ • Max 100       │    │ • Check uploads table   │  │
│  │ • queue best N  │    │ • Validate      │    │ • Add to queue          │  │
│  └────────┬────────┘    └────────┬────────┘    └────────────┬────────────┘  │
│           │                      │                          │               │
│           └──────────────────────┼──────────────────────────┘               │
│                                  │                                          │
│                                  ▼                                          │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                    VALIDATION + DUPLICATE PROTECTION                 │   │
│  │                                                                      │   │
│  │  1. Parse song name + URL                                            │   │
│  │  2. Validate YouTube URL format                                      │   │
│  │  3. Check uploads table:                                             │   │
│  │     SELECT 1 FROM uploads WHERE youtube_url = ?                      │   │
│  │     → If exists: REJECT "Already uploaded"                           │   │
│  │  4. Check queue table for pending/processing duplicates              │   │
│  │  5. Deduplicate within batch                                         │   │
│  │                                                                      │   │
│  │  Output: valid_items[], invalid_items[], duplicate_items[]           │   │
│  └──────────────────────────────────┬───────────────────────────────────┘   │
│                                     │                                       │
│                                     ▼                                       │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                      CONFIRMATION DIALOG                             │   │
│  │                                                                      │   │
│  │  "📊 Summary: 5 valid, 2 invalid, 1 duplicate"                       │   │
│  │                                                                      │   │
│  │  [✅ Add To Queue]  [❌ Cancel]                                      │   │
│  │                                                                      │   │
│  └──────────────────────────────────┬───────────────────────────────────┘   │
│                                     │                                       │
│                    ┌────────────────┴────────────────┐                      │
│                    ▼                                  ▼                      │
│           [Confirm]                                  [Cancel]               │
│                    │                                  │                      │
│                    ▼                                  ▼                      │
│  ┌─────────────────────────┐              ┌─────────────────────────┐       │
│  │  INSERT INTO            │              │  Clear user_data        │       │
│  │  upload_queue           │              │  Show "Cancelled"       │       │
│  │  (status='pending')     │              └─────────────────────────┘       │
│  └────────────┬────────────┘                                                │
│               │                                                             │
│               ▼                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │              PERSISTENT STORAGE (Supabase PG or Render Disk)         │   │
│  │                                                                      │   │
│  │  upload_queue: id | song_name | youtube_url | status | source | ... │   │
│  │  uploads: id | youtube_url | youtube_video_id | uploaded_at        │   │
│  │                                                                      │   │
│  │  Status: pending → processing → completed/failed/cancelled          │   │
│  └──────────────────────────────────┬───────────────────────────────────┘   │
│                                     │                                       │
└─────────────────────────────────────┼───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      BACKGROUND QUEUE PROCESSOR                              │
│                     (asyncio coroutine in bot loop)                          │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  while True:                                                         │    │
│  │      if is_paused():                                                 │    │
│  │          sleep(5) → continue                                         │    │
│  │                                                                      │    │
│  │      item = get_next_pending()  ← oldest pending item                │    │
│  │      if not item:                                                    │    │
│  │          sleep(5) → continue                                         │    │
│  │                                                                      │    │
│  │      update_status(item.id, 'processing')                            │    │
│  │      update_started_at(item.id, now)                                 │    │
│  │                                                                      │    │
│  │      ┌─────────────────────────────────────────────────────────┐     │    │
│  │      │  STEP 1: SELECT OUTRO                                   │     │    │
│  │      │  outro = select_outro()                                 │     │    │
│  │      │  • Exclude last 3 used                                  │     │    │
│  │      │  • Weighted random from remaining                       │     │    │
│  │      │  • Fallback to all active if exhausted                  │     │    │
│  │      └─────────────────────────────────────────────────────────┘     │    │
│  │                                                                      │    │
│  │      ┌─────────────────────────────────────────────────────────┐     │    │
│  │      │  STEP 2: GENERATE METADATA                              │     │    │
│  │      │  metadata = generate_seo_metadata(song, artist, url)    │     │    │
│  │      │  • Title: "Song - Artist | Official Audio"              │     │    │
│  │      │  • Description with hashtags                            │     │    │
│  │      │  • Tags array                                           │     │    │
│  │      └─────────────────────────────────────────────────────────┘     │    │
│  │                                                                      │    │
│  │      ┌─────────────────────────────────────────────────────────┐     │    │
│  │      │  STEP 3: BUILD PAYLOAD                                  │     │    │
│  │      │  payload = {                                            │     │    │
│  │      │      job_id, chat_id, user_id,                          │     │    │
│  │      │      song_name, artist,                                 │     │    │
│  │      │      source_type: 'youtube_url',                        │     │    │
│  │      │      youtube_url,                                       │     │    │
│  │      │      privacy,                                           │     │    │
│  │      │      custom_title, custom_description, custom_tags,     │     │    │
│  │      │      outro_file_id, outro_ext                           │     │    │
│  │      │  }                                                      │     │    │
│  │      └─────────────────────────────────────────────────────────┘     │    │
│  │                                                                      │    │
│  │      ┌─────────────────────────────────────────────────────────┐     │    │
│  │      │  STEP 4: DISPATCH TO GITHUB ACTIONS                     │     │    │
│  │      │  dispatch_github_worker(payload)                        │     │    │
│  │      │  • HTTP POST to GitHub API                              │     │    │
│  │      │  • repository_dispatch event                            │     │    │
│  │      │  • Max 60s timeout                                      │     │    │
│  │      └─────────────────────────────────────────────────────────┘     │    │
│  │                                                                      │    │
│  │      record_outro_usage(outro.id, job_id)                            │    │
│  │      update_status(item.id, 'completed')                             │    │
│  │      send_telegram_message("✅ Dispatched: song_name")               │    │
│  │                                                                      │    │
│  │      sleep(2)  ← rate limit between items                            │    │
│  │                                                                      │    │
│  │  except Exception as e:                                              │    │
│  │      update_status(item.id, 'failed', error=str(e))                  │    │
│  │      send_telegram_message("❌ Failed: song_name - error")           │    │
│  │      sleep(2)                                                        │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                              │
└────────────────────────────────────────┬─────────────────────────────────────┘
                                         │
                                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         GITHUB ACTIONS WORKER                                │
│                          (separate VM per job)                               │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  STEP 1: DOWNLOAD AUDIO                                             │    │
│  │  download_youtube_audio(youtube_url, output_dir)                    │    │
│  │  • yt-dlp --extract-audio --audio-format mp3                        │    │
│  │  • Handle: blocked, age-restricted, deleted, private                │    │
│  │  • On failure → raise → Telegram error message                      │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                      │                                       │
│                                      ▼                                       │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  STEP 2: DOWNLOAD OUTRO                                             │    │
│  │  download_telegram_file(outro_file_id, outro_path)                  │    │
│  │  • Get file_path from Telegram API                                  │    │
│  │  • Stream download to local file                                    │    │
│  │  • On failure → raise → Telegram error message                      │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                      │                                       │
│                                      ▼                                       │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  STEP 3: GENERATE THUMBNAIL                                         │    │
│  │  • If thumbnail_file_id: use exact thumbnail                        │    │
│  │  • Else if reference_file_id: generate from reference               │    │
│  │  • Else: auto-generate gradient thumbnail                           │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                      │                                       │
│                                      ▼                                       │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  STEP 4: RENDER VIDEO                                               │    │
│  │  render_video(thumb, audio, outro, output, workdir)                 │    │
│  │  • ffmpeg: thumbnail loop + audio → main_video.mp4                  │    │
│  │  • ffmpeg: normalize outro → outro_norm.mp4                         │    │
│  │  • ffmpeg concat: main + outro → final_video.mp4                    │    │
│  │  • Settings: 1280×720, 24fps, ultrafast                             │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                      │                                       │
│                                      ▼                                       │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  STEP 5: UPLOAD TO YOUTUBE                                          │    │
│  │  upload_to_youtube(video, thumbnail, metadata, privacy)             │    │
│  │  • YouTube Data API v3                                              │    │
│  │  • Privacy: private (default)                                       │    │
│  │  • Category: 10 (Music)                                             │    │
│  │  • Set thumbnail after upload                                       │    │
│  │  • Returns video_id                                                 │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                      │                                       │
│                                      ▼                                       │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  STEP 6: RECORD UPLOAD + NOTIFY                                     │    │
│  │  • INSERT INTO uploads (youtube_url, youtube_video_id, ...)         │    │
│  │  • send_message(chat_id, "✅ Upload complete: {video_id}")          │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## State Machine: Queue Item Lifecycle

```
                    ┌─────────────┐
                    │   START     │
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
         ┌─────────│   PENDING   │◄────────┐
         │         └──────┬──────┘         │
         │                │                │
         │   ┌────────────┘                │
         │   ▼                             │
         │   │ [queue_cancel]              │
         │   ▼                             │
         │   ┌─────────────┐               │
         │   │  CANCELLED  │               │
         │   └─────────────┘               │
         │                                 │
         │                │ [processor picks up]
         │                ▼
         │         ┌─────────────┐
         │         │  PROCESSING │
         │         └──────┬──────┘
         │                │
         │     ┌─────────┼─────────┐
         │     ▼         ▼         ▼
         │  ┌──────┐ ┌──────┐ ┌──────┐
         │  │SUCCESS│ │ FAIL │ │ CANCEL│
         │  └──┬───┘ └──┬───┘ └──┬───┘
         │     │        │        │
         │     ▼        ▼        │
         │  ┌────────┐ ┌────────┐│
         │  │COMPLETED│ │ FAILED ││
         │  └────────┘ └────────┘│
         │                       │
         └───────────────────────┘
              [retry not supported — user must re-queue]
```

## Error Recovery Flow

```
┌─────────────────┐
│  Error Detected │
└────────┬────────┘
         │
         ▼
┌─────────────────────────┐
│ 1. Log error to queue   │
│    error_message column │
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐
│ 2. Update status to     │
│    'failed'             │
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐
│ 3. Send Telegram        │
│    notification         │
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐
│ 4. Sleep 2 seconds      │
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐
│ 5. Continue to next     │
│    pending item         │
└─────────────────────────┘
```

## Resume After Restart Flow

```
┌─────────────────┐
│  Bot Starts     │
└────────┬────────┘
         │
         ▼
┌─────────────────────────┐
│ 1. Initialize all       │
│    database tables      │
│    (persistent storage) │
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐
│ 2. Start Flask server   │
│    (webhook endpoint)   │
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐
│ 3. Set Telegram webhook │
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐
│ 4. Start background     │
│    queue processor      │
│    coroutine            │
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐
│ 5. Processor checks     │
│    is_paused()          │
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐
│ 6. If not paused:       │
│    get_next_pending()   │
│    → finds items from   │
│    before restart       │
│    → resumes processing │
└─────────────────────────┘
```

## Outro Conversation Flow

```
User: /outro_add
Bot: "📤 Send your outro video as MP4 or MOV."

User: [uploads video file]
Bot: "✅ Video received.\n\nWhat name should I save this as?"
      → Save file_id, file_unique_id, ext
      → Set state: WAITING_OUTRO_NAME

User: "Summer Outro"
Bot: "Got it. What weight for selection? (1-100, higher = more frequent)"
      → Save name
      → Set state: WAITING_OUTRO_WEIGHT

User: "10"
Bot: "✅ Outro added!\nName: Summer Outro\nWeight: 10\nID: 4"
      → Validate weight (1-100)
      → Save to DB
      → Clear conversation state
```

## Approve Button Flow

```
/daily_report shows:

1. Song A | Artist A | Opp: 95
   [Approve #1]  ← callback_data="approve_trend_1"

2. Song B | Artist B | Opp: 88
   [Approve #2]  ← callback_data="approve_trend_2"

3. Song C | Artist C | Opp: 82
   [Approve #3]  ← callback_data="approve_trend_3"

4. Song D | Artist D | Opp: 76
   [Approve #4]  ← callback_data="approve_trend_4"

5. Song E | Artist E | Opp: 71
   [Approve #5]  ← callback_data="approve_trend_5"

On click Approve #3:
  1. Parse callback_data → trend_id = 3
  2. SELECT * FROM trends WHERE id = 3
  3. SELECT 1 FROM uploads WHERE youtube_url = trend.youtube_url
  4. If exists → "❌ Already uploaded"
  5. If new → INSERT INTO upload_queue (...) VALUES (...)
  6. Send confirmation: "✅ Song C added to queue"
```

## Agent Reach Import Flow

```
User: /agentreach
Bot: "📥 Agent Reach Import\n\nPaste your song list:\nSong Name | YouTube URL\nMax 100 songs."
      → Set state: awaiting_agentreach = True

User: [pastes]
  Dil Hi Gawaah | https://youtube.com/watch?v=abc
  Raataan Lambiyan | https://youtube.com/watch?v=def
  Humnava Mere | https://youtube.com/watch?v=ghi

Bot parses each line:
  Line 1: "Dil Hi Gawaah" | "https://youtube.com/watch?v=abc"
    → Valid URL ✓
    → Check uploads table: NOT FOUND ✓
    → Check queue (pending/processing): NOT FOUND ✓
    → Valid ✓

  Line 2: "Raataan Lambiyan" | "https://youtube.com/watch?v=def"
    → Valid URL ✓
    → Check uploads table: FOUND ✗
    → DUPLICATE

  Line 3: "Humnava Mere" | "invalid-url"
    → Invalid URL ✗
    → INVALID

Summary:
  ✅ Valid: 1
  ❌ Invalid: 1
  🔄 Duplicate: 1

  [Add To Queue] [Cancel]

User clicks [Add To Queue]
  → INSERT INTO upload_queue for each valid item
  → "✅ 1 song added to queue"
```

## Auto Mode Flow

```
Environment:
  AUTO_UPLOAD_ENABLED=true
  AUTO_UPLOADS_PER_DAY=3
  AUTO_UPLOAD_START_TIME="06:00"

Daily at 06:00:
  1. trend_engine.collect_all_sources()
     → TikTok, Instagram, YT Shorts, Spotify, YT Music, Agent Reach
  2. Score all trends
     → viral_score, growth_score, competition_score, opportunity_score
  3. Sort by opportunity_score DESC
  4. Take top N = AUTO_UPLOADS_PER_DAY
  5. For each selected trend:
     a. SELECT 1 FROM uploads WHERE youtube_url = ?
     b. If exists → skip (already uploaded)
     c. If new → INSERT INTO upload_queue (source='auto')
  6. Background processor handles the rest
  7. Update auto_mode_config.last_run = now
```
