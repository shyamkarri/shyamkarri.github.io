"""
SmartRecruiters adapter — jobs.smartrecruiters.com/{Company}/{id}.
"I'm interested" opens the application panel; known input names first,
label-driven fallback for everything else.
"""

from adapters.base import ATSAdapter, RunContext, NeedsReview


class SmartRecruitersAdapter(ATSAdapter):
    name = "smartrecruiters"

    @staticmethod
    def detect(url: str) -> bool:
        return "smartrecruiters.com" in (url or "").lower()

    def fill(self, ctx: RunContext):
        page, app = ctx.page, ctx.app
        page.goto(app.apply_url, wait_until="domcontentloaded", timeout=45000)
        ctx.check_captcha()
        for sel in ("button:has-text(\"I'm interested\")", "a:has-text(\"I'm interested\")",
                    "button:has-text('Apply')", "[data-test='apply-button']"):
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=2500):
                    loc.click()
                    break
            except Exception:
                continue
        page.wait_for_selector("form", timeout=25000)
        ctx.snap("form_loaded")

        ctx.fill("input[name='firstName'], input#first-name",
                 app.first_name, "First name")
        ctx.fill("input[name='lastName'], input#last-name",
                 app.last_name, "Last name")
        ctx.fill("input[name='email'], input[type='email']", app.email, "Email")
        ctx.fill("input[name='phoneNumber'], input[type='tel']",
                 app.phone, "Phone", required=False)

        try:
            file_input = page.locator("form input[type='file']").first
            if file_input.count():
                ctx.upload_element(file_input, app.resume_pdf_path, "Resume")
                page.wait_for_timeout(4000)  # CV parsing may autofill fields
        except NeedsReview:
            raise
        except Exception:
            pass

        ctx.fill_labeled_form("form")   # screening questions etc.
        ctx.check_captcha()

    def submit(self, ctx: RunContext) -> str:
        page = ctx.page
        ctx.check_captcha()
        page.locator("form button[type='submit'], button:has-text('Submit'), "
                     "[data-test='submit-application']").first.click(timeout=15000)
        try:
            page.wait_for_selector(
                "text=/thank you|application (sent|received|submitted)/i", timeout=30000)
        except Exception:
            if page.locator("form button[type='submit']").first.is_visible(timeout=1000):
                raise NeedsReview("Submit clicked but form still visible — check screenshots")
        return f"Submitted (landed on {page.url})"
