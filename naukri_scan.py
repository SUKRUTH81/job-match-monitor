#!/usr/bin/env python3
"""Naukri: new openings for the candidate's experience, read from Naukri's public job search.

No Naukri account is used and nothing is signed in to. A real browser opens the same public search
pages a visitor sees (Naukri gives its job list only to a full browser) and this script reads the
list the page receives. It solves no CAPTCHA: if Naukri answers with a block page or a CAPTCHA, the
scan reports that it was refused and stops.

It is meant to run on a GitHub runner (see .github/workflows/naukri-scan.yml), so the requests come
from GitHub's network, not from the candidate's own computer.

The filters, the score, the two tables and the "each opening once" memory are those of
company_portals.py; only the source differs.

Usage (needs a display for the browser, e.g. xvfb-run on Linux):
  xvfb-run -a python naukri_scan.py              scan, mail what is new, remember it
  xvfb-run -a python naukri_scan.py --dry-run    scan and print; send nothing, remember nothing
"""
from __future__ import annotations

import argparse
import logging
import re
import time
from typing import Any, Dict, List, Sequence, Tuple

import company_portals as cp
import job_match_runner as core

LOG = logging.getLogger("naukri_scan")

SITE = "https://www.naukri.com"
STATE_FILE = core.DATA_DIR / "naukri_state.json"
SEARCH_KEYWORDS = ["python developer", "ai engineer", "generative ai", "software engineer", "full stack developer", "backend developer"]
# (name, place in the address, extra filter). "bangalore" is Naukri's own spelling in addresses.
SEARCH_PLACES: List[Tuple[str, str, str]] = [
    ("Hyderabad", "hyderabad", ""),
    ("Bengaluru", "bangalore", ""),
    ("Chennai", "chennai", ""),
    ("Remote", "", "&wfhType=2"),
]
# Result pages read per search, newest first (20 jobs a page). The scan runs every hour.
PAGES_PER_SEARCH = 3
# Days back Naukri is asked for. One day is its shortest window.
JOB_AGE_DAYS = 1
SCAN_BUDGET_SECONDS = 15 * 60.0
REFUSALS_TO_STOP = 3

REPORT: Dict[str, Any] = {
    "name": "Naukri",
    "short": "Naukri",
    "read_from": "Read from Naukri's public job search, without a login: {sites}; jobs posted in the last day.",
    "footer": 'Automated report from the Naukri scan, which runs every hour. "Posted" is what Naukri shows.',
    "preview": "naukri_preview.html",
    "not_covered": {},
    "state_file": STATE_FILE,
}


def search_address(keyword: str, place: str, extra: str, page: int, candidate_yoe: float) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")
    path = f"{slug}-jobs" + (f"-in-{place}" if place else "") + (f"-{page}" if page > 1 else "")
    return f"{SITE}/{path}?experience={int(candidate_yoe)}&jobAge={JOB_AGE_DAYS}&sort=f{extra}"


def _years(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def naukri_record(item: Dict[str, Any]) -> Dict[str, Any] | None:
    """One job of Naukri's search answer as a record for the shared filters, or None for an entry that is not a job."""
    job_id = str(item.get("jobId") or "").strip()
    path = str(item.get("jdURL") or "")
    label = cp._one_line(item.get("footerPlaceholderLabel"))
    # Entries with no posting date are course and training promotions ("Starts in 1-3 months").
    if not job_id or not path.startswith("/") or not item.get("createdDate") or label.lower().startswith("starts in"):
        return None
    labels = {str(p.get("type")): cp._one_line(p.get("label")) for p in item.get("placeholders") or [] if isinstance(p, dict)}
    place = labels.get("location", "")
    mode = "Remote" if re.search(r"\bremote\b", place, re.I) else ("Hybrid" if re.search(r"\bhybrid\b", place, re.I) else "")
    skills = ", ".join(s.strip() for s in str(item.get("tagsAndSkills") or "").split(",") if s.strip())
    description = cp._text(item.get("jobDescription"))
    if skills:
        description = f"{description}\nKey skills: {skills}".strip()
    record = cp._record(
        "naukri", cp._one_line(item.get("companyName")) or "Unknown Company", job_id, item.get("title"),
        # Naukri lists jobs in India; the country is spelled out because the location rule looks for it.
        f"{place or 'India'}, India", SITE + path, description=description, posted=label, workplace=mode,
    )
    record["source"] = "Naukri"
    low, high = _years(item.get("minimumExperience")), _years(item.get("maximumExperience"))
    if item.get("experienceText") and low is not None and high is not None and low <= high:
        # Naukri states the range as a field of its own, so it is taken from there, not guessed from the text.
        record["min_experience_years"], record["max_experience_years"] = low, high
        record["experience_requirement"] = cp._one_line(item.get("experienceText"))
    return record


def naukri_records(answer: Any) -> List[Dict[str, Any]]:
    jobs = answer.get("jobDetails") if isinstance(answer, dict) else None
    return [r for r in (naukri_record(item) for item in jobs or [] if isinstance(item, dict)) if r]


def read_naukri(candidate_yoe: float, keywords: Sequence[str], places: Sequence[Tuple[str, str, str]]):
    """A reader for company_portals.run_scan: opens the search pages in a real browser, no login."""

    def read(site: Any) -> List[Dict[str, Any]]:
        from playwright.sync_api import sync_playwright

        answers: List[Tuple[int, Any]] = []

        def on_response(response: Any) -> None:
            if "/jobapi/v3/search" not in response.url:
                return
            try:
                answers.append((response.status, response.json()))
            except Exception:  # the page moved on, or the answer was not JSON
                answers.append((response.status, None))

        jobs: Dict[str, Dict[str, Any]] = {}
        refused = pages = 0
        deadline = time.monotonic() + SCAN_BUDGET_SECONDS
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page = browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata", viewport={"width": 1366, "height": 900}).new_page()
            page.on("response", on_response)
            try:
                for place_name, place, extra in places:
                    for keyword in keywords:
                        for number in range(1, PAGES_PER_SEARCH + 1):
                            if time.monotonic() > deadline:
                                raise cp.PortalError("time limit for the Naukri scan reached")
                            answers.clear()
                            pages += 1
                            status = None
                            try:
                                opened = page.goto(search_address(keyword, place, extra, number, candidate_yoe),
                                                   wait_until="domcontentloaded", timeout=45000)
                                status = opened.status if opened else None
                            except Exception as exc:
                                LOG.info("Naukri: %s / %s page %d did not open (%s).", keyword, place_name, number, type(exc).__name__)
                            for _ in range(20):
                                if answers:
                                    break
                                page.wait_for_timeout(1000)
                            listed = next((data for code, data in answers if code == 200 and isinstance(data, dict)), None)
                            if listed is None:
                                # A block page, a CAPTCHA demand, or no answer at all. Nothing is worked around.
                                refused += 1
                                codes = [code for code, _ in answers]
                                LOG.warning("Naukri gave no job list for %s / %s page %d (page HTTP %s, search answers %s).",
                                            keyword, place_name, number, status, codes or "none")
                                if refused >= REFUSALS_TO_STOP:
                                    raise cp.PortalError(
                                        f"Naukri refused the browser {refused} times in a row "
                                        f"(page HTTP {status}, search answers {codes or 'none'}); the scan stopped without retrying"
                                    )
                                break
                            refused = 0
                            found = naukri_records(listed)
                            for record in found:
                                jobs.setdefault(record["job_id"], record)
                            total = int(listed.get("noOfJobs") or 0)
                            page.wait_for_timeout(1500)
                            if len(listed.get("jobDetails") or []) < 20 or number * 20 >= total:
                                break
            finally:
                browser.close()
        if site is not None:
            site.requests_made = pages
        return list(jobs.values())

    return read


def main() -> int:
    parser = argparse.ArgumentParser(description="New openings on Naukri's public search, strictly for the candidate's experience")
    parser.add_argument("--candidate-yoe", type=float, default=2.0, help="Candidate years of experience (default 2.0)")
    parser.add_argument("--min-match", type=int, default=60, help="Skill match a posting needs to be listed (default 60)")
    parser.add_argument("--keywords", type=str, default="", help="Comma-separated search words (default: the built-in list)")
    parser.add_argument("--dry-run", action="store_true", help="Send nothing and keep no state; write the mail to reports/ instead")
    args = parser.parse_args()
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()] or SEARCH_KEYWORDS
    args.company_list = ["naukri"]
    args.keyword_list = keywords
    # Naukri carries far more postings than a company site, so table 1 has the same floor as table 2.
    args.stated_min_match = args.min_match
    portals = {"naukri": ("Naukri", read_naukri(args.candidate_yoe, keywords, SEARCH_PLACES))}
    report = dict(REPORT, read_from=REPORT["read_from"].replace("{sites}", ", ".join(name for name, _, _ in SEARCH_PLACES)))
    return cp.run_scan(args, portals=portals, report=report)


if __name__ == "__main__":
    raise SystemExit(main())
