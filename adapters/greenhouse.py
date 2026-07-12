"""
Greenhouse adapter — classic boards.greenhouse.io application forms
(stable ids: #first_name, #last_name, #email, #phone, #custom_fields …).
The easiest ATS: one page, one submit.
"""

import re

from adapters.base import ATSAdapter, RunContext, NeedsReview


class GreenhouseAdapter(ATSAdapter):
    name = "greenhouse"

    @staticmethod
    def detect(url: str) -> bool:
        return "greenhouse.io" in (url or "").lower()

    def fill(self, ctx: RunContext):
        page, app = ctx.page, ctx.app
        page.goto(app.apply_url, wait_until="domcontentloaded", timeout=45000)
        # some postings put the form behind an "Apply" tab/button
        for sel in ("#apply_button", "a[href*='#app']", "button:has-text('Apply')"):
            try:
                if page.locator(sel).first.is_visible(timeout=1500) and \
                        not page.locator("#first_name").first.is_visible(timeout=500):
                    page.locator(sel).first.click()
                    break
            except Exception:
                continue
        page.wait_for_selector("#first_name, #application-form, #application_form",
                               timeout=20000)
        ctx.check_captcha()
        ctx.snap("form_loaded")

        ctx.fill("#first_name", app.first_name, "First name")
        ctx.fill("#last_name", app.last_name, "Last name")
        ctx.fill("#email", app.email, "Email")
        ctx.fill("#phone", app.phone, "Phone", required=False)
        ctx.fill("#job_application_location, input[name*='location']",
                 app.location, "Location", required=False)

        # resume: prefer the raw file input (present even behind styled buttons)
        ctx.upload("input[type='file']", app.resume_pdf_path, "Resume")
        page.wait_for_timeout(2500)  # let the S3 upload/parse settle

        if app.cover_letter_text:
            try:
                if page.locator("#cover_letter_text").first.is_visible(timeout=1500):
                    ctx.fill("#cover_letter_text", app.cover_letter_text,
                             "Cover letter", source="approved_cover_letter")
            except Exception:
                pass

        self._custom_questions(ctx)
        self._eeo(ctx)
        ctx.check_captcha()

    def _custom_questions(self, ctx: RunContext):
        """#custom_fields holds one .field per question: label + widget."""
        fields = ctx.page.locator("#custom_fields .field, div[class*='custom-field']")
        for i in range(fields.count()):
            f = fields.nth(i)
            try:
                label = re.sub(r"\s+", " ", f.locator("label").first.inner_text(timeout=1500)).strip()
            except Exception:
                continue
            widget = f.locator("input:not([type=hidden]):not([type=file]), textarea, select").first
            if not widget.count():
                continue
            wtype = (widget.get_attribute("type") or "text").lower()
            if wtype in ("checkbox", "radio"):
                continue  # demographic/consent boxes are handled in _eeo or left
            tag = widget.evaluate("el => el.tagName.toLowerCase()")
            try:
                # a select's placeholder option ("--") counts as a value — only
                # treat text inputs/textareas as pre-filled
                if tag != "select" and widget.input_value(timeout=1000):
                    continue
            except Exception:
                pass
            required = "*" in label or widget.get_attribute("aria-required") == "true"
            clean = label.replace("*", "").strip()
            try:
                ctx.fill_question(widget, clean)
            except NeedsReview:
                if required:
                    raise
                ctx.receipt(clean, "(left blank — optional, no grounded answer)", "skipped")

    def _eeo(self, ctx: RunContext):
        """EEOC selects — filled ONLY from profile.eeo_answers, else the
        'decline to self identify' option."""
        eeo = ctx.app.eeo or {}
        block = ctx.page.locator("#eeoc_fields, #demographic_questions")
        if not block.count():
            return
        selects = block.locator("select")
        for i in range(selects.count()):
            s = selects.nth(i)
            try:
                label = re.sub(r"\s+", " ", s.locator(
                    "xpath=preceding::label[1]").inner_text(timeout=1000)).strip()
            except Exception:
                label = f"EEO question {i + 1}"
            answer = None
            for key, val in eeo.items():
                if key.lower() in label.lower():
                    answer = val
                    break
            opts = s.evaluate("el => Array.from(el.options).map(o => o.label)")
            if not answer:
                answer = next((o for o in opts if re.search(
                    r"decline|don'?t wish|do not wish|prefer not", o, re.I)), None)
            if answer:
                from adapters.base import _closest_option
                match = _closest_option(answer, opts) or answer
                try:
                    s.select_option(label=match)
                    ctx.receipt(label, match, "profile" if label.lower() in
                                str(eeo).lower() else "declined")
                except Exception:
                    pass

    def submit(self, ctx: RunContext) -> str:
        page = ctx.page
        ctx.check_captcha()
        page.locator("#submit_app, input[type='submit'], button[type='submit']").first.click(timeout=15000)
        try:
            page.wait_for_selector(
                "#application_confirmation, .application-confirmation, "
                "text=/thank you/i", timeout=30000)
        except Exception:
            # no confirmation marker — check we at least left the form
            if page.locator("#submit_app").first.is_visible(timeout=1000):
                raise NeedsReview("Clicked submit but the form is still showing — "
                                  "possible validation error")
        for sel in ("#application_confirmation", ".application-confirmation"):
            try:
                if page.locator(sel).first.is_visible(timeout=1000):
                    return page.locator(sel).first.inner_text()[:1000]
            except Exception:
                continue
        return f"Submitted (landed on {page.url})"
