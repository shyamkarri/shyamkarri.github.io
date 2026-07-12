"""
Answer engine for open-ended ATS questions. Resolution order:

  1. DETERMINISTIC — work authorization, sponsorship, links, salary, source.
     Answered exactly from the Profile, never optimistically, never by an LLM.
  2. ANSWER BANK — approved answers are reused verbatim (company-scoped for
     motivation-type questions, global otherwise).
  3. GENERATED — LLM drafts from FactBank + tone notes. Generated answers are
     stored UNAPPROVED and the run pauses in awaiting_approval; nothing is
     submitted until the user approves them (they then live in the bank, so
     the next run reuses them without pausing).

Every answer returns (text, source, needs_approval).
"""

import re
import logging

from sqlalchemy import or_

from database import AnswerBankEntry, ResumeFact

logger = logging.getLogger("agent_logger")

# work auth values that mean "currently authorized to work in the US"
_AUTHORIZED_NOW = {"citizen", "green_card", "opt", "stem_opt", "h1b"}
# values that will NOT need employer sponsorship now or in the future
_NEVER_NEEDS_SPONSORSHIP = {"citizen", "green_card"}

_MOTIVATION_PAT = re.compile(
    r"why (do you want|are you interested|this role|us|.{0,30}company)|"
    r"what (excites|interests|draws|attracts)|motivat|cover letter|"
    r"why would you like", re.I)


def normalize_question(question: str, company: str = "") -> str:
    """Lowercase, strip punctuation/whitespace/company name → bank lookup key."""
    q = (question or "").lower()
    if company:
        q = q.replace(company.lower(), " ")
    q = re.sub(r"[^a-z0-9 ]", " ", q)
    return re.sub(r"\s+", " ", q).strip()[:512]


def is_company_specific(question: str) -> bool:
    return bool(_MOTIVATION_PAT.search(question or ""))


def deterministic_answer(question: str, profile):
    """Profile-verbatim answers for compliance-critical questions. Returns
    text or None. NEVER guesses: unknown auth state answers conservatively."""
    q = (question or "").lower()
    auth = (profile.work_authorization or "").lower() if profile else ""
    links = (profile.links or {}) if profile else {}

    if "sponsor" in q:  # "will you now or in the future require sponsorship"
        if not auth:
            return None  # profile not filled in — do not guess
        return "No" if auth in _NEVER_NEEDS_SPONSORSHIP else "Yes"
    if re.search(r"(legally )?authoriz|eligible to work|right to work", q):
        if not auth:
            return None
        return "Yes" if auth in _AUTHORIZED_NOW else "No"
    if "visa status" in q or ("work" in q and "status" in q and "authorization" in q):
        pretty = {"citizen": "U.S. Citizen", "green_card": "Permanent Resident (Green Card)",
                  "opt": "F-1 OPT", "stem_opt": "F-1 STEM OPT", "h1b": "H-1B",
                  "needs_sponsorship": "Requires sponsorship"}
        return pretty.get(auth)
    if "linkedin" in q:
        return links.get("linkedin")
    if "github" in q or "portfolio" in q or "website" in q:
        return links.get("github") or links.get("portfolio") or links.get("website")
    if re.search(r"salary|compensation|pay expectation", q):
        floor = getattr(profile, "salary_floor", None) if profile else None
        return f"${floor:,} or commensurate with the role's range" if floor else None
    if "how did you hear" in q or "hear about" in q:
        return "Company careers page"
    return None


def lookup_bank(db, question: str, company: str):
    """Approved bank entry for this question — company-scoped ones win."""
    norm = normalize_question(question, company)
    scoped = is_company_specific(question)
    q = (db.query(AnswerBankEntry)
         .filter_by(question_norm=norm, approved=True))
    if scoped:
        q = q.filter(AnswerBankEntry.company == company)
    else:
        q = q.filter(or_(AnswerBankEntry.company.is_(None),
                         AnswerBankEntry.company == company))
    entry = q.order_by(AnswerBankEntry.company.isnot(None).desc()).first()
    if entry:
        entry.times_reused = (entry.times_reused or 0) + 1
        db.commit()
    return entry


_GEN_PROMPT = """Answer this job-application question as the candidate.

CANDIDATE FACTS (only permitted claims):
{facts}

TONE NOTES: {tone}
COMPANY: {company} — ROLE: {title}
QUESTION: {question}

Rules: 60-120 words unless the question implies shorter, first person, specific,
zero clichés, never invent employers/metrics/skills beyond the facts.
Reply with ONLY the answer text."""


def generate_answer(db, question: str, company: str, title: str, profile) -> str:
    from llm import complete
    facts = db.query(ResumeFact).filter(ResumeFact.verified.is_(True)).limit(60).all()
    return complete(_GEN_PROMPT.format(
        facts="\n".join(f"- {f.fact}" for f in facts)[:6000],
        tone=(profile.tone_notes if profile else None) or "confident, concrete, warm",
        company=company, title=title, question=question[:500],
    ), tier="smart", max_tokens=600).strip()


def get_answer(db, question: str, company: str, title: str, profile):
    """→ (answer_text, source, needs_approval). answer_text may be None when
    even the LLM path is unavailable (caller marks the run needs_review)."""
    det = deterministic_answer(question, profile)
    if det:
        return det, "profile", False

    entry = lookup_bank(db, question, company)
    if entry:
        return entry.answer, "answer_bank", False

    try:
        text = generate_answer(db, question, company, title, profile)
    except Exception as e:
        logger.warning(f"[Answers] generation failed for '{question[:60]}': {e}")
        return None, "unavailable", True
    if not text:
        return None, "unavailable", True

    # store unapproved — the run pauses until the user approves it
    db.add(AnswerBankEntry(
        question=question[:2000],
        question_norm=normalize_question(question, company),
        answer=text,
        company=company if is_company_specific(question) else None,
        source="generated", approved=False,
    ))
    db.commit()
    return text, "generated", True
