"""
Ops & hardening — adapter health, selector-drift alarm, nightly maintenance,
application funnel, and boot-time env validation.

Every apply run already writes an AgentExecution row (agent_name = apply_<ats>,
success = reached "submitted"). This module reads those to score each adapter
and to raise the selector-drift alarm the spec asks for: if an adapter's
success rate over its last N runs falls below a threshold, notify that it is
likely broken (selectors drifted).
"""

import os
import logging
from datetime import datetime, timedelta

from sqlalchemy import func

from database import (
    AgentExecution, ApplicationRun, JobApplication, JobPosting, Notification,
)

logger = logging.getLogger("agent_logger")

DRIFT_WINDOW = 10          # look at each adapter's last N runs
DRIFT_MIN_SAMPLE = 5       # ...but only alarm once we have enough signal
DRIFT_THRESHOLD = 0.70     # success rate below this ⇒ likely broken
ADAPTERS = ("greenhouse", "lever", "ashby", "smartrecruiters", "workday")


def adapter_health(db) -> list:
    """Per-adapter stats over each adapter's most recent DRIFT_WINDOW runs."""
    out = []
    for ats in ADAPTERS:
        rows = (db.query(AgentExecution)
                .filter(AgentExecution.agent_name == f"apply_{ats}")
                .order_by(AgentExecution.timestamp.desc())
                .limit(DRIFT_WINDOW).all())
        if not rows:
            out.append({"adapter": ats, "runs": 0, "success_rate": None,
                        "healthy": None, "last_run": None})
            continue
        ok = sum(1 for r in rows if r.success)
        rate = ok / len(rows)
        healthy = not (len(rows) >= DRIFT_MIN_SAMPLE and rate < DRIFT_THRESHOLD)
        out.append({
            "adapter": ats, "runs": len(rows),
            "success_rate": round(rate, 2),
            "healthy": healthy,
            "last_run": rows[0].timestamp.isoformat() if rows[0].timestamp else None,
        })
    return out


def check_selector_drift(db) -> str:
    """Raise a notification for any adapter that has drifted. De-duped: won't
    re-notify for the same adapter within 24h."""
    alarms = []
    for h in adapter_health(db):
        if h["healthy"] is False:
            title = f"⚠️ Adapter '{h['adapter']}' likely broken"
            since = datetime.utcnow() - timedelta(hours=24)
            already = (db.query(Notification)
                       .filter(Notification.title == title)
                       .filter(Notification.created_at >= since).first())
            if not already:
                db.add(Notification(
                    type="system", title=title,
                    message=(f"Only {int(h['success_rate'] * 100)}% of the last "
                             f"{h['runs']} {h['adapter']} apply runs succeeded "
                             f"(threshold {int(DRIFT_THRESHOLD * 100)}%). Its selectors "
                             "have probably changed — check recent run screenshots.")))
                db.commit()
            alarms.append(h["adapter"])
    return f"drift alarms: {', '.join(alarms)}" if alarms else "all adapters healthy"


def retry_failed_runs(db) -> str:
    """Nightly: re-queue each failed run once. A '[nightly-retried]' tag in the
    error keeps us from looping on the same permanently-broken run."""
    failed = (db.query(ApplicationRun)
              .filter(ApplicationRun.status == "failed")
              .filter(~ApplicationRun.error.like("%[nightly-retried]%"))
              .all())
    for run in failed:
        run.status = "queued"
        run.attempt = 0
        run.error = ((run.error or "") + " [nightly-retried]")[:2000]
    db.commit()
    return f"re-queued {len(failed)} failed run(s)"


def application_funnel(db, since_days: int = 30) -> dict:
    """Real-run + tracker funnel for the weekly report and the ops view."""
    since = datetime.utcnow() - timedelta(days=since_days)
    run_counts = dict(
        db.query(ApplicationRun.status, func.count(ApplicationRun.id))
        .filter(ApplicationRun.created_at >= since)
        .group_by(ApplicationRun.status).all())
    tracker_counts = dict(
        db.query(JobApplication.status, func.count(JobApplication.id))
        .group_by(JobApplication.status).all())
    submitted = run_counts.get("submitted", 0)
    interviews = tracker_counts.get("interview", 0) + tracker_counts.get("final_round", 0)
    offers = tracker_counts.get("offer", 0)
    return {
        "window_days": since_days,
        "runs_by_status": run_counts,
        "tracker_by_status": tracker_counts,
        "auto_submitted": submitted,
        "needs_review": run_counts.get("needs_review", 0),
        "failed": run_counts.get("failed", 0),
        "interview_rate": round(interviews / submitted, 2) if submitted else None,
        "offer_rate": round(offers / submitted, 2) if submitted else None,
    }


def nightly_maintenance(db) -> str:
    """One scheduler job: refresh scrapes, re-queue failed runs, drift check."""
    parts = []
    try:
        from job_scraper import run_scrape
        from job_enrich import enrich_and_score_new_jobs
        parts.append("scrape: " + run_scrape())
        parts.append("score: " + enrich_and_score_new_jobs(db))
    except Exception as e:
        parts.append(f"scrape/score failed: {e}")
    try:
        parts.append(retry_failed_runs(db))
    except Exception as e:
        parts.append(f"retry failed: {e}")
    try:
        parts.append(check_selector_drift(db))
    except Exception as e:
        parts.append(f"drift check failed: {e}")
    return " | ".join(parts)


# ─── Boot-time env validation ────────────────────────────────────────────────

def validate_env(context: str = "web") -> list:
    """Warn (not crash) about missing/weak config. Returns the list of issues."""
    issues = []
    if not os.getenv("DATABASE_URL"):
        issues.append("DATABASE_URL unset → using ephemeral SQLite; data is wiped on every "
                      "Render restart. Set a durable Postgres URL.")
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("GROQ_API_KEY")):
        issues.append("Neither ANTHROPIC_API_KEY nor GROQ_API_KEY set → tailoring, "
                      "answer generation, and email classification will fail.")
    secret = os.getenv("SECRET_KEY", "")
    if not secret or "change" in secret.lower():
        issues.append("SECRET_KEY is default/weak → set a strong random value "
                      "(openssl rand -hex 32).")
    if not os.getenv("ATS_CREDS_PUBLIC_KEY"):
        issues.append("ATS_CREDS_PUBLIC_KEY unset → Workday credential sealing is disabled "
                      "(python3 crypto_box.py keygen).")
    if not (os.getenv("GMAIL_OAUTH_REFRESH_TOKEN") or os.getenv("GMAIL_APP_PASSWORD")):
        issues.append("No Gmail source (GMAIL_OAUTH_* or GMAIL_USER/GMAIL_APP_PASSWORD) → "
                      "email routing / Inbox will be idle.")
    if context == "worker" and not os.getenv("ATS_CREDS_PRIVATE_KEY"):
        issues.append("ATS_CREDS_PRIVATE_KEY unset on the worker → Workday sign-in can't decrypt.")
    for msg in issues:
        logger.warning(f"[env:{context}] ⚠️  {msg}")
    if not issues:
        logger.info(f"[env:{context}] all critical env vars present")
    return issues


def init_sentry(context: str = "web") -> bool:
    """Optional error tracking. No-op unless SENTRY_DSN is set and the SDK is installed."""
    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        return False
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=dsn, traces_sample_rate=0.0,
                        environment=os.getenv("RENDER_SERVICE_NAME", context))
        logger.info(f"[sentry] initialized for {context}")
        return True
    except Exception as e:
        logger.warning(f"[sentry] init skipped: {e}")
        return False
