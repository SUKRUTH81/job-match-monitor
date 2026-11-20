#!/usr/bin/env python3
"""Naukri: new openings for the candidate's experience, read from Naukri's public job search.

No Naukri account is used and nothing is signed in to. A real browser opens the same public search
pages a visitor sees (Naukri gives its job list only to a full browser) and this script reads the
list the page receives. It solves no CAPTCHA and hides nothing about the browser: when Naukri
answers with a block page or a CAPTCHA demand, the scan keeps what it has read so far and stops.

Naukri refuses a browser that opens many pages, so the scan is deliberately small: two searches a
run (the three cities together, and remote), a few result pages each, with a pause between pages.
Some runs are refused outright; the next run tries again from a new runner.

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
from typing import Any, Dict, List, Sequence, Tuple
from urllib.parse import parse_qs, quote, urlsplit

import company_portals as cp
import job_match_runner as core

LOG = logging.getLogger("naukri_scan")

SITE = "https://www.naukri.com"
STATE_FILE = core.DATA_DIR / "naukri_state.json"
SEARCH_KEYWORDS = ["python developer", "ai engineer", "generative ai", "software engineer", "full stack developer", "backend developer"]
# (name in the log and the mail, places in the address, extra filter). One search covers every keyword.
# "bangalore" is Naukri's own spelling in addresses.
SEARCHES: List[Tuple[str, Tuple[str, ...], str]] = [
    ("Hyderabad, Bengaluru, Chennai", ("hyderabad", "bangalore", "chennai"), ""),
    ("Remote", (), "&wfhType=2"),
]
# Result pages read per search, newest first (20 jobs a page). The scan runs every hour.
PAGES_PER_SEARCH = 3
# Days back Naukri is asked for. One day is its shortest window.
JOB_AGE_DAYS = 1
# Wait between two search pages. A visitor does not open a page every second, and neither does this.
PAGE_PAUSE_MS = 8000

REPORT: Dict[str, Any] = {
    "name": "Naukri",
    "short": "Naukri",
    "read_from": "Read from Naukri's public job search, without a login: {sites}; jobs posted in the last day.",
    "footer": 'Automated report from the Naukri scan, which runs every hour. "Posted" is what Naukri shows.',
    "preview": "naukri_preview.html",
    "not_covered": {},
    "state_file": STATE_FILE,
}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def search_address(keywords: Sequence[str], places: Sequence[str], extra: str, page: int, candidate_yoe: float) -> str:
    """Address of one result page: every keyword (any of them) in every place, newest first."""
    path = "-".join(_slug(k) for k in keywords) + "-jobs"
    if places:
        path += "-in-" + "-".join(_slug(p) for p in places)
    if page > 1:
        path += f"-{page}"
    query = "k=" + quote(", ".join(keywords))
    if places:
        query += "&l=" + quote(", ".join(places))
    return f"{SITE}/{path}?{query}&experience={int(candidate_yoe)}&jobAge={JOB_AGE_DAYS}&sort=f{extra}"


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


def read_naukri(candidate_yoe: float, keywords: Sequence[str], searches: Sequence[Tuple[str, Sequence[str], str]]):
    """A reader for company_portals.run_scan: opens the search pages in a real browser, no login."""

    def read(site: Any) -> List[Dict[str, Any]]:
        from playwright.sync_api import sync_playwright

        answers: List[Tuple[int, Any, str]] = []

        def on_response(response: Any) -> None:
            if "/jobapi/v3/search" not in response.url:
                return
            try:
                answers.append((response.status, response.json(), response.url))
            except Exception:  # the page moved on, or the answer was not JSON
                answers.append((response.status, None, response.url))

        jobs: Dict[str, Dict[str, Any]] = {}
        pages = 0
        refusal = ""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page = browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata", viewport={"width": 1366, "height": 900}).new_page()
            page.on("response", on_response)
            try:
                for name, places, extra in searches:
                    for number in range(1, PAGES_PER_SEARCH + 1):
                        if pages:
                            page.wait_for_timeout(PAGE_PAUSE_MS)
                        answers.clear()
                        pages += 1
                        status, problem = None, ""
                        try:
                            opened = page.goto(search_address(keywords, places, extra, number, candidate_yoe),
                                               wait_until="domcontentloaded", timeout=45000)
                            status = opened.status if opened else None
                        except Exception as exc:
                            problem = " ".join(str(exc).split())[:90]
                        for _ in range(20):
                            if answers or problem:
                                break
                            page.wait_for_timeout(1000)
                        listed = next(((data, url) for code, data, url in answers if code == 200 and isinstance(data, dict)), None)
                        if listed is None:
                            # A block page, a CAPTCHA demand, or a dropped connection. Nothing is worked around
                            # and nothing is retried: the scan ends here with what it has.
                            codes = [code for code, _, _ in answers]
                            refusal = f"{name} page {number}: page HTTP {status}, search answers {codes or 'none'}" + (f", {problem}" if problem else "")
                            break
                        data, asked = listed
                        if number == 1:
                            sent = parse_qs(urlsplit(asked).query)
                            LOG.info("Naukri search %r: keyword=%s location=%s", name, sent.get("keyword"), sent.get("location"))
                        total = int(data.get("noOfJobs") or 0)
                        on_page = len(data.get("jobDetails") or [])
                        for record in naukri_records(data):
                            jobs.setdefault(record["job_id"], record)
                        LOG.info("Naukri search %r page %d: %d posting(s) in the last day, %d on this page.", name, number, total, on_page)
                        if on_page < 20 or number * 20 >= total:
                            break
                    if refusal:
                        break
            finally:
                browser.close()
        if site is not None:
            site.requests_made = pages
        if refusal:
            if not jobs:
                raise cp.PortalError(f"Naukri refused the browser ({refusal}); nothing was retried or worked around")
            LOG.warning("Naukri refused the browser part-way (%s); this run goes on with the %d posting(s) already read.", refusal, len(jobs))
            print(f"::warning::Naukri refused the browser part-way: {refusal}")
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
    portals = {"naukri": ("Naukri", read_naukri(args.candidate_yoe, keywords, SEARCHES))}
    report = dict(REPORT, read_from=REPORT["read_from"].replace("{sites}", "; ".join(name for name, _, _ in SEARCHES)))
    return cp.run_scan(args, portals=portals, report=report)


if __name__ == "__main__":
    raise SystemExit(main())
