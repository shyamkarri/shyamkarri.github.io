"""
ATSAdapter interface + RunContext (the toolbox every adapter fills through).

Contract:
    detect(url)        class-level — does this adapter handle the URL?
    login(ctx)         optional (Workday)
    fill(ctx)          navigate + fill the whole form; every field through
                       ctx.fill/ctx.select/ctx.upload/ctx.answer so receipts
                       are complete
    review(ctx)        pre-submit verification; returns the receipts
    submit(ctx)        click submit; returns confirmation text

Rules baked into RunContext:
  * every filled field produces a receipt {label, value, source, selector}
  * ctx.snap() screenshots go to the DB (RunArtifact) immediately, so a run
    that dies mid-way still has evidence
  * CAPTCHA is NEVER bypassed — ctx.check_captcha() raises CaptchaDetected
    and the worker pauses the run for the human
  * unknown/unfillable required fields raise NeedsReview — never silently
    skipped
"""

import io
import re
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("agent_logger")


class CaptchaDetected(Exception):
    pass


class NeedsReview(Exception):
    pass


@dataclass
class ApplicationData:
    """Everything an adapter may put in a form — assembled by the worker."""
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    links: dict = field(default_factory=dict)
    resume_pdf_path: str = ""
    cover_letter_text: str = ""
    company: str = ""
    title: str = ""
    apply_url: str = ""
    eeo: dict = field(default_factory=dict)
    profile: object = None          # CandidateProfile row (answer engine needs it)


class RunContext:
    def __init__(self, page, db, run, app: ApplicationData):
        self.page = page
        self.db = db
        self.run = run
        self.app = app
        self.receipts = []
        self.pending = []           # generated answers awaiting user approval
        self._shot_idx = 0

    # ── receipts ─────────────────────────────────────────────────────────────
    def receipt(self, label: str, value, source: str, selector: str = None):
        self.receipts.append({"label": str(label)[:300], "value": str(value)[:1500],
                              "source": source, "selector": selector})
        self.run.field_receipts = list(self.receipts)
        self.db.commit()

    # ── screenshots (straight to DB so evidence survives crashes) ───────────
    def snap(self, name: str):
        from database import RunArtifact
        try:
            data = self.page.screenshot(full_page=True, type="jpeg", quality=55)
        except Exception as e:
            logger.warning(f"[Run {self.run.id}] screenshot '{name}' failed: {e}")
            return
        self._shot_idx += 1
        self.db.add(RunArtifact(run_id=self.run.id,
                                name=f"{self._shot_idx:02d}_{name}.jpg",
                                content=data))
        self.run.current_url = self.page.url[:1024]
        self.db.commit()

    # ── field helpers (fill + receipt in one move) ───────────────────────────
    def fill(self, selector: str, value, label: str, source: str = "profile",
             required: bool = True):
        if value in (None, ""):
            if required:
                raise NeedsReview(f"No value for required field '{label}'")
            return False
        try:
            loc = self.page.locator(selector).first
            loc.fill(str(value), timeout=8000)
            self.receipt(label, value, source, selector)
            return True
        except Exception as e:
            if required:
                raise NeedsReview(f"Could not fill '{label}' ({selector}): {type(e).__name__}")
            return False

    def select(self, selector: str, value: str, label: str, source: str = "profile",
               required: bool = True):
        try:
            self.page.locator(selector).first.select_option(label=value, timeout=8000)
            self.receipt(label, value, source, selector)
            return True
        except Exception:
            try:  # some selects match on value attr instead of visible label
                self.page.locator(selector).first.select_option(value=value, timeout=4000)
                self.receipt(label, value, source, selector)
                return True
            except Exception as e:
                if required:
                    raise NeedsReview(f"Could not select '{value}' for '{label}': {type(e).__name__}")
                return False

    def upload(self, selector: str, path: str, label: str = "Resume"):
        if not path:
            raise NeedsReview(f"No file to upload for '{label}'")
        try:
            self.page.locator(selector).first.set_input_files(path, timeout=15000)
            self.receipt(label, path.split("/")[-1], "tailored_resume", selector)
        except Exception as e:
            raise NeedsReview(f"Could not upload '{label}': {type(e).__name__}")

    def click(self, selector: str, timeout: int = 10000):
        self.page.locator(selector).first.click(timeout=timeout)

    # ── open-ended questions via the answer engine ───────────────────────────
    def answer(self, question: str):
        """→ answer text (may queue it for approval); None if unanswerable."""
        from answer_engine import get_answer
        text, source, needs_approval = get_answer(
            self.db, question, self.app.company, self.app.title, self.app.profile)
        if text is None:
            raise NeedsReview(f"Could not produce an answer for: '{question[:120]}'")
        if needs_approval:
            self.pending.append({"question": question, "answer": text, "source": source})
        return text, source

    def fill_question(self, element, question: str):
        """Fill one custom-question element (input/textarea/select) with an
        engine answer and a receipt."""
        text, source = self.answer(question)
        tag = element.evaluate("el => el.tagName.toLowerCase()")
        if tag == "select":
            try:
                element.select_option(label=text)
            except Exception:
                opts = element.evaluate(
                    "el => Array.from(el.options).map(o => o.label)")
                match = _closest_option(text, opts)
                if not match:
                    raise NeedsReview(f"No matching option for '{question[:80]}' → '{text[:60]}'")
                element.select_option(label=match)
                text = match
        else:
            element.fill(str(text))
        self.receipt(question, text, source)

    # ── CAPTCHA — detect, never bypass ───────────────────────────────────────
    CAPTCHA_SELECTORS = (
        'iframe[src*="recaptcha"]', 'iframe[src*="hcaptcha"]',
        'iframe[src*="turnstile"]', '.g-recaptcha', '.h-captcha',
        '[data-sitekey]',
    )

    def captcha_present(self) -> bool:
        for sel in self.CAPTCHA_SELECTORS:
            try:
                if self.page.locator(sel).first.is_visible(timeout=300):
                    return True
            except Exception:
                continue
        return False

    def check_captcha(self):
        if self.captcha_present():
            self.snap("captcha")
            raise CaptchaDetected("CAPTCHA on page — pausing for human")

    # ── generic label-driven fill (JS-heavy boards: Ashby/SmartRecruiters) ───
    LABEL_MAP = [
        (re.compile(r"first\s*name", re.I), "first_name", "profile"),
        (re.compile(r"last\s*name|family\s*name|surname", re.I), "last_name", "profile"),
        (re.compile(r"full\s*name|^name\b|your\s*name", re.I), "full_name", "profile"),
        (re.compile(r"e-?mail", re.I), "email", "profile"),
        (re.compile(r"phone|mobile", re.I), "phone", "profile"),
        (re.compile(r"location|city|address", re.I), "location", "profile"),
        (re.compile(r"linkedin", re.I), "links.linkedin", "profile"),
        (re.compile(r"github|portfolio|website", re.I), "links.github", "profile"),
        (re.compile(r"current\s*company|employer|organization", re.I), "_company", "profile"),
    ]

    def _value_for(self, key: str):
        if key.startswith("links."):
            return (self.app.links or {}).get(key.split(".", 1)[1])
        if key == "_company":
            return None  # only from answer engine if truly required
        return getattr(self.app, key, None)

    def fill_labeled_form(self, form_selector: str = "form"):
        """Walk every <label> in the form: known labels from the profile, file
        inputs get the resume, everything else goes to the answer engine.
        Required fields we can't resolve raise NeedsReview."""
        labels = self.page.locator(f"{form_selector} label")
        for i in range(labels.count()):
            lab = labels.nth(i)
            try:
                text = re.sub(r"\s+", " ", lab.inner_text(timeout=2000)).strip()
            except Exception:
                continue
            if not text:
                continue
            target = self._labeled_input(lab)
            if target is None:
                continue
            input_type = (target.get_attribute("type") or "text").lower()
            tag = target.evaluate("el => el.tagName.toLowerCase()")
            required = bool(re.search(r"[*✱]|required", text, re.I)) or \
                (target.get_attribute("aria-required") == "true") or \
                (target.get_attribute("required") is not None)
            clean = re.sub(r"[*✱]|\brequired\b", "", text, flags=re.I).strip()

            if input_type == "file":
                self.upload_element(target, self.app.resume_pdf_path, clean or "Resume")
                continue
            if input_type in ("checkbox", "radio", "hidden", "submit"):
                continue  # consents/EEO handled per-adapter, never blind-ticked
            if target.input_value(timeout=1500) if tag in ("input", "textarea") else "":
                continue  # already filled (e.g. autofill)

            matched = False
            for pat, key, source in self.LABEL_MAP:
                if pat.search(clean):
                    val = self._value_for(key)
                    if val:
                        target.fill(str(val))
                        self.receipt(clean, val, source)
                        matched = True
                    break
            if matched:
                continue
            if required or tag in ("textarea", "select"):
                self.fill_question(target, clean)

    def _labeled_input(self, label_loc):
        """The input/textarea/select a <label> points at (for= or nested)."""
        try:
            for_id = label_loc.get_attribute("for")
            if for_id:
                escaped = re.sub(r"([:.\[\]])", lambda m: "\\" + m.group(1), for_id)
                t = self.page.locator("#" + escaped).first
                if t.count():
                    return t
            nested = label_loc.locator("input, textarea, select").first
            if nested.count():
                return nested
            sib = label_loc.locator(
                "xpath=following::*[self::input or self::textarea or self::select][1]").first
            return sib if sib.count() else None
        except Exception:
            return None

    def upload_element(self, element, path: str, label: str):
        if not path:
            raise NeedsReview(f"No file for '{label}'")
        element.set_input_files(path, timeout=15000)
        self.receipt(label, path.split("/")[-1], "tailored_resume")


def _closest_option(answer: str, options: list) -> Optional[str]:
    """Map an engine answer onto a select's options ('Yes'/'No' etc.)."""
    a = (answer or "").strip().lower()
    for o in options:
        if o and o.strip().lower() == a:
            return o
    for o in options:
        if o and (a.startswith(o.strip().lower()) or o.strip().lower() in a):
            if o.strip().lower() not in ("select", "select...", "--", ""):
                return o
    return None


class ATSAdapter:
    name = "base"

    @staticmethod
    def detect(url: str) -> bool:
        return False

    def login(self, ctx: RunContext):
        return None

    def fill(self, ctx: RunContext):
        raise NotImplementedError

    def review(self, ctx: RunContext) -> list:
        """Default review: screenshot + return receipts for the approval log."""
        ctx.snap("pre_submit_review")
        return ctx.receipts

    def submit(self, ctx: RunContext) -> str:
        raise NotImplementedError
