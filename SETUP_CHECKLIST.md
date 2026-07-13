# Setup Checklist — Free, single-user

Tick these top to bottom. Everything here is $0.

---

## 0. One-time key generation (on your computer)

```bash
python3 crypto_box.py keygen
```
Copy the two lines it prints (`ATS_CREDS_PUBLIC_KEY=...`, `ATS_CREDS_PRIVATE_KEY=...`) —
you'll paste them below.

---

## 1. A free database (Neon)

- [ ] Sign up at **neon.tech** (free), create a project.
- [ ] Copy its connection string (starts `postgresql://…`).
- [ ] You'll paste it as `DATABASE_URL` in step 3.

*(You can skip this and use Render's built-in free Postgres, but it gets deleted
after 90 days. Neon doesn't expire.)*

---

## 2. A free LLM key (Groq)

- [ ] Sign up at **console.groq.com** (free), create an API key.
- [ ] You'll paste it as `GROQ_API_KEY` in step 3.

---

## 3. Render web service — Environment variables

Render dashboard → your service → **Environment** → add these:

**Required (free setup):**

- [ ] `DATABASE_URL` = your Neon string from step 1
- [ ] `GROQ_API_KEY` = your Groq key from step 2
- [ ] `ADMIN_EMAIL` = your login email
- [ ] `ADMIN_PASSWORD` = **a strong password you choose** ← change it from the default!
- [ ] `SECRET_KEY` = run `openssl rand -hex 32` and paste the result
- [ ] `ATS_CREDS_PUBLIC_KEY` = the public key from step 0
- [ ] `GMAIL_USER` = your Gmail address
- [ ] `GMAIL_APP_PASSWORD` = a Gmail **app password**
      (myaccount.google.com → Security → 2-Step Verification → App passwords)

**Leave blank / default (already fine):**

- [ ] `ANTHROPIC_API_KEY` = *(blank — stays free on Groq)*
- [ ] `KEEP_AWAKE` = `true` *(already set; stops the free instance sleeping)*
- [ ] `UPLOADS_DIR`, `REPORTS_DIR`, `ACCESS_TOKEN_EXPIRE_MINUTES` = leave as-is
- [ ] `SENTRY_DSN`, `TELEGRAM_*`, `SMTP_*` = blank unless you want them

Save → Render redeploys automatically (~2 min).

> ⚠️ Do **not** put `ATS_CREDS_PRIVATE_KEY` on the Render web service. That one
> goes only on your laptop worker (step 6).

---

## 4. Log in

- [ ] Open `https://<your-app>.onrender.com/admin`
- [ ] If the screen is blank, **wait ~60 s and refresh** (free instance waking up).
- [ ] Log in with the `ADMIN_EMAIL` / `ADMIN_PASSWORD` you set.

---

## 5. Fill in your details (in the dashboard, not env vars)

- [ ] **👤 Profile & Résumes → Profile:** name, email, phone, location, LinkedIn/GitHub,
      **Work Authorization** (this answers ATS sponsorship questions verbatim),
      salary floor, target titles/locations, tone notes → **Save Profile**.
- [ ] **Upload your résumé PDF** → wait for "✓ ready" → review the **Fact Bank**.
- [ ] *(optional)* **Agent Guardrails:** max applications/day, auto-mode, allowlist.
- [ ] *(optional)* **Workday Tenant Credentials:** only if you already have Workday
      accounts you want to reuse — otherwise the worker creates them automatically.

Then try it out:
- [ ] **🎯 Jobs Feed → 🕷️ Scrape Now** → wait ~30 s → refresh → scored postings appear.
- [ ] Click **🪄** on a posting → review the résumé diff → **Approve**.

---

## 6. The apply worker — on your laptop (only when submitting)

The robot that fills and submits ATS forms can't run on Render free (no browser
in 512 MB). Run it on your machine, only while you're applying:

```bash
pip install -r requirements-worker.txt
playwright install chromium

DATABASE_URL="<same Neon string as Render>" \
GROQ_API_KEY="<same Groq key>" \
ATS_CREDS_PUBLIC_KEY="<public key from step 0>" \
ATS_CREDS_PRIVATE_KEY="<private key from step 0>" \
PW_HEADLESS=false \
python3 apply_worker.py
```

- [ ] Leave it running while you click **🚀 Queue Auto-Apply** in the dashboard.
- [ ] `PW_HEADLESS=false` shows the browser so you can solve CAPTCHAs, then click
      **▶ Resume run** in the dashboard.
- [ ] Close it when you're done applying. The dashboard keeps working without it.

---

## What runs where (summary)

| Runs on Render (free, always) | Runs on your laptop (only when applying) |
|---|---|
| Dashboard, login | The Playwright apply worker |
| Job scraping + AI scoring | (fills & submits ATS forms) |
| Résumé tailoring + PDF | |
| Cover letters, answers | |
| Email inbox routing | |

## Workday logins (e.g. CVS)

You do **not** put company passwords in env vars. Each company's Workday is a
separate account. On first apply to a company's Workday, the worker **creates an
account** (your email + a random password) and stores it **encrypted** in the
database — only your laptop worker can decrypt it. Or pre-enter one under
**Profile → Workday Tenant Credentials** (e.g. tenant `cvshealth` for CVS). The
only env vars involved are the single `ATS_CREDS_PUBLIC_KEY` / `_PRIVATE_KEY`
pair from step 0.
