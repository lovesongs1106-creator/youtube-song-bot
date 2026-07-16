# 🛠️ Fix Deploy Instructions

The current deploy failed because Render cannot reach the Supabase PostgreSQL database via IPv6. I have updated the code to gracefully fallback to SQLite so the bot will at least start, but for persistent storage on Render, you **MUST** use the Supabase IPv4 Pooler URL.

## 1. Updated Database URL
Based on your project ref `vckbjanbeovtqszsmfte` and location, your IPv4 Pooler URL is likely:

```
postgresql://postgres.vckbjanbeovtqszsmfte:buzZug-mattym-jymby5@aws-0-ap-south-1.pooler.supabase.com:6543/postgres?pgbouncer=true
```

*(Note: If ap-south-1 fails, try us-east-1 or check your Supabase dashboard → Settings → Database → Connection Pooler)*

## 2. Steps to Fix
1. Go to **Render Dashboard**.
2. Select your service `srv-d8vo7kbtqb8s73f1gtkg`.
3. Go to **Environment** tab.
4. Update `DATABASE_URL` with the Pooler URL above.
5. Save changes. Render will automatically redeploy.

## 3. Verify
After deploy, check the new dashboard:
`https://youtube-song-bot.onrender.com/dashboard`
Or use the bot command:
`/db_status`

---
## 🚀 Improvements made in this update:
- **Robust Startup:** Bot no longer crashes if PostgreSQL is unreachable; it falls back to SQLite and logs a helpful hint.
- **SQL Adaptation:** Fixed a bug where SQL syntax would mismatch during fallback.
- **HTML Dashboard:** Added `/dashboard` (Phase 3) to monitor bot status.
