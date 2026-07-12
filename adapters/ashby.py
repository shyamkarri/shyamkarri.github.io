"""
Ashby adapter — jobs.ashbyhq.com. Fully client-rendered React; no stable ids,
so filling is label-driven (RunContext.fill_labeled_form) after switching to
the Application tab.
"""

from adapters.base import ATSAdapter, RunContext, NeedsReview


class AshbyAdapter(ATSAdapter):
    name = "ashby"

    @staticmethod
    def detect(url: str) -> bool:
        return "ashbyhq.com" in (url or "").lower()

    def fill(self, ctx: RunContext):
        page, app = ctx.page, ctx.app
        page.goto(app.apply_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_load_state("networkidle", timeout=30000)
        ctx.check_captcha()

        # posting pages have an "Application" tab or an Apply button
        for sel in ("a:has-text('Application')", "button:has-text('Apply for this Job')",
                    "button:has-text('Apply')"):
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=2000):
                    loc.click()
                    page.wait_for_load_state("networkidle", timeout=15000)
                    break
            except Exception:
                continue
        page.wait_for_selector("form", timeout=20000)
        ctx.snap("form_loaded")

        # resume first — Ashby autofills name/email from the parsed resume
        try:
            file_input = page.locator("form input[type='file']").first
            if file_input.count():
                ctx.upload_element(file_input, app.resume_pdf_path, "Resume")
                page.wait_for_timeout(4000)  # autofill settles
        except NeedsReview:
            raise
        except Exception:
            pass

        ctx.fill_labeled_form("form")
        ctx.check_captcha()

    def submit(self, ctx: RunContext) -> str:
        page = ctx.page
        ctx.check_captcha()
        page.locator("form button[type='submit'], "
                     "button:has-text('Submit Application')").first.click(timeout=15000)
        try:
            page.wait_for_selector(
                "text=/success|submitted|thank you|application received/i", timeout=30000)
        except Exception:
            if page.locator("form button[type='submit']").first.is_visible(timeout=1000):
                raise NeedsReview("Submit clicked but form still visible — check screenshots")
        return f"Submitted (landed on {page.url})"
