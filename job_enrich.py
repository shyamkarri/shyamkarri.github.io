"""
Job-description enrichment + explainable match scoring.

Two stages per posting:
  1. extract_requirements() — one fast-tier LLM call pulls structured
     requirements out of the JD text (skills, seniority, salary, sponsorship).
  2. compute_match() — DETERMINISTIC weighted score against the profile +
     fact bank. No LLM in the scoring itself, so every score is explainable:
        skills overlap        40%
        title / seniority     25%
        location / work auth  20%
        recency               15%
     The full breakdown is stored on the posting (match_breakdown) and shown
     on hover in the Jobs Feed.

If the fact bank is still empty (Phase 1 not set up yet), falls back to the
legacy LLM scorer in job_intel so behavior on Render doesn't regress.
"""

import re
import json
import logging
from datetime import datetime
from difflib import SequenceMatcher

from database import JobPosting, CandidateProfile
from llm import complete_json

logger = logging.getLogger("agent_logger")

MAX_ENRICHED_PER_RUN = 40   # rate-limit guard per scheduler run

# ─── Stage 1: LLM requirement extraction ─────────────────────────────────────

_EXTRACT_PROMPT = """Extract structured requirements from this job posting.
Copy only what the posting actually says — use null/[] when it doesn't say.

Title: {title}
Company: {company}
Location: {location}
Description:
{description}

Reply with ONLY this JSON:
{{
  "required_skills": ["hard skills/tools explicitly required"],
  "nice_to_have": ["preferred/bonus skills"],
  "seniority": "intern|junior|mid|senior|staff|principal|manager|null",
  "min_years": <number or null>,
  "salary_range": "as written, or null",
  "sponsorship": "friendly|restricted|unknown",
  "keywords": ["5-10 most important keywords for resume tailoring"],
  "education": "requirement as written, or null"
}}"""


def extract_requirements(posting: JobPosting) -> dict:
    """One LLM call → structured requirements. Raises on unusable output."""
    desc = posting.raw_description or posting.description_snippet or ""
    data = complete_json(_EXTRACT_PROMPT.format(
        title=posting.title, company=posting.company,
        location=posting.location or "?", description=desc[:8000] or "(no description available)",
    ), tier="fast")
    if not isinstance(data, dict):
        raise ValueError("extraction returned non-dict")
    data["_extracted_at"] = datetime.utcnow().isoformat()
    data["_thin"] = len(desc) < 200   # scored from title only — low confidence
    return data


# ─── Stage 2: deterministic scoring ──────────────────────────────────────────

SENIORITY_RANK = {"intern": 0, "junior": 1, "mid": 2, "senior": 3,
                  "staff": 4, "principal": 5, "manager": 4}

_norm_re = re.compile(r"[^a-z0-9+#. ]")


def _norm(s: str) -> str:
    return _norm_re.sub(" ", (s or "").lower()).strip()


def _skill_hit(req_skill: str, skills: set) -> bool:
    """A required skill counts if any fact-bank skill matches it loosely."""
    r = _norm(req_skill)
    if not r:
        return False
    for s in skills:
        s = _norm(s)
        if not s:
            continue
        if r == s or r in s or s in r:
            return True
        if SequenceMatcher(None, r, s).ratio() >= 0.85:
            return True
    return False


def _title_seniority_points(posting, req, profile) -> dict:
    """25 pts: 17 for title similarity to target titles, 8 for seniority fit."""
    title = _norm(posting.title)
    targets = [_norm(t) for t in (profile.target_titles or []) if t]
    best, best_target = 0.0, None
    for t in targets:
        # token overlap handles "Senior Data Engineer II" vs "Data Engineer"
        t_tokens, title_tokens = set(t.split()), set(title.split())
        overlap = len(t_tokens & title_tokens) / len(t_tokens) if t_tokens else 0
        ratio = max(overlap, SequenceMatcher(None, t, title).ratio())
        if ratio > best:
            best, best_target = ratio, t
    title_pts = round(17 * best)

    seniority_pts = 4  # neutral when the posting doesn't say
    req_sen = (req.get("seniority") or "").lower()
    if req_sen in SENIORITY_RANK:
        # infer candidate level from target titles ("senior..." etc.), default senior-track
        mine = 3
        for t in targets:
            for name, rank in SENIORITY_RANK.items():
                if name in t:
                    mine = rank
        gap = abs(SENIORITY_RANK[req_sen] - mine)
        seniority_pts = {0: 8, 1: 6, 2: 3}.get(gap, 0)

    return {"points": title_pts + seniority_pts, "max": 25,
            "detail": f"title~{best:.0%} vs '{best_target or '—'}', seniority {req_sen or 'n/a'}"}


def _location_auth_points(posting, req, profile) -> dict:
    """20 pts: 12 work authorization, 8 location/remote fit."""
    # authorization (12) — answered from Profile, never optimistically
    auth = profile.work_authorization or ""
    sponsorship = req.get("sponsorship") or posting.sponsorship_flag or "unknown"
    if auth in ("citizen", "green_card"):
        auth_pts, auth_note = 12, "no sponsorship needed"
    elif not auth:
        auth_pts, auth_note = 6, "work auth not set in profile"
    elif sponsorship == "friendly":
        auth_pts, auth_note = 12, "sponsorship-friendly posting"
    elif sponsorship == "restricted":
        auth_pts, auth_note = 0, "posting restricts sponsorship"
    else:
        auth_pts, auth_note = 7, "sponsorship unknown"

    # location (8)
    loc = _norm(posting.location)
    remote_pref = profile.remote_pref or "any"
    targets = [_norm(t) for t in (profile.target_locations or []) if t]
    if posting.remote or "remote" in loc:
        loc_pts = 8 if remote_pref in ("remote_only", "any", "hybrid") else 5
        loc_note = "remote"
    elif remote_pref == "remote_only":
        loc_pts, loc_note = 2, "onsite but you want remote-only"
    elif targets and any(t in loc or loc in t for t in targets if t):
        loc_pts, loc_note = 8, "in a target location"
    elif not targets:
        loc_pts, loc_note = 5, "no target locations set"
    else:
        loc_pts, loc_note = 3, "outside target locations"

    return {"points": auth_pts + loc_pts, "max": 20,
            "detail": f"{auth_note}; {loc_note}"}


def _recency_points(posting) -> dict:
    """15 pts: full if posted/found ≤3 days ago, linear decay to 0 at 30 days."""
    ref = posting.posted_at or posting.scraped_at or datetime.utcnow()
    days = max(0.0, (datetime.utcnow() - ref).total_seconds() / 86400)
    if days <= 3:
        pts = 15
    elif days >= 30:
        pts = 0
    else:
        pts = round(15 * (30 - days) / 27)
    return {"points": pts, "max": 15, "detail": f"{days:.0f}d old"}


def compute_match(posting: JobPosting, req: dict, profile: CandidateProfile,
                  skills: set) -> tuple:
    """Returns (score_0_100, breakdown_dict). Pure function of its inputs."""
    required = [s for s in (req.get("required_skills") or []) if s]
    if required:
        matched = [s for s in required if _skill_hit(s, skills)]
        missing = [s for s in required if s not in matched]
        skill_pts = round(40 * len(matched) / len(required))
    else:
        # nothing extractable (thin JD) — neutral half credit
        matched, missing, skill_pts = [], [], 20
    skills_part = {"points": skill_pts, "max": 40,
                   "matched": matched[:15], "missing": missing[:15]}

    title_part = _title_seniority_points(posting, req, profile)
    loc_part = _location_auth_points(posting, req, profile)
    rec_part = _recency_points(posting)

    total = skills_part["points"] + title_part["points"] + loc_part["points"] + rec_part["points"]
    total = max(0, min(100, total))
    breakdown = {"skills": skills_part, "title": title_part,
                 "location_auth": loc_part, "recency": rec_part,
                 "total": total, "thin_jd": bool(req.get("_thin"))}
    return total, breakdown


def match_reason_text(breakdown: dict) -> str:
    s = breakdown["skills"]
    parts = []
    if s["matched"] or s["missing"]:
        parts.append(f"{len(s['matched'])}/{len(s['matched']) + len(s['missing'])} required skills")
    parts.append(breakdown["title"]["detail"])
    parts.append(breakdown["location_auth"]["detail"])
    if breakdown.get("thin_jd"):
        parts.append("thin JD — low confidence")
    return "; ".join(parts)[:500]


# ─── Orchestrator (called after each scrape + on demand) ─────────────────────

def enrich_and_score_new_jobs(db, limit: int = MAX_ENRICHED_PER_RUN,
                              rescore_all: bool = False) -> str:
    """Extract requirements + score postings that don't have them yet.
    Falls back to the legacy LLM scorer when profile/fact bank aren't set up."""
    from resume_bank import fact_bank_skills

    profile = db.query(CandidateProfile).first()
    skills = fact_bank_skills(db)
    if not profile or not skills:
        from job_intel import score_new_jobs
        legacy = score_new_jobs(db)
        return f"profile/fact bank not set up — legacy scorer: {legacy}"

    q = db.query(JobPosting)
    if rescore_all:
        q = q.filter(JobPosting.extracted_requirements.isnot(None))
    else:
        q = q.filter(JobPosting.match_breakdown.is_(None))
    pending = q.order_by(JobPosting.scraped_at.desc()).limit(limit).all()
    if not pending:
        return "0 jobs to enrich"

    enriched, failed = 0, 0
    for job in pending:
        try:
            req = job.extracted_requirements if rescore_all and job.extracted_requirements \
                else extract_requirements(job)
            score, breakdown = compute_match(job, req, profile, skills)
            job.extracted_requirements = req
            job.match_score = score
            job.match_breakdown = breakdown
            job.match_reason = match_reason_text(breakdown)
            # upgrade sponsorship flag if the LLM found signals the regex missed
            if req.get("sponsorship") in ("friendly", "restricted") and \
                    job.sponsorship_flag in (None, "unknown"):
                job.sponsorship_flag = req["sponsorship"]
            db.commit()
            enriched += 1
        except Exception as e:
            db.rollback()
            failed += 1
            logger.warning(f"[Enrich] job {job.id} failed: {e}")

    summary = f"enriched+scored {enriched}, {failed} failed"
    try:  # semi-/full-auto tailoring for high scorers (no-op unless auto_mode on)
        from apply_queue import autopilot_scan
        summary += " | " + autopilot_scan(db, posting_ids=[j.id for j in pending])
    except Exception as e:
        logger.warning(f"[Enrich] autopilot skipped: {e}")
    return summary


def rescore_existing(db, limit: int = 500) -> str:
    """Recompute scores from stored requirements after a profile/fact change.
    No LLM calls — instant."""
    from resume_bank import fact_bank_skills
    profile = db.query(CandidateProfile).first()
    skills = fact_bank_skills(db)
    if not profile or not skills:
        return "profile/fact bank not set up"
    rows = (db.query(JobPosting)
            .filter(JobPosting.extracted_requirements.isnot(None))
            .order_by(JobPosting.scraped_at.desc()).limit(limit).all())
    for job in rows:
        score, breakdown = compute_match(job, job.extracted_requirements, profile, skills)
        job.match_score = score
        job.match_breakdown = breakdown
        job.match_reason = match_reason_text(breakdown)
    db.commit()
    return f"rescored {len(rows)}"
