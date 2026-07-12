"""
Resume bank — PDF upload → text extraction → LLM structured parse → FactBank.

The FactBank is the anti-hallucination layer: tailoring (Phase 2) may only use
statements that exist here, and every rewritten bullet stores the fact ids it
was built from.

Files are stored on local disk under UPLOADS_DIR (Render disk is ephemeral —
raw_text + parsed_json + facts live in the DB, so a disk wipe only loses the
original PDF, which can be re-uploaded).
"""

import os
import json
import logging
from datetime import datetime

from database import SessionLocal, BaseResume, ResumeFact
from llm import complete_json

logger = logging.getLogger("agent_logger")

UPLOADS_DIR = os.getenv("UPLOADS_DIR", os.path.join(os.path.dirname(__file__), "uploads"))
os.makedirs(UPLOADS_DIR, exist_ok=True)


# ─── File storage abstraction (swap for R2/S3 later without touching callers) ─

def save_resume_file(filename: str, content: bytes) -> str:
    """Store the uploaded PDF; returns the storage path."""
    safe = "".join(c for c in filename if c.isalnum() or c in "._-") or "resume.pdf"
    path = os.path.join(UPLOADS_DIR, f"{datetime.utcnow():%Y%m%d%H%M%S}_{safe}")
    with open(path, "wb") as f:
        f.write(content)
    return path


def read_resume_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# ─── PDF text extraction ─────────────────────────────────────────────────────

def extract_pdf_text(path: str) -> str:
    from pypdf import PdfReader
    reader = PdfReader(path)
    return "\n".join((page.extract_text() or "") for page in reader.pages).strip()


# ─── LLM structured parse ────────────────────────────────────────────────────

_PARSE_PROMPT = """Extract this resume into structured JSON. Copy text faithfully —
do NOT invent, embellish, or normalize away any detail. Keep metrics exactly as written.

Resume text:
{text}

Reply with ONLY this JSON shape:
{{
  "contact": {{"name": "", "email": "", "phone": "", "location": "", "links": []}},
  "experiences": [{{"company": "", "title": "", "start": "", "end": "", "location": "", "bullets": ["..."]}}],
  "skills": ["..."],
  "education": [{{"school": "", "degree": "", "field": "", "year": ""}}],
  "projects": [{{"name": "", "description": "", "tech": []}}],
  "certifications": ["..."]
}}"""

_FACTS_PROMPT = """Below is a structured resume. Break it into ATOMIC facts — one
verifiable claim per fact. Rules:
- Copy claims faithfully from the resume; NEVER invent or round numbers
- Each bullet with a metric becomes its own fact (category "metric" if it has a number)
- Each distinct skill/tool actually used is a fact (category "skill")
- Employment periods ("Worked as X at Y from A to B") are facts (category "experience")
- Education/certifications → category "education"; projects → "project"
- "context" = which job/section the fact came from

Resume JSON:
{parsed}

Reply with ONLY a JSON array:
[{{"category": "experience|skill|education|project|metric|other", "fact": "...", "context": "..."}}]"""

VALID_CATEGORIES = {"experience", "skill", "education", "project", "metric", "other"}


def parse_resume_text(raw_text: str) -> dict:
    """LLM parse of raw resume text → structured dict. Raises on failure."""
    parsed = complete_json(_PARSE_PROMPT.format(text=raw_text[:15000]), tier="smart")
    if not isinstance(parsed, dict) or "experiences" not in parsed:
        raise ValueError("LLM returned unusable resume structure")
    return parsed


def extract_facts(parsed: dict) -> list:
    """LLM extraction of atomic facts from a parsed resume."""
    facts = complete_json(
        _FACTS_PROMPT.format(parsed=json.dumps(parsed, indent=1)[:15000]), tier="smart"
    )
    if not isinstance(facts, list):
        raise ValueError("LLM returned unusable fact list")
    clean = []
    for f in facts:
        text = str(f.get("fact", "")).strip()
        if not text:
            continue
        cat = str(f.get("category", "other")).lower()
        clean.append({
            "category": cat if cat in VALID_CATEGORIES else "other",
            "fact": text[:1000],
            "context": str(f.get("context", ""))[:512],
        })
    return clean


def process_resume(resume_id: int):
    """Background task: extract text, parse, build the fact bank.
    Owns its DB session (runs outside the request cycle)."""
    db = SessionLocal()
    try:
        resume = db.query(BaseResume).filter_by(id=resume_id).first()
        if not resume:
            return
        resume.parse_status = "parsing"
        resume.parse_error = None
        db.commit()

        if not resume.raw_text:
            resume.raw_text = extract_pdf_text(resume.file_path)
            db.commit()
        if not resume.raw_text or len(resume.raw_text) < 100:
            raise ValueError("PDF text extraction produced almost nothing — is it a scanned image?")

        resume.parsed_json = parse_resume_text(resume.raw_text)
        db.commit()

        # rebuild this resume's facts (manual facts have resume_id NULL — untouched)
        db.query(ResumeFact).filter_by(resume_id=resume.id).delete()
        for f in extract_facts(resume.parsed_json):
            db.add(ResumeFact(resume_id=resume.id, source="resume", **f))

        resume.parse_status = "ready"
        db.commit()
        logger.info(f"[ResumeBank] resume {resume.id} parsed: "
                    f"{len(resume.parsed_json.get('experiences', []))} roles")
    except Exception as e:
        db.rollback()
        resume = db.query(BaseResume).filter_by(id=resume_id).first()
        if resume:
            resume.parse_status = "failed"
            resume.parse_error = str(e)[:2000]
            db.commit()
        logger.error(f"[ResumeBank] resume {resume_id} parse failed: {e}")
    finally:
        db.close()


def fact_bank_skills(db) -> set:
    """All verified skill-ish tokens from the fact bank, lowercased,
    for deterministic match scoring."""
    rows = (
        db.query(ResumeFact)
        .filter(ResumeFact.verified.is_(True))
        .all()
    )
    skills = set()
    for r in rows:
        if r.category == "skill":
            skills.add(r.fact.lower().strip())
        # also index tool names embedded in metric/experience facts
    # add skills[] from default parsed resume for better coverage
    default = db.query(BaseResume).filter_by(is_default=True, parse_status="ready").first() \
        or db.query(BaseResume).filter_by(parse_status="ready").first()
    if default and default.parsed_json:
        for s in default.parsed_json.get("skills", []) or []:
            skills.add(str(s).lower().strip())
    return {s for s in skills if s}
