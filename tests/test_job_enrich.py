"""
Smoke tests for the deterministic match scorer — no DB, no LLM, no network.
Run directly:  python3 tests/test_job_enrich.py
"""

import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from job_enrich import compute_match, match_reason_text, _skill_hit  # noqa: E402


def profile(**over):
    base = dict(
        work_authorization="stem_opt", remote_pref="any",
        target_titles=["Data Engineer", "Data Platform Engineer"],
        target_locations=["Remote", "New York, NY"],
    )
    base.update(over)
    return SimpleNamespace(**base)


def posting(**over):
    base = dict(
        title="Senior Data Engineer", company="acme", location="Remote, USA",
        remote=True, sponsorship_flag="unknown",
        posted_at=datetime.utcnow() - timedelta(days=1),
        scraped_at=datetime.utcnow(),
    )
    base.update(over)
    return SimpleNamespace(**base)


SKILLS = {"python", "apache spark", "kafka", "airflow", "snowflake", "aws", "sql"}


def test_strong_match_scores_high():
    req = {"required_skills": ["Spark", "Python", "SQL", "AWS"],
           "seniority": "senior", "sponsorship": "friendly"}
    score, b = compute_match(posting(), req, profile(), SKILLS)
    assert score >= 85, f"expected strong match, got {score}: {b}"
    assert b["skills"]["points"] == 40, b["skills"]
    assert b["total"] == score
    assert not b["skills"]["missing"]


def test_missing_skills_lower_the_score():
    req = {"required_skills": ["Rust", "Kubernetes", "Go", "C++"], "seniority": "senior"}
    score, b = compute_match(posting(), req, profile(), SKILLS)
    assert b["skills"]["points"] == 0, b["skills"]
    assert set(b["skills"]["missing"]) == {"Rust", "Kubernetes", "Go", "C++"}
    assert score < 65, score


def test_sponsorship_restricted_zeroes_auth():
    req = {"required_skills": ["Python"], "sponsorship": "restricted"}
    p = profile(work_authorization="needs_sponsorship")
    score_r, b_r = compute_match(posting(), req, p, SKILLS)
    score_f, b_f = compute_match(posting(), {**req, "sponsorship": "friendly"}, p, SKILLS)
    assert b_r["location_auth"]["points"] < b_f["location_auth"]["points"]
    assert "restrict" in b_r["location_auth"]["detail"]
    assert score_r < score_f


def test_citizen_ignores_sponsorship_restriction():
    req = {"required_skills": ["Python"], "sponsorship": "restricted"}
    _, b = compute_match(posting(), req, profile(work_authorization="citizen"), SKILLS)
    assert "no sponsorship needed" in b["location_auth"]["detail"]


def test_old_posting_loses_recency_points():
    req = {"required_skills": ["Python"]}
    fresh, b_fresh = compute_match(posting(), req, profile(), SKILLS)
    stale, b_stale = compute_match(
        posting(posted_at=datetime.utcnow() - timedelta(days=45)), req, profile(), SKILLS)
    assert b_fresh["recency"]["points"] == 15
    assert b_stale["recency"]["points"] == 0
    assert fresh > stale


def test_thin_jd_gets_neutral_skill_credit_and_flag():
    req = {"required_skills": [], "_thin": True}
    score, b = compute_match(posting(), req, profile(), SKILLS)
    assert b["skills"]["points"] == 20
    assert b["thin_jd"] is True
    assert "low confidence" in match_reason_text(b)


def test_title_mismatch_scores_lower_than_title_match():
    req = {"required_skills": ["Python"]}
    _, b_match = compute_match(posting(title="Data Engineer"), req, profile(), SKILLS)
    _, b_miss = compute_match(posting(title="Account Executive"), req, profile(), SKILLS)
    assert b_match["title"]["points"] > b_miss["title"]["points"]


def test_skill_hit_is_fuzzy_but_not_sloppy():
    assert _skill_hit("Apache Spark", SKILLS)          # exact
    assert _skill_hit("Spark", SKILLS)                 # substring
    assert _skill_hit("snowflake dwh", {"snowflake"})  # contained
    assert not _skill_hit("Java", {"javascript"} - {"javascript"} | {"python"})  # no false positive


def test_score_bounds():
    req = {"required_skills": ["Spark", "Python", "SQL", "AWS", "Kafka", "Airflow"],
           "seniority": "senior", "sponsorship": "friendly"}
    score, _ = compute_match(posting(), req, profile(work_authorization="citizen"), SKILLS)
    assert 0 <= score <= 100


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n{len(fns)} scoring smoke tests passed.")
