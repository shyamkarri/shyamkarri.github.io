"""
Apply-pipeline API — Profile, Resume bank, FactBank, JD enrichment.

Auth is applied router-wide when main.py includes this router
(dependencies=[Depends(get_current_user)]), so endpoints here only need get_db.
"""

import os
import logging
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel

from database import (
    SessionLocal, CandidateProfile, BaseResume, ResumeFact,
    TailoredResume, CoverLetter, JobPosting,
    ApplicationRun, RunArtifact, AnswerBankEntry, AtsCredential,
    EmailThread, JobApplication,
)
import resume_bank
import job_enrich
import tailor
import apply_queue
from answer_engine import normalize_question

logger = logging.getLogger("agent_logger")

router = APIRouter(prefix="/api", tags=["pipeline"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─── Pydantic models ─────────────────────────────────────────────────────────

class ProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    links: Optional[dict] = None
    work_authorization: Optional[str] = None
    salary_floor: Optional[int] = None
    target_titles: Optional[List[str]] = None
    target_locations: Optional[List[str]] = None
    remote_pref: Optional[str] = None
    eeo_answers: Optional[dict] = None
    tone_notes: Optional[str] = None
    kill_switch: Optional[bool] = None
    max_apps_per_day: Optional[int] = None
    auto_mode: Optional[bool] = None
    auto_threshold: Optional[int] = None
    company_allowlist: Optional[List[str]] = None


class FactCreate(BaseModel):
    category: str = "other"
    fact: str
    context: Optional[str] = None


class FactUpdate(BaseModel):
    category: Optional[str] = None
    fact: Optional[str] = None
    context: Optional[str] = None
    verified: Optional[bool] = None


class EnrichRequest(BaseModel):
    rescore_all: bool = False
    limit: int = 40


# ─── Profile ─────────────────────────────────────────────────────────────────

def _profile_to_dict(p: CandidateProfile) -> dict:
    return {
        "id": p.id, "full_name": p.full_name, "email": p.email, "phone": p.phone,
        "location": p.location, "links": p.links or {},
        "work_authorization": p.work_authorization, "salary_floor": p.salary_floor,
        "target_titles": p.target_titles or [], "target_locations": p.target_locations or [],
        "remote_pref": p.remote_pref, "eeo_answers": p.eeo_answers or {},
        "tone_notes": p.tone_notes,
        "kill_switch": bool(p.kill_switch), "max_apps_per_day": p.max_apps_per_day,
        "auto_mode": bool(p.auto_mode), "auto_threshold": p.auto_threshold,
        "company_allowlist": p.company_allowlist or [],
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _get_or_create_profile(db) -> CandidateProfile:
    p = db.query(CandidateProfile).first()
    if not p:
        p = CandidateProfile()
        db.add(p)
        db.commit()
        db.refresh(p)
    return p


@router.get("/profile")
async def get_profile(db=Depends(get_db)):
    return _profile_to_dict(_get_or_create_profile(db))


@router.put("/profile")
async def update_profile(req: ProfileUpdate, background_tasks: BackgroundTasks,
                         db=Depends(get_db)):
    p = _get_or_create_profile(db)
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(p, field, value)
    db.commit()
    db.refresh(p)
    # profile changes affect scores — recompute from stored requirements (no LLM)
    background_tasks.add_task(_rescore_bg)
    return _profile_to_dict(p)


def _rescore_bg():
    db = SessionLocal()
    try:
        logger.info(f"[Enrich] {job_enrich.rescore_existing(db)}")
    finally:
        db.close()


# ─── Resumes ─────────────────────────────────────────────────────────────────

def _resume_to_dict(r: BaseResume, fact_count: int = None) -> dict:
    return {
        "id": r.id, "label": r.label, "is_default": bool(r.is_default),
        "uploaded_at": r.uploaded_at.isoformat() if r.uploaded_at else None,
        "parse_status": r.parse_status, "parse_error": r.parse_error,
        "has_file": bool(r.file_path and os.path.exists(r.file_path)),
        "fact_count": fact_count,
    }


@router.get("/resumes")
async def list_resumes(db=Depends(get_db)):
    rows = db.query(BaseResume).order_by(BaseResume.uploaded_at.desc()).all()
    return {"resumes": [
        _resume_to_dict(r, db.query(ResumeFact).filter_by(resume_id=r.id).count())
        for r in rows
    ]}


@router.post("/resumes", status_code=201)
async def upload_resume(background_tasks: BackgroundTasks,
                        file: UploadFile = File(...),
                        label: str = Form(""),
                        db=Depends(get_db)):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="PDF too large (max 10 MB)")
    path = resume_bank.save_resume_file(file.filename, content)
    resume = BaseResume(
        label=label or file.filename,
        file_path=path,
        is_default=db.query(BaseResume).count() == 0,  # first upload becomes default
        parse_status="pending",
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)
    background_tasks.add_task(resume_bank.process_resume, resume.id)
    return _resume_to_dict(resume, 0)


@router.get("/resumes/{resume_id}")
async def get_resume(resume_id: int, db=Depends(get_db)):
    r = db.query(BaseResume).filter_by(id=resume_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Resume not found")
    d = _resume_to_dict(r, db.query(ResumeFact).filter_by(resume_id=r.id).count())
    d["parsed_json"] = r.parsed_json
    return d


@router.get("/resumes/{resume_id}/download")
async def download_resume(resume_id: int, db=Depends(get_db)):
    r = db.query(BaseResume).filter_by(id=resume_id).first()
    if not r or not r.file_path or not os.path.exists(r.file_path):
        raise HTTPException(status_code=404, detail="PDF not on disk (re-upload it)")
    return FileResponse(r.file_path, media_type="application/pdf",
                        filename=f"{r.label}.pdf")


@router.post("/resumes/{resume_id}/reparse")
async def reparse_resume(resume_id: int, background_tasks: BackgroundTasks,
                         db=Depends(get_db)):
    r = db.query(BaseResume).filter_by(id=resume_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Resume not found")
    if not r.raw_text and (not r.file_path or not os.path.exists(r.file_path)):
        raise HTTPException(status_code=400, detail="No PDF on disk and no cached text")
    r.parse_status = "pending"
    db.commit()
    background_tasks.add_task(resume_bank.process_resume, r.id)
    return {"message": "Re-parse started"}


@router.post("/resumes/{resume_id}/default")
async def set_default_resume(resume_id: int, db=Depends(get_db)):
    r = db.query(BaseResume).filter_by(id=resume_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Resume not found")
    db.query(BaseResume).update({"is_default": False})
    r.is_default = True
    db.commit()
    return {"message": f"'{r.label}' is now the default resume"}


@router.delete("/resumes/{resume_id}", status_code=204)
async def delete_resume(resume_id: int, db=Depends(get_db)):
    r = db.query(BaseResume).filter_by(id=resume_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Resume not found")
    db.query(ResumeFact).filter_by(resume_id=r.id).delete()
    if r.file_path and os.path.exists(r.file_path):
        try:
            os.remove(r.file_path)
        except OSError:
            pass
    db.delete(r)
    db.commit()


# ─── FactBank ────────────────────────────────────────────────────────────────

def _fact_to_dict(f: ResumeFact) -> dict:
    return {"id": f.id, "resume_id": f.resume_id, "category": f.category,
            "fact": f.fact, "context": f.context, "source": f.source,
            "verified": bool(f.verified)}


@router.get("/facts")
async def list_facts(category: Optional[str] = None,
                     resume_id: Optional[int] = None, db=Depends(get_db)):
    q = db.query(ResumeFact)
    if category:
        q = q.filter_by(category=category)
    if resume_id:
        q = q.filter_by(resume_id=resume_id)
    rows = q.order_by(ResumeFact.category, ResumeFact.id).all()
    return {"facts": [_fact_to_dict(f) for f in rows], "total": len(rows)}


@router.post("/facts", status_code=201)
async def create_fact(req: FactCreate, db=Depends(get_db)):
    if not req.fact.strip():
        raise HTTPException(status_code=400, detail="Fact text is required")
    cat = req.category if req.category in resume_bank.VALID_CATEGORIES else "other"
    f = ResumeFact(category=cat, fact=req.fact.strip(),
                   context=req.context, source="manual", resume_id=None)
    db.add(f)
    db.commit()
    db.refresh(f)
    return _fact_to_dict(f)


@router.patch("/facts/{fact_id}")
async def update_fact(fact_id: int, req: FactUpdate, db=Depends(get_db)):
    f = db.query(ResumeFact).filter_by(id=fact_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="Fact not found")
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(f, field, value)
    db.commit()
    return _fact_to_dict(f)


@router.delete("/facts/{fact_id}", status_code=204)
async def delete_fact(fact_id: int, db=Depends(get_db)):
    f = db.query(ResumeFact).filter_by(id=fact_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="Fact not found")
    db.delete(f)
    db.commit()


# ─── JD enrichment ───────────────────────────────────────────────────────────

@router.post("/jobs/enrich")
async def enrich_jobs(req: EnrichRequest, background_tasks: BackgroundTasks):
    """LLM-extract requirements + score postings missing them (background)."""
    def _run(limit, rescore_all):
        db = SessionLocal()
        try:
            logger.info(f"[Enrich] {job_enrich.enrich_and_score_new_jobs(db, limit=limit, rescore_all=rescore_all)}")
        finally:
            db.close()
    background_tasks.add_task(_run, min(req.limit, 100), req.rescore_all)
    return {"message": "Enrichment started — scores update as postings are processed"}


@router.post("/jobs/rescore")
async def rescore_jobs(db=Depends(get_db)):
    """Instant deterministic rescore from stored requirements (no LLM)."""
    return {"message": job_enrich.rescore_existing(db)}


# ─── Tailoring (Phase 2) ─────────────────────────────────────────────────────

class TailorRequest(BaseModel):
    base_resume_id: Optional[int] = None


class BulletEdit(BaseModel):
    ref: str
    accepted: Optional[bool] = None
    edited: Optional[str] = None


class TailoredUpdate(BaseModel):
    changes: Optional[List[BulletEdit]] = None
    skills_accepted: Optional[bool] = None
    summary_accepted: Optional[bool] = None
    status: Optional[str] = None    # approved / rejected / draft (re-open)


class CoverLetterUpdate(BaseModel):
    text: Optional[str] = None
    status: Optional[str] = None    # draft / approved / rejected


def _tailored_to_dict(t: TailoredResume, full: bool = False) -> dict:
    d = {
        "id": t.id, "job_posting_id": t.job_posting_id,
        "base_resume_id": t.base_resume_id, "status": t.status,
        "error": t.error, "has_pdf": bool(t.pdf_path and os.path.exists(t.pdf_path)),
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "approved_at": t.approved_at.isoformat() if t.approved_at else None,
        "changed_count": (t.diff_json or {}).get("changed_count"),
    }
    if full:
        d["diff_json"] = t.diff_json
    return d


def _letter_to_dict(c: CoverLetter) -> dict:
    return {"id": c.id, "job_posting_id": c.job_posting_id, "text": c.text,
            "status": c.status,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None}


@router.post("/jobs/{job_id}/tailor", status_code=201)
async def tailor_job(job_id: int, req: TailorRequest,
                     background_tasks: BackgroundTasks, db=Depends(get_db)):
    posting = db.query(JobPosting).filter_by(id=job_id).first()
    if not posting:
        raise HTTPException(status_code=404, detail="Job posting not found")

    resume = (db.query(BaseResume).filter_by(id=req.base_resume_id).first()
              if req.base_resume_id else
              db.query(BaseResume).filter_by(is_default=True, parse_status="ready").first()
              or db.query(BaseResume).filter_by(parse_status="ready").first())
    if not resume or resume.parse_status != "ready":
        raise HTTPException(status_code=400,
                            detail="No parsed base resume — upload one in Profile & Resumes first")
    if not db.query(ResumeFact).filter(ResumeFact.verified.is_(True)).count():
        raise HTTPException(status_code=400, detail="Fact bank is empty — parse a resume first")

    # reuse an in-flight run instead of stacking duplicates
    existing = (db.query(TailoredResume).filter_by(job_posting_id=job_id)
                .order_by(TailoredResume.created_at.desc()).first())
    if existing and existing.status in ("queued", "generating"):
        return _tailored_to_dict(existing)

    t = TailoredResume(job_posting_id=job_id, base_resume_id=resume.id, status="queued")
    db.add(t)
    db.commit()
    db.refresh(t)
    background_tasks.add_task(tailor.run_tailor, t.id)
    return _tailored_to_dict(t)


@router.get("/jobs/{job_id}/review")
async def job_review_bundle(job_id: int, db=Depends(get_db)):
    """Everything the approval modal needs in one call."""
    posting = db.query(JobPosting).filter_by(id=job_id).first()
    if not posting:
        raise HTTPException(status_code=404, detail="Job posting not found")
    t = (db.query(TailoredResume).filter_by(job_posting_id=job_id)
         .order_by(TailoredResume.created_at.desc()).first())
    letter = (db.query(CoverLetter).filter_by(job_posting_id=job_id)
              .order_by(CoverLetter.created_at.desc()).first())
    run = (db.query(ApplicationRun).filter_by(job_posting_id=job_id)
           .order_by(ApplicationRun.created_at.desc()).first())
    return {
        "posting": {"id": posting.id, "title": posting.title, "company": posting.company,
                    "url": posting.url, "match_score": posting.match_score,
                    "keywords": (posting.extracted_requirements or {}).get("keywords") or []},
        "tailored": _tailored_to_dict(t, full=True) if t else None,
        "cover_letter": _letter_to_dict(letter) if letter else None,
        "run": apply_queue.run_to_dict(run, db, full=True) if run else None,
    }


@router.patch("/tailored/{tailored_id}")
async def update_tailored(tailored_id: int, req: TailoredUpdate, db=Depends(get_db)):
    t = db.query(TailoredResume).filter_by(id=tailored_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Tailored resume not found")
    if t.status in ("queued", "generating"):
        raise HTTPException(status_code=409, detail="Still generating — wait for the draft")

    diff = dict(t.diff_json or {})
    if req.changes:
        by_ref = {b["ref"]: b for exp in diff.get("experiences", []) for b in exp["bullets"]}
        for edit in req.changes:
            b = by_ref.get(edit.ref)
            if not b:
                continue
            if edit.accepted is not None:
                b["accepted"] = edit.accepted and bool(b.get("new") or edit.edited)
            if edit.edited is not None:
                b["edited"] = edit.edited.strip() or None
    if req.skills_accepted is not None:
        diff["skills_accepted"] = req.skills_accepted
    if req.summary_accepted is not None:
        diff.setdefault("summary", {})["accepted"] = req.summary_accepted
    t.diff_json = diff

    if req.status == "approved":
        if not diff.get("experiences"):
            raise HTTPException(status_code=400, detail="No diff to approve")
        tailor.approve_and_render(db, t)   # human approval gate → render PDF
    elif req.status in ("rejected", "draft"):
        t.status = req.status
        db.commit()
    else:
        db.commit()
    return _tailored_to_dict(t, full=True)


@router.get("/tailored/{tailored_id}")
async def get_tailored(tailored_id: int, db=Depends(get_db)):
    t = db.query(TailoredResume).filter_by(id=tailored_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Tailored resume not found")
    return _tailored_to_dict(t, full=True)


@router.get("/tailored/{tailored_id}/pdf")
async def download_tailored_pdf(tailored_id: int, db=Depends(get_db)):
    t = db.query(TailoredResume).filter_by(id=tailored_id).first()
    if not t or not t.pdf_path or not os.path.exists(t.pdf_path):
        raise HTTPException(status_code=404, detail="PDF not rendered yet — approve the draft first")
    posting = db.query(JobPosting).filter_by(id=t.job_posting_id).first()
    return FileResponse(t.pdf_path, media_type="application/pdf",
                        filename=f"resume_{(posting.company if posting else 'job')}_{t.id}.pdf")


@router.post("/jobs/{job_id}/cover-letter", status_code=201)
def create_cover_letter(job_id: int, db=Depends(get_db)):
    """Sync (threadpool) — one LLM call, returns the stored draft."""
    try:
        return _letter_to_dict(tailor.generate_cover_letter(db, job_id))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/cover-letters/{letter_id}")
async def update_cover_letter(letter_id: int, req: CoverLetterUpdate, db=Depends(get_db)):
    c = db.query(CoverLetter).filter_by(id=letter_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Cover letter not found")
    if req.text is not None:
        c.text = req.text
    if req.status in ("draft", "approved", "rejected"):
        c.status = req.status
    db.commit()
    return _letter_to_dict(c)


# ─── Auto-apply runs (Phase 3) ───────────────────────────────────────────────

class AnswerApproval(BaseModel):
    question: str
    answer: str


class ApproveAnswersRequest(BaseModel):
    answers: List[AnswerApproval]


class MarkSubmittedRequest(BaseModel):
    note: Optional[str] = ""


class AnswerCreate(BaseModel):
    question: str
    answer: str
    company: Optional[str] = None


class AnswerUpdate(BaseModel):
    answer: Optional[str] = None
    approved: Optional[bool] = None
    company: Optional[str] = None


class CredentialCreate(BaseModel):
    tenant: str
    username: str
    password: str
    ats_type: str = "workday"


@router.post("/jobs/{job_id}/apply", status_code=201)
async def queue_apply(job_id: int, db=Depends(get_db)):
    """Enqueue an auto-apply run. All guardrails enforced in apply_queue."""
    run, reason = apply_queue.enqueue_run(db, job_id)
    if not run:
        raise HTTPException(status_code=400, detail=reason)
    return apply_queue.run_to_dict(run, db)


@router.get("/pipeline")
async def get_pipeline(stage: Optional[str] = None, db=Depends(get_db)):
    """Apply Queue page data: every posting in the pipeline + stage counts."""
    items = apply_queue.pipeline_items(db)
    counts = {}
    for it in items:
        counts[it["stage"]] = counts.get(it["stage"], 0) + 1
    if stage:
        items = [i for i in items if i["stage"] == stage]
    return {"items": items, "counts": counts, "total": sum(counts.values())}


@router.post("/pipeline/submit-approved")
async def submit_approved(db=Depends(get_db)):
    """Enqueue runs for all approved-but-not-running pipeline items."""
    queued, skipped = apply_queue.submit_all_approved(db)
    return {"queued": queued, "skipped": skipped,
            "message": f"Queued {queued} run(s)" +
            (f", skipped {len(skipped)}" if skipped else "")}


@router.post("/tailored/{tailored_id}/approve-and-send")
async def approve_and_send(tailored_id: int, db=Depends(get_db)):
    """Single 'Approve & Send': render the approved PDF and enqueue the run."""
    t = db.query(TailoredResume).filter_by(id=tailored_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Tailored resume not found")
    if t.status in ("queued", "generating"):
        raise HTTPException(status_code=409, detail="Still generating")
    if not (t.diff_json or {}).get("experiences"):
        raise HTTPException(status_code=400, detail="No diff to approve")
    if t.status != "approved":
        tailor.approve_and_render(db, t)
    run, reason = apply_queue.enqueue_run(db, t.job_posting_id)
    if not run:
        raise HTTPException(status_code=400, detail=f"Approved, but not queued: {reason}")
    return {"tailored": _tailored_to_dict(t), "run": apply_queue.run_to_dict(run, db),
            "message": "Approved and queued for auto-apply"}


@router.get("/runs")
async def list_runs(status: Optional[str] = None, job_id: Optional[int] = None,
                    limit: int = 50, db=Depends(get_db)):
    q = db.query(ApplicationRun)
    if status:
        q = q.filter_by(status=status)
    if job_id:
        q = q.filter_by(job_posting_id=job_id)
    rows = q.order_by(ApplicationRun.created_at.desc()).limit(min(limit, 200)).all()
    counts = {}
    for s, in db.query(ApplicationRun.status).all():
        counts[s] = counts.get(s, 0) + 1
    return {"runs": [apply_queue.run_to_dict(r, db) for r in rows], "counts": counts}


@router.get("/runs/{run_id}")
async def get_run(run_id: int, db=Depends(get_db)):
    run = db.query(ApplicationRun).filter_by(id=run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return apply_queue.run_to_dict(run, db, full=True)


@router.get("/runs/{run_id}/artifact/{artifact_id}")
async def get_run_artifact(run_id: int, artifact_id: int, db=Depends(get_db)):
    from fastapi.responses import Response
    a = db.query(RunArtifact).filter_by(id=artifact_id, run_id=run_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return Response(content=a.content, media_type=a.content_type or "image/jpeg")


@router.post("/runs/{run_id}/approve-answers")
async def approve_run_answers(run_id: int, req: ApproveAnswersRequest, db=Depends(get_db)):
    """Approve (possibly edited) generated answers → bank; run re-queues and
    the next attempt reuses them without pausing."""
    run = db.query(ApplicationRun).filter_by(id=run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "awaiting_approval":
        raise HTTPException(status_code=409, detail=f"Run is {run.status}, not awaiting approval")
    posting = db.query(JobPosting).filter_by(id=run.job_posting_id).first()
    company = posting.company if posting else ""
    for a in req.answers:
        norm = normalize_question(a.question, company)
        entry = (db.query(AnswerBankEntry).filter_by(question_norm=norm)
                 .order_by(AnswerBankEntry.created_at.desc()).first())
        if entry:
            entry.answer = a.answer
            entry.approved = True
        else:
            db.add(AnswerBankEntry(question=a.question[:2000], question_norm=norm,
                                   answer=a.answer, source="generated", approved=True))
    run.status = "queued"
    run.pending_answers = None
    run.needs_review_reason = None
    db.commit()
    return {"message": f"{len(req.answers)} answer(s) approved — run re-queued"}


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: int, db=Depends(get_db)):
    """After solving a CAPTCHA (headed local worker) — or to re-queue a paused run."""
    run = db.query(ApplicationRun).filter_by(id=run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status not in ("needs_review", "awaiting_approval"):
        raise HTTPException(status_code=409, detail=f"Run is {run.status}")
    run.resume_requested = True
    db.commit()
    return {"message": "Resume requested — the worker will continue"}


@router.post("/runs/{run_id}/retry")
async def retry_run(run_id: int, db=Depends(get_db)):
    run = db.query(ApplicationRun).filter_by(id=run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status not in ("failed", "needs_review", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Run is {run.status} — nothing to retry")
    run.status = "queued"
    run.attempt = 0
    run.error = None
    run.needs_review_reason = None
    run.resume_requested = False
    db.commit()
    return {"message": "Run re-queued"}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: int, db=Depends(get_db)):
    run = db.query(ApplicationRun).filter_by(id=run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status == "submitted":
        raise HTTPException(status_code=409, detail="Already submitted — cannot cancel")
    run.status = "cancelled"
    db.commit()
    return {"message": "Run cancelled"}


@router.post("/runs/{run_id}/mark-submitted")
async def mark_run_submitted(run_id: int, req: MarkSubmittedRequest, db=Depends(get_db)):
    """You finished a paused run by hand — record it + create the tracker card."""
    run = db.query(ApplicationRun).filter_by(id=run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status not in ("needs_review", "awaiting_approval", "failed"):
        raise HTTPException(status_code=409, detail=f"Run is {run.status}")
    apply_queue.mark_submitted_manually(db, run, req.note or "")
    return apply_queue.run_to_dict(run, db)


# ─── Answer bank ─────────────────────────────────────────────────────────────

def _answer_to_dict(a: AnswerBankEntry) -> dict:
    return {"id": a.id, "question": a.question, "answer": a.answer,
            "company": a.company, "source": a.source, "approved": bool(a.approved),
            "times_reused": a.times_reused or 0}


@router.get("/answers")
async def list_answers(db=Depends(get_db)):
    rows = db.query(AnswerBankEntry).order_by(AnswerBankEntry.created_at.desc()).all()
    return {"answers": [_answer_to_dict(a) for a in rows]}


@router.post("/answers", status_code=201)
async def create_answer(req: AnswerCreate, db=Depends(get_db)):
    a = AnswerBankEntry(question=req.question[:2000],
                        question_norm=normalize_question(req.question, req.company or ""),
                        answer=req.answer, company=req.company,
                        source="manual", approved=True)
    db.add(a)
    db.commit()
    db.refresh(a)
    return _answer_to_dict(a)


@router.patch("/answers/{answer_id}")
async def update_answer(answer_id: int, req: AnswerUpdate, db=Depends(get_db)):
    a = db.query(AnswerBankEntry).filter_by(id=answer_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Answer not found")
    for f, v in req.model_dump(exclude_unset=True).items():
        setattr(a, f, v)
    db.commit()
    return _answer_to_dict(a)


@router.delete("/answers/{answer_id}", status_code=204)
async def delete_answer(answer_id: int, db=Depends(get_db)):
    a = db.query(AnswerBankEntry).filter_by(id=answer_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Answer not found")
    db.delete(a)
    db.commit()


# ─── ATS credentials (Workday) — write-only from the web service ─────────────

@router.get("/ats-credentials")
async def list_credentials(db=Depends(get_db)):
    rows = db.query(AtsCredential).order_by(AtsCredential.tenant).all()
    return {"credentials": [
        {"id": c.id, "ats_type": c.ats_type, "tenant": c.tenant,
         "username": c.username, "notes": c.notes,
         "created_at": c.created_at.isoformat() if c.created_at else None}
        for c in rows]}   # sealed password is intentionally never returned


@router.post("/ats-credentials", status_code=201)
async def save_credential(req: CredentialCreate, db=Depends(get_db)):
    import crypto_box
    try:
        sealed = crypto_box.seal(req.password)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    existing = db.query(AtsCredential).filter_by(
        ats_type=req.ats_type, tenant=req.tenant.strip().lower()).first()
    if existing:
        existing.username = req.username
        existing.password_sealed = sealed
    else:
        db.add(AtsCredential(ats_type=req.ats_type, tenant=req.tenant.strip().lower(),
                             username=req.username, password_sealed=sealed))
    db.commit()
    return {"message": f"Credentials sealed for tenant '{req.tenant}' "
                       "(web service cannot read them back)"}


@router.delete("/ats-credentials/{cred_id}", status_code=204)
async def delete_credential(cred_id: int, db=Depends(get_db)):
    c = db.query(AtsCredential).filter_by(id=cred_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Credential not found")
    db.delete(c)
    db.commit()


# ─── Email routing / Inbox (Phase 5) ─────────────────────────────────────────

def _thread_to_dict(t: EmailThread, company: str = None) -> dict:
    return {
        "id": t.id, "gmail_thread_id": t.gmail_thread_id,
        "matched_application_id": t.matched_application_id,
        "company": company,
        "classification": t.classification, "confidence": t.confidence,
        "summary": t.summary, "from_email": t.from_email, "from_name": t.from_name,
        "subject": t.subject, "snippet": t.snippet, "is_read": bool(t.is_read),
        "auto_action": t.auto_action, "can_undo": bool(t.prev_status),
        "last_message_at": t.last_message_at.isoformat() if t.last_message_at else None,
    }


@router.get("/email/status")
async def email_status():
    from email_reader import source_status
    return source_status()


@router.get("/inbox")
async def get_inbox(classification: Optional[str] = None, db=Depends(get_db)):
    """Threads grouped by matched application (unmatched in their own group)."""
    q = db.query(EmailThread)
    if classification:
        q = q.filter_by(classification=classification)
    threads = q.order_by(EmailThread.last_message_at.desc().nullslast()).limit(300).all()

    app_names = {a.id: a.company_name for a in db.query(JobApplication).all()}
    groups = {}
    for t in threads:
        key = t.matched_application_id or 0
        groups.setdefault(key, []).append(_thread_to_dict(t, app_names.get(t.matched_application_id)))

    ordered = []
    for app_id, items in groups.items():
        ordered.append({
            "application_id": app_id or None,
            "company": app_names.get(app_id) if app_id else None,
            "threads": items,
            "count": len(items),
        })
    ordered.sort(key=lambda g: (g["application_id"] is None, -g["count"]))
    counts = {}
    for t in threads:
        counts[t.classification] = counts.get(t.classification, 0) + 1
    return {"groups": ordered, "counts": counts,
            "unread": sum(1 for t in threads if not t.is_read)}


@router.post("/inbox/check")
async def check_inbox(background_tasks: BackgroundTasks):
    """Run one read-only ingestion cycle now."""
    def _run():
        from email_reader import get_reader
        from email_router import ingest
        db = SessionLocal()
        try:
            reader = get_reader()
            logger.info("[Inbox] " + (ingest(db, reader, since_minutes=1440)
                        if reader else "no email source configured"))
        finally:
            db.close()
    background_tasks.add_task(_run)
    return {"message": "Checking inbox (read-only) — threads will appear shortly"}


@router.post("/inbox/{thread_id}/read")
async def mark_thread_read(thread_id: int, db=Depends(get_db)):
    t = db.query(EmailThread).filter_by(id=thread_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")
    t.is_read = True
    db.commit()
    return {"message": "marked read"}


@router.post("/inbox/{thread_id}/undo")
async def undo_thread_move(thread_id: int, db=Depends(get_db)):
    from email_router import undo_status_move
    t = db.query(EmailThread).filter_by(id=thread_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")
    if not undo_status_move(db, t):
        raise HTTPException(status_code=400, detail="Nothing to undo for this thread")
    return {"message": t.auto_action}
