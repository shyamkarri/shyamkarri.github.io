"""
ADAPTER TEMPLATE — copy this file to adapters/<newats>.py, implement the
methods, then register the class in adapters/__init__.get_adapter() (and add a
detection branch to detect_ats() if the URL is recognizable).

The RunContext (ctx) gives you everything and records receipts/screenshots for
you. Golden rules:
  * fill every field through ctx.fill / ctx.select / ctx.upload / ctx.answer /
    ctx.fill_question — that is what produces the field-by-field receipt log
  * call ctx.snap("step_name") at each meaningful step (screenshots → DB)
  * call ctx.check_captcha() before and after filling, and before submit —
    NEVER try to solve or bypass a CAPTCHA
  * raise NeedsReview(...) for anything you can't safely complete (unknown
    required field, validation error, ambiguous option) — never guess
  * open-ended questions go through ctx.answer()/ctx.fill_question() so the
    answer engine (profile → answer bank → generated-with-approval) handles them
  * never invent work-authorization answers — the answer engine reads them
    from the profile

Test it with a saved HTML fixture like tests/fixtures/greenhouse_form.html and
a file:// URL (see tests/test_adapters.py).
"""

from adapters.base import ATSAdapter, RunContext, NeedsReview  # noqa: F401


class TemplateAdapter(ATSAdapter):
    name = "template"

    @staticmethod
    def detect(url: str) -> bool:
        return "example-ats.com" in (url or "").lower()

    def login(self, ctx: RunContext):
        # Only needed for account-based ATSes (see workday.py). Delete otherwise.
        return None

    def fill(self, ctx: RunContext):
        page, app = ctx.page, ctx.app
        page.goto(app.apply_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_selector("form", timeout=20000)
        ctx.check_captcha()
        ctx.snap("form_loaded")

        # Known profile fields (each also writes a receipt):
        ctx.fill("#first_name", app.first_name, "First name")
        ctx.fill("#last_name", app.last_name, "Last name")
        ctx.fill("#email", app.email, "Email")
        ctx.fill("#phone", app.phone, "Phone", required=False)
        ctx.upload("input[type='file']", app.resume_pdf_path, "Resume")

        # Custom/open-ended questions — hand each to the answer engine, e.g.:
        #   ctx.fill_question(page.locator("#q1"), "Why do you want to work here?")
        # or, for a whole label-driven form:
        #   ctx.fill_labeled_form("form")

        ctx.check_captcha()

    # review() has a sensible default (screenshot + return receipts); override
    # only if you need extra pre-submit verification.

    def submit(self, ctx: RunContext) -> str:
        page = ctx.page
        ctx.check_captcha()
        page.locator("button[type='submit']").first.click(timeout=15000)
        try:
            page.wait_for_selector("text=/thank you|submitted|received/i", timeout=30000)
        except Exception:
            if page.locator("button[type='submit']").first.is_visible(timeout=1000):
                raise NeedsReview("Submit clicked but the form is still visible — "
                                  "likely a validation error; see screenshots")
        return f"Submitted (landed on {page.url})"
