"""
Phase 6 smoke tests — adapter health / selector-drift alarm, nightly failed-run
retry, application funnel, env validation. Isolated SQLite, no network.
Run directly:  python3 tests/test_ops.py
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import (Base, AgentExecution, ApplicationRun, JobApplication,  # noqa: E402
                      Notification)
import ops  # noqa: E402


def fresh_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def log_runs(db, ats, results, base_time=None):
    """results: list of bool (success). Oldest first."""
    base_time = base_time or (datetime.utcnow() - timedelta(hours=len(results)))
    for i, ok in enumerate(results):
        db.add(AgentExecution(agent_name=f"apply_{ats}", workflow="auto_apply",
                              success=ok, timestamp=base_time + timedelta(minutes=i)))
    db.commit()


def test_adapter_health_reports_rate():
    db = fresh_db()
    log_runs(db, "greenhouse", [True] * 8 + [False] * 2)
    h = {a["adapter"]: a for a in ops.adapter_health(db)}
    assert h["greenhouse"]["runs"] == 10
    assert h["greenhouse"]["success_rate"] == 0.8
    assert h["greenhouse"]["healthy"] is True
    assert h["lever"]["runs"] == 0 and h["lever"]["healthy"] is None


def test_drift_flags_low_success_adapter():
    db = fresh_db()
    log_runs(db, "workday", [False] * 7 + [True] * 3)   # 30% over 10
    h = {a["adapter"]: a for a in ops.adapter_health(db)}
    assert h["workday"]["healthy"] is False
    msg = ops.check_selector_drift(db)
    assert "workday" in msg
    assert db.query(Notification).filter(
        Notification.title.like("%workday%")).count() == 1


def test_drift_not_flagged_below_min_sample():
    db = fresh_db()
    log_runs(db, "ashby", [False, False, True])   # 33% but only 3 runs
    h = {a["adapter"]: a for a in ops.adapter_health(db)}
    assert h["ashby"]["healthy"] is True   # not enough sample to alarm
    assert "healthy" in ops.check_selector_drift(db)


def test_drift_alarm_deduped_within_24h():
    db = fresh_db()
    log_runs(db, "workday", [False] * 8 + [True] * 2)
    ops.check_selector_drift(db)
    ops.check_selector_drift(db)   # second call same day
    assert db.query(Notification).filter(Notification.title.like("%workday%")).count() == 1


def test_nightly_retry_requeues_failed_once():
    db = fresh_db()
    r1 = ApplicationRun(job_posting_id=1, tailored_resume_id=1, ats_type="greenhouse",
                        status="failed", error="TimeoutError")
    r2 = ApplicationRun(job_posting_id=2, tailored_resume_id=1, ats_type="lever",
                        status="submitted")
    db.add_all([r1, r2])
    db.commit()
    assert ops.retry_failed_runs(db) == "re-queued 1 failed run(s)"
    db.refresh(r1)
    assert r1.status == "queued" and "[nightly-retried]" in r1.error
    # a second nightly pass does NOT loop on the same run
    r1.status = "failed"
    db.commit()
    assert ops.retry_failed_runs(db) == "re-queued 0 failed run(s)"


def test_application_funnel():
    db = fresh_db()
    db.add_all([
        ApplicationRun(job_posting_id=1, tailored_resume_id=1, ats_type="greenhouse", status="submitted"),
        ApplicationRun(job_posting_id=2, tailored_resume_id=1, ats_type="lever", status="submitted"),
        ApplicationRun(job_posting_id=3, tailored_resume_id=1, ats_type="ashby", status="needs_review"),
    ])
    db.add_all([
        JobApplication(company_name="A", position="DE", status="interview"),
        JobApplication(company_name="B", position="DE", status="offer"),
    ])
    db.commit()
    f = ops.application_funnel(db)
    assert f["auto_submitted"] == 2 and f["needs_review"] == 1
    assert f["interview_rate"] == 0.5   # 1 interview / 2 submitted
    assert f["offer_rate"] == 0.5


def test_env_validation_reports_issues():
    # in the test env none of these are set → several issues, but never raises
    issues = ops.validate_env("web")
    assert isinstance(issues, list)
    worker_issues = ops.validate_env("worker")
    assert any("PRIVATE_KEY" in i for i in worker_issues)


def test_sentry_noop_without_dsn():
    old = os.environ.pop("SENTRY_DSN", None)
    try:
        assert ops.init_sentry("web") is False
    finally:
        if old:
            os.environ["SENTRY_DSN"] = old


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n{len(fns)} ops tests passed.")
