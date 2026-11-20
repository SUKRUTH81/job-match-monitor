#!/usr/bin/env python3
"""Naukri: new openings for the candidate's experience, read from Naukri's public job search.

No Naukri account is used and nothing is signed in to. A real browser opens the same public search
page a visitor sees (Naukri gives its job list only to a full browser) and this script reads the
list the page receives. It solves no CAPTCHA and hides nothing about the browser: when Naukri
answers with a block page or a CAPTCHA demand, the scan stops.

Naukri drops a browser that opens a second page, so each run opens exactly one: one search, its
newest 20 postings. The searches take turns, hour by hour (see SEARCHES), so each is read a few
times a day. Some runs are refused outright; that hour is skipped and the next run carries on.
Coverage is therefore best effort, not complete.

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
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, urlsplit

import company_portals as cp
import job_match_runner as core

LOG = logging.getLogger("naukri_scan")

SITE = "https://www.naukri.com"
STATE_FILE = core.DATA_DIR / "naukri_state.json"
# The searches, read one per run in turn. Each is one keyword in one place, which is the form of
# address Naukri filters correctly (a combined address was tried: Naukri ignored its experience filter).
# (name in the log and the mail, keyword, place in the address, extra filter). "bangalore" is Naukri's own spelling.
SEARCHES: List[Tuple[str, str, str, str]] = [
    (f"{keyword} / {name}", keyword, place, extra)
    for keyword in ("python developer", "ai engineer")
    for name, place, extra in (("Hyderabad", "hyderabad", ""), ("Bengaluru", "bangalore", ""), ("Chennai", "chennai", ""), ("Remote", "", "&wfhType=2"))
]
# Result pages opened per run. Naukri drops the connection on a second page, so this stays 1.
PAGES_PER_RUN = 1
# Days back Naukri is asked for. One day is its shortest window.
JOB_AGE_DAYS = 1
# A run that is refused outright is a skipped hour, not a failure, until this many happen in a row.
REFUSED_RUNS_TO_FAIL = 6

REPORT: Dict[str, Any] = {
    "name": "Naukri",
    "short": "Naukri",
    "read_from": "Read from Naukri's public job search, without a login. This run read: {sites} (the 20 newest postings of the last day). "
                 "Python developer and AI engineer searches in Hyderabad, Bengaluru, Chennai and remote take turns, one a run.",
    "footer": 'Automated report from the Naukri scan, which runs every hour. "Posted" is what Naukri shows.',
    "preview": "naukri_preview.html",
    "not_covered": {},
    "state_file": STATE_FILE,
}


def search_address(keyword: str, place: str, extra: str, page: int, candidate_yoe: float) -> str:
    """Address of one result page: one keyword in one place, the candidate's experience, last day, newest first."""
    slug = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")
    path = f"{slug}-jobs" + (f"-in-{place}" if place else "") + (f"-{page}" if page > 1 else "")
    return f"{SITE}/{path}?experience={int(candidate_yoe)}&jobAge={JOB_AGE_DAYS}&sort=f{extra}"


def search_for_this_run(turn: int | None = None) -> Tuple[str, str, str, str]:
    """The search whose turn it is. The hour of the clock picks it, so hourly runs go through all of them."""
    index = int(time.time() // 3600) if turn is None else int(turn)
    return SEARCHES[index % len(SEARCHES)]


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


def read_naukri(candidate_yoe: float, search: Tuple[str, str, str, str]):
    """A reader for company_portals.run_scan: opens one search in a real browser, no login."""

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

        name, keyword, place, extra = search
        jobs: Dict[str, Dict[str, Any]] = {}
        pages = 0
        refusal = ""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page = browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata", viewport={"width": 1366, "height": 900}).new_page()
            page.on("response", on_response)
            try:
                for number in range(1, PAGES_PER_RUN + 1):
                    answers.clear()
                    pages += 1
                    status, problem = None, ""
                    try:
                        opened = page.goto(search_address(keyword, place, extra, number, candidate_yoe), wait_until="domcontentloaded", timeout=45000)
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
                    sent = parse_qs(urlsplit(asked).query)
                    total = int(data.get("noOfJobs") or 0)
                    on_page = len(data.get("jobDetails") or [])
                    for record in naukri_records(data):
                        jobs.setdefault(record["job_id"], record)
                    LOG.info("Naukri search %r (keyword=%s location=%s experience=%s): %d posting(s) in the last day, %d read.",
                             name, sent.get("keyword"), sent.get("location"), sent.get("experience"), total, on_page)
                    if on_page < 20 or number * 20 >= total:
                        break
            finally:
                browser.close()
        if site is not None:
            site.requests_made = pages
        if refusal:
            if not jobs:
                raise cp.PortalError(f"Naukri refused the browser ({refusal}); nothing was retried or worked around")
            LOG.warning("Naukri refused the browser part-way (%s); this run goes on with the %d posting(s) already read.", refusal, len(jobs))
        return list(jobs.values())

    return read


def main() -> int:
    parser = argparse.ArgumentParser(description="New openings on Naukri's public search, strictly for the candidate's experience")
    parser.add_argument("--candidate-yoe", type=float, default=2.0, help="Candidate years of experience (default 2.0)")
    parser.add_argument("--min-match", type=int, default=60, help="Skill match a posting needs to be listed (default 60)")
    parser.add_argument("--turn", type=int, default=None, help="Which search to read, as a number (default: by the hour of the clock)")
    parser.add_argument("--dry-run", action="store_true", help="Send nothing and keep no state; write the mail to reports/ instead")
    args = parser.parse_args()
    search = search_for_this_run(args.turn)
    args.company_list = ["naukri"]
    args.keyword_list = [search[1]]
    # Naukri carries far more postings than a company site, so table 1 has the same floor as table 2.
    args.stated_min_match = args.min_match
    portals = {"naukri": ("Naukri", read_naukri(args.candidate_yoe, search))}
    report = dict(REPORT, read_from=REPORT["read_from"].replace("{sites}", search[0]))
    code = cp.run_scan(args, portals=portals, report=report)
    refused_runs = int(cp.load_state(STATE_FILE)["failures"].get("naukri", 0) or 0)
    if code and 0 < refused_runs < REFUSED_RUNS_TO_FAIL:
        LOG.warning("Naukri refused this run (%d in a row); the hour is skipped. The run fails only after %d in a row.",
                    refused_runs, REFUSED_RUNS_TO_FAIL)
        print(f"::warning::Naukri refused this run ({refused_runs} in a row); this hour's search was skipped.")
        return 0
    return code


if __name__ == "__main__":
    raise SystemExit(main())
