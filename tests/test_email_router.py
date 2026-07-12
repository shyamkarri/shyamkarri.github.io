"""
Phase 5 smoke tests — email classification routing, application matching,
reversible status moves. Isolated SQLite; classify() monkeypatched (no LLM).
Run directly:  python3 tests/test_email_router.py
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base, JobApplication, EmailThread, Notification  # noqa: E402
import email_router  # noqa: E402
from email_router import (match_application, route, undo_status_move,  # noqa: E402
                          domain_of, company_from_domain, ingest)


def fresh_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def app(db, company, position="Data Engineer", status="applied", url=None):
    a = JobApplication(company_name=company, position=position, status=status, job_url=url)
    db.add(a)
    db.commit()
    return a


def msg(tid="t1", frm="recruiter@stripe.com", name="Jane @ Stripe",
        subject="Your Data Engineer application", body="", date=None):
    return {"thread_id": tid, "from_email": frm, "from_name": name,
            "subject": subject, "snippet": body[:120], "body": body,
            "date": date or datetime.utcnow()}


def stub_classify(classification, summary="", conf=0.9):
    email_router.classify = lambda m: {"classification": classification,
                                       "confidence": conf, "summary": summary}


# ── helpers ──
def test_domain_parsing():
    assert domain_of("a@jobs.stripe.com") == "jobs.stripe.com"
    assert company_from_domain("jobs.stripe.com") == "stripe"
    assert company_from_domain("greenhouse-mail.io") == ""   # relay → no company


# ── matching ──
def test_match_by_company_domain():
    db = fresh_db()
    app(db, "Stripe")
    app(db, "Datadog")
    m = msg(frm="jane@stripe.com", name="Jane", subject="Next steps")
    matched, score = match_application(db, m)
    assert matched.company_name == "Stripe" and score >= 0.6


def test_match_ats_relay_falls_back_to_text():
    db = fresh_db()
    app(db, "Airbnb", position="Senior Data Engineer")
    m = msg(frm="no-reply@greenhouse-mail.io", name="Airbnb Recruiting",
            subject="Airbnb — Senior Data Engineer")
    matched, score = match_application(db, m)
    assert matched.company_name == "Airbnb", (matched, score)


def test_no_false_match_below_threshold():
    db = fresh_db()
    app(db, "Stripe", position="Data Engineer")
    m = msg(frm="newsletter@medium.com", name="Medium Daily", subject="Today's top stories")
    matched, score = match_application(db, m)
    assert matched is None, (matched, score)


def test_rejected_cards_excluded_from_matching():
    db = fresh_db()
    app(db, "Stripe", status="rejected")
    m = msg(frm="jane@stripe.com", subject="Stripe")
    matched, _ = match_application(db, m)
    assert matched is None


# ── routing + status moves ──
def test_interview_invite_moves_card_and_notifies():
    db = fresh_db()
    a = app(db, "Stripe", status="applied")
    stub_classify("interview_invite", "Proposes a call Thursday")
    t = route(db, msg(frm="jane@stripe.com", subject="Interview with Stripe"))
    db.refresh(a)
    assert a.status == "interview"
    assert t.prev_status == "applied" and "moved" in t.auto_action
    assert a.interview_stages and a.interview_stages[0]["stage"] == "Interview invite"
    assert db.query(Notification).filter_by(type="interview_invite").count() == 1


def test_rejection_moves_card_to_rejected():
    db = fresh_db()
    a = app(db, "Datadog", status="interview")
    stub_classify("rejection", "Decided not to move forward")
    route(db, msg(frm="recruiter@datadog.com", name="Datadog", subject="Datadog update"))
    db.refresh(a)
    assert a.status == "rejected"


def test_recruiter_reply_does_not_move_card():
    db = fresh_db()
    a = app(db, "Stripe", status="applied")
    stub_classify("recruiter_reply", "Thanks, will be in touch")
    t = route(db, msg(frm="jane@stripe.com", subject="Re: application"))
    db.refresh(a)
    assert a.status == "applied" and t.auto_action is None
    assert db.query(Notification).count() == 1  # still notified


def test_spam_is_recorded_but_inert():
    db = fresh_db()
    a = app(db, "Stripe", status="applied")
    stub_classify("spam")
    t = route(db, msg(frm="promo@ads.com", subject="Buy now"))
    db.refresh(a)
    assert a.status == "applied"
    assert t.classification == "spam"
    assert db.query(Notification).count() == 0  # no notification for spam


def test_undo_restores_prev_status():
    db = fresh_db()
    a = app(db, "Stripe", status="applied")
    stub_classify("interview_invite", "call")
    t = route(db, msg(frm="jane@stripe.com", subject="Stripe interview"))
    assert a.status == "interview"
    assert undo_status_move(db, t) is True
    db.refresh(a)
    assert a.status == "applied" and t.prev_status is None
    assert undo_status_move(db, t) is False  # nothing left to undo


def test_thread_upsert_no_duplicate_and_newer_wins():
    db = fresh_db()
    app(db, "Stripe", status="applied")
    stub_classify("recruiter_reply", "first")
    route(db, msg(tid="tX", subject="hi", date=datetime.utcnow() - timedelta(hours=2)))
    stub_classify("interview_invite", "second")
    route(db, msg(tid="tX", subject="hi", date=datetime.utcnow()))
    threads = db.query(EmailThread).filter_by(gmail_thread_id="tX").all()
    assert len(threads) == 1 and threads[0].classification == "interview_invite"


def test_ingest_collapses_to_newest_per_thread():
    db = fresh_db()
    app(db, "Stripe", status="applied")
    stub_classify("interview_invite", "x")

    class FakeReader:
        def fetch_recent(self, **kw):
            now = datetime.utcnow()
            return [
                msg(tid="tA", subject="old", date=now - timedelta(hours=1)),
                msg(tid="tA", subject="new", date=now),
                msg(tid="tB", frm="r@datadog.com", subject="Datadog", date=now),
            ]
    out = ingest(db, FakeReader())
    assert db.query(EmailThread).count() == 2, out
    assert "routed 2" in out


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n{len(fns)} email-router tests passed.")
