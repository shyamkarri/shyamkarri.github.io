"""
Tailoring engine — rewrites base-resume bullets against a job description,
grounded in the FactBank, with a deterministic validator between the LLM and
the stored diff.

Anti-hallucination design (three layers):
  1. Structure: the LLM may ONLY rewrite bullet text. Employers, titles,
     dates, education come verbatim from the parsed base resume — they are
     never in the LLM's output, so they cannot be invented.
  2. Citation: every rewritten bullet must cite verified FactBank ids that
     exist. Rewrites with no citations are dropped.
  3. Numbers: every number/metric in a rewritten bullet must already appear
     in the original bullet or in a cited fact. Violations are dropped and
     recorded in diff_json["dropped_by_validator"] so the user can see what
     the model tried to do.

PDF: single-column ATS-safe layout via reportlab (already a dependency; a
headless-Chrome HTML→PDF pipeline does not fit the 512MB Render web instance —
the Playwright worker in Phase 3 can take over rendering later if wanted).
"""

import os
import re
import json
import logging
from datetime import datetime

from database import (
    SessionLocal, JobPosting, BaseResume, ResumeFact, TailoredResume,
    CoverLetter, CandidateProfile,
)
from llm import complete, complete_json

logger = logging.getLogger("agent_logger")

UPLOADS_DIR = os.getenv("UPLOADS_DIR", os.path.join(os.path.dirname(__file__), "uploads"))
TAILORED_DIR = os.path.join(UPLOADS_DIR, "tailored")
os.makedirs(TAILORED_DIR, exist_ok=True)


# ─── Validator ───────────────────────────────────────────────────────────────

_NUM_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s*(?:%|percent|[kKmMbB]\b|\+)?")


def _numbers(text: str) -> set:
    """Normalized numeric tokens: '1,200+' → '1200+', '$3 M' → '$3m'."""
    out = set()
    for m in _NUM_RE.findall(text or ""):
        out.add(re.sub(r"[,\s]", "", m).lower().rstrip("percent") or m)
    return out


def validate_change(new_text: str, old_text: str, cited_facts: list):
    """Returns None if the rewrite is safe, else a human-readable reason."""
    if not cited_facts:
        return "no fact citations"
    allowed = _numbers(old_text)
    for f in cited_facts:
        allowed |= _numbers(f.fact)
    invented = _numbers(new_text) - allowed
    if invented:
        return f"metric(s) not in original bullet or cited facts: {', '.join(sorted(invented))}"
    return None


# ─── Prompt ──────────────────────────────────────────────────────────────────

_TAILOR_PROMPT = """You are tailoring a resume to a job description. STRICT RULES:
- You may ONLY rephrase and re-emphasize using the numbered FACT BANK and the original bullet itself.
- NEVER invent employers, titles, dates, metrics, numbers, tools, or skills. If a metric is not in the fact bank or the original bullet, do NOT add one.
- Every rewritten bullet MUST cite the fact ids that support each claim in it.
- Rewrite ONLY bullets where alignment with this job genuinely improves; omit the rest.
- Strong verb first, ≤ 28 words, no first person, mirror the job's own vocabulary where honest.

JOB: {title} at {company}
KEY REQUIREMENTS: {requirements}
KEYWORDS TO MIRROR: {keywords}

FACT BANK (the only permitted sources):
{facts}

RESUME BULLETS (ref → text):
{bullets}

CURRENT SKILLS SECTION: {skills}

Reply with ONLY this JSON:
{{
  "changes": [{{"ref": "e0.b1", "new": "rewritten bullet", "fact_ids": [12, 34]}}],
  "skills_order": ["the SAME skills, reordered most-relevant-first — add nothing"],
  "summary": "optional 1-2 sentence professional summary built only from facts, or null"
}}"""


# ─── Tailoring ───────────────────────────────────────────────────────────────

def _bullet_map(parsed: dict) -> dict:
    """{'e0.b1': bullet_text} for every bullet in the parsed base resume."""
    refs = {}
    for ei, exp in enumerate(parsed.get("experiences", []) or []):
        for bi, b in enumerate(exp.get("bullets", []) or []):
            refs[f"e{ei}.b{bi}"] = b
    return refs


def build_diff(parsed: dict, changes: list, facts_by_id: dict,
               skills_order, summary) -> dict:
    """Validate LLM changes and assemble the reviewable diff structure."""
    refs = _bullet_map(parsed)
    accepted_changes, dropped = {}, []
    for ch in changes or []:
        ref = str(ch.get("ref", ""))
        new = str(ch.get("new", "")).strip()
        ids = [int(i) for i in (ch.get("fact_ids") or []) if str(i).isdigit()]
        if ref not in refs or not new or new == refs[ref]:
            continue
        cited = [facts_by_id[i] for i in ids if i in facts_by_id]
        reason = validate_change(new, refs[ref], cited)
        if reason:
            dropped.append({"ref": ref, "new": new, "reason": reason})
            continue
        accepted_changes[ref] = {"new": new, "fact_ids": [f.id for f in cited]}

    experiences = []
    for ei, exp in enumerate(parsed.get("experiences", []) or []):
        bullets = []
        for bi, old in enumerate(exp.get("bullets", []) or []):
            ref = f"e{ei}.b{bi}"
            ch = accepted_changes.get(ref)
            bullets.append({
                "ref": ref, "old": old,
                "new": ch["new"] if ch else None,
                "fact_ids": ch["fact_ids"] if ch else [],
                "accepted": bool(ch),   # changed bullets start accepted; user can untick
                "edited": None,
            })
        experiences.append({
            "company": exp.get("company", ""), "title": exp.get("title", ""),
            "start": exp.get("start", ""), "end": exp.get("end", ""),
            "location": exp.get("location", ""), "bullets": bullets,
        })

    # skills_order may only reorder — never introduce new skills
    base_skills = [str(s) for s in (parsed.get("skills") or [])]
    base_lower = {s.lower(): s for s in base_skills}
    safe_order = [base_lower[str(s).lower()] for s in (skills_order or [])
                  if str(s).lower() in base_lower]
    safe_order += [s for s in base_skills if s not in safe_order]

    summary = (summary or "").strip() or None
    if summary:
        all_fact_text = " ".join(f.fact for f in facts_by_id.values())
        invented = _numbers(summary) - _numbers(all_fact_text)
        if invented:
            dropped.append({"ref": "summary", "new": summary,
                            "reason": f"metric(s) not in fact bank: {', '.join(sorted(invented))}"})
            summary = None

    return {
        "experiences": experiences,
        "skills_order": safe_order,
        "skills_accepted": True,
        "summary": {"new": summary, "accepted": bool(summary)},
        "dropped_by_validator": dropped,
        "changed_count": len(accepted_changes),
    }


def run_tailor(tailored_id: int):
    """Background task: generate the diff for a queued TailoredResume."""
    db = SessionLocal()
    try:
        t = db.query(TailoredResume).filter_by(id=tailored_id).first()
        if not t:
            return
        t.status = "generating"
        db.commit()

        posting = db.query(JobPosting).filter_by(id=t.job_posting_id).first()
        resume = db.query(BaseResume).filter_by(id=t.base_resume_id).first()
        if not posting or not resume or not resume.parsed_json:
            raise ValueError("posting or parsed base resume missing")

        # make sure requirements exist (one fast LLM call if the scraper's
        # enrichment hasn't reached this posting yet)
        req = posting.extracted_requirements
        if not req:
            from job_enrich import extract_requirements
            req = extract_requirements(posting)
            posting.extracted_requirements = req
            db.commit()

        facts = (db.query(ResumeFact)
                 .filter(ResumeFact.verified.is_(True)).all())
        if not facts:
            raise ValueError("fact bank is empty — upload/parse a resume first")
        facts_by_id = {f.id: f for f in facts}

        parsed = resume.parsed_json
        bullets_text = "\n".join(f"[{ref}] {b}" for ref, b in _bullet_map(parsed).items())
        facts_text = "\n".join(
            f"[{f.id}] {f.fact}" + (f" ({f.context})" if f.context else "")
            for f in facts
        )
        result = complete_json(_TAILOR_PROMPT.format(
            title=posting.title, company=posting.company,
            requirements=json.dumps((req.get("required_skills") or []) +
                                    (req.get("nice_to_have") or []))[:1200],
            keywords=", ".join(req.get("keywords") or [])[:400],
            facts=facts_text[:9000], bullets=bullets_text[:6000],
            skills=", ".join(parsed.get("skills") or [])[:800],
        ), tier="smart", max_tokens=8000)
        if not isinstance(result, dict):
            raise ValueError("LLM returned unusable tailoring output")

        t.diff_json = build_diff(parsed, result.get("changes"), facts_by_id,
                                 result.get("skills_order"), result.get("summary"))
        t.status = "draft"
        db.commit()
        logger.info(f"[Tailor] #{t.id} draft ready: {t.diff_json['changed_count']} rewrites, "
                    f"{len(t.diff_json['dropped_by_validator'])} dropped by validator")
    except Exception as e:
        db.rollback()
        t = db.query(TailoredResume).filter_by(id=tailored_id).first()
        if t:
            t.status = "failed"
            t.error = str(e)[:2000]
            db.commit()
        logger.error(f"[Tailor] #{tailored_id} failed: {e}")
    finally:
        db.close()


# ─── Final resume assembly + ATS-safe PDF ────────────────────────────────────

def apply_diff(parsed: dict, diff: dict) -> dict:
    """Base resume + accepted rewrites → final structure for rendering."""
    final = json.loads(json.dumps(parsed))  # deep copy
    for ei, exp in enumerate(diff.get("experiences", [])):
        for bi, b in enumerate(exp.get("bullets", [])):
            text = (b.get("edited") or b.get("new")) if b.get("accepted") else None
            if text and ei < len(final.get("experiences", [])) and \
                    bi < len(final["experiences"][ei].get("bullets", [])):
                final["experiences"][ei]["bullets"][bi] = text
    if diff.get("skills_accepted") and diff.get("skills_order"):
        final["skills"] = diff["skills_order"]
    s = diff.get("summary") or {}
    final["summary"] = s.get("new") if s.get("accepted") else None
    return final


def render_pdf(final: dict, profile: CandidateProfile, out_path: str):
    """Single-column, no tables/columns/images — parses cleanly in ATS scanners."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.enums import TA_CENTER
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    styles = {
        "name": ParagraphStyle("name", fontName="Helvetica-Bold", fontSize=17,
                               alignment=TA_CENTER, spaceAfter=2),
        "contact": ParagraphStyle("contact", fontName="Helvetica", fontSize=9,
                                  alignment=TA_CENTER, spaceAfter=10, textColor="#333333"),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11,
                             spaceBefore=8, spaceAfter=3),
        "role": ParagraphStyle("role", fontName="Helvetica-Bold", fontSize=10, spaceBefore=5),
        "meta": ParagraphStyle("meta", fontName="Helvetica-Oblique", fontSize=9,
                               spaceAfter=2, textColor="#333333"),
        "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9.5, leading=12.5),
        "bullet": ParagraphStyle("bullet", fontName="Helvetica", fontSize=9.5,
                                 leading=12.5, leftIndent=12, bulletIndent=2, spaceAfter=1),
    }

    def esc(s):
        return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    contact = (final.get("contact") or {})
    name = profile.full_name or contact.get("name") or "Resume"
    contact_bits = [x for x in [
        profile.email or contact.get("email"),
        profile.phone or contact.get("phone"),
        profile.location or contact.get("location"),
        (profile.links or {}).get("linkedin"),
        (profile.links or {}).get("github"),
    ] if x]

    story = [Paragraph(esc(name), styles["name"]),
             Paragraph(esc("  |  ".join(contact_bits)), styles["contact"])]

    if final.get("summary"):
        story += [Paragraph("SUMMARY", styles["h2"]),
                  Paragraph(esc(final["summary"]), styles["body"])]

    if final.get("skills"):
        story += [Paragraph("SKILLS", styles["h2"]),
                  Paragraph(esc(", ".join(str(s) for s in final["skills"])), styles["body"])]

    if final.get("experiences"):
        story.append(Paragraph("EXPERIENCE", styles["h2"]))
        for exp in final["experiences"]:
            story.append(Paragraph(
                f'{esc(exp.get("title", ""))} — {esc(exp.get("company", ""))}', styles["role"]))
            meta = " | ".join(x for x in [
                f'{exp.get("start", "")} – {exp.get("end", "")}'.strip(" –"),
                exp.get("location", "")] if x)
            if meta:
                story.append(Paragraph(esc(meta), styles["meta"]))
            for b in exp.get("bullets", []) or []:
                story.append(Paragraph(esc(b), styles["bullet"], bulletText="•"))

    if final.get("projects"):
        story.append(Paragraph("PROJECTS", styles["h2"]))
        for p in final["projects"]:
            line = p.get("name", "")
            if p.get("description"):
                line += f' — {p["description"]}'
            if p.get("tech"):
                line += f' ({", ".join(p["tech"])})'
            story.append(Paragraph(esc(line), styles["bullet"], bulletText="•"))

    if final.get("education"):
        story.append(Paragraph("EDUCATION", styles["h2"]))
        for e in final["education"]:
            line = " — ".join(x for x in [
                e.get("degree", ""), e.get("field", ""), e.get("school", ""),
                str(e.get("year", ""))] if x)
            story.append(Paragraph(esc(line), styles["body"]))

    if final.get("certifications"):
        story += [Paragraph("CERTIFICATIONS", styles["h2"]),
                  Paragraph(esc(", ".join(str(c) for c in final["certifications"])), styles["body"])]

    SimpleDocTemplate(
        out_path, pagesize=LETTER,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.55 * inch, bottomMargin=0.55 * inch,
        title=f"{name} — Resume", author=name,
    ).build(story)
    return out_path


def approve_and_render(db, t: TailoredResume) -> str:
    """Approval gate: render the PDF from accepted changes and mark approved."""
    resume = db.query(BaseResume).filter_by(id=t.base_resume_id).first()
    profile = db.query(CandidateProfile).first() or CandidateProfile()
    posting = db.query(JobPosting).filter_by(id=t.job_posting_id).first()
    final = apply_diff(resume.parsed_json, t.diff_json or {})
    safe_company = re.sub(r"[^A-Za-z0-9_-]", "_", (posting.company if posting else "job"))[:40]
    out = os.path.join(TAILORED_DIR, f"tailored_{t.id}_{safe_company}.pdf")
    render_pdf(final, profile, out)
    t.pdf_path = out
    t.status = "approved"
    t.approved_at = datetime.utcnow()
    db.commit()
    return out


# ─── Cover letter ────────────────────────────────────────────────────────────

_COVER_PROMPT = """Write a cover letter for this application.

CANDIDATE FACTS (the only permitted claims — never invent beyond these):
{facts}

JOB: {title} at {company}
KEY REQUIREMENTS: {requirements}
JOB DESCRIPTION (excerpt): {description}

CANDIDATE: {name}
TONE NOTES FROM THE CANDIDATE: {tone}

Rules:
- Exactly 3 short paragraphs, 170-220 words total, plain text, no header block
- Paragraph 1: why this specific role/company (reference something real about them)
- Paragraph 2: 2-3 SPECIFIC achievements from the facts, with their real metrics
- Paragraph 3: brief close with a clear ask
- Zero clichés ("passionate", "team player", "fast-paced", "excited to leverage")
- Sign off as {name}"""


def generate_cover_letter(db, job_posting_id: int) -> CoverLetter:
    posting = db.query(JobPosting).filter_by(id=job_posting_id).first()
    if not posting:
        raise ValueError("posting not found")
    profile = db.query(CandidateProfile).first()
    facts = db.query(ResumeFact).filter(ResumeFact.verified.is_(True)).all()
    if not facts:
        raise ValueError("fact bank is empty — upload/parse a resume first")

    req = posting.extracted_requirements or {}
    text = complete(_COVER_PROMPT.format(
        facts="\n".join(f"- {f.fact}" for f in facts)[:7000],
        title=posting.title, company=posting.company,
        requirements=", ".join(req.get("required_skills") or [])[:500],
        description=(posting.raw_description or posting.description_snippet or "")[:2500],
        name=(profile.full_name if profile else None) or "the candidate",
        tone=(profile.tone_notes if profile else None) or "confident, concrete, warm",
    ), tier="smart", max_tokens=1200)

    letter = (db.query(CoverLetter)
              .filter_by(job_posting_id=job_posting_id)
              .order_by(CoverLetter.created_at.desc()).first())
    if letter and letter.status != "approved":
        letter.text = text
        letter.status = "draft"
    else:  # none yet, or the existing one is approved — keep it, make a new draft
        letter = CoverLetter(job_posting_id=job_posting_id, text=text, status="draft")
        db.add(letter)
    db.commit()
    db.refresh(letter)
    return letter
