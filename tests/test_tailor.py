"""
Smoke tests for the tailoring engine's deterministic parts — validator,
diff builder, diff application, ATS PDF render. No LLM, no network.
Run directly:  python3 tests/test_tailor.py
"""

import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tailor import validate_change, build_diff, apply_diff, render_pdf, _numbers  # noqa: E402


def fact(id, text):
    return SimpleNamespace(id=id, fact=text, context=None)


FACTS = {
    1: fact(1, "Reduced Spark job runtime by 40% through partition tuning"),
    2: fact(2, "Built Kafka pipeline processing 2M events/day"),
    3: fact(3, "Led a team of 5 data engineers"),
}

PARSED = {
    "contact": {"name": "Test User", "email": "t@x.com"},
    "skills": ["Python", "Spark", "Kafka", "SQL"],
    "experiences": [
        {"company": "Acme", "title": "Data Engineer", "start": "2021", "end": "2024",
         "bullets": ["Worked on Spark jobs", "Maintained Kafka pipeline"]},
        {"company": "Beta", "title": "Analyst", "start": "2019", "end": "2021",
         "bullets": ["Built SQL reports"]},
    ],
    "education": [{"school": "State U", "degree": "BS", "field": "CS", "year": "2019"}],
}


def test_validator_blocks_invented_metrics():
    reason = validate_change(
        "Optimized Spark jobs, cutting runtime 65%", "Worked on Spark jobs", [FACTS[1]])
    assert reason and "65%" in reason, reason


def test_validator_allows_grounded_metrics():
    assert validate_change(
        "Cut Spark runtime 40% via partition tuning", "Worked on Spark jobs", [FACTS[1]]) is None
    # numbers already in the original bullet are also fine
    assert validate_change(
        "Handled 2M events/day on Kafka", "Processed 2M events daily", [FACTS[3]]) is None


def test_validator_requires_citations():
    assert validate_change("Improved Spark jobs", "Worked on Spark jobs", []) == "no fact citations"


def test_number_normalization():
    assert _numbers("cut costs $1,200 by 40 %") == _numbers("cut costs $1200 by 40%")


def test_build_diff_drops_bad_changes_and_keeps_good():
    changes = [
        {"ref": "e0.b0", "new": "Cut Spark runtime 40% through partition tuning", "fact_ids": [1]},
        {"ref": "e0.b1", "new": "Scaled Kafka pipeline to 9M events/day", "fact_ids": [2]},  # invented 9M
        {"ref": "e9.b9", "new": "Ghost bullet", "fact_ids": [1]},  # bad ref → ignored
    ]
    diff = build_diff(PARSED, changes, FACTS,
                      ["Spark", "Kafka", "Python", "SQL", "Rust"],  # Rust must be filtered out
                      "Data engineer who cut Spark runtime 40%")
    assert diff["changed_count"] == 1
    b0 = diff["experiences"][0]["bullets"][0]
    assert b0["accepted"] and b0["fact_ids"] == [1]
    assert diff["experiences"][0]["bullets"][1]["new"] is None
    assert len(diff["dropped_by_validator"]) == 1
    assert "9m" in diff["dropped_by_validator"][0]["reason"].lower()
    assert "Rust" not in diff["skills_order"]
    assert diff["skills_order"][:2] == ["Spark", "Kafka"]
    assert diff["summary"]["new"]  # grounded 40% → kept


def test_build_diff_blocks_summary_with_invented_numbers():
    diff = build_diff(PARSED, [], FACTS, None, "Delivered $10M in savings")
    assert diff["summary"]["new"] is None
    assert any(x["ref"] == "summary" for x in diff["dropped_by_validator"])


def test_apply_diff_respects_acceptance_and_edits():
    diff = build_diff(PARSED, [
        {"ref": "e0.b0", "new": "Cut Spark runtime 40%", "fact_ids": [1]},
        {"ref": "e1.b0", "new": "Built SQL reports for leadership", "fact_ids": [3]},
    ], FACTS, None, None)
    # user rejects the second change and hand-edits the first
    diff["experiences"][0]["bullets"][0]["edited"] = "Cut Spark runtime 40% (per-job tuning)"
    diff["experiences"][1]["bullets"][0]["accepted"] = False
    final = apply_diff(PARSED, diff)
    assert final["experiences"][0]["bullets"][0] == "Cut Spark runtime 40% (per-job tuning)"
    assert final["experiences"][1]["bullets"][0] == "Built SQL reports"  # rejected → original
    assert PARSED["experiences"][0]["bullets"][0] == "Worked on Spark jobs"  # source untouched


def test_render_pdf_is_single_column_parseable():
    profile = SimpleNamespace(full_name="Test User", email="t@x.com", phone="555",
                              location="NYC", links={"linkedin": "li.com/t", "github": None})
    final = apply_diff(PARSED, build_diff(PARSED, [
        {"ref": "e0.b0", "new": "Cut Spark runtime 40% through partition tuning", "fact_ids": [1]},
    ], FACTS, ["Spark", "Python", "Kafka", "SQL"], None))
    out = os.path.join(tempfile.mkdtemp(), "test_resume.pdf")
    render_pdf(final, profile, out)
    with open(out, "rb") as f:
        data = f.read()
    assert data[:5] == b"%PDF-" and len(data) > 1500
    try:
        from pypdf import PdfReader  # verify the text layer survives ATS-style extraction
        text = PdfReader(out).pages[0].extract_text()
        for needle in ("Test User", "Cut Spark runtime 40%", "EXPERIENCE", "Acme", "State U"):
            assert needle in text, f"missing '{needle}' in extracted PDF text"
        print("  (pypdf text-extraction check included)")
    except ImportError:
        print("  (pypdf not installed locally — skipped text-extraction check)")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n{len(fns)} tailoring smoke tests passed.")
