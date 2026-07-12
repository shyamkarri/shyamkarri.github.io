"""
Guardrail smoke tests for the apply queue — isolated in-memory SQLite,
no LLM, no network.  Run directly:  python3 tests/test_apply_queue.py
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import (Base, CandidateProfile, JobPosting, TailoredResume,  # noqa: E402
                      ApplicationRun)
from apply_queue import enqueue_run  # noqa: E402


def fresh_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def seed(db, company="acme", ext="1", approved=True, dedupe="hash1"):
    p = JobPosting(source="greenhouse", ats_type="greenhouse", external_id=ext,
                   company=company, title="Data Engineer",
                   url="https://boards.greenhouse.io/x", apply_url="https://boards.greenhouse.io/x",
                   dedupe_hash=dedupe)
    db.add(p)
    db.commit()
    if approved:
        db.add(TailoredResume(job_posting_id=p.id, base_resume_id=1,
                              status="approved", approved_at=datetime.utcnow(),
                              diff_json={"experiences": []}))
        db.commit()
    return p


def test_requires_approved_tailored_resume():
    db = fresh_db()
    p = seed(db, approved=False)
    run, reason = enqueue_run(db, p.id)
    assert run is None and "APPROVED tailored resume" in reason


def test_draft_tailored_resume_is_not_enough():
    db = fresh_db()
    p = seed(db, approved=False)
    db.add(TailoredResume(job_posting_id=p.id, base_resume_id=1, status="draft"))
    db.commit()
    run, reason = enqueue_run(db, p.id)
    assert run is None and "APPROVED" in reason


def test_happy_path_queues():
    db = fresh_db()
    p = seed(db)
    run, reason = enqueue_run(db, p.id)
    assert reason is None and run.status == "queued" and run.ats_type == "greenhouse"


def test_kill_switch_blocks_everything():
    db = fresh_db()
    db.add(CandidateProfile(kill_switch=True))
    p = seed(db)
    run, reason = enqueue_run(db, p.id)
    assert run is None and "Kill switch" in reason


def test_dedupe_same_posting():
    db = fresh_db()
    p = seed(db)
    enqueue_run(db, p.id)
    run, reason = enqueue_run(db, p.id)
    assert run is None and "Already have run" in reason


def test_dedupe_same_job_via_other_board():
    db = fresh_db()
    p1 = seed(db, ext="1", dedupe="samehash")
    p2 = seed(db, ext="2", dedupe="samehash")   # same job scraped twice
    enqueue_run(db, p1.id)
    run, reason = enqueue_run(db, p2.id)
    assert run is None and "Already have run" in reason


def test_daily_cap():
    db = fresh_db()
    db.add(CandidateProfile(max_apps_per_day=2))
    ps = [seed(db, company=f"c{i}", ext=str(i), dedupe=f"h{i}") for i in range(3)]
    assert enqueue_run(db, ps[0].id)[0] is not None
    assert enqueue_run(db, ps[1].id)[0] is not None
    run, reason = enqueue_run(db, ps[2].id)
    assert run is None and "Daily cap" in reason


def test_company_cooldown_14_days():
    db = fresh_db()
    p1 = seed(db, company="acme", ext="1", dedupe="h1")
    p2 = seed(db, company="acme", ext="2", dedupe="h2")
    run1, _ = enqueue_run(db, p1.id)
    run1.status = "submitted"
    run1.finished_at = datetime.utcnow() - timedelta(days=3)
    db.commit()
    run, reason = enqueue_run(db, p2.id)
    assert run is None and "cooldown" in reason

    # 15 days later the cooldown has lapsed
    run1.finished_at = datetime.utcnow() - timedelta(days=15)
    db.commit()
    run, reason = enqueue_run(db, p2.id)
    assert run is not None, reason


def test_unknown_ats_refused():
    db = fresh_db()
    p = JobPosting(source="craigslist", external_id="9", company="x", title="DE",
                   url="http://x")
    db.add(p)
    db.commit()
    run, reason = enqueue_run(db, p.id)
    assert run is None and "No adapter" in reason


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n{len(fns)} apply-queue guardrail tests passed.")
