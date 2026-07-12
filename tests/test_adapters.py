"""
Adapter smoke tests. URL detection runs everywhere; the Playwright section
drives the Greenhouse adapter against a mock HTML fixture (file://) and is
skipped when playwright isn't installed.
Run directly:  python3 tests/test_adapters.py
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base, ApplicationRun, JobPosting, AnswerBankEntry  # noqa: E402
from adapters import detect_ats, get_adapter  # noqa: E402
from adapters.base import RunContext, ApplicationData, _closest_option  # noqa: E402
import answer_engine  # noqa: E402

FIXTURE = "file://" + os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "fixtures", "greenhouse_form.html")


def test_detect_routing():
    assert detect_ats("https://boards.greenhouse.io/acme/jobs/123") == "greenhouse"
    assert detect_ats("https://jobs.lever.co/acme/uuid/apply") == "lever"
    assert detect_ats("https://jobs.ashbyhq.com/acme/uuid") == "ashby"
    assert detect_ats("https://jobs.smartrecruiters.com/Acme/123") == "smartrecruiters"
    assert detect_ats("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/x") == "workday"
    assert detect_ats("https://example.com/careers") == ""
    for ats in ("greenhouse", "lever", "ashby", "smartrecruiters", "workday"):
        assert get_adapter(ats).name == ats


def test_closest_option_mapping():
    assert _closest_option("Yes", ["--", "Yes", "No"]) == "Yes"
    assert _closest_option("yes", ["--", "Yes", "No"]) == "Yes"
    assert _closest_option("I don't wish to answer",
                           ["A", "I don't wish to answer"]) == "I don't wish to answer"
    assert _closest_option("Purple", ["Yes", "No"]) is None


def _mk_ctx(page):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    posting = JobPosting(source="greenhouse", external_id="1", company="Acme",
                         title="Data Engineer", url=FIXTURE)
    db.add(posting)
    db.commit()
    run = ApplicationRun(job_posting_id=posting.id, tailored_resume_id=1,
                         ats_type="greenhouse", status="running")
    db.add(run)
    db.commit()

    pdf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "dummy.pdf")
    if not os.path.exists(pdf):
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.4 dummy for upload test")

    profile = SimpleNamespace(work_authorization="stem_opt", tone_notes=None,
                              links={"linkedin": "https://linkedin.com/in/test"},
                              salary_floor=140000)
    app = ApplicationData(
        first_name="Karri", last_name="Prasad", full_name="Karri Prasad",
        email="k@test.com", phone="555-0100", location="Nashville, TN",
        links=profile.links, resume_pdf_path=pdf,
        cover_letter_text="Approved cover letter text.",
        company="Acme", title="Data Engineer", apply_url=FIXTURE,
        eeo={}, profile=profile)
    return RunContext(page, db, run, app), db


def test_greenhouse_fill_receipts_and_submit():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  (playwright not installed — skipped browser tests)")
        return

    # motivation question → generated answer (LLM stubbed) → pending approval
    answer_engine.generate_answer = lambda *a, **k: "Because Acme's data platform work matches my Spark background."

    adapter = get_adapter("greenhouse")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        ctx, db = _mk_ctx(page)

        adapter.fill(ctx)
        labels = {r["label"] for r in ctx.receipts}
        assert {"First name", "Last name", "Email", "Phone", "Resume",
                "Cover letter"} <= labels, labels
        # deterministic sponsorship answer: STEM-OPT → "Yes", source=profile, no approval needed
        sponsor = next(r for r in ctx.receipts if "sponsorship" in r["label"].lower())
        assert sponsor["value"] == "Yes" and sponsor["source"] == "profile", sponsor
        # linkedin custom question answered from profile links
        li = next(r for r in ctx.receipts if "linkedin" in r["label"].lower())
        assert li["value"] == "https://linkedin.com/in/test"
        # EEO → declined option, never guessed
        eeo = next(r for r in ctx.receipts if "veteran" in r["label"].lower())
        assert "wish to answer" in eeo["value"].lower()
        # motivation question paused for approval
        assert len(ctx.pending) == 1 and "Why do you want" in ctx.pending[0]["question"]
        # screenshots landed in the DB
        from database import RunArtifact
        assert db.query(RunArtifact).count() >= 1

        # simulate user approval → bank entry approved → refill has no pendings
        entry = db.query(AnswerBankEntry).filter_by(approved=False).first()
        assert entry is not None
        entry.approved = True
        db.commit()
        page2 = browser.new_page()
        ctx2, _ = _mk_ctx(page2)
        ctx2.db = db   # share the bank
        adapter.fill(ctx2)
        assert ctx2.pending == [], ctx2.pending

        confirmation = adapter.submit(ctx2)
        assert "Thank you" in confirmation or "received" in confirmation.lower(), confirmation
        browser.close()
    print("  (full Playwright fill→approve→submit cycle on mock Greenhouse form)")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n{len(fns)} adapter tests passed.")
