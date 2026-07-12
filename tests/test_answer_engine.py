"""
Answer-engine smoke tests — deterministic work-auth answers, bank reuse,
approval flags. No LLM (generation is monkeypatched), no network.
Run directly:  python3 tests/test_answer_engine.py
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base, AnswerBankEntry  # noqa: E402
import answer_engine  # noqa: E402
from answer_engine import (deterministic_answer, normalize_question,  # noqa: E402
                           is_company_specific, get_answer)


def fresh_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def prof(auth, **kw):
    return SimpleNamespace(work_authorization=auth, links={"linkedin": "li/x", "github": "gh/x"},
                           salary_floor=140000, tone_notes=None, **kw)


SPONSOR_Q = "Will you now or in the future require sponsorship for employment visa status?"
AUTH_Q = "Are you legally authorized to work in the United States?"


def test_sponsorship_answered_from_profile_never_optimistically():
    assert deterministic_answer(SPONSOR_Q, prof("citizen")) == "No"
    assert deterministic_answer(SPONSOR_Q, prof("green_card")) == "No"
    # OPT/STEM-OPT/H1B WILL need sponsorship in the future — must answer Yes
    for auth in ("opt", "stem_opt", "h1b", "needs_sponsorship"):
        assert deterministic_answer(SPONSOR_Q, prof(auth)) == "Yes", auth
    # profile not filled in → refuse to guess
    assert deterministic_answer(SPONSOR_Q, prof("")) is None


def test_work_authorization_now():
    for auth in ("citizen", "green_card", "opt", "stem_opt", "h1b"):
        assert deterministic_answer(AUTH_Q, prof(auth)) == "Yes", auth
    assert deterministic_answer(AUTH_Q, prof("needs_sponsorship")) == "No"


def test_links_and_salary():
    assert deterministic_answer("LinkedIn profile URL", prof("citizen")) == "li/x"
    assert deterministic_answer("GitHub or portfolio", prof("citizen")) == "gh/x"
    assert "140,000" in deterministic_answer("What are your salary expectations?", prof("citizen"))


def test_normalization_strips_company_and_punctuation():
    a = normalize_question("Why do you want to work at Stripe?", "Stripe")
    b = normalize_question("Why do you want to work at Datadog??", "Datadog")
    assert a == b == "why do you want to work at"


def test_motivation_questions_are_company_specific():
    assert is_company_specific("Why do you want to work here?")
    assert is_company_specific("What excites you about this role?")
    assert not is_company_specific("How many years of Python experience do you have?")


def test_bank_reuse_no_approval_needed():
    db = fresh_db()
    db.add(AnswerBankEntry(question="Years of Spark experience?",
                           question_norm=normalize_question("Years of Spark experience?"),
                           answer="6 years", approved=True, source="manual"))
    db.commit()
    text, source, needs = get_answer(db, "Years of Spark experience?", "acme", "DE", prof("citizen"))
    assert (text, source, needs) == ("6 years", "answer_bank", False)
    assert db.query(AnswerBankEntry).first().times_reused == 1


def test_unapproved_bank_entries_are_not_reused():
    db = fresh_db()
    db.add(AnswerBankEntry(question="Q?", question_norm=normalize_question("Q?"),
                           answer="draft", approved=False, source="generated"))
    db.commit()
    answer_engine.generate_answer = lambda *a, **k: "generated answer"
    text, source, needs = get_answer(db, "Q?", "acme", "DE", prof("citizen"))
    assert source == "generated" and needs is True


def test_generated_answers_stored_unapproved_and_flagged():
    db = fresh_db()
    answer_engine.generate_answer = lambda *a, **k: "Because your data platform work is public and impressive."
    text, source, needs = get_answer(db, "Why do you want to work at Acme?", "Acme", "DE", prof("citizen"))
    assert needs is True and source == "generated"
    entry = db.query(AnswerBankEntry).first()
    assert entry.approved is False
    assert entry.company == "Acme"   # motivation questions are company-scoped


def test_company_scoped_answers_do_not_leak_across_companies():
    db = fresh_db()
    q = "Why do you want to work here?"
    db.add(AnswerBankEntry(question=q, question_norm=normalize_question(q, "Acme"),
                           answer="acme-specific", approved=True, company="Acme"))
    db.commit()
    answer_engine.generate_answer = lambda *a, **k: "fresh for beta"
    text, source, needs = get_answer(db, q, "Beta", "DE", prof("citizen"))
    assert text == "fresh for beta" and needs is True  # did NOT reuse Acme's answer


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n{len(fns)} answer-engine tests passed.")
