"""
Job scraper — pulls Data-Engineering roles from public job-board APIs.

All sources here expose official public JSON endpoints (no login, no browser
automation, no ToS games):

  greenhouse       boards-api.greenhouse.io/v1/boards/{token}/jobs
  lever            api.lever.co/v0/postings/{company}?mode=json
  ashby            api.ashbyhq.com/posting-api/job-board/{org}
  smartrecruiters  api.smartrecruiters.com/v1/companies/{company}/postings
  workday          {tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

Edit COMPANIES below (or override via the scheduler job's `config` JSON) to
add/remove companies. Wrong tokens fail gracefully and are skipped.
"""

import re
import logging
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

# Work-auth signals scanned in job descriptions
RESTRICTED_PATTERNS = [
    "no sponsorship", "without sponsorship", "unable to sponsor",
    "not able to sponsor", "cannot sponsor", "us citizenship required",
    "citizens only", "must be a us citizen", "security clearance",
]
FRIENDLY_PATTERNS = [
    "visa sponsorship", "sponsorship available", "will sponsor",
    "h1b", "h-1b", "opt", "cpt", "work authorization assistance",
]

# ─── Company registry (edit me) ──────────────────────────────────────────────
COMPANIES = {
    "greenhouse": [
        # board token = the slug in boards.greenhouse.io/<token>
        "databricks", "stripe", "airbnb", "gitlab", "cloudflare",
        "datadog", "coinbase", "instacart", "dropbox", "reddit",
    ],
    "lever": [
        # slug in jobs.lever.co/<slug>
        "palantir", "plaid",
    ],
    "ashby": [
        # org in jobs.ashbyhq.com/<org>
        "openai", "ramp", "linear", "notion",
    ],
    "smartrecruiters": [
        # company id in careers.smartrecruiters.com/<id>
        "ServiceNow",
    ],
    "workday": [
        # each entry: tenant, host (wd1/wd5/wd12...), site name
        {"company": "NVIDIA", "tenant": "nvidia", "host": "wd5",
         "site": "NVIDIAExternalCareerSite"},
    ],
}

TIMEOUT = httpx.Timeout(20.0)
HEADERS = {"User-Agent": "Mozilla/5.0 (job-tracker; personal use)"}


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
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    resp = client.get(url)
    resp.raise_for_status()
    jobs = []
    for j in resp.json().get("jobs", []):
        kws = _matches_keywords(j.get("title", ""))
        if not kws:
            continue
        desc = _strip_html(j.get("content", ""))[:2000]
        jobs.append({
            "source": "greenhouse", "external_id": str(j["id"]),
            "company": token, "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name"),
            "url": j.get("absolute_url", ""),
            "description_snippet": desc,
            "posted_at": _parse_dt(j.get("updated_at")),
            "remote": "remote" in ((j.get("location") or {}).get("name") or "").lower(),
            "sponsorship_flag": _sponsorship_flag(desc),
            "matched_keywords": kws,
        })
    return jobs


def fetch_lever(client: httpx.Client, company: str) -> list:
    url = f"https://api.lever.co/v0/postings/{company}?mode=json"
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

    Returns a human-readable summary string (stored as the scheduler run output).
    """
    companies = companies or COMPANIES
    found, new_count, errors = 0, 0, []

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
                    for j in jobs:
                        exists = db.query(JobPosting).filter_by(
                            source=j["source"], external_id=j["external_id"]
                        ).first()
                        if exists:
                            continue
                        db.add(JobPosting(**j))
                        new_count += 1
                    db.commit()
                except Exception as e:
                    errors.append(f"{source}/{label}: {e}")
                    logger.warning(f"[Scraper] {source}/{label} failed: {e}")

        if new_count:
            db.add(Notification(
                type="job_match",
                title=f"{new_count} new job matches found",
                message=f"The scraper found {new_count} new matching postings "
                        f"across {found} total matches. Check the Jobs feed.",
            ))
            db.commit()

        summary = f"{new_count} new / {found} matched; {len(errors)} source errors"
        if errors:
            summary += " — " + "; ".join(errors[:5])
        logger.info(f"[Scraper] {summary}")
        return summary
    finally:
        db.close()
