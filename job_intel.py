"""
Job intelligence — AI scoring, writing, and instant alerts.

- score_new_jobs(db)      : rate every unscored posting 0-100 against the
                            profile in knowledge.txt; Telegram-alert hot ones
- draft_cover_letter(...) : tailored cover letter from real experience
- draft_follow_up(...)    : recruiter follow-up email (subject + body)
- interview_prep(...)     : likely questions with suggested answers
- send_telegram(text)     : push notification via a free Telegram bot

Telegram setup (2 minutes, optional):
  1. Message @BotFather on Telegram → /newbot → copy the token
  2. Message your new bot anything, then open
     https://api.telegram.org/bot<TOKEN>/getUpdates → copy "chat":{"id": ...}
  3. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars
"""

import os
import json
import re
import logging

import httpx
from langchain_groq import ChatGroq

from database import JobPosting

logger = logging.getLogger("agent_logger")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Alert thresholds
HOT_SCORE = 75                 # telegram-alert anything scoring >= this
FRIENDLY_SCORE = 60            # ... or sponsorship-friendly scoring >= this
SCORE_BATCH = 8                # jobs per LLM call
MAX_SCORED_PER_RUN = 40        # rate-limit guard per scheduler run

_llm = ChatGroq(model_name="llama-3.3-70b-versatile", temperature=0.2)

_profile_cache = None


def _profile() -> str:
    global _profile_cache
    if _profile_cache is None:
        try:
            with open("knowledge.txt", "r") as f:
                _profile_cache = f.read()
        except FileNotFoundError:
            _profile_cache = "Senior Data Platform Engineer: Spark, Kafka, Databricks, Snowflake, AWS/GCP/Azure."
    return _profile_cache


def send_telegram(text: str) -> bool:
    """Push a message to your phone. No-op if the bot isn't configured."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "disable_web_page_preview": True},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        logger.warning(f"[Telegram] send failed: {e}")
        return False


# ─── Match scoring ───────────────────────────────────────────────────────────

_SCORE_PROMPT = """You are a ruthless job-match evaluator for this candidate:

{profile}

Rate each job below 0-100 on how strong a match it is for THIS candidate
(seniority fit, skill overlap, domain fit). Be strict: 80+ means "apply today".

Jobs:
{jobs}

Reply with ONLY a JSON array, no other text:
[{{"id": <id>, "score": <0-100>, "reason": "<one short sentence>"}}]"""


def score_new_jobs(db) -> str:
    """Score all unscored postings; Telegram-alert the hot ones."""
    pending = (
        db.query(JobPosting)
        .filter(JobPosting.match_score.is_(None))
        .order_by(JobPosting.scraped_at.desc())
        .limit(MAX_SCORED_PER_RUN)
        .all()
    )
    if not pending:
        return "0 jobs to score"

    scored, alerts = 0, []
    for i in range(0, len(pending), SCORE_BATCH):
        batch = pending[i:i + SCORE_BATCH]
        jobs_text = "\n".join(
            f'- id={j.id} | {j.title} @ {j.company} | {j.location or "?"} | '
            f'{(j.description_snippet or "")[:400]}'
            for j in batch
        )
        try:
            raw = _llm.invoke(
                _SCORE_PROMPT.format(profile=_profile()[:3000], jobs=jobs_text)
            ).content
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            results = json.loads(match.group(0)) if match else []
        except Exception as e:
            logger.warning(f"[Scorer] batch failed: {e}")
            continue

        by_id = {j.id: j for j in batch}
        for r in results:
            job = by_id.get(int(r.get("id", -1)))
            if not job:
                continue
            job.match_score = max(0, min(100, int(r.get("score", 0))))
            job.match_reason = str(r.get("reason", ""))[:500]
            scored += 1
            if job.match_score >= HOT_SCORE or (
                job.sponsorship_flag == "friendly" and job.match_score >= FRIENDLY_SCORE
            ):
                alerts.append(job)
        db.commit()

    for job in alerts[:5]:  # cap pings per run
        send_telegram(
            f"🔥 {job.match_score}/100 match — {job.title} @ {job.company}\n"
            f"{job.location or ''} | sponsorship: {job.sponsorship_flag}\n"
            f"{job.match_reason}\n{job.url}"
        )

    return f"scored {scored}, {len(alerts)} hot alerts"


# ─── AI writing ──────────────────────────────────────────────────────────────

def draft_cover_letter(job_title: str, company: str, description: str,
                       extra_notes: str = "") -> str:
    prompt = (
        f"Candidate profile:\n{_profile()[:3000]}\n\n"
        f"Job: {job_title} at {company}\n"
        f"Description: {(description or '')[:1500]}\n"
        f"{f'Extra notes from candidate: {extra_notes}' + chr(10) if extra_notes else ''}\n"
        "Write a cover letter for this application. Rules:\n"
        "- 180-230 words, 3 short paragraphs, plain text\n"
        "- Reference 2-3 SPECIFIC achievements from the profile with real metrics\n"
        "- Mirror the job's own language where honest\n"
        "- Confident, warm, zero clichés ('passionate', 'team player'), no buzzword soup\n"
        "- Sign off as Karri Prasad"
    )
    return _llm.invoke(prompt).content.strip()


def draft_follow_up(company: str, position: str, status: str,
                    days_quiet: int, contact_name: str = "") -> str:
    prompt = (
        f"Candidate profile (for one relevant credibility detail):\n{_profile()[:1500]}\n\n"
        f"Situation: applied for {position} at {company}, current stage '{status}', "
        f"no response for {days_quiet} days. "
        f"Recruiter contact: {contact_name or 'name unknown'}.\n\n"
        "Write a follow-up email. Rules:\n"
        "- Format: first line 'Subject: ...', blank line, then the body\n"
        "- Body under 110 words, polite but confident, not desperate\n"
        "- Reference the specific role, add ONE brief value reminder from the profile\n"
        "- End with a clear, easy ask (a quick status update or a 15-minute call)\n"
        "- Sign off as Karri Prasad"
    )
    return _llm.invoke(prompt).content.strip()


def interview_prep(job_title: str, company: str, description: str) -> str:
    prompt = (
        f"Candidate profile:\n{_profile()[:3000]}\n\n"
        f"Job: {job_title} at {company}\n"
        f"Description: {(description or '')[:1500]}\n\n"
        "Create an interview prep pack:\n"
        "1. The 6 most likely technical questions for THIS role, each with a "
        "2-3 sentence suggested answer drawing on the candidate's real experience\n"
        "2. Two likely behavioral questions with STAR-outline answers\n"
        "3. Three sharp questions the candidate should ask the interviewer\n"
        "Plain text, concise."
    )
    return _llm.invoke(prompt).content.strip()
