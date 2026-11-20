#!/usr/bin/env python3
"""Company career sites: new openings at a fixed list of employers, strictly for the candidate's experience.

This runs beside the LinkedIn monitor (job_match_runner.py) and uses its filters and its score. It
reads each employer's own career site, and mails once per new opening that is all of:
  - in Hyderabad, Bengaluru or Chennai, or remote in India,
  - one of the candidate's roles (the same title and Python role-fit rules as the hourly report),
  - full-time,
  - asking for experience that includes the candidate's years (table 1 of the mail).
A posting whose stated experience leaves the candidate out (3+ years, 5-8 years, 0-1 year) is never
listed. A posting that states no years at all cannot be checked, so it goes into a separate table 2,
and only when its skill match is --min-match or more.

A career site shows the day a job was posted, not the hour, so there is no "last hour" window here.
Instead the scan remembers every posting it has judged and reports each opening once.

Usage:
  python company_portals.py                    scan, mail what is new, remember it
  python company_portals.py --dry-run          scan and print; send nothing, remember nothing
  python company_portals.py --companies amazon,jpmc
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Sequence, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo

import job_match_runner as core

LOG = logging.getLogger("company_portals")

STATE_FILE = core.DATA_DIR / "company_portal_state.json"
# What each site is asked for. Every site is also filtered to India, and the title, location,
# experience and role-fit rules decide afterwards, so a broad word here costs requests, not accuracy.
SEARCH_KEYWORDS = ["Python", "AI Engineer", "Generative AI", "Software Engineer", "Full Stack Developer", "Backend Developer"]
SEARCH_CITIES = ["Hyderabad", "Bengaluru", "Chennai"]
# Newest postings read per search. The scan runs every hour, so new postings are always inside this.
NEWEST_PER_SEARCH = 100
# Job pages opened per site in one run; the rest wait for the next run.
MAX_DETAIL_PAGES = 40
# Longest one site may take in one run.
SITE_BUDGET_SECONDS = 240.0
REQUEST_PAUSE_SECONDS = 0.5
# A posting waits this long for a mail that could not be delivered, then it is dropped.
HELD_DAYS = 7.0
# A judged posting is forgotten this long after it was last seen on the site.
DECIDED_DAYS = 21.0
REPORTED_DAYS = 180.0

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


class PortalError(Exception):
    """A career site could not be read."""


class Site:
    """Requests to one career site: retries, a pause after each request, and a time budget."""

    def __init__(self, keywords: Sequence[str]):
        self.session = core.requests.Session()
        self.keywords = list(keywords)
        self.deadline = time.monotonic() + SITE_BUDGET_SECONDS
        self.requests_made = 0

    def out_of_time(self) -> bool:
        return time.monotonic() > self.deadline

    def fetch(self, url: str, params: Dict[str, Any] | None = None, accept: str = "application/json") -> Any:
        if self.out_of_time():
            raise PortalError("time limit for this site reached")
        problem = "no answer"
        for attempt in range(3):
            self.requests_made += 1
            try:
                response = self.session.get(url, params=params, headers={**BROWSER_HEADERS, "Accept": accept}, timeout=25)
            except core.requests.RequestException as exc:
                problem = type(exc).__name__
            else:
                if response.status_code == 200:
                    time.sleep(REQUEST_PAUSE_SECONDS)
                    return response
                problem = f"HTTP {response.status_code}"
                if response.status_code not in (429, 500, 502, 503, 504):
                    break
            time.sleep(2.0 * (attempt + 1))
        raise PortalError(f"{problem} from {url.split('?')[0]}")

    def json(self, url: str, params: Dict[str, Any] | None = None) -> Any:
        try:
            return self.fetch(url, params).json()
        except ValueError as exc:
            raise PortalError(f"unreadable answer from {url.split('?')[0]}") from exc

    def soup(self, url: str, params: Dict[str, Any] | None = None) -> Any:
        return core.BeautifulSoup(self.fetch(url, params, accept="text/html,application/xhtml+xml").text, "html.parser")


def _one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _text(markup: Any) -> str:
    """Text of a description, with line breaks where the page has them."""
    if not markup:
        return ""
    return core._html_to_text(core.BeautifulSoup(str(markup), "html.parser"))


def _sections(*parts: Tuple[str, Any]) -> str:
    """Join (heading, html) parts into one description; a heading is on a line of its own."""
    out = []
    for heading, markup in parts:
        body = _text(markup)
        if body:
            out.append(f"{heading}:\n{body}" if heading else body)
    return "\n\n".join(out)


def _record(
    key: str,
    company: str,
    native_id: Any,
    title: Any,
    location: Any,
    url: str,
    description: str = "",
    posted: Any = "",
    workplace: Any = "",
    employment: Any = "",
) -> Dict[str, Any]:
    posted_text = _one_line(posted)
    return {
        "job_id": f"{key}:{native_id}",
        "portal": key,
        "source": f"{company} careers",
        "company": company,
        "title": _one_line(title),
        "location": _one_line(location),
        "description": description,
        "job_url": url,
        "apply_url": url,
        "workplace_type": _one_line(workplace) or "Not specified",
        "employment_type": _one_line(employment),
        "is_easy_apply": False,
        "posted_time": posted_text,
        "freshness_badge": posted_text or "Date not shown",
    }


# ---------------------------------------------------------------------------
# One reader per career site. Each returns the site's India postings as records; a record whose
# description needs a second request carries it as record["_detail"](site, record).
# ---------------------------------------------------------------------------
def read_amazon(site: Site) -> List[Dict[str, Any]]:
    jobs: Dict[str, Dict[str, Any]] = {}
    for keyword in site.keywords:
        data = site.json("https://www.amazon.jobs/en/search.json", {
            "base_query": keyword, "loc_query": "India", "country": "IND",
            "result_limit": NEWEST_PER_SEARCH, "sort": "recent", "offset": 0,
        })
        for item in data.get("jobs") or []:
            native_id = str(item.get("id_icims") or item.get("id") or "")
            if not native_id or not item.get("job_path"):
                continue
            jobs[native_id] = _record(
                "amazon", "Amazon", native_id, item.get("title"),
                item.get("normalized_location") or item.get("location"),
                "https://www.amazon.jobs" + str(item["job_path"]),
                description=_sections(
                    ("", item.get("description")),
                    ("Basic Qualifications", item.get("basic_qualifications")),
                    ("Preferred Qualifications", item.get("preferred_qualifications")),
                ),
                posted=item.get("posted_date"), employment=item.get("job_schedule_type"),
            )
    return list(jobs.values())


def read_capgemini(site: Site) -> List[Dict[str, Any]]:
    jobs: Dict[str, Dict[str, Any]] = {}
    for keyword in site.keywords:
        for page in (1, 2):
            data = site.json("https://cg-job-search-microservices.azurewebsites.net/api/job-search", {
                "page": page, "size": 50, "country_code": "in-en", "search": keyword,
            })
            items = data.get("data") or []
            for item in items:
                native_id = str(item.get("id") or item.get("_id") or "")
                url = str(item.get("apply_job_url") or "")
                if not native_id or not url.startswith("https://"):
                    continue
                contract = _one_line(item.get("contract_type"))
                jobs[native_id] = _record(
                    "capgemini", "Capgemini", native_id, item.get("title"), f"{_one_line(item.get('location'))}, India", url,
                    description=_text(item.get("description")),
                    posted=str(item.get("updated_at") or item.get("indexed_at") or "")[:10],
                    employment="" if contract.lower() in ("", "permanent") else f"Contract ({contract})",
                )
            if len(items) < 50 or page * 50 >= int(data.get("count") or 0):
                break
    return list(jobs.values())


_JPMC_API = "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/"
_JPMC_INDIA = "300000000289360"


def _jpmc_detail(site: Site, job: Dict[str, Any]) -> None:
    native_id = job["job_id"].split(":", 1)[1]
    data = site.json(
        f"{_JPMC_API}recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
        f"&finder=ById;Id=%22{quote(native_id)}%22,siteNumber=CX_1001"
    )
    item = (data.get("items") or [{}])[0]
    job["description"] = _sections(
        ("", item.get("ExternalDescriptionStr")),
        ("Responsibilities", item.get("ExternalResponsibilitiesStr")),
        ("Qualifications", item.get("ExternalQualificationsStr")),
    )
    if item.get("WorkplaceType"):
        job["workplace_type"] = _one_line(item["WorkplaceType"])


def read_jpmc(site: Site) -> List[Dict[str, Any]]:
    jobs: Dict[str, Dict[str, Any]] = {}
    for keyword in site.keywords:
        data = site.json(
            f"{_JPMC_API}recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations"
            f"&finder=findReqs;siteNumber=CX_1001,limit={NEWEST_PER_SEARCH},keyword=%22{quote(keyword)}%22,"
            f"locationId={_JPMC_INDIA},sortBy=POSTING_DATES_DESC"
        )
        for item in ((data.get("items") or [{}])[0].get("requisitionList")) or []:
            native_id = str(item.get("Id") or "")
            if not native_id:
                continue
            places = [_one_line(item.get("PrimaryLocation"))]
            places += [_one_line(other.get("Name")) for other in item.get("secondaryLocations") or [] if isinstance(other, dict)]
            record = _record(
                "jpmc", "JPMorgan Chase", native_id, item.get("Title"), " / ".join(p for p in places if p),
                f"https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/{native_id}",
                posted=item.get("PostedDate"), workplace=item.get("WorkplaceType"),
            )
            record["_detail"] = _jpmc_detail
            jobs[native_id] = record
    return list(jobs.values())


def read_tiger_analytics(site: Site) -> List[Dict[str, Any]]:
    data = site.json("https://apply.workable.com/api/v1/widget/accounts/tiger-analytics", {"details": "true"})
    jobs = []
    for item in data.get("jobs") or []:
        places = item.get("locations") or [{"country": item.get("country"), "city": item.get("city"), "region": item.get("state")}]
        india = [p for p in places if isinstance(p, dict) and str(p.get("country") or "").lower() == "india"]
        if not india or not item.get("shortcode"):
            continue
        location = " / ".join(", ".join(_one_line(p.get(k)) for k in ("city", "region", "country") if p.get(k)) for p in india)
        jobs.append(_record(
            "tiger", "Tiger Analytics", item["shortcode"], item.get("title"), location,
            str(item.get("url") or f"https://apply.workable.com/j/{item['shortcode']}"),
            description=_text(item.get("description")), posted=item.get("published_on"),
            workplace="Remote" if item.get("telecommuting") else "", employment=item.get("employment_type"),
        ))
    return jobs


def read_infor(site: Site) -> List[Dict[str, Any]]:
    data = site.json("https://careers.infor.com/postings.json")
    jobs = []
    for item in data.get("data") or []:
        country = ((((item.get("job") or {}).get("structure_custom_group_one")) or {}).get("name")) or ""
        url = str(item.get("url") or "")
        if str(country).lower() != "india" or not item.get("id") or not url.startswith("https://"):
            continue
        place = item.get("location") or {}
        jobs.append(_record(
            "infor", "Infor", item["id"], item.get("title"), f"{_one_line(place.get('city') or place.get('name'))}, India", url,
            description=_sections(
                ("", item.get("description")),
                (_one_line(item.get("key_responsibilities_header")).rstrip(":") or "Responsibilities", item.get("key_responsibilities")),
                (_one_line(item.get("skills_knowledge_expertise_header")).rstrip(":") or "Qualifications", item.get("skills_knowledge_expertise")),
                (_one_line(item.get("benefits_header")).rstrip(":") or "Preferred Qualifications", item.get("benefits")),
            ),
            workplace=item.get("workplace_type_text"), employment=item.get("employment_type_text"),
        ))
    return jobs


_NTT_SITE = "https://careers-inc.nttdata.com"


def _ntt_detail(site: Site, job: Dict[str, Any]) -> None:
    page = site.soup(job["job_url"])
    body = page.select_one("span.jobdescription") or page.select_one('[itemprop="description"]')
    job["description"] = core._html_to_text(body) if body else ""


def read_ntt_data(site: Site) -> List[Dict[str, Any]]:
    jobs: Dict[str, Dict[str, Any]] = {}
    for keyword in site.keywords:
        for start in range(0, NEWEST_PER_SEARCH, 25):
            page = site.soup(f"{_NTT_SITE}/search/", {
                "q": keyword, "locationsearch": "India", "sortColumn": "referencedate", "sortDirection": "desc", "startrow": start,
            })
            rows = page.select("tr.data-row")
            for row in rows:
                link = row.select_one("a.jobTitle-link")
                number = re.search(r"/(\d{5,})/?$", str(link.get("href") if link else ""))
                if not link or not number:
                    continue
                place = row.select_one("span.jobLocation")
                posted = row.select_one("span.jobDate")
                # "Bangalore, KA, IN": spell the country out, which is what the location rule looks for.
                location = re.sub(r",\s*IN$", ", India", _one_line(place.get_text() if place else ""))
                record = _record(
                    "ntt", "NTT DATA", number.group(1), link.get_text(), location, _NTT_SITE + str(link["href"]),
                    posted=posted.get_text() if posted else "",
                )
                record["_detail"] = _ntt_detail
                jobs[number.group(1)] = record
            if len(rows) < 25:
                break
    return list(jobs.values())


_COGNIZANT_SITE = "https://careers.cognizant.com"
_WORK_MODE_WORDS = re.compile(r"\b(?:remote|work from home|hybrid|on-?site|office)\b", re.I)


def _cognizant_detail(site: Site, job: Dict[str, Any]) -> None:
    page = site.soup(job["job_url"])
    body = page.select_one(".cms-content")
    job["description"] = core._html_to_text(body) if body else ""
    for item in page.select(".job-meta-item"):
        label = _one_line(item.get_text())
        if label.lower().startswith("date published"):
            job["posted_time"] = job["freshness_badge"] = label[len("date published"):].strip()
        elif label.lower().startswith("work model"):
            mode = label[len("work model"):].strip()
            job["workplace_type"] = "Remote" if re.search(r"remote|work from home", mode, re.I) else mode


def read_cognizant(site: Site) -> List[Dict[str, Any]]:
    jobs: Dict[str, Dict[str, Any]] = {}
    for city in SEARCH_CITIES:
        for keyword in site.keywords:
            page = site.soup(f"{_COGNIZANT_SITE}/global-en/jobs/", {"keyword": keyword, "location": city, "pagesize": 50})
            for card in page.select("div.card-job"):
                link = card.select_one("a.js-view-job") or card.select_one("h2 a")
                native_id = str(card.get("data-id") or "")
                if not link or not native_id or not link.get("href"):
                    continue
                details = [_one_line(item.get_text()) for item in card.select("ul.job-meta li")]
                mode = next((d for d in details[1:] if _WORK_MODE_WORDS.search(d)), "")
                record = _record(
                    "cognizant", "Cognizant", native_id, link.get_text(), details[0] if details else city,
                    _COGNIZANT_SITE + str(link["href"]),
                    workplace="Remote" if re.search(r"remote|work from home", mode, re.I) else mode,
                )
                record["_detail"] = _cognizant_detail
                jobs[native_id] = record
    return list(jobs.values())


# key: (name shown in the mail, reader). The order is the order of the scan.
PORTALS: Dict[str, Tuple[str, Callable[[Site], List[Dict[str, Any]]]]] = {
    "ntt": ("NTT DATA", read_ntt_data),
    "amazon": ("Amazon", read_amazon),
    "cognizant": ("Cognizant", read_cognizant),
    "tiger": ("Tiger Analytics", read_tiger_analytics),
    "infor": ("Infor", read_infor),
    "jpmc": ("JPMorgan Chase", read_jpmc),
    "capgemini": ("Capgemini", read_capgemini),
}
# Asked for, but not readable by a script. Each entry is said in every mail, so the gap is not forgotten.
NOT_COVERED = {
    "EPAM": "its India career site (jobs.welcome.epam.in) hands a script only 10 of its roughly 340 jobs, in no date order",
    "Cimpress": "its career site (cimpress.ripplehire.com) answers with an empty job list",
}


# ---------------------------------------------------------------------------
# State, judging and the mail
# ---------------------------------------------------------------------------
def load_state() -> Dict[str, Any]:
    """decided : {job id: when last seen} - postings already judged, so they are not judged or opened again
    reported: {job id or "k:" + fingerprint: when} - openings already mailed
    queue   : openings that qualify and have not been mailed yet
    held    : openings that state no experience (table 2 of the mail), not mailed yet
    failures: {site: runs in a row it could not be read}
    """
    state: Dict[str, Any] = {"decided": {}, "reported": {}, "queue": [], "held": [], "failures": {}}
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        for key, kind in (("decided", dict), ("reported", dict), ("queue", list), ("held", list), ("failures", dict)):
            if isinstance(payload.get(key), kind):
                state[key] = payload[key]
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError) as exc:
        LOG.warning("Could not read the company-site state; starting empty: %s", exc)
    return state


def save_state(state: Dict[str, Any]) -> None:
    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    def newer_than(stamps: Dict[str, str], days: float) -> Dict[str, str]:
        cutoff = (now - timedelta(days=days)).isoformat()
        return {key: stamp for key, stamp in stamps.items() if str(stamp) >= cutoff}

    state["decided"] = newer_than(state["decided"], DECIDED_DAYS)
    state["reported"] = newer_than(state["reported"], REPORTED_DAYS)
    STATE_FILE.write_text(json.dumps({**state, "updated_at": now.isoformat()}, ensure_ascii=False), encoding="utf-8")


_RECORD_FIELDS = (
    "job_id", "portal", "source", "title", "company", "location", "also_in", "duplicate_ids", "posting_key", "workplace_type",
    "is_easy_apply", "skill_match_pct", "matched_skills", "missing_skills", "min_experience_years", "max_experience_years",
    "experience_requirement", "job_url", "apply_url", "posted_time", "freshness_badge", "found_at",
)


def _stored(job: Dict[str, Any], now_iso: str) -> Dict[str, Any]:
    record = dict(job, found_at=now_iso)
    return {key: record[key] for key in _RECORD_FIELDS if key in record}


def _ids(record: Dict[str, Any]) -> List[str]:
    ids = [str(record.get("job_id")), *[str(x) for x in record.get("duplicate_ids") or []]]
    if record.get("posting_key"):
        ids.append(core.SEEN_KEY_PREFIX + str(record["posting_key"]))
    return ids


BELOW_MATCH = "No experience stated and skill match too low"


def judge(job: Dict[str, Any], candidate_yoe: float, min_match: int = 60) -> Tuple[str, str]:
    """('match' | 'no_yoe' | '', reason it was dropped) for one posting whose description has been read.

    match  : passes every filter of the hourly report and states experience that includes candidate_yoe.
    no_yoe : passes every filter and scores min_match or more, but states no experience at all.
    """
    reason = core.rejection_reason(job, candidate_yoe=candidate_yoe)
    if reason:
        return "", reason
    core.score_job(job)
    if core.experience_is_stated(job):
        return "match", ""
    return ("no_yoe", "") if int(job.get("skill_match_pct", 0)) >= min_match else ("", BELOW_MATCH)


def scan_site(key: str, state: Dict[str, Any], args: argparse.Namespace, now_iso: str) -> Dict[str, Any]:
    """Read one career site and judge the postings not judged before."""
    name, reader = PORTALS[key]
    site = Site(args.keyword_list)
    listing = reader(site)
    summary: Dict[str, Any] = {"listed": len(listing), "new": 0, "waiting": 0, "candidates": [], "reasons": {}}
    pages_left = MAX_DETAIL_PAGES
    for job in listing:
        job_id = job["job_id"]
        fill = job.pop("_detail", None)
        if job_id in state["decided"]:
            state["decided"][job_id] = now_iso  # still on the site
            continue
        summary["new"] += 1
        # A description cannot rescue a posting whose title or place is already out, so those are not opened.
        early = "" if core.title_is_eligible(job) else "Job title"
        early = early or ("" if core.location_is_eligible(job) else "Location")
        if not early and fill and not job["description"].strip():
            if pages_left <= 0 or site.out_of_time():
                summary["waiting"] += 1
                continue
            pages_left -= 1
            try:
                fill(site, job)
            except PortalError as exc:
                LOG.info("%s: could not open %s (%s); it is tried again next run.", name, job["job_url"], exc)
        if not early and not job["description"].strip():
            summary["waiting"] += 1
            continue
        state["decided"][job_id] = now_iso
        kind, reason = ("", early) if early else judge(job, args.candidate_yoe, args.min_match)
        summary["reasons"][reason or kind] = summary["reasons"].get(reason or kind, 0) + 1
        if kind:
            job["portal_class"] = kind
            summary["candidates"].append(job)
    LOG.info(
        "%s: %d India posting(s) listed, %d not judged before, %d still to open; %s (%d request(s)).",
        name, summary["listed"], summary["new"], summary["waiting"],
        ", ".join(f"{k} {v}" for k, v in sorted(summary["reasons"].items(), key=lambda kv: -kv[1])) or "nothing new",
        site.requests_made,
    )
    return summary


def rule_text(candidate_yoe: float) -> str:
    return (
        "in Hyderabad, Bengaluru or Chennai (or remote in India), full-time, a Python developer role, "
        f"and asking for experience that includes your {candidate_yoe:g} years"
    )


def build_mail(
    matches: Sequence[Dict[str, Any]],
    held: Sequence[Dict[str, Any]],
    candidate_yoe: float,
    notes: Sequence[str],
    covered: Sequence[str],
    min_match: int = 60,
) -> str:
    run_time = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
    none = '<p style="color:#666;margin:8px 0 18px">None new since the last mail.</p>'
    notes_html = (
        '<p style="margin-top:22px"><b>Notes on this scan</b></p><ul>' + "".join(f"<li>{html.escape(n)}</li>" for n in notes) + "</ul>"
        if notes else ""
    )
    return f"""<!doctype html><html><body style="font-family:Arial,sans-serif;color:#202124">
    <h2>Company career sites: new jobs for your experience</h2>
    <p>Run time: <b>{run_time}</b></p>
    <p>Read from the companies' own career sites: {html.escape(', '.join(covered))}.</p>
    <p>Every job in table 1 is {html.escape(rule_text(candidate_yoe))}. The years are stated in the posting.</p>
    <h3>1. New openings for your experience ({len(matches)})</h3>
    <p>Sorted by skill match, highest first. Each opening is mailed once.</p>
    {core.html_table(matches) if matches else none}
    <h3 style="margin-top:28px">2. Experience not stated ({len(held)})</h3>
    <p>Same place, role and full-time rules, and a skill match of {min_match}% or more, but the posting gives no
    years of experience, so it could not be checked against yours. A posting whose stated experience leaves
    you out is never listed in either table.</p>
    {core.html_table(held) if held else none}
    <p>{core.SCORE_EXPLANATION}</p>
    {notes_html}
    <p style="font-size:11px;color:#666;margin-top:22px">Automated report from the company-site scan, which runs every hour.
    "Posted" is the date the career site shows. Verify the requirements on the employer's page.</p>
    </body></html>"""


def _order(record: Dict[str, Any]) -> Tuple[int, str, str]:
    return (-int(record.get("skill_match_pct", 0)), str(record.get("company", "")), str(record.get("title", "")).lower())


def run_scan(args: argparse.Namespace) -> int:
    state = load_state()
    now_iso = datetime.now(timezone.utc).isoformat()
    notes: List[str] = []
    read_ok: List[str] = []
    candidates: List[Dict[str, Any]] = []
    for key in args.company_list:
        name = PORTALS[key][0]
        try:
            summary = scan_site(key, state, args, now_iso)
        except Exception as exc:  # one site failing must not stop the others
            runs = int(state["failures"].get(key, 0)) + 1
            state["failures"][key] = runs
            detail = str(exc) if isinstance(exc, PortalError) else f"{type(exc).__name__}: {exc}"
            LOG.warning("%s could not be read (%s); %d run(s) in a row.", name, detail, runs)
            print(f"::warning::{name} career site could not be read: {detail}")
            notes.append(f"{name} could not be read this run ({detail}); {runs} run(s) in a row.")
            continue
        state["failures"][key] = 0
        read_ok.append(name)
        candidates += summary["candidates"]
        if not summary["listed"]:
            notes.append(f"{name} listed no openings in India at all, which is unusual; the site may have changed.")
        if summary["waiting"]:
            notes.append(f"{name}: {summary['waiting']} posting(s) are still to be opened; they are judged in the next runs.")
    for name, why in NOT_COVERED.items():
        notes.append(f"{name} is not covered: {why}.")

    known = set(state["reported"]) | {x for record in state["queue"] + state["held"] for x in _ids(record)}
    new_matches = new_held = 0
    for job in core.merge_duplicate_postings(candidates):
        record = _stored(job, now_iso)
        ids = _ids(record)
        if any(x in known for x in ids):
            continue
        known.update(ids)
        if job["portal_class"] == "match":
            state["queue"].append(record)
            new_matches += 1
        else:
            state["held"].append(record)
            new_held += 1
    held_cutoff = (datetime.now(timezone.utc) - timedelta(days=HELD_DAYS)).isoformat()
    state["held"] = [r for r in state["held"] if str(r.get("found_at") or now_iso) >= held_cutoff]

    matches = sorted(state["queue"], key=_order)
    held = sorted(state["held"], key=_order)
    LOG.info("Company sites: %d read, %d new opening(s) for %g years, %d new with no experience stated.",
             len(read_ok), new_matches, args.candidate_yoe, new_held)
    for record in matches:
        LOG.info("  %3d%%  %s | %s | %s | %s | %s", int(record.get("skill_match_pct", 0)), record.get("company"), record.get("title"),
                 record.get("location"), core.experience_label(record), record.get("job_url"))

    mailed = True
    if matches or held:
        body = build_mail(matches, held, args.candidate_yoe, notes, read_ok, args.min_match)
        stamp = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %I:%M %p IST")
        subject = (
            f"Company career sites: {len(matches)} new job(s) for {args.candidate_yoe:g} years' experience, "
            f"{len(held)} with no experience stated | {stamp}"
        )
        if args.dry_run:
            core.REPORT_DIR.mkdir(parents=True, exist_ok=True)
            (core.REPORT_DIR / "company_portals_preview.html").write_text(body, encoding="utf-8")
            LOG.info("Dry run: %r not sent; preview written to %s", subject, core.REPORT_DIR)
            for record in held:
                LOG.info("  not stated %3d%%  %s | %s | %s | %s", int(record.get("skill_match_pct", 0)), record.get("company"),
                         record.get("title"), record.get("location"), record.get("job_url"))
        else:
            top = (matches or held)[0]
            more = f" (+{len(matches) + len(held) - 1} more)" if len(matches) + len(held) > 1 else ""
            pushed = core.send_push_notification(
                f"Company sites: {len(matches)} new job(s) for your experience, {len(held)} not stated",
                f"{top.get('title')} at {top.get('company')}{more}: {int(top.get('skill_match_pct', 0))}% match, "
                f"experience {core.experience_label(top).lower()}, {top.get('location')}.",
                click_url=str(top.get("job_url") or ""), priority="high",
            )
            mailed = core.send_gmail(subject, body)
            if pushed or mailed:
                for record in state["queue"] + state["held"]:
                    for x in _ids(record):
                        state["reported"][x] = now_iso
                state["queue"], state["held"] = [], []
            else:
                LOG.error("The report could not be delivered by push or mail; it stays queued for the next run.")
    for note in notes:
        LOG.info("Note: %s", note)
    if not args.dry_run:
        save_state(state)
    if not read_ok:
        LOG.error("No career site could be read.")
        return 1
    return core._exit_code(mailed)


def main() -> int:
    parser = argparse.ArgumentParser(description="New openings on company career sites, strictly for the candidate's experience")
    parser.add_argument("--candidate-yoe", type=float, default=2.0, help="Candidate years of experience (default 2.0)")
    parser.add_argument("--min-match", type=int, default=60,
                        help="Skill match a posting needs when it states no experience (default 60)")
    parser.add_argument("--companies", type=str, default="", help=f"Comma-separated sites to scan (default all): {', '.join(PORTALS)}")
    parser.add_argument("--keywords", type=str, default="", help="Comma-separated search words (default: the built-in list)")
    parser.add_argument("--dry-run", action="store_true", help="Send nothing and keep no state; write the mail to reports/ instead")
    args = parser.parse_args()
    wanted = [c.strip().lower() for c in args.companies.split(",") if c.strip()]
    unknown = [c for c in wanted if c not in PORTALS]
    if unknown:
        parser.error(f"unknown site(s): {', '.join(unknown)}. Known: {', '.join(PORTALS)}")
    args.company_list = wanted or list(PORTALS)
    args.keyword_list = [k.strip() for k in args.keywords.split(",") if k.strip()] or SEARCH_KEYWORDS
    return run_scan(args)


if __name__ == "__main__":
    raise SystemExit(main())
