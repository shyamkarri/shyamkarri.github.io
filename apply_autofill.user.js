// ==UserScript==
// @name         Karri's One-Click Job Apply Autofill
// @namespace    karri-job-tools
// @version      1.0
// @description  Autofills job application forms on Greenhouse, Lever, Ashby & Workday — you just review and hit Submit.
// @match        https://boards.greenhouse.io/*
// @match        https://job-boards.greenhouse.io/*
// @match        https://jobs.lever.co/*
// @match        https://jobs.ashbyhq.com/*
// @match        https://*.myworkdayjobs.com/*
// @match        https://jobs.smartrecruiters.com/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

/*
 * SETUP (once):
 *   1. Install the Tampermonkey browser extension (chrome.google.com/webstore)
 *   2. Tampermonkey icon → Create new script → paste this whole file → Save
 *   3. Edit the PROFILE object below with your real details
 *   4. Open any job application page → click the floating "⚡ Autofill" button
 *   5. Review what was filled, answer anything custom, hit Submit yourself
 *
 * The resume auto-attach works if resume_url is publicly reachable with CORS
 * (GitHub Pages URLs work, e.g. https://shyamkarri.github.io/resume.pdf).
 */

(function () {
  "use strict";

  // ─── EDIT ME ────────────────────────────────────────────────────────────
  const PROFILE = {
    first_name: "Karri",
    last_name: "Prasad",
    full_name: "Karri Prasad",
    email: "ksprasadmvsa.1999@gmail.com",
    phone: "+1 817-601-5406",
    location: "Dallas, Texas, United States",
    city: "Dallas",
    linkedin: "https://www.linkedin.com/in/karri-prasad-293374293/",
    github: "https://github.com/shyamkarri",
    website: "https://shyamkarri.github.io",
    current_company: "HCA Healthcare",
    current_title: "Senior Data Platform Engineer",
    school: "", // fill in your university
    resume_url: "https://shyamkarri.github.io/resume.pdf", // must be a real, public PDF URL
    resume_filename: "Karri_Prasad_Resume.pdf",
  };

  // Answers matched against question text (lowercase). Add your own.
  // ⚠️ Answer work-authorization questions truthfully for your situation.
  const ANSWERS = [
    { match: /authorized to work|legally authorized|work authorization/i, value: "Yes" },
    { match: /require.*sponsorship|need.*sponsorship|sponsorship.*(now|future)/i, value: "Yes" },
    { match: /how did you hear/i, value: "Company careers page" },
    { match: /willing to relocate/i, value: "Yes" },
    { match: /hybrid|onsite|on-site.*comfortable/i, value: "Yes" },
    { match: /notice period|when.*can you start|available to start/i, value: "2 weeks" },
    { match: /desired salary|salary expectation|compensation expectation/i, value: "Open to discussion" },
    { match: /pronouns/i, value: "He/Him" },
  ];
  // ─── END EDIT ───────────────────────────────────────────────────────────

  // Field-name patterns → profile values (checked against label + name +
  // placeholder + aria-label + id, in order; first hit wins)
  const FIELD_MAP = [
    { match: /first[\s_-]?name/i, value: () => PROFILE.first_name },
    { match: /last[\s_-]?name|surname|family name/i, value: () => PROFILE.last_name },
    { match: /full[\s_-]?name|your name|^name$|legal name/i, value: () => PROFILE.full_name },
    { match: /e-?mail/i, value: () => PROFILE.email },
    { match: /phone|mobile/i, value: () => PROFILE.phone },
    { match: /linkedin/i, value: () => PROFILE.linkedin },
    { match: /github|git hub/i, value: () => PROFILE.github },
    { match: /portfolio|personal (web)?site|website|url/i, value: () => PROFILE.website },
    { match: /current (company|employer)|employer/i, value: () => PROFILE.current_company },
    { match: /current (title|role)|job title/i, value: () => PROFILE.current_title },
    { match: /school|university|college/i, value: () => PROFILE.school },
    { match: /city/i, value: () => PROFILE.city },
    { match: /location|address/i, value: () => PROFILE.location },
  ];

  // React-compatible value setter: uses the native setter so React's
  // onChange actually fires (plain .value= is ignored by React forms)
  function setValue(el, value) {
    if (!value) return false;
    const proto = el instanceof HTMLTextAreaElement
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
    return true;
  }

  function labelTextFor(el) {
    const bits = [];
    if (el.labels) for (const l of el.labels) bits.push(l.textContent);
    if (el.getAttribute("aria-label")) bits.push(el.getAttribute("aria-label"));
    if (el.getAttribute("aria-labelledby")) {
      const ref = document.getElementById(el.getAttribute("aria-labelledby"));
      if (ref) bits.push(ref.textContent);
    }
    // nearest label-ish ancestor text (Ashby/Workday render labels as divs)
    const wrapper = el.closest("[class*=field], [class*=Field], [class*=question], li, fieldset, div");
    if (wrapper) {
      const lbl = wrapper.querySelector("label, [class*=label], [class*=Label], legend");
      if (lbl) bits.push(lbl.textContent);
    }
    bits.push(el.name || "", el.placeholder || "", el.id || "");
    return bits.join(" | ").slice(0, 300);
  }

  function isFillable(el) {
    if (el.type === "hidden" || el.disabled || el.readOnly) return false;
    if (el.offsetParent === null) return false; // invisible
    if (el.value && el.value.trim().length > 0) return false; // already filled
    return true;
  }

  function fillTextFields() {
    let filled = 0;
    const inputs = document.querySelectorAll(
      'input[type="text"], input[type="email"], input[type="tel"], input[type="url"], input:not([type]), textarea'
    );
    for (const el of inputs) {
      if (!isFillable(el)) continue;
      const label = labelTextFor(el);

      let done = false;
      for (const f of FIELD_MAP) {
        if (f.match.test(label) && f.value()) {
          done = setValue(el, f.value());
          break;
        }
      }
      if (!done) {
        for (const a of ANSWERS) {
          if (a.match.test(label)) { done = setValue(el, a.value); break; }
        }
      }
      if (done) { el.style.outline = "2px solid #34d399"; filled++; }
    }
    return filled;
  }

  function fillSelects() {
    let filled = 0;
    for (const sel of document.querySelectorAll("select")) {
      if (sel.value && sel.value !== "" && sel.selectedIndex > 0) continue;
      const label = labelTextFor(sel);
      const want = ANSWERS.find(a => a.match.test(label));
      if (!want) continue;
      const opt = Array.from(sel.options).find(o =>
        o.textContent.trim().toLowerCase().startsWith(String(want.value).toLowerCase().slice(0, 3)));
      if (opt) {
        sel.value = opt.value;
        sel.dispatchEvent(new Event("change", { bubbles: true }));
        sel.style.outline = "2px solid #34d399";
        filled++;
      }
    }
    return filled;
  }

  async function attachResume() {
    const fileInputs = Array.from(document.querySelectorAll('input[type="file"]'))
      .filter(el => !el.disabled && /resume|cv|attach/i.test(labelTextFor(el)) || true);
    if (!fileInputs.length || !PROFILE.resume_url) return 0;
    try {
      const resp = await fetch(PROFILE.resume_url);
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const blob = await resp.blob();
      const file = new File([blob], PROFILE.resume_filename, { type: "application/pdf" });
      const dt = new DataTransfer();
      dt.items.add(file);
      const target = fileInputs[0];
      target.files = dt.files;
      target.dispatchEvent(new Event("change", { bubbles: true }));
      return 1;
    } catch (e) {
      console.warn("[Autofill] resume attach failed:", e.message,
        "— check that resume_url is public and CORS-accessible");
      return 0;
    }
  }

  async function runAutofill() {
    const t = fillTextFields();
    const s = fillSelects();
    const r = await attachResume();
    flash(`⚡ Filled ${t} fields, ${s} dropdowns${r ? ", resume attached" : ""} — review & submit!`);
  }

  // ─── Floating button + toast ─────────────────────────────────────────────
  function flash(msg) {
    const n = document.createElement("div");
    n.textContent = msg;
    Object.assign(n.style, {
      position: "fixed", bottom: "80px", right: "20px", zIndex: 999999,
      background: "#0f172a", color: "#34d399", padding: "12px 18px",
      borderRadius: "10px", fontSize: "14px", fontFamily: "system-ui",
      boxShadow: "0 8px 30px rgba(0,0,0,.4)", border: "1px solid #34d399",
    });
    document.body.appendChild(n);
    setTimeout(() => n.remove(), 5000);
  }

  function addButton() {
    if (document.getElementById("karri-autofill-btn")) return;
    const btn = document.createElement("button");
    btn.id = "karri-autofill-btn";
    btn.textContent = "⚡ Autofill";
    Object.assign(btn.style, {
      position: "fixed", bottom: "20px", right: "20px", zIndex: 999999,
      background: "linear-gradient(135deg,#6366f1,#06b6d4)", color: "#fff",
      border: "none", borderRadius: "999px", padding: "12px 22px",
      fontSize: "15px", fontWeight: "700", cursor: "pointer",
      fontFamily: "system-ui", boxShadow: "0 8px 30px rgba(0,0,0,.35)",
    });
    btn.onclick = runAutofill;
    document.body.appendChild(btn);
  }

  // SPA-friendly: keep the button alive as pages re-render (Workday, Ashby)
  addButton();
  new MutationObserver(() => addButton())
    .observe(document.body, { childList: true, subtree: true });
})();
