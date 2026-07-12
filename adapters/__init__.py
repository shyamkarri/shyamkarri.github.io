"""
ATS adapter registry. Adding a new ATS = drop a module here that subclasses
ATSAdapter, then register it below (see adapters/base.py for the interface
and Phase-6 README for the template).
"""

from adapters.base import ATSAdapter, RunContext, ApplicationData, CaptchaDetected, NeedsReview


def get_adapter(ats_type: str) -> ATSAdapter:
    from adapters.greenhouse import GreenhouseAdapter
    from adapters.lever import LeverAdapter
    from adapters.ashby import AshbyAdapter
    from adapters.smartrecruiters import SmartRecruitersAdapter
    from adapters.workday import WorkdayAdapter

    registry = {
        "greenhouse": GreenhouseAdapter,
        "lever": LeverAdapter,
        "ashby": AshbyAdapter,
        "smartrecruiters": SmartRecruitersAdapter,
        "workday": WorkdayAdapter,
    }
    cls = registry.get((ats_type or "").lower())
    if not cls:
        raise ValueError(f"No adapter for ATS '{ats_type}'")
    return cls()


def detect_ats(url: str) -> str:
    """Best-effort ATS detection from an apply URL."""
    u = (url or "").lower()
    if "greenhouse.io" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "ashbyhq.com" in u:
        return "ashby"
    if "smartrecruiters.com" in u:
        return "smartrecruiters"
    if "myworkdayjobs.com" in u or "myworkdaysite.com" in u:
        return "workday"
    return ""
