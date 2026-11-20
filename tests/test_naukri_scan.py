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


# --- 1. search addresses, and whose turn it is ---------------------------------------------------------
check("address: one keyword in one city, your experience, last day, newest first",
      nk.search_address("python developer", "hyderabad", "", 1, 2.0) == "https://www.naukri.com/python-developer-jobs-in-hyderabad?experience=2&jobAge=1&sort=f",
      nk.search_address("python developer", "hyderabad", "", 1, 2.0))
check("address: remote, no city", nk.search_address("ai engineer", "", "&wfhType=2", 1, 2.0) == "https://www.naukri.com/ai-engineer-jobs?experience=2&jobAge=1&sort=f&wfhType=2")
check("the scan is small: one page a run", nk.PAGES_PER_RUN == 1)
turns = [nk.search_for_this_run(hour)[0] for hour in range(500000, 500000 + len(nk.SEARCHES))]
check("the searches take turns: eight hourly runs read all eight", len(nk.SEARCHES) == 8 and sorted(turns) == sorted(s[0] for s in nk.SEARCHES), str(turns))
check("the turns cover both keywords in the three cities and remote",
      {(s[1], s[2] or "remote") for s in nk.SEARCHES} == {(k, c) for k in ("python developer", "ai engineer") for c in ("hyderabad", "bangalore", "chennai", "remote")})
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


HYDERABAD = nk.SEARCHES[0]
page, launches = install_browser(lambda url: (200, [(200, {"noOfJobs": 73, "jobDetails": [item(1000 + n) for n in range(20)]})]))
jobs = nk.read_naukri(2.0, HYDERABAD)(None)
check("walk: exactly one page is opened, even when there are more postings", page.visited == ["https://www.naukri.com/python-developer-jobs-in-hyderabad?experience=2&jobAge=1&sort=f"], str(page.visited))
check("walk: its 20 postings are read", len(jobs) == 20 and len({j["job_id"] for j in jobs}) == 20, str(len(jobs)))
check("walk: a full browser window is used, not a hidden one", launches == [{"headless": False}], str(launches))

page, _ = install_browser(lambda url: (403, []))
try:
    nk.read_naukri(2.0, HYDERABAD)(None)
    refused = "no error"
except cp.PortalError as exc:
    refused = str(exc)
check("refused (block page): one page opened, no retry, the scan says so", "refused the browser" in refused and "HTTP 403" in refused and len(page.visited) == 1,
      f"{refused} {len(page.visited)}")

page, _ = install_browser(lambda url: (200, [(406, {"message": "recaptcha required", "statusCode": 406})]))
try:
    nk.read_naukri(2.0, HYDERABAD)(None)
    refused = "no error"
except cp.PortalError as exc:
    refused = str(exc)
check("refused (CAPTCHA demanded): nothing is solved or retried", "search answers [406]" in refused and len(page.visited) == 1, f"{refused} {len(page.visited)}")


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


# --- 5. main(): a refused run is a skipped hour, until it keeps happening -------------------------------
nk.STATE_FILE = work / "naukri_main_state.json"
nk.REPORT = dict(nk.REPORT, state_file=nk.STATE_FILE)
install_browser(lambda url: (403, []))
codes = []
for _ in range(nk.REFUSED_RUNS_TO_FAIL):
    sys.argv = ["naukri_scan.py", "--turn", "0"]
    codes.append(nk.main())
check("refused runs: skipped quietly, then the run fails once it has happened six times in a row",
      codes == [0] * (nk.REFUSED_RUNS_TO_FAIL - 1) + [1] and not sent["mail"][1:], str(codes))
install_browser(lambda url: (200, [(200, {"noOfJobs": 1, "jobDetails": [item(900, company="Zeta")]})]))
sys.argv = ["naukri_scan.py", "--turn", "0"]
sent["mail"].clear()
code = nk.main()
check("a run that gets through again: mails the job and resets the count", code == 0 and len(sent["mail"]) == 1 and "python developer / Hyderabad" in sent["mail"][0][1]
      and cp.load_state(nk.STATE_FILE)["failures"] == {"naukri": 0}, f"{code} {len(sent['mail'])}")

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
