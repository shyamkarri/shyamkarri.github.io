"""
Email routing engine — classify recruiter mail, match it to an application,
and (reversibly) advance the tracker. Read-only: nothing here sends or edits
mail; it only reads normalized message dicts produced by email_reader.

A "message" dict:
  {thread_id, from_email, from_name, subject, snippet, body, date (datetime)}

Pipeline per new thread:
  1. classify()  — Haiku (fast tier) → recruiter_reply / interview_invite /
     OA_link / rejection / spam / other, with a one-line summary
  2. match_application() — sender domain + company + fuzzy position, against
     open tracker cards. Deterministic and testable.
  3. route()     — upsert EmailThread; on interview_invite → Interview and
     rejection → Rejected (storing prev_status so it can be undone); push to the
     Notification Center; interview invites also flag the card + add a stage.
"""

import re
import logging
from datetime import datetime
from difflib import SequenceMatcher

from database import (
    EmailThread, JobApplication, Notification, JobPosting,
)
from llm import complete_json

logger = logging.getLogger("agent_logger")

# ATS/job-board relay domains — mail from here won't carry the employer's own
# domain, so matching must lean on company/position text instead.
RELAY_DOMAINS = {
    "greenhouse-mail.io", "greenhouse.io", "us.greenhouse-mail.io",
    "hire.lever.co", "lever.co", "ashbyhq.com", "app.ashbyhq.com",
    "smartrecruiters.com", "myworkday.com", "myworkdayjobs.com",
    "workday.com", "icims.com", "myworkdaysite.com", "gmail.com",
    "google.com", "linkedin.com", "indeedemail.com", "indeed.com",
}

CLASSIFICATIONS = ("recruiter_reply", "interview_invite", "OA_link",
                   "rejection", "spam", "other")

# status moves that fire automatically (reversible); everything else is
# notify-only so we never silently regress a card
STATUS_MOVES = {"interview_invite": "interview", "rejection": "rejected"}

_word = re.compile(r"[a-z0-9]+")
_STOP = {"inc", "llc", "ltd", "corp", "co", "the", "team", "careers", "talent",
         "recruiting", "hr", "people", "jobs", "notification", "notifications",
         "no", "reply", "noreply", "donotreply", "mail", "via"}


def _tokens(text: str) -> set:
    return {t for t in _word.findall((text or "").lower()) if t not in _STOP and len(t) > 1}


def domain_of(email: str) -> str:
    m = re.search(r"@([\w.-]+)", email or "")
    return m.group(1).lower() if m else ""


def company_from_domain(domain: str) -> str:
    """'jobs.stripe.com' → 'stripe' (drops known public suffixes & subdomains)."""
    if not domain or domain in RELAY_DOMAINS:
        return ""
    parts = domain.split(".")
    if len(parts) >= 2:
        return parts[-2]
    return parts[0]


# ─── Classification ──────────────────────────────────────────────────────────

_CLASSIFY_PROMPT = """Classify this job-application email into exactly one bucket:
- interview_invite: proposes/schedules an interview or asks for availability
- OA_link: sends an online assessment / coding test / take-home link
- rejection: declines the candidate or says the role was filled/closed
- recruiter_reply: a recruiter/coordinator message that isn't the above (intro,
  follow-up, scheduling logistics after an interview is already set, questions)
- spam: marketing, newsletters, job-board digests, unrelated
- other: anything that doesn't fit

From: {sender}
Subject: {subject}
Body (truncated):
{body}

Reply with ONLY JSON: {{"classification": "...", "confidence": 0.0-1.0, "summary": "one short sentence"}}"""


def classify(msg: dict) -> dict:
    data = complete_json(_CLASSIFY_PROMPT.format(
        sender=f'{msg.get("from_name", "")} <{msg.get("from_email", "")}>',
        subject=msg.get("subject", ""),
        body=(msg.get("body") or msg.get("snippet") or "")[:2500],
    ), tier="fast")
    if not isinstance(data, dict) or data.get("classification") not in CLASSIFICATIONS:
        # conservative fallback: treat as a recruiter reply so it surfaces,
        # never as an auto-moving class
        return {"classification": "other", "confidence": 0.3,
                "summary": (msg.get("subject") or "")[:200]}
    data["confidence"] = float(data.get("confidence", 0.5) or 0.5)
    data["summary"] = str(data.get("summary", ""))[:500]
    return data


# ─── Matching ────────────────────────────────────────────────────────────────

def match_application(db, msg: dict):
    """Best open tracker card for this email, or None. Score = company-domain
    match (strong) + company-name-in-text + position-token overlap."""
    apps = (db.query(JobApplication)
            .filter(JobApplication.status != "rejected")
            .order_by(JobApplication.updated_at.desc()).all())
    if not apps:
        return None, 0.0

    domain = domain_of(msg.get("from_email", ""))
    dom_company = company_from_domain(domain)
    haystack = " ".join([msg.get("from_name", ""), msg.get("subject", ""),
                         msg.get("snippet", ""), (msg.get("body") or "")[:1500]]).lower()
    subj_tokens = _tokens(msg.get("subject", ""))

    best, best_score = None, 0.0
    for app in apps:
        score = 0.0
        comp_tokens = _tokens(app.company_name)
        if not comp_tokens:
            continue

        # 1) employer's own domain in the sender (strongest signal)
        if dom_company and comp_tokens:
            if dom_company in comp_tokens or any(
                    SequenceMatcher(None, dom_company, c).ratio() >= 0.9 for c in comp_tokens):
                score += 0.6

        # 2) company name appears in the email text
        if comp_tokens & _tokens(haystack):
            score += 0.3
        elif app.company_name.lower() in haystack:
            score += 0.3

        # 3) fuzzy position overlap with the subject
        pos_tokens = _tokens(app.position)
        if pos_tokens and subj_tokens:
            overlap = len(pos_tokens & subj_tokens) / len(pos_tokens)
            score += 0.3 * overlap

        # tie-break nudge toward the linked posting's company via its ATS relay
        if score and app.job_url and domain and domain in app.job_url.lower():
            score += 0.1

        if score > best_score:
            best, best_score = app, score

    # require a real signal, not just a stray token
    return (best, round(best_score, 2)) if best_score >= 0.4 else (None, round(best_score, 2))


# ─── Routing ─────────────────────────────────────────────────────────────────

def _parse_date(value):
    if isinstance(value, datetime):
        return value
    return None


def route(db, msg: dict, cls: dict = None) -> EmailThread:
    """Upsert the thread, match it, apply reversible status moves + notify.
    Returns the EmailThread. classify() is called here if cls not supplied."""
    tid = msg.get("thread_id")
    thread = db.query(EmailThread).filter_by(gmail_thread_id=tid).first()
    last_at = _parse_date(msg.get("date")) or datetime.utcnow()

    # skip re-processing if we've already seen this thread at this timestamp
    if thread and thread.last_message_at and last_at <= thread.last_message_at:
        return thread

    cls = cls or classify(msg)
    app, confidence_match = match_application(db, msg)
    is_new_thread = thread is None
    if not thread:
        thread = EmailThread(gmail_thread_id=tid)
        db.add(thread)

    thread.classification = cls["classification"]
    thread.confidence = cls.get("confidence")
    thread.summary = cls.get("summary")
    thread.from_email = (msg.get("from_email") or "")[:320]
    thread.from_name = (msg.get("from_name") or "")[:255]
    thread.subject = (msg.get("subject") or "")[:998]
    thread.snippet = (msg.get("snippet") or "")[:2000]
    thread.last_message_at = last_at
    thread.matched_application_id = app.id if app else None
    thread.is_read = False
    db.flush()

    classification = cls["classification"]
    if classification == "spam":
        db.commit()
        return thread

    # ── reversible tracker status moves ──────────────────────────────────────
    action = None
    if app and classification in STATUS_MOVES:
        target = STATUS_MOVES[classification]
        if app.status != target:
            thread.prev_status = app.status
            app.status = target
            app.updated_at = datetime.utcnow()
            action = f"moved '{app.company_name}' card to {target.title()}"
            if classification == "interview_invite":
                app.priority = max(app.priority or 0, 1)
                stages = list(app.interview_stages or [])
                stages.append({"stage": "Interview invite",
                               "date": last_at.isoformat(),
                               "notes": cls.get("summary", ""), "passed": None})
                app.interview_stages = stages
            thread.auto_action = action

    # ── notification center ──────────────────────────────────────────────────
    ntype = {"interview_invite": "interview_invite", "rejection": "recruiter_email",
             "OA_link": "recruiter_email", "recruiter_reply": "recruiter_email"}.get(
                 classification, "system")
    icon = {"interview_invite": "🎉", "rejection": "🙁", "OA_link": "📝",
            "recruiter_reply": "✉️"}.get(classification, "✉️")
    who = thread.from_name or thread.from_email or "Someone"
    title = f"{icon} {classification.replace('_', ' ').title()}" + \
            (f" — {app.company_name}" if app else "")
    message = cls.get("summary", "") + (f" ({action})" if action else "")
    db.add(Notification(type=ntype, title=title[:512], message=message[:2000],
                        data={"email_thread_id": thread.id,
                              "application_id": app.id if app else None,
                              "classification": classification}))

    db.commit()
    logger.info(f"[EmailRouter] thread {tid[:12]} → {classification} "
                f"(match={app.company_name if app else 'none'} {confidence_match}) "
                f"{action or ''}")
    return thread


def undo_status_move(db, thread: EmailThread) -> bool:
    """Revert an auto status move. Returns True if something was reverted."""
    if not thread.prev_status or not thread.matched_application_id:
        return False
    app = db.query(JobApplication).filter_by(id=thread.matched_application_id).first()
    if not app:
        return False
    app.status = thread.prev_status
    app.updated_at = datetime.utcnow()
    thread.auto_action = f"undone (restored to {thread.prev_status.title()})"
    thread.prev_status = None
    db.commit()
    return True


# ─── Ingestion orchestrator (called by the scheduler) ────────────────────────

def ingest(db, reader, since_minutes: int = 30, max_threads: int = 40) -> str:
    """Pull recent messages via the reader and route each. reader.fetch_recent()
    returns normalized message dicts; the newest message per thread wins."""
    try:
        messages = reader.fetch_recent(since_minutes=since_minutes, limit=max_threads)
    except Exception as e:
        logger.warning(f"[EmailRouter] reader failed: {e}")
        return f"email read failed: {e}"
    if not messages:
        return "0 new emails"

    # collapse to newest message per thread
    by_thread = {}
    for m in messages:
        tid = m.get("thread_id")
        if not tid:
            continue
        cur = by_thread.get(tid)
        if not cur or (m.get("date") and cur.get("date") and m["date"] > cur["date"]):
            by_thread[tid] = m

    routed, moved = 0, 0
    for m in by_thread.values():
        try:
            t = route(db, m)
            routed += 1
            if t.auto_action and "moved" in (t.auto_action or ""):
                moved += 1
        except Exception as e:
            db.rollback()
            logger.warning(f"[EmailRouter] route failed for {m.get('thread_id')}: {e}")
    return f"routed {routed} thread(s), {moved} status move(s)"
