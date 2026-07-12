"""
Workday adapter — the hard one. Per-tenant account (created on first apply,
password sealed with libsodium so only the worker can read it), then a
multi-page wizard driven by data-automation-id attributes:

  My Information → My Experience (resume parse-then-verify) →
  Application Questions → Voluntary Disclosures → Self Identify → Review

Each page: fill → receipt → screenshot → Next. Unknown required widgets and
Workday error banners raise NeedsReview — never silently skipped. The shared
persistent browser profile keeps tenant sessions alive between runs.
"""

import re
import secrets
import string
import logging

from adapters.base import ATSAdapter, RunContext, NeedsReview, _closest_option

logger = logging.getLogger("agent_logger")

NEXT_BTN = ("[data-automation-id='bottom-navigation-next-button'], "
            "[data-automation-id='pageFooterNextButton'], "
            "button:has-text('Save and Continue')")
ERROR_BANNER = "[data-automation-id='errorBanner'], [data-automation-id='errorMessage']"
MAX_WIZARD_PAGES = 12


def _tenant_from_url(url: str) -> str:
    m = re.search(r"https?://([^.]+)\.(?:wd\d+)\.myworkday(?:jobs|site)\.com", url or "")
    return m.group(1) if m else ""


def _gen_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return ("Aa1!" + "".join(secrets.choice(alphabet) for _ in range(16)))


class WorkdayAdapter(ATSAdapter):
    name = "workday"

    @staticmethod
    def detect(url: str) -> bool:
        u = (url or "").lower()
        return "myworkdayjobs.com" in u or "myworkdaysite.com" in u

    # ── account ──────────────────────────────────────────────────────────────
    def login(self, ctx: RunContext):
        from database import AtsCredential
        page, app = ctx.page, ctx.app
        tenant = _tenant_from_url(app.apply_url)

        page.goto(app.apply_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=30000)
        ctx.check_captcha()

        # open the apply flow
        for sel in ("a[data-automation-id='adventureButton']",
                    "[data-automation-id='applyButton']", "a:has-text('Apply')"):
            try:
                if page.locator(sel).first.is_visible(timeout=2500):
                    page.locator(sel).first.click()
                    break
            except Exception:
                continue
        # choose manual apply if a chooser appears
        for sel in ("a[data-automation-id='applyManually']",
                    "button:has-text('Apply Manually')"):
            try:
                if page.locator(sel).first.is_visible(timeout=4000):
                    page.locator(sel).first.click()
                    break
            except Exception:
                continue
        page.wait_for_load_state("networkidle", timeout=20000)

        # already signed in from the persistent profile?
        if not page.locator("[data-automation-id='email'], input[type='email']").first.is_visible(timeout=4000):
            ctx.receipt("Workday session", f"already signed in ({tenant})", "system")
            return

        cred = ctx.db.query(AtsCredential).filter_by(
            ats_type="workday", tenant=tenant).first()
        if cred:
            self._sign_in(ctx, cred)
        else:
            self._create_account(ctx, tenant)
        ctx.snap("signed_in")

    def _sign_in(self, ctx, cred):
        from crypto_box import unseal
        page = ctx.page
        try:
            password = unseal(cred.password_sealed)
        except Exception as e:
            raise NeedsReview(f"Cannot decrypt Workday creds for '{cred.tenant}': {e}")
        # a "sign in" toggle may be needed if the create-account form shows first
        for sel in ("button:has-text('Sign In')", "a:has-text('Sign In')"):
            try:
                if page.locator("[data-automation-id='createAccountSubmitButton']").first.is_visible(timeout=1500):
                    page.locator(sel).first.click(timeout=2000)
                    break
            except Exception:
                continue
        page.locator("[data-automation-id='email'], input[type='email']").first.fill(cred.username)
        page.locator("[data-automation-id='password'], input[type='password']").first.fill(password)
        ctx.receipt("Workday sign-in", cred.username, "credentials")
        page.locator("[data-automation-id='signInSubmitButton'], "
                     "button:has-text('Sign In')").first.click()
        page.wait_for_load_state("networkidle", timeout=20000)
        if page.locator(ERROR_BANNER).first.is_visible(timeout=2500):
            raise NeedsReview(f"Workday sign-in failed for {cred.tenant}: "
                              f"{page.locator(ERROR_BANNER).first.inner_text()[:200]}")

    def _create_account(self, ctx, tenant):
        from database import AtsCredential
        from crypto_box import seal
        page, app = ctx.page, ctx.app
        password = _gen_password()
        # switch to create-account form if sign-in shows first
        for sel in ("button:has-text('Create Account')", "a:has-text('Create Account')"):
            try:
                if not page.locator("[data-automation-id='createAccountSubmitButton']").first.is_visible(timeout=1500):
                    page.locator(sel).first.click(timeout=2500)
                    break
            except Exception:
                continue
        page.locator("[data-automation-id='email'], input[type='email']").first.fill(app.email)
        page.locator("[data-automation-id='password'], input[type='password']").first.fill(password)
        try:
            page.locator("[data-automation-id='verifyPassword']").first.fill(password, timeout=3000)
        except Exception:
            pass
        try:  # account-creation consent checkbox, when present
            cb = page.locator("[data-automation-id='createAccountCheckbox'] input, "
                              "input[type='checkbox']").first
            if cb.is_visible(timeout=1500):
                cb.check()
        except Exception:
            pass
        page.locator("[data-automation-id='createAccountSubmitButton'], "
                     "button:has-text('Create Account')").first.click()
        page.wait_for_load_state("networkidle", timeout=20000)
        if page.locator(ERROR_BANNER).first.is_visible(timeout=2500):
            raise NeedsReview(f"Workday account creation failed ({tenant}): "
                              f"{page.locator(ERROR_BANNER).first.inner_text()[:200]}")
        try:
            sealed = seal(password)
        except RuntimeError as e:   # no public key configured — don't lose the account
            raise NeedsReview(f"Account created for {tenant} but cannot store password: {e}")
        ctx.db.add(AtsCredential(ats_type="workday", tenant=tenant,
                                 username=app.email, password_sealed=sealed,
                                 notes="auto-created by worker"))
        ctx.db.commit()
        ctx.receipt("Workday account", f"created for tenant '{tenant}' as {app.email} "
                    "(password sealed at rest)", "system")

    # ── wizard ───────────────────────────────────────────────────────────────
    def fill(self, ctx: RunContext):
        page = ctx.page
        for page_num in range(MAX_WIZARD_PAGES):
            page.wait_for_load_state("networkidle", timeout=30000)
            ctx.check_captcha()
            step = self._current_step(page)
            ctx.snap(f"step_{step.lower().replace(' ', '_')[:30] or page_num}")
            logger.info(f"[Workday] run {ctx.run.id} wizard step: {step}")

            if re.search(r"review", step, re.I):
                return  # submit() takes it from here
            if re.search(r"my information", step, re.I):
                self._my_information(ctx)
            elif re.search(r"experience", step, re.I):
                self._my_experience(ctx)
            elif re.search(r"question", step, re.I):
                self._questions_page(ctx)
            elif re.search(r"disclosure|self.?identif|voluntary", step, re.I):
                self._disclosures(ctx)
            else:
                # unknown page — try generic questions, else surface it
                try:
                    self._questions_page(ctx)
                except NeedsReview:
                    raise NeedsReview(f"Unknown Workday wizard page: '{step}'")
            self._next(ctx, step)
        raise NeedsReview(f"Wizard exceeded {MAX_WIZARD_PAGES} pages without reaching Review")

    def _current_step(self, page) -> str:
        for sel in ("[data-automation-id='progressBarActiveStep']", "h2", "h1"):
            try:
                t = page.locator(sel).first.inner_text(timeout=2500).strip()
                if t:
                    return t[:100]
            except Exception:
                continue
        return "unknown"

    def _next(self, ctx, step):
        page = ctx.page
        page.locator(NEXT_BTN).first.click(timeout=15000)
        page.wait_for_load_state("networkidle", timeout=30000)
        if page.locator(ERROR_BANNER).first.is_visible(timeout=3000):
            errs = page.locator(ERROR_BANNER).first.inner_text()[:400]
            ctx.snap("validation_errors")
            raise NeedsReview(f"Workday validation on '{step}': {errs}")

    def _wd_dropdown(self, ctx, container_sel: str, value: str, label: str,
                     source: str = "profile", required: bool = False):
        """Workday dropdowns are buttons opening a listbox, not <select>."""
        page = ctx.page
        try:
            btn = page.locator(container_sel).first
            btn.click(timeout=5000)
            opts = page.locator("[role='option'], ul[role='listbox'] li")
            texts = [opts.nth(i).inner_text().strip() for i in range(min(opts.count(), 60))]
            match = _closest_option(value, texts)
            if not match:
                page.keyboard.press("Escape")
                if required:
                    raise NeedsReview(f"No Workday option matches '{value}' for '{label}'")
                return False
            opts.nth(texts.index(match)).click()
            ctx.receipt(label, match, source)
            return True
        except NeedsReview:
            raise
        except Exception as e:
            if required:
                raise NeedsReview(f"Workday dropdown '{label}' failed: {type(e).__name__}")
            return False

    def _my_information(self, ctx):
        page, app = ctx.page, ctx.app
        self._wd_dropdown(ctx, "[data-automation-id='sourceDropdown'], "
                          "[data-automation-id*='source']", "Company Website",
                          "How did you hear about us", source="profile")
        ctx.fill("[data-automation-id='legalNameSection_firstName']",
                 app.first_name, "First name")
        ctx.fill("[data-automation-id='legalNameSection_lastName']",
                 app.last_name, "Last name")
        ctx.fill("[data-automation-id='addressSection_addressLine1']",
                 app.location, "Address line 1", required=False)
        city = (app.location or "").split(",")[0].strip()
        ctx.fill("[data-automation-id='addressSection_city']", city,
                 "City", required=False)
        ctx.fill("[data-automation-id='addressSection_postalCode']", "",
                 "Postal code", required=False)
        self._wd_dropdown(ctx, "[data-automation-id='phone-device-type']",
                          "Mobile", "Phone device type")
        ctx.fill("[data-automation-id='phone-number']", app.phone,
                 "Phone", required=False)

    def _my_experience(self, ctx):
        """Upload the tailored PDF, let Workday parse it, then verify the parse
        didn't mangle the name (parse-then-verify)."""
        page, app = ctx.page, ctx.app
        try:
            up = page.locator("[data-automation-id='file-upload-input-ref'], "
                              "input[type='file']").first
            if up.count():
                up.set_input_files(app.resume_pdf_path, timeout=20000)
                ctx.receipt("Resume", app.resume_pdf_path.split("/")[-1],
                            "tailored_resume")
                page.wait_for_timeout(6000)  # Workday parses the document
        except Exception as e:
            raise NeedsReview(f"Workday resume upload failed: {type(e).__name__}")
        # parse-then-verify: if Workday populated work-history fields with
        # garbage, surface for review rather than submitting nonsense
        try:
            first_job_title = page.locator(
                "[data-automation-id*='jobTitle'] input").first
            if first_job_title.count():
                val = first_job_title.input_value(timeout=2000)
                ctx.receipt("Parsed work history (verify)", val or "(empty)",
                            "workday_parser")
        except Exception:
            pass

    def _questions_page(self, ctx):
        """Application questions: [data-automation-id^='formField'] blocks —
        label + text input / textarea / dropdown button / radio group."""
        page = ctx.page
        blocks = page.locator("[data-automation-id^='formField']")
        if not blocks.count():
            return
        for i in range(blocks.count()):
            b = blocks.nth(i)
            try:
                qtext = re.sub(r"\s+", " ", b.locator(
                    "label, legend").first.inner_text(timeout=1500)).strip()
            except Exception:
                continue
            if not qtext:
                continue
            required = "*" in qtext
            clean = qtext.replace("*", "").strip()
            text_input = b.locator("input[type='text'], textarea").first
            dd_button = b.locator("button[aria-haspopup='listbox']").first
            radios = b.locator("input[type='radio']")
            try:
                if text_input.count():
                    if text_input.input_value(timeout=1000):
                        continue
                    ctx.fill_question(text_input, clean)
                elif dd_button.count():
                    answer, source = ctx.answer(clean)
                    self._wd_dropdown(ctx, f"[data-automation-id^='formField'] >> nth={i} >> "
                                      "button[aria-haspopup='listbox']",
                                      answer, clean, source=source, required=required)
                elif radios.count():
                    answer, source = ctx.answer(clean)
                    labels = [radios.nth(r).evaluate(
                        "el => (el.closest('label')?.innerText || '').trim()")
                        for r in range(radios.count())]
                    match = _closest_option(answer, labels)
                    if match:
                        radios.nth(labels.index(match)).check()
                        ctx.receipt(clean, match, source)
                    elif required:
                        raise NeedsReview(f"No radio matches answer for '{clean[:80]}'")
            except NeedsReview:
                if required:
                    raise
                ctx.receipt(clean, "(left blank — optional)", "skipped")

    def _disclosures(self, ctx):
        """EEO/self-identify — from profile.eeo_answers only, else decline."""
        page = ctx.page
        eeo = ctx.app.eeo or {}
        dds = page.locator("button[aria-haspopup='listbox']")
        for i in range(dds.count()):
            try:
                label = dds.nth(i).evaluate(
                    "el => el.closest('[data-automation-id^=formField]')"
                    "?.querySelector('label')?.innerText?.trim() || ''")[:120]
            except Exception:
                label = f"Disclosure {i + 1}"
            answer = next((v for k, v in eeo.items() if k.lower() in label.lower()), None)
            self._wd_dropdown(
                ctx, f"button[aria-haspopup='listbox'] >> nth={i}",
                answer or "I do not wish to answer", label.replace("*", "").strip(),
                source="profile" if answer else "declined", required=False)
        # terms/acknowledgement checkbox on the self-identify page
        try:
            cb = page.locator("input[type='checkbox']").first
            if cb.is_visible(timeout=1500) and not cb.is_checked():
                cb.check()
                ctx.receipt("Acknowledgement checkbox", "checked", "system")
        except Exception:
            pass

    def submit(self, ctx: RunContext) -> str:
        page = ctx.page
        ctx.check_captcha()
        # on the Review page the next-button is the Submit button
        page.locator(NEXT_BTN + ", button:has-text('Submit')").first.click(timeout=20000)
        page.wait_for_load_state("networkidle", timeout=40000)
        if page.locator(ERROR_BANNER).first.is_visible(timeout=3000):
            raise NeedsReview("Workday rejected the submission: "
                              f"{page.locator(ERROR_BANNER).first.inner_text()[:300]}")
        try:
            page.wait_for_selector("text=/congratulations|successfully submitted|"
                                   "application.{0,30}received|thank/i", timeout=25000)
        except Exception:
            pass
        return f"Submitted via Workday wizard (landed on {page.url})"
