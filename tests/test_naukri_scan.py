"""Offline test of the Naukri scan (naukri_scan.py).

Usage: python tests/test_naukri_scan.py [<repo_dir>]   (default: the folder above tests/)
Naukri is not contacted and no browser is started: the browser, mail and push are replaced by stand-ins.
"""
import argparse
import os
import sys
import tempfile
import types
from pathlib import Path

repo_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo_dir)
import company_portals as cp  # noqa: E402
import job_match_runner as core  # noqa: E402
import naukri_scan as nk  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


GOOD_TEXT = "<p>We build products with Python, Django, React, PostgreSQL, REST APIs and Docker.</p>"


def item(job_id, title="Python Developer", low="2", high="5", place="Hyderabad", company="Acme", text=GOOD_TEXT,
         skills="Python,Django,React,PostgreSQL", **extra):
    entry = {
        "jobId": str(job_id), "title": title, "companyName": company, "jobDescription": text, "tagsAndSkills": skills,
        "jdURL": f"/job-listings-python-developer-acme-hyderabad-{low}-to-{high}-years-{job_id}",
        "placeholders": [{"type": "experience", "label": f"{low}-{high} Yrs"}, {"type": "salary", "label": "Not disclosed"},
                         {"type": "location", "label": place}],
        "footerPlaceholderLabel": "Just Now", "createdDate": 1795147443273,
        "experienceText": f"{low}-{high} Yrs", "minimumExperience": low, "maximumExperience": high,
    }
    entry.update(extra)
    return entry


# --- 1. search addresses ---------------------------------------------------------------------------
check("address: city, experience, last day, newest first",
      nk.search_address("python developer", "hyderabad", "", 1, 2.0) == "https://www.naukri.com/python-developer-jobs-in-hyderabad?experience=2&jobAge=1&sort=f",
      nk.search_address("python developer", "hyderabad", "", 1, 2.0))
check("address: page 2", nk.search_address("ai engineer", "bangalore", "", 2, 2.0) == "https://www.naukri.com/ai-engineer-jobs-in-bangalore-2?experience=2&jobAge=1&sort=f")
check("address: remote, no city", nk.search_address("generative ai", "", "&wfhType=2", 1, 2.0) == "https://www.naukri.com/generative-ai-jobs?experience=2&jobAge=1&sort=f&wfhType=2")
check("the scan needs no account: nothing in it signs in", not any(word in Path(nk.__file__).read_text(encoding="utf-8").lower()
                                                             for word in ("password", "login(", "nlogin", "cookie")))

# --- 2. one search entry -> one record ----------------------------------------------------------------
record = nk.naukri_record(item(201126009961))
check("record: id, link, company, place", (record["job_id"], record["job_url"], record["company"], record["location"]) == (
    "naukri:201126009961", "https://www.naukri.com/job-listings-python-developer-acme-hyderabad-2-to-5-years-201126009961", "Acme", "Hyderabad, India"), str(record))
check("record: the stated range is taken from Naukri's own field", (record["min_experience_years"], record["max_experience_years"]) == (2.0, 5.0)
      and core.experience_label(record) == "2-5 years")
check("record: key skills count towards the score", "Key skills: Python, Django, React, PostgreSQL" in record["description"])
check("record: its link is the Naukri job page", core.clean_job_url(record).startswith("https://www.naukri.com/job-listings-") and core.source_is_allowed(record))
check("a training promotion is not a job", nk.naukri_record(item(1, footerPlaceholderLabel="Starts in 1-3 months", createdDate=0, experienceText=None)) is None)
check("an entry without an id or a link is skipped", nk.naukri_record(item("")) is None and nk.naukri_record(item(2, jdURL="")) is None)
check("a text that also names other years does not override the stated range",
      core.experience_label(nk.naukri_record(item(3, text="<p>Python and Django. Our founders have 15 years of experience. 8+ years preferred for leads.</p>"))) == "2-5 years")
hybrid = nk.naukri_record(item(4, place="Hybrid - Bengaluru"))
check("work mode is read from the place label", hybrid["workplace_type"] == "Hybrid" and nk.naukri_record(item(5, place="Remote"))["workplace_type"] == "Remote")

JUDGED = [
    ("2-5 Yrs in Hyderabad", item(10), "match"),
    ("1-2 Yrs", item(10, low="1", high="2"), "match"),
    ("0-5 Yrs", item(10, low="0", high="5"), "match"),
    ("3-8 Yrs", item(10, low="3", high="8"), "Experience"),
    ("0-1 Yrs", item(10, low="0", high="1"), "Experience"),
    ("several cities, one of them yours", item(10, place="Hyderabad, Pune, Bengaluru"), "match"),
    ("Hybrid - Bengaluru", item(10, place="Hybrid - Bengaluru"), "match"),
    ("Remote", item(10, place="Remote"), "match"),
    ("Pune only", item(10, place="Pune"), "Location"),
    ("QA title", item(10, title="QA Tester"), "Job title"),
    ("Java role", item(10, title="Java Full Stack Developer"), "Different stack: Java"),
    ("right range, weak skill match", item(10, text="<p>Python scripting on AWS with Kafka, Terraform, Jenkins, Snowflake and Airflow pipelines for the web application.</p>",
                                            skills="AWS,Kafka,Terraform"), cp.BELOW_STATED_MATCH),
    ("no range stated, good match", item(10, experienceText=None, minimumExperience=None, maximumExperience=None), "no_yoe"),
]
for name, entry, expected in JUDGED:
    job = nk.naukri_record(entry)
    kind, reason = cp.judge(job, 2.0, 60, 60)
    check(f"judge: {name}", (kind or reason) == expected, f"expected {expected!r}, got {(kind or reason)!r} ({job.get('skill_match_pct')}%)")


# --- 3. the browser walk: paging, and stopping when refused --------------------------------------------
class FakeResponse:
    def __init__(self, status, data):
        self.url, self.status, self._data = "https://www.naukri.com/jobapi/v3/search?noOfResults=20", status, data

    def json(self):
        if self._data is None:
            raise ValueError("not JSON")
        return self._data


class FakePage:
    def __init__(self, script):
        self.script, self.handlers, self.visited = script, [], []

    def on(self, name, handler):
        self.handlers.append(handler)

    def goto(self, url, **kwargs):
        self.visited.append(url)
        status, answers = self.script(url)
        for code, data in answers:
            for handler in self.handlers:
                handler(FakeResponse(code, data))
        return types.SimpleNamespace(status=status)

    def wait_for_timeout(self, ms):
        pass


def install_browser(script):
    page = FakePage(script)
    launches = []
    browser = types.SimpleNamespace(new_context=lambda **k: types.SimpleNamespace(new_page=lambda: page), close=lambda: None)

    class Manager:
        def __enter__(self):
            return types.SimpleNamespace(chromium=types.SimpleNamespace(launch=lambda **k: (launches.append(k), browser)[1]))

        def __exit__(self, *exc):
            return False

    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: Manager()
    sys.modules["playwright"] = types.ModuleType("playwright")
    sys.modules["playwright.sync_api"] = module
    return page, launches


def paged(url):
    if "-jobs-in-hyderabad-2?" in url:
        return 200, [(200, {"noOfJobs": 25, "jobDetails": [item(2000 + n) for n in range(5)]})]
    if "-jobs-in-hyderabad?" in url:
        return 200, [(200, {"noOfJobs": 25, "jobDetails": [item(1000 + n) for n in range(20)]})]
    return 200, [(200, {"noOfJobs": 0, "jobDetails": []})]


page, launches = install_browser(paged)
places = [("Hyderabad", "hyderabad", ""), ("Remote", "", "&wfhType=2")]
jobs = nk.read_naukri(2.0, ["python developer"], places)(None)
check("walk: page 2 is read when there are more than 20, page 3 is not", [u.split("?")[0].rsplit("/", 1)[1] for u in page.visited] ==
      ["python-developer-jobs-in-hyderabad", "python-developer-jobs-in-hyderabad-2", "python-developer-jobs"], str(page.visited))
check("walk: every job of both pages, once", len(jobs) == 25 and len({j["job_id"] for j in jobs}) == 25, str(len(jobs)))
check("walk: a full browser window is used, not a hidden one", launches == [{"headless": False}], str(launches))

page, _ = install_browser(lambda url: (403, []))
try:
    nk.read_naukri(2.0, nk.SEARCH_KEYWORDS, nk.SEARCH_PLACES)(None)
    refused = "no error"
except cp.PortalError as exc:
    refused = str(exc)
check("refused (block page): the scan stops after three tries and says so", "refused the browser 3 times" in refused and len(page.visited) == 3, f"{refused} {len(page.visited)}")

page, _ = install_browser(lambda url: (200, [(406, {"message": "recaptcha required", "statusCode": 406})]))
try:
    nk.read_naukri(2.0, nk.SEARCH_KEYWORDS, nk.SEARCH_PLACES)(None)
    refused = "no error"
except cp.PortalError as exc:
    refused = str(exc)
check("refused (CAPTCHA demanded): nothing is solved or retried; the scan stops", "search answers [406]" in refused and len(page.visited) == 3, f"{refused} {len(page.visited)}")


# --- 4. a scan: its own mail and its own memory ---------------------------------------------------------
work = Path(tempfile.mkdtemp(prefix="jmr_naukri_"))
core.DATA_DIR = work
core.REPORT_DIR = work / "reports"
cp.STATE_FILE = work / "company_portal_state.json"
report = dict(nk.REPORT, state_file=work / "naukri_state.json", read_from="Read from Naukri's public job search, without a login: Hyderabad.")
sent = {"mail": [], "push": []}
core.send_gmail = lambda subject, body, attachment=None: sent["mail"].append((subject, body)) or True
core.send_push_notification = lambda *a, **k: sent["push"].append(a) or True
listing = []


def scan(dry_run=False):
    sent["mail"].clear()
    sent["push"].clear()
    args = argparse.Namespace(candidate_yoe=2.0, min_match=60, stated_min_match=60, dry_run=dry_run, company_list=["naukri"], keyword_list=["python developer"])
    portals = {"naukri": ("Naukri", lambda site: [r for r in (nk.naukri_record(dict(e)) for e in listing) if r])}
    return cp.run_scan(args, portals=portals, report=report)


listing[:] = [item(500), item(501, low="4", high="9", company="Beta"), item(502, place="Pune", company="Gamma"),
              item(503, company="Delta", text="<p>Python scripting on AWS with Kafka, Terraform and Jenkins for the web application.</p>", skills="AWS,Kafka")]
code = scan()
subject, body = sent["mail"][0] if sent["mail"] else ("", "")
check("first scan: one mail, named Naukri", code == 0 and len(sent["mail"]) == 1 and subject.startswith("Naukri: 1 new job(s) for 2 years' experience"), f"{code} {subject}")
check("first scan: only the job that fits is in it", "-500" in body and "-501" not in body and "-502" not in body and "-503" not in body)
check("first scan: the mail says no login was used and names the 60% floor", "without a login" in body and "skill match of 60% or more" in body)
check("first scan: its memory is its own file", (work / "naukri_state.json").exists() and not (work / "company_portal_state.json").exists())
code = scan()
check("same listing again: no mail", code == 0 and not sent["mail"] and not sent["push"])
listing.append(item(504, company="Epsilon", title="AI Engineer", low="1", high="3", place="Remote",
                    text="<p>LLM applications with Python, LangChain, RAG, FastAPI and Azure OpenAI.</p>", skills="Python,LangChain,RAG"))
code = scan()
check("a new job: one mail with only that job", len(sent["mail"]) == 1 and "-504" in sent["mail"][0][1] and "-500" not in sent["mail"][0][1])

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
