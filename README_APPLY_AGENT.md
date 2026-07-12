# Self-Hosted AI Job-Application Agent

A single-user, self-hosted job-application agent built **on top of** the existing
Karri Prasad AI Operations Dashboard (FastAPI + SQLAlchemy + the admin dashboard).
It adds an end-to-end pipeline:

> **match → tailor résumé & cover letter → you approve → agent fills and submits the ATS form → receipt stored → recruiter replies auto-route to the tracker.**

Strictly single-user. No billing, no multi-tenancy.

---

## Architecture

Two services share one Postgres database:

| Service | Runs | Responsibility |
|---|---|---|
| **web** (`main.py`) | FastAPI on Render web / your machine | dashboard, API, scraping, scoring, tailoring, scheduling, email routing |
| **apply-worker** (`apply_worker.py`) | Playwright, Render worker **or your laptop** | claims queued apply runs, fills & submits ATS forms |

The worker is separate because Playwright/Chromium doesn't fit the web dyno's
memory, and running it **locally headed** lets you solve CAPTCHAs in a visible
browser. Both talk only through the `application_runs` queue table.

```
Jobs Feed ──scrape──> JobPosting ──enrich (LLM extract + deterministic score)──> match_breakdown
   │
   └─ Tailor ─> TailoredResume (diff, fact-cited) ─ you approve ─> ATS-safe PDF
                     │                                                  │
                CoverLetter (approve)                                   │
                     │                                                  ▼
              Apply Queue ── enqueue_run (guardrails) ──> ApplicationRun (queued)
                                                              │
                          apply-worker: adapter.login/fill/review/submit
                          receipts + screenshots every step ─┤
                                                              ▼
                    submitted ─> tracker card = Applied (+ receipt linked)
                                                              │
   Gmail (read-only) ── classify + match ──> EmailThread ── auto-move tracker (undoable)
```

## Data model (added to the existing schema)

`CandidateProfile` (single row: identity, work authorization, targets, tone,
**guardrail settings**), `BaseResume`, `ResumeFact` (FactBank), `TailoredResume`,
`CoverLetter`, `ApplicationRun`, `RunArtifact` (screenshots, in DB), `AnswerBankEntry`,
`AtsCredential` (sealed), `EmailThread`. `JobPosting` gained `ats_type`, `apply_url`,
`raw_description`, `extracted_requirements`, `dedupe_hash`, `match_breakdown`.

New tables are created on boot; new columns on pre-existing tables are added by
`init_db()`'s lightweight migrator.

---

## Running 100% free

Every part of this can run at **$0**. The one thing that can't live on Render's
free tier is the Playwright worker (no free worker service; Chromium won't fit
in 512 MB) — so you run *that* on your own machine, only while you're submitting.

| Piece | Free option | Notes |
|---|---|---|
| **Web + dashboard + APIs** | Render **free** web service | Same stack you already deploy; the AI work is API calls, so no new memory load. |
| **Database** | **Neon** free (or Render free PG) | Neon doesn't expire; Render free PG is deleted after 90 days. Put the URL in `DATABASE_URL`. |
| **LLM** | **Groq** free tier | Set `GROQ_API_KEY`, leave `ANTHROPIC_API_KEY` blank (or `LLM_PROVIDER=groq`). |
| **Apply worker** | **Your laptop**, headed | `PW_HEADLESS=false` also lets you solve CAPTCHAs. Run only when applying. |
| **Gmail routing** | App password (IMAP) | Free; OAuth read-only also free. |

**Steps**

1. Deploy this repo to your existing Render free web service. In its env, set
   `GROQ_API_KEY`, leave `ANTHROPIC_API_KEY` empty, and point `DATABASE_URL` at a
   free **Neon** database (or the Render free PG).
2. `python3 crypto_box.py keygen` → set `ATS_CREDS_PUBLIC_KEY` on the web service
   (keep both keys for the worker).
3. Set `GMAIL_USER` + `GMAIL_APP_PASSWORD` for read-only email routing.
4. Run the worker free on your machine, only when submitting:
   ```bash
   pip install -r requirements-worker.txt && playwright install chromium
   DATABASE_URL="<same DB url>" GROQ_API_KEY=... \
     ATS_CREDS_PUBLIC_KEY=... ATS_CREDS_PRIVATE_KEY=... \
     PW_HEADLESS=false python3 apply_worker.py
   ```

**Two free-tier caveats**

- Render free web **spins down after ~15 min idle**, and the schedulers
  (email 5 min, scrape 6 h, nightly) run *inside* that process — they won't fire
  while it's asleep. Either point a free uptime pinger (UptimeRobot /
  cron-job.org) at `/health` every ~10 min to keep it awake (uses most of your
  750 monthly free hours — fine if it's your only free service), or just click
  **🕷️ Scrape Now** / **📥 Check now** in the dashboard when you're job-hunting.
- The disk is **ephemeral**: uploaded résumé PDFs vanish on restart, but the
  parsed content, Fact Bank, and tailoring diffs live in Postgres (the worker
  re-renders the tailored PDF from the diff), so a restart never loses data —
  only the original upload, which you can re-add.

The worker connects to Postgres over the normal external connection string, so
Render's "same-region private network" doesn't apply and isn't needed.

---

## Setup

```bash
pip install -r requirements.txt                 # web service
python3 crypto_box.py keygen                     # → ATS_CREDS_PUBLIC_KEY / _PRIVATE_KEY
cp .env.example .env                             # fill in the vars below
uvicorn main:app --reload                        # web at http://localhost:8000/admin

# worker (separate shell / machine)
pip install -r requirements-worker.txt
playwright install chromium
DATABASE_URL=<same postgres> PW_HEADLESS=false python3 apply_worker.py
```

First run: open **/admin → Profile & Resumes**, fill the profile (work
authorization matters — it answers ATS questions verbatim), upload a résumé PDF,
review the auto-extracted **Fact Bank**.

### Environment variables

| Var | Service | Purpose |
|---|---|---|
| `DATABASE_URL` | both | Postgres. **Required** — SQLite fallback is wiped on every Render restart. |
| `ANTHROPIC_API_KEY` | both | Preferred LLM (Sonnet tailoring, Haiku classify). Falls back to `GROQ_API_KEY`. |
| `GROQ_API_KEY` | both | Fallback LLM (existing). |
| `SECRET_KEY` | web | JWT signing — set a strong random value. |
| `UPLOADS_DIR` | both | Résumé PDFs + Playwright profile (`/tmp/uploads` on Render). |
| `ATS_CREDS_PUBLIC_KEY` | web | Seals Workday passwords (web can encrypt only). |
| `ATS_CREDS_PRIVATE_KEY` | worker | Decrypts Workday passwords at fill time. |
| `PW_HEADLESS` | worker | `false` locally to solve CAPTCHAs; `true` on Render. |
| `GMAIL_OAUTH_CLIENT_ID/_SECRET/_REFRESH_TOKEN` | web | Gmail **read-only** routing (preferred). |
| `GMAIL_USER` / `GMAIL_APP_PASSWORD` | web | Read-only IMAP fallback for routing. |
| `SENTRY_DSN` | both | Optional error tracking (no-op when blank). |

Deterministic settings live in the DB (Profile → Agent Guardrails): kill switch,
max applications/day, per-company cooldown, auto-mode, company allowlist.

---

## Safety guardrails (non-negotiable, enforced in code)

- **Truthfulness** — tailored bullets may use only FactBank-verified facts; a
  deterministic validator drops any rewrite whose metrics aren't in the original
  bullet or a cited fact (`tailor.validate_change`). Work-authorization answers
  come verbatim from the profile, never optimistically (`answer_engine`).
- **Human-in-the-loop** — a run cannot be queued without an **approved**
  `TailoredResume`, re-checked again immediately before the submit click. Full-auto
  exists only for allowlisted companies and still uses pre-approved materials.
- **Never bypass CAPTCHA** — the run pauses (`needs_review`); headed workers wait
  for your "Resume" click, headless workers park it for manual finishing.
- **Rate/dedupe** — kill switch, max/day, 14-day company cooldown, dedupe on
  `dedupe_hash` (same job across boards). See `apply_queue.enqueue_run`.
- **Secrets** — Workday passwords in libsodium sealed boxes; the web service
  can never read them back. Answers/PII are never logged at info level.

---

## Adding a new ATS adapter

1. Copy `adapters/_template.py` → `adapters/<newats>.py`, implement
   `detect / login? / fill / review? / submit`.
2. Register it in `adapters/__init__.py` (`get_adapter` map + a `detect_ats`
   branch), and add `<newats>` to `apply_queue.KNOWN_ATS` and `ops.ADAPTERS`.
3. Fill **every** field through `ctx.fill/select/upload/answer/fill_question`
   (that's what builds the receipt log), `ctx.snap()` at each step,
   `ctx.check_captcha()` before/after fill and before submit, and raise
   `NeedsReview(...)` for anything unsafe — never guess.
4. Save a real form's HTML as `tests/fixtures/<newats>_form.html` and test the
   adapter against a `file://` URL (see `tests/test_adapters.py`).

## Operations

- **Scheduler** (existing cron UI): `job_scrape` (6h), `email_routing` (5m),
  `nightly_maintenance` (3am: re-scrape, re-score, retry failed runs once, drift
  check), plus the existing report/digest jobs.
- **Selector-drift alarm** — each adapter logs an `AgentExecution`; if an
  adapter's success rate over its last 10 runs drops below 70% you get a
  "likely broken" notification (`ops.check_selector_drift`). Live view on the
  Apply Queue page and `GET /api/ops/health`.
- **Health** — `GET /health` (liveness), `GET /api/ops/health` (DB, adapter
  rates, env issues, funnel).

## Tests

```bash
for t in tests/test_*.py; do python3 "$t"; done
```

No network or live LLM required — LLM calls are stubbed and the Greenhouse
adapter test drives real Chromium against a local HTML fixture.

| File | Covers |
|---|---|
| `test_job_enrich.py` | deterministic weighted scoring |
| `test_tailor.py` | anti-hallucination validator, diff, ATS PDF round-trip |
| `test_apply_queue.py` | all queue guardrails |
| `test_answer_engine.py` | work-auth answers, bank reuse, approval gating |
| `test_adapters.py` | ATS detection + full Greenhouse fill→approve→submit |
| `test_pipeline.py` | pipeline stages, submit-all, autopilot |
| `test_email_router.py` | classify routing, matching, reversible moves |
| `test_ops.py` | adapter health, drift alarm, retry, funnel |
