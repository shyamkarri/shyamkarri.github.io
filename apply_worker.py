"""
Auto-apply worker — separate process/service that consumes the DB apply queue
with Playwright. Run it:

  locally (headed — lets you solve CAPTCHAs in the visible browser):
      PW_HEADLESS=false python3 apply_worker.py
  on Render: worker service built from Dockerfile.worker (headless)

Lifecycle per run:
  queued → running → (fill, screenshot every step, receipt every field)
    → awaiting_approval  when generated answers need your sign-off
                         (approve in the dashboard → run re-queues and reuses
                          the now-approved AnswerBank entries)
    → needs_review       on CAPTCHA / unknown fields / validation errors —
                         with screenshots + the URL so you can finish by hand.
                         In headed mode the browser stays open and polls for
                         your "Resume run" click after you solve the CAPTCHA.
    → submitting → submitted   (tracker card auto-created, receipt linked)
  Retries: transient errors requeue with backoff, max 3 attempts, then failed.
  NOTHING submits without an approved TailoredResume and the kill switch off —
  both re-checked here immediately before the submit click.
"""

import os
import sys
import time
import socket
import logging
from datetime import datetime

from database import (
    SessionLocal, init_db, ApplicationRun, JobPosting, TailoredResume,
    CoverLetter, CandidateProfile, Notification, AgentExecution,
)
from apply_queue import _create_tracker_card
from adapters import get_adapter, RunContext, ApplicationData, CaptchaDetected, NeedsReview

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("agent_logger")

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
POLL_SECONDS = int(os.getenv("WORKER_POLL_SECONDS", "5"))
HEADLESS = os.getenv("PW_HEADLESS", "true").lower() != "false"
CAPTCHA_WAIT_SECONDS = int(os.getenv("CAPTCHA_WAIT_SECONDS", "600"))
MAX_ATTEMPTS = 3
PW_PROFILE_DIR = os.getenv("PW_PROFILE_DIR", os.path.join(
    os.getenv("UPLOADS_DIR", "./uploads"), "pw_profile"))

RETRYABLE = ("TimeoutError", "TargetClosedError", "ConnectionError", "NetworkError")


def validate_env():
    problems = []
    if not os.getenv("DATABASE_URL"):
        problems.append("DATABASE_URL not set — worker would write to a local SQLite "
                        "the web service can't see")
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("GROQ_API_KEY")):
        problems.append("No ANTHROPIC_API_KEY or GROQ_API_KEY — answer generation will fail")
    try:
        import playwright  # noqa: F401
    except ImportError:
        problems.append("playwright not installed (pip install playwright && playwright install chromium)")
    for p in problems:
        logger.warning(f"[Worker] ⚠️  {p}")
    return problems


def notify(db, ntype: str, title: str, message: str, run_id: int = None):
    db.add(Notification(type=ntype, title=title, message=message,
                        data={"run_id": run_id} if run_id else None))
    db.commit()


def claim_next(db):
    """Claim one runnable row. Single worker per deployment, so a simple
    check-then-set is fine; the status filter keeps double-claims out anyway."""
    run = (db.query(ApplicationRun)
           .filter(
               (ApplicationRun.status == "queued") |
               ((ApplicationRun.status.in_(("needs_review", "awaiting_approval"))) &
                (ApplicationRun.resume_requested.is_(True))))
           .order_by(ApplicationRun.created_at)
           .first())
    if not run:
        return None
    if run.status in ("needs_review", "awaiting_approval"):
        run.attempt = 0  # human intervened — fresh attempt budget
    run.status = "running"
    run.resume_requested = False
    run.worker_id = WORKER_ID
    run.started_at = datetime.utcnow()
    db.commit()
    return run


def build_app_data(db, run) -> ApplicationData:
    posting = db.query(JobPosting).filter_by(id=run.job_posting_id).first()
    tailored = db.query(TailoredResume).filter_by(id=run.tailored_resume_id).first()
    profile = db.query(CandidateProfile).first()
    letter = (db.query(CoverLetter).filter_by(id=run.cover_letter_id).first()
              if run.cover_letter_id else None)

    # ── hard gates (re-checked even though enqueue already checked) ──────────
    if profile and profile.kill_switch:
        raise NeedsReview("Kill switch is ON — run paused")
    if not tailored or tailored.status != "approved":
        raise NeedsReview("Tailored resume is not approved — refusing to apply")
    if not tailored.pdf_path or not os.path.exists(tailored.pdf_path):
        # worker and web service have separate disks — re-render from the diff
        import tailor as tailor_mod
        tailor_mod.approve_and_render(db, tailored)

    full_name = (profile.full_name if profile else "") or ""
    parts = full_name.split()
    return ApplicationData(
        first_name=parts[0] if parts else "",
        last_name=" ".join(parts[1:]) if len(parts) > 1 else "",
        full_name=full_name,
        email=(profile.email if profile else "") or "",
        phone=(profile.phone if profile else "") or "",
        location=(profile.location if profile else "") or "",
        links=(profile.links if profile else {}) or {},
        resume_pdf_path=tailored.pdf_path,
        cover_letter_text=(letter.text if letter and letter.status == "approved" else ""),
        company=posting.company, title=posting.title,
        apply_url=posting.apply_url or posting.url,
        eeo=(profile.eeo_answers if profile else {}) or {},
        profile=profile,
    )


def execute_run(db, run):
    from playwright.sync_api import sync_playwright

    start = time.time()
    posting = db.query(JobPosting).filter_by(id=run.job_posting_id).first()
    logger.info(f"[Worker] run #{run.id} start: {posting.title} @ {posting.company} "
                f"({run.ats_type}, attempt {run.attempt + 1})")
    app = build_app_data(db, run)
    adapter = get_adapter(run.ats_type)

    os.makedirs(PW_PROFILE_DIR, exist_ok=True)
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            PW_PROFILE_DIR, headless=HEADLESS,
            viewport={"width": 1366, "height": 900},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"),
        )
        page = context.pages[0] if context.pages else context.new_page()
        ctx = RunContext(page, db, run, app)
        try:
            adapter.login(ctx)
            try:
                adapter.fill(ctx)
            except CaptchaDetected:
                if not _captcha_pause(db, run, ctx):
                    return
                adapter.fill(ctx)  # human cleared it — same page, refill/continue

            if ctx.pending:
                run.status = "awaiting_approval"
                run.pending_answers = ctx.pending
                run.needs_review_reason = (f"{len(ctx.pending)} generated answer(s) "
                                           "need your approval before submitting")
                db.commit()
                notify(db, "system", f"✋ Approve answers for {app.company}",
                       f"Run #{run.id} ({app.title}) generated "
                       f"{len(ctx.pending)} answer(s) — review them in the dashboard.",
                       run.id)
                logger.info(f"[Worker] run #{run.id} → awaiting_approval")
                return

            adapter.review(ctx)   # pre-submit screenshot + receipts

            # ── final gates immediately before the irreversible click ─────────
            db.refresh(run)
            profile = db.query(CandidateProfile).first()
            tailored = db.query(TailoredResume).filter_by(id=run.tailored_resume_id).first()
            if profile and profile.kill_switch:
                raise NeedsReview("Kill switch flipped ON during run — not submitting")
            if not tailored or tailored.status != "approved":
                raise NeedsReview("Tailored resume approval was revoked — not submitting")
            if run.status == "cancelled":
                logger.info(f"[Worker] run #{run.id} cancelled by user — stopping")
                return

            run.status = "submitting"
            db.commit()
            try:
                confirmation = adapter.submit(ctx)
            except CaptchaDetected:
                if not _captcha_pause(db, run, ctx):
                    return
                confirmation = adapter.submit(ctx)
            ctx.snap("confirmation")

            run.status = "submitted"
            run.confirmation_text = (confirmation or "")[:2000]
            run.finished_at = datetime.utcnow()
            run.duration_ms = (time.time() - start) * 1000
            _create_tracker_card(db, run)
            db.commit()
            notify(db, "system", f"✅ Applied: {app.title} @ {app.company}",
                   f"Run #{run.id} submitted via {run.ats_type} with "
                   f"{len(ctx.receipts)} field receipts.", run.id)
            _log_execution(db, run, True, None)
            logger.info(f"[Worker] run #{run.id} SUBMITTED in {run.duration_ms:.0f}ms")

        except NeedsReview as e:
            ctx.snap("needs_review")
            run.status = "needs_review"
            run.needs_review_reason = str(e)[:1000]
            run.finished_at = datetime.utcnow()
            run.duration_ms = (time.time() - start) * 1000
            db.commit()
            notify(db, "system", f"👀 Needs review: {app.company}",
                   f"Run #{run.id} paused: {e} — screenshots and the form URL "
                   "are in the run details.", run.id)
            _log_execution(db, run, False, str(e))
            logger.info(f"[Worker] run #{run.id} → needs_review: {e}")
        except Exception as e:
            _handle_error(db, run, ctx, e, start)
        finally:
            try:
                context.close()
            except Exception:
                pass


def _captcha_pause(db, run, ctx) -> bool:
    """CAPTCHA hit. Headed mode: keep the browser open and wait for the user
    to solve it + click Resume in the dashboard. Headless: park as
    needs_review (finish manually or retry from your laptop). Never bypassed."""
    run.status = "needs_review"
    run.needs_review_reason = ("CAPTCHA — solve it in the worker's browser window and "
                               "click ▶ Resume" if not HEADLESS else
                               "CAPTCHA — headless worker can't proceed; finish manually "
                               "at the run URL or retry from a headed local worker")
    db.commit()
    notify(db, "system", f"🧩 CAPTCHA on {ctx.app.company} application",
           run.needs_review_reason, run.id)
    if HEADLESS:
        return False
    logger.info(f"[Worker] run #{run.id} waiting up to {CAPTCHA_WAIT_SECONDS}s for human…")
    waited = 0
    while waited < CAPTCHA_WAIT_SECONDS:
        time.sleep(5)
        waited += 5
        db.refresh(run)
        if run.status == "cancelled":
            return False
        if run.resume_requested:
            run.resume_requested = False
            run.status = "running"
            run.needs_review_reason = None
            db.commit()
            if ctx.captcha_present():
                run.status = "needs_review"
                run.needs_review_reason = "CAPTCHA still present after resume"
                db.commit()
                return False
            logger.info(f"[Worker] run #{run.id} resumed by user after CAPTCHA")
            return True
    return False


def _handle_error(db, run, ctx, e, start):
    err = f"{type(e).__name__}: {e}"
    try:
        ctx.snap("error")
    except Exception:
        pass
    run.attempt = (run.attempt or 0) + 1
    retryable = any(t in type(e).__name__ for t in RETRYABLE)
    if retryable and run.attempt < MAX_ATTEMPTS:
        run.status = "queued"     # backoff: picked up again next poll cycles
        run.error = f"attempt {run.attempt} failed, will retry: {err[:800]}"
        db.commit()
        logger.warning(f"[Worker] run #{run.id} retrying ({run.attempt}/{MAX_ATTEMPTS}): {err}")
        time.sleep(min(30 * run.attempt, 90))
    else:
        run.status = "failed"
        run.error = err[:2000]
        run.finished_at = datetime.utcnow()
        run.duration_ms = (time.time() - start) * 1000
        db.commit()
        notify(db, "system", f"❌ Apply run failed: run #{run.id}",
               f"{err[:300]} — screenshots in run details.", run.id)
        _log_execution(db, run, False, err)
        logger.error(f"[Worker] run #{run.id} FAILED: {err}")


def _log_execution(db, run, success: bool, error):
    """Feed the existing Agent Analytics — each adapter is an 'agent'."""
    db.add(AgentExecution(
        agent_name=f"apply_{run.ats_type}", workflow="auto_apply",
        input_text=f"run #{run.id} posting #{run.job_posting_id}",
        output_text=(run.confirmation_text or run.needs_review_reason or "")[:500],
        latency_ms=run.duration_ms, success=success,
        error_message=(error or "")[:500] or None,
        metadata_={"run_id": run.id, "receipts": len(run.field_receipts or [])},
    ))
    db.commit()


def main():
    logger.info(f"[Worker] starting {WORKER_ID} — headless={HEADLESS}, "
                f"poll={POLL_SECONDS}s, profile={PW_PROFILE_DIR}")
    validate_env()
    try:
        import ops
        ops.init_sentry("worker")
        ops.validate_env("worker")
    except Exception as e:
        logger.warning(f"[Worker] ops init skipped: {e}")
    init_db()
    while True:
        db = SessionLocal()
        try:
            run = claim_next(db)
            if run:
                execute_run(db, run)
            else:
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            logger.info("[Worker] stopped")
            sys.exit(0)
        except Exception as e:
            logger.error(f"[Worker] loop error: {e}")
            time.sleep(POLL_SECONDS)
        finally:
            db.close()


if __name__ == "__main__":
    main()
