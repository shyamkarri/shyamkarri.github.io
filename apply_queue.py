"""
DB-backed apply queue + the non-negotiable guardrails.

enqueue_run() is the ONLY way a run enters the queue, and it enforces:
  1. Global kill switch (Profile settings) — refuses everything when on
  2. An APPROVED TailoredResume must exist for the posting — no exceptions
  3. Dedupe — never two active/submitted runs for the same posting or the
     same dedupe_hash (same job found via another board)
  4. Daily cap — max_apps_per_day from Profile (default 20)
  5. Per-company cooldown — no application to the same company within 14 days
The worker re-checks the kill switch and the approval before submitting, so a
run that was queued before the switch flipped still cannot submit.
"""

import logging
from datetime import datetime, timedelta

from database import (
    ApplicationRun, JobPosting, TailoredResume, CoverLetter,
    CandidateProfile, JobApplication, BaseResume,
)

logger = logging.getLogger("agent_logger")

COMPANY_COOLDOWN_DAYS = 14
ACTIVE_STATUSES = ("queued", "running", "needs_review", "awaiting_approval", "submitting")
KNOWN_ATS = ("greenhouse", "lever", "ashby", "smartrecruiters", "workday")


def enqueue_run(db, job_posting_id: int):
    """Returns (run, None) on success or (None, reason) when a guardrail refuses."""
    profile = db.query(CandidateProfile).first()
    if profile and profile.kill_switch:
        return None, "Kill switch is ON (Profile → Agent Guardrails)"

    posting = db.query(JobPosting).filter_by(id=job_posting_id).first()
    if not posting:
        return None, "Posting not found"
    ats = (posting.ats_type or posting.source or "").lower()
    if ats not in KNOWN_ATS:
        return None, f"No adapter for ATS '{ats}'"
    if not (posting.apply_url or posting.url):
        return None, "Posting has no apply URL"

    # human approval gate — hard requirement
    tailored = (db.query(TailoredResume)
                .filter_by(job_posting_id=job_posting_id, status="approved")
                .order_by(TailoredResume.approved_at.desc()).first())
    if not tailored:
        return None, "No APPROVED tailored resume for this posting — review and approve it first"

    # dedupe: same posting, or same job seen through another board
    dupe = (db.query(ApplicationRun)
            .join(JobPosting, ApplicationRun.job_posting_id == JobPosting.id)
            .filter(ApplicationRun.status.in_(ACTIVE_STATUSES + ("submitted",)))
            .filter((ApplicationRun.job_posting_id == job_posting_id) |
                    (JobPosting.dedupe_hash == posting.dedupe_hash)
                    if posting.dedupe_hash else
                    (ApplicationRun.job_posting_id == job_posting_id))
            .first())
    if dupe:
        return None, f"Already have run #{dupe.id} ({dupe.status}) for this job"

    # daily cap
    max_per_day = (profile.max_apps_per_day if profile and profile.max_apps_per_day else 20)
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_count = (db.query(ApplicationRun)
                   .filter(ApplicationRun.created_at >= today)
                   .filter(ApplicationRun.status.notin_(("cancelled", "skipped")))
                   .count())
    if today_count >= max_per_day:
        return None, f"Daily cap reached ({today_count}/{max_per_day} runs today)"

    # per-company cooldown
    cutoff = datetime.utcnow() - timedelta(days=COMPANY_COOLDOWN_DAYS)
    recent = (db.query(ApplicationRun)
              .join(JobPosting, ApplicationRun.job_posting_id == JobPosting.id)
              .filter(JobPosting.company.ilike(posting.company))
              .filter(ApplicationRun.status == "submitted")
              .filter(ApplicationRun.finished_at >= cutoff)
              .first())
    if recent:
        return None, (f"Applied to {posting.company} {(datetime.utcnow() - recent.finished_at).days}d ago "
                      f"— {COMPANY_COOLDOWN_DAYS}d cooldown")

    letter = (db.query(CoverLetter)
              .filter_by(job_posting_id=job_posting_id, status="approved")
              .order_by(CoverLetter.created_at.desc()).first())

    run = ApplicationRun(
        job_posting_id=job_posting_id,
        tailored_resume_id=tailored.id,
        cover_letter_id=letter.id if letter else None,
        ats_type=ats, status="queued",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    logger.info(f"[Queue] run #{run.id} queued: {posting.title} @ {posting.company} ({ats})")
    return run, None


def pipeline_items(db, stage: str = None):
    """Every posting that has entered the apply pipeline (has a tailored resume
    or a run), with the combined stage the queue page groups by."""
    from sqlalchemy import or_
    posting_ids = {
        pid for (pid,) in db.query(TailoredResume.job_posting_id).distinct().all()
    } | {
        pid for (pid,) in db.query(ApplicationRun.job_posting_id).distinct().all()
    }
    items = []
    for pid in posting_ids:
        posting = db.query(JobPosting).filter_by(id=pid).first()
        if not posting:
            continue
        tailored = (db.query(TailoredResume).filter_by(job_posting_id=pid)
                    .order_by(TailoredResume.created_at.desc()).first())
        letter = (db.query(CoverLetter).filter_by(job_posting_id=pid)
                  .order_by(CoverLetter.created_at.desc()).first())
        run = (db.query(ApplicationRun).filter_by(job_posting_id=pid)
               .order_by(ApplicationRun.created_at.desc()).first())
        items.append({
            "job_posting_id": pid, "company": posting.company, "title": posting.title,
            "url": posting.url, "ats_type": posting.ats_type or posting.source,
            "match_score": posting.match_score,
            "tailored_id": tailored.id if tailored else None,
            "tailored_status": tailored.status if tailored else None,
            "cover_letter_status": letter.status if letter else None,
            "run_id": run.id if run else None,
            "run_status": run.status if run else None,
            "stage": _stage(tailored, run),
            "needs_review_reason": run.needs_review_reason if run else None,
            "created_at": (run or tailored).created_at.isoformat()
            if (run or tailored) and (run or tailored).created_at else None,
        })
    items.sort(key=lambda x: (x["match_score"] or 0), reverse=True)
    if stage:
        items = [i for i in items if i["stage"] == stage]
    return items


def _stage(tailored, run) -> str:
    if run and run.status not in ("cancelled", "failed"):
        return {"submitted": "submitted", "running": "running", "submitting": "running",
                "queued": "queued", "needs_review": "needs_review",
                "awaiting_approval": "awaiting_approval"}.get(run.status, run.status)
    if not tailored:
        return "cancelled" if (run and run.status == "cancelled") else "unknown"
    if tailored.status == "approved":
        return "ready_to_send"        # approved materials, not yet queued (or last run failed)
    if tailored.status in ("queued", "generating"):
        return "tailoring"
    if tailored.status == "draft":
        return "needs_your_approval"  # tailoring done, awaiting your review
    return tailored.status            # rejected / failed


def submit_all_approved(db):
    """Enqueue a run for every pipeline item whose materials are approved and
    that has no active run. Returns (queued_count, skipped[])."""
    queued, skipped = 0, []
    for item in pipeline_items(db):
        if item["stage"] != "ready_to_send":
            continue
        run, reason = enqueue_run(db, item["job_posting_id"])
        if run:
            queued += 1
        else:
            skipped.append({"company": item["company"], "title": item["title"], "reason": reason})
    return queued, skipped


AUTOPILOT_TAILOR_CAP = 5   # max auto-tailors per enrich cycle (rate/cost guard)


def _company_allowlisted(profile, company: str) -> bool:
    allow = [c.strip().lower() for c in (profile.company_allowlist or []) if c]
    return (company or "").strip().lower() in allow


def autopilot_scan(db, posting_ids=None) -> str:
    """Semi-auto: for postings scoring ≥ threshold under auto_mode, auto-tailor
    (creates a DRAFT that still needs your approval). Full-auto (auto_mode AND
    company allowlisted) additionally auto-approves + queues the run — this is
    the only path that reaches the queue without a click, and it still can't
    submit anything the tailoring validator flagged. Kill switch stops all."""
    import tailor  # local import avoids a tailor↔apply_queue cycle

    profile = db.query(CandidateProfile).first()
    if not profile or not profile.auto_mode or profile.kill_switch:
        return "autopilot off"
    threshold = profile.auto_threshold or 80

    q = db.query(JobPosting).filter(JobPosting.match_score >= threshold)
    if posting_ids:
        q = q.filter(JobPosting.id.in_(posting_ids))
    candidates = q.order_by(JobPosting.match_score.desc()).limit(50).all()

    resume = (db.query(BaseResume).filter_by(is_default=True, parse_status="ready").first()
              or db.query(BaseResume).filter_by(parse_status="ready").first())
    if not resume:
        return "autopilot: no parsed base resume"

    tailored_count, sent = 0, 0
    for posting in candidates:
        if tailored_count >= AUTOPILOT_TAILOR_CAP:
            break
        if db.query(TailoredResume).filter_by(job_posting_id=posting.id).first():
            continue  # already in the pipeline
        ats = (posting.ats_type or posting.source or "").lower()
        if ats not in KNOWN_ATS:
            continue
        t = TailoredResume(job_posting_id=posting.id, base_resume_id=resume.id, status="queued")
        db.add(t)
        db.commit()
        db.refresh(t)
        tailor.run_tailor(t.id)          # inline (this runs off the request path)
        tailored_count += 1
        db.refresh(t)
        if t.status == "draft" and _company_allowlisted(profile, posting.company):
            # full-auto: approve pre-vetted materials and queue
            tailor.approve_and_render(db, t)
            run, reason = enqueue_run(db, posting.id)
            if run:
                sent += 1
            else:
                logger.info(f"[Autopilot] {posting.company} allowlisted but not queued: {reason}")
    return f"autopilot: tailored {tailored_count}, auto-queued {sent}"


def run_to_dict(run: ApplicationRun, db=None, full: bool = False) -> dict:
    d = {
        "id": run.id, "job_posting_id": run.job_posting_id,
        "tailored_resume_id": run.tailored_resume_id,
        "application_id": run.application_id,
        "ats_type": run.ats_type, "status": run.status, "attempt": run.attempt,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_ms": run.duration_ms,
        "needs_review_reason": run.needs_review_reason,
        "error": run.error, "current_url": run.current_url,
        "confirmation_text": run.confirmation_text,
        "receipt_count": len(run.field_receipts or []),
        "pending_answers": run.pending_answers,
    }
    if full:
        d["field_receipts"] = run.field_receipts or []
        if db is not None:
            from database import RunArtifact
            d["screenshots"] = [
                {"id": a.id, "name": a.name}
                for a in db.query(RunArtifact).filter_by(run_id=run.id)
                .order_by(RunArtifact.id).all()
            ]
    if run.posting:
        d["company"] = run.posting.company
        d["title"] = run.posting.title
        d["url"] = run.posting.url
    return d


def mark_submitted_manually(db, run: ApplicationRun, note: str = ""):
    """User finished a needs_review run by hand in their own browser."""
    run.status = "submitted"
    run.finished_at = datetime.utcnow()
    run.confirmation_text = f"Submitted manually by user. {note}".strip()
    _create_tracker_card(db, run)
    db.commit()


def _create_tracker_card(db, run: ApplicationRun):
    """On submit: create/advance the kanban card and link the receipt."""
    posting = db.query(JobPosting).filter_by(id=run.job_posting_id).first()
    if not posting:
        return
    app = None
    if posting.tracked_application_id:
        app = db.query(JobApplication).filter_by(id=posting.tracked_application_id).first()
    if not app:
        app = JobApplication(company_name=posting.company, position=posting.title,
                             job_url=posting.url, location=posting.location,
                             remote=posting.remote)
        db.add(app)
        db.flush()
        posting.tracked_application_id = app.id
    app.status = "applied"
    app.application_date = datetime.utcnow()
    app.notes = ((app.notes or "") +
                 f"\nAuto-applied via {run.ats_type} — run #{run.id} receipt in Apply Runs.").strip()
    run.application_id = app.id
