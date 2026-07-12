"""
Lever adapter — jobs.lever.co/{company}/{id}/apply.
Flat form (name/email/phone/org/urls) + card-based custom questions
(.application-question blocks).
"""

import re

from adapters.base import ATSAdapter, RunContext, NeedsReview


class LeverAdapter(ATSAdapter):
    name = "lever"

    @staticmethod
    def detect(url: str) -> bool:
        return "lever.co" in (url or "").lower()

    def fill(self, ctx: RunContext):
        page, app = ctx.page, ctx.app
        url = app.apply_url
        if not url.rstrip("/").endswith("/apply"):
            url = url.rstrip("/") + "/apply"
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_selector("input[name='name'], #application-form", timeout=20000)
        ctx.check_captcha()
        ctx.snap("form_loaded")

        ctx.fill("input[name='name']", app.full_name, "Full name")
        ctx.fill("input[name='email']", app.email, "Email")
        ctx.fill("input[name='phone']", app.phone, "Phone", required=False)
        ctx.fill("input[name='location']", app.location, "Location", required=False)
        links = app.links or {}
        ctx.fill("input[name='urls[LinkedIn]']", links.get("linkedin"),
                 "LinkedIn", required=False)
        ctx.fill("input[name='urls[GitHub]']", links.get("github"),
                 "GitHub", required=False)

        ctx.upload("input[name='resume'], #resume-upload-input",
                   app.resume_pdf_path, "Resume")
        page.wait_for_timeout(2500)

        if app.cover_letter_text:
            try:
                cl = page.locator("textarea[name='comments'], #additional-information").first
                if cl.is_visible(timeout=1500):
                    cl.fill(app.cover_letter_text)
                    ctx.receipt("Additional information / cover letter",
                                app.cover_letter_text[:200] + "…",
                                "approved_cover_letter")
            except Exception:
                pass

        self._card_questions(ctx)
        ctx.check_captcha()

    def _card_questions(self, ctx: RunContext):
        """Lever custom questions: .application-question → label + field.
        Radio/checkbox groups get engine answers mapped onto option labels."""
        cards = ctx.page.locator(".application-question")
        for i in range(cards.count()):
            card = cards.nth(i)
            try:
                qtext = re.sub(r"\s+", " ", card.locator(
                    ".application-label, .text").first.inner_text(timeout=1500)).strip()
            except Exception:
                continue
            if not qtext or re.search(r"resume|cv\b", qtext, re.I):
                continue
            required = "✱" in qtext or "*" in qtext
            clean = re.sub(r"[✱*]", "", qtext).strip()

            text_widget = card.locator(
                "input[type=text], input[type=number], textarea, select").first
            radios = card.locator("input[type=radio], input[type=checkbox]")
            try:
                if text_widget.count():
                    if text_widget.input_value(timeout=1000):
                        continue
                    ctx.fill_question(text_widget, clean)
                elif radios.count():
                    answer, source = ctx.answer(clean)
                    options = []
                    for r in range(radios.count()):
                        opt_label = radios.nth(r).evaluate(
                            "el => (el.closest('label')?.innerText || el.value || '').trim()")
                        options.append((opt_label, radios.nth(r)))
                    from adapters.base import _closest_option
                    match = _closest_option(answer, [o[0] for o in options])
                    if not match:
                        raise NeedsReview(
                            f"No option matches answer for '{clean[:80]}' → '{answer[:60]}'")
                    dict(options)[match].check()
                    ctx.receipt(clean, match, source)
            except NeedsReview:
                if required:
                    raise
                ctx.receipt(clean, "(left blank — optional)", "skipped")

    def submit(self, ctx: RunContext) -> str:
        page = ctx.page
        ctx.check_captcha()
        page.locator("#btn-submit, button[type='submit']").first.click(timeout=15000)
        try:
            page.wait_for_selector(
                ".application-confirmation, [data-qa='msg-submit-success'], "
                "text=/application.{0,20}(submitted|received)|thank you/i",
                timeout=30000)
        except Exception:
            if page.locator("#btn-submit").first.is_visible(timeout=1000):
                raise NeedsReview("Submit clicked but form still visible — validation error?")
        return f"Submitted (landed on {page.url})"
