"""
Job scraper — pulls Data-Engineering roles from public job-board APIs.

All sources expose official public JSON endpoints (no login, no browser
automation):

  greenhouse       boards-api.greenhouse.io/v1/boards/{token}/jobs
  lever            api.lever.co/v0/postings/{company}?mode=json
  ashby            api.ashbyhq.com/posting-api/job-board/{org}
  smartrecruiters  api.smartrecruiters.com/v1/companies/{company}/postings
  workday          {tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

MEMORY DESIGN (important — this runs on a 512MB Render instance):
  * Greenhouse list calls do NOT include descriptions; full content is
    fetched per-job, only for keyword-matched jobs, capped per company.
  * One company processed at a time; the DB session is flushed and
    expunged after each so memory never accumulates.
  * A module lock prevents two scrapes from running at once (double
    "Scrape Now" clicks used to stack runs and OOM the instance).

Edit COMPANIES below (or override via the scheduler job's `config` JSON) to
add/remove companies. Wrong or retired tokens 404 and are skipped silently —
adding guesses is free.
"""

import re
import time
import logging
import threading
from datetime import datetime
from typing import Optional

import httpx

from database import SessionLocal, JobPosting, Notification

logger = logging.getLogger("agent_logger")

# ─── What to search for ──────────────────────────────────────────────────────
KEYWORDS = [
    "data engineer", "data platform", "analytics engineer",
    "big data", "spark", "databricks", "snowflake", "etl",
]

RESTRICTED_PATTERNS = [
    "no sponsorship", "without sponsorship", "unable to sponsor",
    "not able to sponsor", "cannot sponsor", "us citizenship required",
    "citizens only", "must be a us citizen", "security clearance",
]
FRIENDLY_PATTERNS = [
    "visa sponsorship", "sponsorship available", "will sponsor",
    "h1b", "h-1b", "opt", "cpt", "work authorization assistance",
]

# ─── Company registry ────────────────────────────────────────────────────────
# Board tokens for companies known/likely to use each ATS. A wrong token just
# 404s and is skipped, so it is safe to add guesses. To find a token: visit
# boards.greenhouse.io/<guess> or jobs.lever.co/<guess> in a browser.
COMPANIES = {
    "greenhouse": [
        # Big tech / data & infra
        "databricks", "stripe", "airbnb", "gitlab", "cloudflare", "datadog",
        "coinbase", "instacart", "dropbox", "reddit", "pinterest", "lyft",
        "twitch", "doordashusa", "robinhood", "mongodb", "elastic",
        "hashicorp", "twilio", "okta", "confluent", "snowflake", "asana",
        "figma", "airtable", "amplitude", "anthropic", "vercel", "netlify",
        "sourcegraph", "grammarly", "duolingo", "discord", "roblox",
        # Fintech / commerce
        "affirm", "chime", "brex", "carta", "sofi", "gusto", "checkr",
        "marqeta", "mercury", "wise", "adyen", "klarna",
        # SaaS / product
        "hubspot", "klaviyo", "braze", "lattice", "launchdarkly", "mixpanel",
        "segment", "sentry", "postman", "zapier", "calendly", "loom",
        "miro", "monzo", "intercom", "amplitude", "clari", "gong",
        # Marketplace / consumer
        "faire", "flexport", "thumbtack", "nextdoor", "patreon", "peloton",
        "etsy", "wish", "upwork", "udemy", "coursera", "quora", "buzzfeed",
        "warbyparker", "squarespace", "vimeo", "toast", "samsara",
        # Health / bio / other
        "benchling", "tempus", "flatironhealth", "oscar", "ro", "cityblock",
        "devoted", "komodohealth", "zocdoc", "hingehealth",
        # Data / AI
        "scaleai", "huggingface", "weightsandbiases", "pinecone", "dbtlabs",
        "starburstdata", "fivetran", "airbyte", "astronomer", "montecarlodata",
        "atlan", "alation", "sigmacomputing", "hex", "census", "hightouch",
        "prefect", "dagsterlabs", "clickhouse", "singlestore", "cockroachlabs",
        "planetscale", "timescale", "redpandadata", "materialize", "voltrondata",
    ],
    "lever": [
        "palantir", "plaid", "ramp", "attentive", "outreach", "highspot",
        "veeva", "zoox", "aurora", "nuro", "kodiak", "saronic",
        "voleon", "matchgroup", "spotify", "netflix", "atlassian",
        "shield-ai", "eightsleep", "mistral", "valence",
    ],
    "ashby": [
        "openai", "ramp", "linear", "notion", "cursor", "replit", "supabase",
        "posthog", "vanta", "deel", "modal", "sierra", "perplexity-ai",
        "elevenlabs", "cohere", "runway", "wander", "clever", "docker",
        "monad", "ashby", "warp", "zed", "browserbase", "temporal-technologies",
    ],
    "smartrecruiters": [
        "ServiceNow", "Visa", "Square", "Bosch", "McDonaldsCorporation",
        "Experian", "Devsu", "Publicissapient", "Equinox",
    ],
    "workday": [
        # each entry: tenant, host (wd1/wd5/wd12...), site name — find via the
        # company careers page URL: <tenant>.<host>.myworkdayjobs.com/<site>
        {"company": "NVIDIA", "tenant": "nvidia", "host": "wd5",
         "site": "NVIDIAExternalCareerSite"},
        {"company": "Adobe", "tenant": "adobe", "host": "wd5",
         "site": "external_experienced"},
        {"company": "Salesforce", "tenant": "salesforce", "host": "wd12",
         "site": "External_Career_Site"},
        {"company": "Capital One", "tenant": "capitalone", "host": "wd12",
         "site": "Capital_One"},
        {"company": "Humana", "tenant": "humana", "host": "wd5",
         "site": "Humana_External_Career_Site"},
        {"company": "CVS Health", "tenant": "cvshealth", "host": "wd1",
         "site": "CVS_Health_Careers"},
    ],
}

TIMEOUT = httpx.Timeout(15.0)
HEADERS = {"User-Agent": "Mozilla/5.0 (job-tracker; personal use)"}

MAX_CONTENT_FETCHES = 20   # per-company cap on Greenhouse detail calls
COMPANY_PAUSE = 0.3        # seconds between companies (smooth CPU, be polite)

_scrape_lock = threading.Lock()


def _matches_keywords(title: str) -> list:
    t = title.lower()
    return [k for k in KEYWORDS if k in t]


def _sponsorship_flag(description: str) -> str:
    d = (description or "").lower()
    if any(p in d for p in RESTRICTED_PATTERNS):
        return "restricted"
    if any(p in d for p in FRIENDLY_PATTERNS):
        return "friendly"
    return "unknown"


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "").replace("&nbsp;", " ").strip()


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):  # epoch ms (lever)
            return datetime.utcfromtimestamp(value / 1000)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


# ─── Per-source fetchers: each returns a list of normalized job dicts ────────

def fetch_greenhouse(client: httpx.Client, token: str) -> list:
    # Lightweight list call — titles/locations only, NO descriptions.
    # (content=true on big boards returns tens of MB and OOMs the instance.)
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    resp = client.get(url)
    resp.raise_for_status()
    matched = [j for j in resp.json().get("jobs", [])
               if _matches_keywords(j.get("title", ""))]

    jobs = []
    for j in matched[:MAX_CONTENT_FETCHES]:
        desc = ""
        try:  # fetch description per matched job only
            detail = client.get(f"{url}/{j['id']}")
            if detail.status_code == 200:
                desc = _strip_html(detail.json().get("content", ""))[:2000]
        except Exception:
            pass
        loc = (j.get("location") or {}).get("name") or ""
        jobs.append({
            "source": "greenhouse", "external_id": str(j["id"]),
            "company": token, "title": j.get("title", ""),
            "location": loc, "url": j.get("absolute_url", ""),
            "description_snippet": desc,
            "posted_at": _parse_dt(j.get("updated_at")),
            "remote": "remote" in loc.lower(),
            "sponsorship_flag": _sponsorship_flag(desc),
            "matched_keywords": _matches_keywords(j.get("title", "")),
        })
    return jobs


def fetch_lever(client: httpx.Client, company: str) -> list:
    url = f"https://api.lever.co/v0/postings/{company}?mode=json&limit=250"
    resp = client.get(url)
    resp.raise_for_status()
    jobs = []
    for j in resp.json():
        kws = _matches_keywords(j.get("text", ""))
        if not kws:
            continue
        desc = (j.get("descriptionPlain") or "")[:2000]
        loc = (j.get("categories") or {}).get("location") or ""
        jobs.append({
            "source": "lever", "external_id": str(j["id"]),
            "company": company, "title": j.get("text", ""),
            "location": loc, "url": j.get("hostedUrl", ""),
            "description_snippet": desc,
            "posted_at": _parse_dt(j.get("createdAt")),
            "remote": "remote" in loc.lower(),
            "sponsorship_flag": _sponsorship_flag(desc),
            "matched_keywords": kws,
        })
    return jobs


def fetch_ashby(client: httpx.Client, org: str) -> list:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{org}"
    resp = client.get(url)
    resp.raise_for_status()
    jobs = []
    for j in resp.json().get("jobs", []):
        kws = _matches_keywords(j.get("title", ""))
        if not kws:
            continue
        jobs.append({
            "source": "ashby", "external_id": str(j["id"]),
            "company": org, "title": j.get("title", ""),
            "location": j.get("location"),
            "url": j.get("jobUrl") or j.get("applyUrl") or "",
            "description_snippet": None,  # list endpoint has no description
            "posted_at": _parse_dt(j.get("publishedAt")),
            "remote": bool(j.get("isRemote")),
            "sponsorship_flag": "unknown",
            "matched_keywords": kws,
        })
    return jobs


def fetch_smartrecruiters(client: httpx.Client, company: str) -> list:
    url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100"
    resp = client.get(url)
    resp.raise_for_status()
    jobs = []
    for j in resp.json().get("content", []):
        kws = _matches_keywords(j.get("name", ""))
        if not kws:
            continue
        loc = (j.get("location") or {})
        loc_str = ", ".join(filter(None, [loc.get("city"), loc.get("country")]))
        jobs.append({
            "source": "smartrecruiters", "external_id": str(j["id"]),
            "company": company, "title": j.get("name", ""),
            "location": loc_str,
            "url": f"https://jobs.smartrecruiters.com/{company}/{j['id']}",
            "description_snippet": None,
            "posted_at": _parse_dt(j.get("releasedDate")),
            "remote": bool(loc.get("remote")),
            "sponsorship_flag": "unknown",
            "matched_keywords": kws,
        })
    return jobs


def fetch_workday(client: httpx.Client, cfg: dict) -> list:
    tenant, host, site = cfg["tenant"], cfg["host"], cfg["site"]
    base = f"https://{tenant}.{host}.myworkdayjobs.com"
    url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    jobs = []
    for keyword in ["data engineer", "data platform"]:
        resp = client.post(url, json={
            "appliedFacets": {}, "limit": 20, "offset": 0, "searchText": keyword,
        }, headers={"Accept": "application/json"})
        resp.raise_for_status()
        if "json" not in resp.headers.get("content-type", ""):
            # Workday redirects to an HTML maintenance page during their
            # scheduled maintenance windows — skip cleanly, retry next run
            raise RuntimeError("Workday returned non-JSON (maintenance window or blocked)")
        for j in resp.json().get("jobPostings", []):
            title = j.get("title", "")
            kws = _matches_keywords(title)
            if not kws:
                continue
            ext_id = (j.get("bulletFields") or [j.get("externalPath", "")])[0]
            loc = j.get("locationsText") or ""
            jobs.append({
                "source": "workday", "external_id": str(ext_id),
                "company": cfg.get("company", tenant), "title": title,
                "location": loc,
                "url": base + "/en-US/" + site + (j.get("externalPath") or ""),
                "description_snippet": None,
                "posted_at": None,
                "remote": "remote" in loc.lower(),
                "sponsorship_flag": "unknown",
                "matched_keywords": kws,
            })
    return jobs


# ─── Orchestrator ────────────────────────────────────────────────────────────

def run_scrape(companies: dict = None) -> str:
    """Scrape all configured sources, insert new postings, notify on matches.

    Memory-safe: one company at a time, session cleared after each commit,
    and a lock so concurrent scrapes can't stack up.
    Returns a human-readable summary (stored as the scheduler run output).
    """
    if not _scrape_lock.acquire(blocking=False):
        return "skipped — a scrape is already running"

    companies = companies or COMPANIES
    found, new_count, deduped, errors = 0, 0, 0, []

    db = SessionLocal()
    try:
        with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
            targets = (
                [("greenhouse", t, fetch_greenhouse) for t in companies.get("greenhouse", [])]
                + [("lever", t, fetch_lever) for t in companies.get("lever", [])]
                + [("ashby", t, fetch_ashby) for t in companies.get("ashby", [])]
                + [("smartrecruiters", t, fetch_smartrecruiters) for t in companies.get("smartrecruiters", [])]
                + [("workday", t, fetch_workday) for t in companies.get("workday", [])]
            )
            for source, target, fetcher in targets:
                label = target if isinstance(target, str) else target.get("company", "?")
                try:
                    jobs = fetcher(client, target)
                    found += len(jobs)

                    # one query per company instead of one per job
                    existing_ids = {
                        row[0] for row in db.query(JobPosting.external_id)
                        .filter_by(source=source)
                        .filter(JobPosting.external_id.in_(
                            [j["external_id"] for j in jobs] or [""]
                        )).all()
                    }
                    for j in jobs:
                        if j["external_id"] in existing_ids:
                            deduped += 1
                            continue
                        db.add(JobPosting(**j))
                        new_count += 1
                    db.commit()
                    db.expunge_all()  # release ORM objects — keep memory flat
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:
                        pass  # unknown board token — free to ignore
                    else:
                        errors.append(f"{source}/{label}: HTTP {e.response.status_code}")
                except Exception as e:
                    errors.append(f"{source}/{label}: {type(e).__name__}")
                    logger.warning(f"[Scraper] {source}/{label} failed: {e}")
                time.sleep(COMPANY_PAUSE)

        if new_count:
            db.add(Notification(
                type="job_match",
                title=f"{new_count} new job matches found",
                message=f"The scraper found {new_count} new matching postings "
                        f"across {found} total matches. Check the Jobs feed.",
            ))
            db.commit()

        summary = f"{new_count} new + {deduped} deduped = {found} found; {len(errors)} errors"
        if errors:
            summary += " — " + "; ".join(errors[:5])
        logger.info(f"[Scraper] {summary}")
        return summary
    finally:
        db.close()
        _scrape_lock.release()
