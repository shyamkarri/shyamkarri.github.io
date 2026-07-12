"""
Phase 4 smoke tests — pipeline aggregation, stage derivation, submit-all,
and autopilot gating. Isolated in-memory SQLite; tailoring/LLM monkeypatched.
Run directly:  python3 tests/test_pipeline.py
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import (Base, CandidateProfile, JobPosting, BaseResume,  # noqa: E402
                      TailoredResume, CoverLetter, ApplicationRun)
import apply_queue  # noqa: E402
from apply_queue import pipeline_items, submit_all_approved, _stage, autopilot_scan  # noqa: E402


def fresh_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def posting(db, company="acme", ext="1", ats="greenhouse", score=90, dedupe=None):
    p = JobPosting(source=ats, ats_type=ats, external_id=ext, company=company,
                   title="Data Engineer", url="https://x/apply", apply_url="https://x/apply",
                   match_score=score, dedupe_hash=dedupe or f"h{ext}")
    db.add(p)
    db.commit()
    return p


def approved_resume(db, p):
    db.add(TailoredResume(job_posting_id=p.id, base_resume_id=1, status="approved",
                          approved_at=datetime.utcnow(), diff_json={"experiences": [{}]}))
    db.commit()


def test_stage_derivation():
    assert _stage(None, None) == "unknown"
    assert _stage(type("T", (), {"status": "draft"})(), None) == "needs_your_approval"
    assert _stage(type("T", (), {"status": "generating"})(), None) == "tailoring"
    assert _stage(type("T", (), {"status": "approved"})(), None) == "ready_to_send"
    run = type("R", (), {"status": "submitted"})()
    assert _stage(type("T", (), {"status": "approved"})(), run) == "submitted"


def test_pipeline_lists_only_pipeline_postings():
    db = fresh_db()
    p1 = posting(db, ext="1")               # in pipeline (has tailored)
    approved_resume(db, p1)
    posting(db, ext="2")                     # NOT in pipeline (no tailored/run)
    items = pipeline_items(db)
    assert len(items) == 1 and items[0]["job_posting_id"] == p1.id
    assert items[0]["stage"] == "ready_to_send"


def test_pipeline_sorted_by_score():
    db = fresh_db()
    lo = posting(db, ext="1", score=70); approved_resume(db, lo)
    hi = posting(db, ext="2", score=95); approved_resume(db, hi)
    items = pipeline_items(db)
    assert [i["match_score"] for i in items] == [95, 70]


def test_submit_all_approved_queues_and_reports_skips():
    db = fresh_db()
    p1 = posting(db, company="a", ext="1"); approved_resume(db, p1)
    p2 = posting(db, company="b", ext="2"); approved_resume(db, p2)
    p3 = posting(db, company="c", ext="3")   # draft only — not ready
    db.add(TailoredResume(job_posting_id=p3.id, base_resume_id=1, status="draft"))
    db.commit()
    queued, skipped = submit_all_approved(db)
    assert queued == 2, queued
    # p3 is not "ready_to_send" so it's simply not attempted (not a skip)
    assert all(s["company"] != "c" for s in skipped)
    # a second call finds nothing ready (they're queued now)
    queued2, _ = submit_all_approved(db)
    assert queued2 == 0


def test_kill_switch_makes_submit_all_skip_with_reason():
    db = fresh_db()
    db.add(CandidateProfile(kill_switch=True))
    p = posting(db); approved_resume(db, p)
    queued, skipped = submit_all_approved(db)
    assert queued == 0 and skipped and "Kill switch" in skipped[0]["reason"]


def test_autopilot_off_by_default():
    db = fresh_db()
    db.add(CandidateProfile(auto_mode=False))
    posting(db, score=95)
    assert autopilot_scan(db) == "autopilot off"


def test_autopilot_semi_auto_tailors_draft_but_does_not_send():
    db = fresh_db()
    db.add(CandidateProfile(auto_mode=True, auto_threshold=80, company_allowlist=[]))
    db.add(BaseResume(label="b", parse_status="ready", is_default=True,
                      parsed_json={"experiences": []}))
    db.commit()
    p = posting(db, company="acme", score=90)

    # stub tailoring: mark the row a draft (as run_tailor would on success)
    def fake_run_tailor(tid):
        t = db.query(TailoredResume).filter_by(id=tid).first()
        t.status = "draft"; t.diff_json = {"experiences": [{}]}; db.commit()
    import tailor
    tailor.run_tailor = fake_run_tailor

    msg = autopilot_scan(db, posting_ids=[p.id])
    t = db.query(TailoredResume).filter_by(job_posting_id=p.id).first()
    assert t and t.status == "draft", (msg, t and t.status)   # NOT auto-approved
    assert db.query(ApplicationRun).count() == 0               # NOT queued
    assert "tailored 1" in msg and "auto-queued 0" in msg


def test_autopilot_full_auto_for_allowlisted_company():
    db = fresh_db()
    db.add(CandidateProfile(auto_mode=True, auto_threshold=80,
                            company_allowlist=["acme"]))
    db.add(BaseResume(label="b", parse_status="ready", is_default=True,
                      parsed_json={"experiences": []}))
    db.commit()
    p = posting(db, company="Acme", score=92)

    import tailor
    tailor.run_tailor = lambda tid: (
        db.query(TailoredResume).filter_by(id=tid)
        .update({"status": "draft", "diff_json": {"experiences": [{}]}}), db.commit())
    tailor.approve_and_render = lambda db_, t: (
        setattr(t, "status", "approved"), setattr(t, "approved_at", datetime.utcnow()),
        db_.commit())

    msg = autopilot_scan(db, posting_ids=[p.id])
    assert db.query(ApplicationRun).count() == 1, msg   # allowlisted → queued
    assert "auto-queued 1" in msg


def test_autopilot_below_threshold_ignored():
    db = fresh_db()
    db.add(CandidateProfile(auto_mode=True, auto_threshold=85))
    db.add(BaseResume(label="b", parse_status="ready", is_default=True,
                      parsed_json={"experiences": []}))
    db.commit()
    posting(db, score=70)   # under threshold
    msg = autopilot_scan(db)
    assert db.query(TailoredResume).count() == 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n{len(fns)} pipeline/autopilot tests passed.")
