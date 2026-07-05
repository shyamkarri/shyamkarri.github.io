# 🚀 Deployment & Troubleshooting Checklist

## Before You Deploy

### Critical (Jobs won't work without these)
- [ ] **DATABASE_URL**: Set on Render to a durable Postgres (Neon or Supabase free tier)
  - Without this: all logs/jobs/postings wipe on every restart
  - Check: `echo $DATABASE_URL` on Render → should start with `postgresql://`

- [ ] **GROQ_API_KEY**: Set on Render (get free tier from console.groq.com)
  - Without this: chat/scoring will crash
  - Check: `curl https://api.groq.com/v1/models` with the key

- [ ] **SMTP_HOST, SMTP_USER, SMTP_PASS**: Set on Render for emails OR explicitly accept no emails
  - Gmail: use "App Passwords" at myaccount.google.com/apppasswords, not your login password
  - Default SMTP_HOST: `smtp.gmail.com`, SMTP_PORT: `587`
  - Without these: `_send_email()` logs and returns silently (no crash, no email)
  - Check in logs: `[Email] SMTP not configured` = emails are disabled

### Optional (Nice to have)
- [ ] **TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID**: Instant alerts when hot jobs land
  - Message @BotFather on Telegram → /newbot → copy token
  - Message your bot once, then `curl https://api.telegram.org/bot<TOKEN>/getUpdates` → copy chat.id
  - Without these: briefings still email, just no instant pings

- [ ] **ADMIN_EMAIL, ADMIN_PASSWORD**: Dashboard login (defaults are fine for solo use)

---

## After Deploy: Verify Each Job Works

### 1. Check Scheduler Jobs Registered
```bash
curl https://prasad-voice-agent.onrender.com/api/scheduler/jobs \
  -H "Authorization: Bearer <your_jwt_token>"
```
**Should return:** `weekly_report`, `daily_digest`, `gmail_auto_responder`, `job_scrape` (every 6h), `followup_reminder` (daily 8:30 AM), `morning_briefing` (daily 8:00 AM), `monthly_report`

If empty: `_start_scheduler()` failed. Check logs for Python import errors.

### 2. Test Job Scraper
**Manually trigger:**
```bash
curl -X POST https://prasad-voice-agent.onrender.com/api/jobs/scrape \
  -H "Authorization: Bearer <token>"
```
**Check logs:** should say `[Scraper] greenhouse/databricks: 16 matched jobs` etc.
**Issues:**
- `[Scraper] greenhouse/... failed: ...` = bad token/company, check `COMPANIES` in `job_scraper.py`
- Silent (no log) = request never reached backend, check CORS or auth

### 3. Test AI Scoring
Scoring only runs if jobs are unscored. After scrape completes:
```bash
curl 'https://prasad-voice-agent.onrender.com/api/jobs?limit=5' \
  -H "Authorization: Bearer <token>" | jq '.jobs[0] | {title, match_score, match_reason}'
```
**Should show:** `match_score: 65-85`, `match_reason: "Strong Spark overlap"` etc.
**Issues:**
- All `match_score: null` after 30 sec = scoring job didn't run or Groq failed
  - Check Render logs: `[Scorer] batch failed: ...`
  - Verify GROQ_API_KEY is set and valid

### 4. Test Email Send (if SMTP configured)
Manually trigger a follow-up check:
```bash
curl -X POST https://prasad-voice-agent.onrender.com/api/scheduler/jobs/3/run \
  -H "Authorization: Bearer <token>"
```
(Replace `3` with actual job ID for `followup_reminder`)

**Check:** 
- Render logs should say `[Email] Sent '...' to <email>` 
- Your email inbox should have it
**Issues:**
- `[Email] SMTP not configured` = SMTP_HOST/SMTP_USER empty → set them or skip emails
- `[Email] Failed to send: ...` = bad credentials or Gmail 2FA issue
  - Gmail: use App Password from myaccount.google.com/apppasswords, not regular password

### 5. Test Morning Briefing (full integration)
Wait until 8 AM UTC (or manually run):
```bash
curl -X POST https://prasad-voice-agent.onrender.com/api/scheduler/jobs/7/run \
  -H "Authorization: Bearer <token>"
```
**Check:**
- Render logs: `[...] briefing sent: X jobs, Y follow-ups`
- Email inbox (if SMTP set): 1 email with top matches + follow-ups due + pipeline snapshot
- Telegram (if set): same briefing as a message

---

## What Each Cron Job Does

| Job | Schedule | Requires | Output |
|---|---|---|---|
| `job_scrape` | Every 6h | — | New postings stored, score_new_jobs runs, Telegram alerts hot ones |
| `morning_briefing` | 8 AM UTC | SMTP (optional) | Email + Telegram with top matches + follow-ups |
| `followup_reminder` | 8:30 AM UTC | SMTP (optional) | Email listing apps quiet 5+ days + recruiter contact names |
| `gmail_auto_responder` | Every 10 min | — | Checks Gmail, replies via RAG (if enabled) |
| `weekly_report` | Mon 8 AM | — | Report stored in DB |
| `monthly_report` | 1st of month 7 AM | — | Report stored in DB |
| `daily_notification_digest` | 9 AM UTC | SMTP (optional) | Email summary |

---

## Common Issues & Fixes

### "Scraper finds 0 jobs"
**Cause:** Company tokens wrong, or site is down
**Fix:** 
1. Verify the company token exists: visit `https://boards.greenhouse.io/databricks` (should show jobs)
2. Add/remove companies in `job_scraper.py` line ~77 `COMPANIES` dict
3. Redeploy

### "Emails aren't arriving"
**Cause 1:** SMTP not set
**Check:** Render logs — `[Email] SMTP not configured` = set the env vars
**Fix:** 
```
On Render dashboard:
Settings → Environment → add:
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=your-email@gmail.com
  SMTP_PASS=<16-char app password from myaccount.google.com/apppasswords>
  SMTP_FROM=your-email@gmail.com
```

**Cause 2:** 2FA blocking
**Fix:** Gmail doesn't allow plain passwords if 2FA is on. Go to myaccount.google.com/apppasswords → generate a 16-char password → use that as SMTP_PASS

**Cause 3:** Jobs run but email fails silently
**Check:** Render logs — `[Email] Failed to send: ...` = bad credentials
**Fix:** Test creds locally:
```bash
python3 -c "
import smtplib
server = smtplib.SMTP('smtp.gmail.com', 587)
server.starttls()
server.login('your-email@gmail.com', '<app-password>')
print('✅ login OK')
server.quit()
"
```

### "AI scoring returns null"
**Cause:** Groq API key invalid or quota exceeded
**Check:** Render logs — `[Scorer] batch failed: ...`
**Fix:** 
1. Verify key works: `curl https://api.groq.com/v1/models -H "Authorization: Bearer $GROQ_API_KEY"`
2. Check Groq usage at console.groq.com (free tier has limits)
3. If quota exceeded, wait 24h or upgrade

### "Jobs Feed shows 0 postings"
**Cause:** Scraper never ran, or DB error
**Check:** 
1. Manually hit `POST /api/jobs/scrape` — does it return 200?
2. Check Render logs — any scraper errors?
3. Check Postgres: is `job_postings` table created? (`SELECT * FROM job_postings LIMIT 1`)

---

## Render Logs

To see what's actually happening:

```bash
# Tail logs (live)
render logs <service-name>

# Or on Render dashboard: Service → Logs tab

# Filter for specific words:
# [Email] = email sends
# [Scraper] = job scraping
# [Scheduler] = cron jobs registering/running
# [Scorer] = AI scoring
```

---

## Quick Health Check

All at once:
```bash
TOKEN=$(curl -s -X POST https://prasad-voice-agent.onrender.com/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@karriprasad.ai","password":"changeme123"}' | jq -r .access_token)

echo "=== Scheduler Jobs ==="
curl -s https://prasad-voice-agent.onrender.com/api/scheduler/jobs \
  -H "Authorization: Bearer $TOKEN" | jq '.jobs | length'

echo "=== Job Postings ==="
curl -s https://prasad-voice-agent.onrender.com/api/jobs?limit=1 \
  -H "Authorization: Bearer $TOKEN" | jq '.total'

echo "=== Top Scored Job ==="
curl -s 'https://prasad-voice-agent.onrender.com/api/jobs?sort=score&limit=1' \
  -H "Authorization: Bearer $TOKEN" | jq '.jobs[0] | {title, company, score: .match_score}'
```

If any return errors, check Render logs for the specific error.
