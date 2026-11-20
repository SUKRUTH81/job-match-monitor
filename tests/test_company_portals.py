"""Offline test of the company career-site scan (company_portals.py).

Usage: python tests/test_company_portals.py [<repo_dir>]   (default: the folder above tests/)
Nothing is sent and no career site is contacted: the sites, mail and push are replaced by stand-ins.
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

repo_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo_dir)
import company_portals as cp  # noqa: E402
import job_match_runner as core  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


STACK = "Python, Django, React, PostgreSQL, REST APIs, Docker."


def posting(number, title="Python Developer", description=f"2-4 years of experience in {STACK}", location="Hyderabad, Telangana, India",
            key="acme", company="Acme", **extra):
    record = cp._record(key, company, number, title, location, f"https://careers.acme.example/job/{number}", description=description)
    record.update(extra)
    return record


# --- 1. a career-site record in the shared filters ---------------------------------------------------
job = posting(1)
check("career-site link is kept as the job link", core.clean_job_url(job) == "https://careers.acme.example/job/1", core.clean_job_url(job))
check("career-site record is an allowed source", core.source_is_allowed(job))
check("career-site record without an https link is refused", not core.source_is_allowed(dict(job, job_url="http://careers.acme.example/job/1", apply_url="")))
linkedin = {"source": "LinkedIn", "job_id": "4471386121", "job_url": "https://www.linkedin.com/jobs/view/4471386121/"}
check("LinkedIn records are judged as before", core.source_is_allowed(linkedin) and not core.source_is_allowed({"source": "x", "job_url": "https://evil.example/1"}))
check("the mail table links to the career site", 'href="https://careers.acme.example/job/1"' in core.html_table([core.score_job(dict(job))]))

# --- 2. strictly the candidate's experience -----------------------------------------------------------
NO_YEARS = f"We build products with {STACK}"
JUDGED = [
    ("2-4 years", posting(2), "match"),
    ("0-2 years includes 2", posting(2, description=f"Experience: 0-2 years\n{STACK}"), "match"),
    ("2+ years", posting(2, description=f"2+ years of experience in {STACK}"), "match"),
    ("years only in the title", posting(2, title="Python Developer (1-3 yrs)", description=NO_YEARS), "match"),
    ("stated experience, low skill match, still table 1", posting(2, description="1+ years of experience. Python, AWS, Kafka, Linux, Elasticsearch, Cassandra, GraphQL."), "match"),
    ("3+ years", posting(2, description=f"3+ years of experience in {STACK}"), "Experience"),
    ("5-8 years", posting(2, description=f"5-8 years of experience in {STACK}"), "Experience"),
    ("0-1 year", posting(2, description=f"Experience: 0-1 year\n{STACK}"), "Experience"),
    ("Senior title, no years", posting(2, title="Senior Python Developer", description=NO_YEARS), "Experience"),
    ("no years, good skill match -> table 2", posting(2, description=NO_YEARS), "no_yoe"),
    ("no years, weak skill match -> dropped", posting(2, description="Python scripting, AWS, Kafka, Terraform, Jenkins, Snowflake."), cp.BELOW_MATCH),
    ("Pune", posting(2, location="Pune, Maharashtra, India"), "Location"),
    ("remote in India", posting(2, location="Remote, India", workplace_type="Remote"), "match"),
    ("Java title", posting(2, title="Java Full Stack Developer"), "Different stack: Java"),
    ("internship", posting(2, employment_type="Internship"), "Not full-time"),
]
for name, record, expected in JUDGED:
    kind, reason = cp.judge(record, 2.0, 60)
    check(f"judge: {name}", (kind or reason) == expected, f"expected {expected!r}, got {(kind or reason)!r} ({record.get('skill_match_pct')}%)")


# --- 3. the readers, against answers shaped like each site's ------------------------------------------
class FakeSite:
    """Stands in for cp.Site: answers from a table of url fragment -> payload."""

    def __init__(self, answers, keywords=("Python",)):
        self.answers, self.keywords, self.requests_made, self.asked = answers, list(keywords), 0, []

    def out_of_time(self):
        return False

    def _find(self, url):
        self.requests_made += 1
        self.asked.append(url)
        for fragment, payload in self.answers.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise cp.PortalError(f"HTTP 404 from {url}")

    def json(self, url, params=None):
        return self._find(url)

    def soup(self, url, params=None):
        return core.BeautifulSoup(self._find(url), "html.parser")


def one(jobs):
    return jobs[0] if jobs else {}


amazon = one(cp.read_amazon(FakeSite({"amazon.jobs": {"jobs": [{
    "id_icims": "10594150", "title": "Software Development Engineer", "normalized_location": "Hyderabad, Telangana, IND",
    "job_path": "/en/jobs/10594150/sde", "posted_date": "November 19, 2026", "job_schedule_type": "full-time",
    "description": "Build services.", "basic_qualifications": "- 1+ years of experience<br/>- Python", "preferred_qualifications": "- 5+ years of Java",
}]}})))
check("Amazon: id, link and place", (amazon.get("job_id"), amazon.get("job_url"), amazon.get("location")) ==
      ("amazon:10594150", "https://www.amazon.jobs/en/jobs/10594150/sde", "Hyderabad, Telangana, IND"), str(amazon))
check("Amazon: preferred qualifications do not set the experience", core.parse_experience_requirement(amazon) == (1.0, None), str(core.parse_experience_requirement(amazon)))

infor = cp.read_infor(FakeSite({"infor.com": {"data": [
    {"id": "1", "title": "Software Engineer", "url": "https://careers.infor.com/en/postings/a", "description": "<div>Python</div>",
     "skills_knowledge_expertise_header": "Basic Qualifications:", "skills_knowledge_expertise": "<ul><li>2-4 years of experience</li></ul>",
     "workplace_type_text": "Onsite", "employment_type_text": "Full Time", "location": {"city": "Hyderabad"},
     "job": {"structure_custom_group_one": {"name": "India"}}},
    {"id": "2", "title": "Software Engineer", "url": "https://careers.infor.com/en/postings/b", "description": "x", "location": {"city": "Barneveld"},
     "job": {"structure_custom_group_one": {"name": "Netherlands"}}},
]}}))
check("Infor: only the India posting, with its headings", len(infor) == 1 and infor[0]["location"] == "Hyderabad, India"
      and "Basic Qualifications:\n2-4 years of experience" in infor[0]["description"], str(infor))

tiger = cp.read_tiger_analytics(FakeSite({"workable.com": {"jobs": [
    {"shortcode": "AB12", "title": "AI Engineer", "url": "https://apply.workable.com/j/AB12", "description": "<p>Python</p>", "published_on": "2026-11-01",
     "telecommuting": False, "locations": [{"country": "India", "city": "Chennai", "region": "Tamil Nadu"}]},
    {"shortcode": "CD34", "title": "AI Engineer", "url": "https://apply.workable.com/j/CD34", "description": "x", "locations": [{"country": "Canada", "city": ""}]},
]}}))
check("Tiger Analytics: only the India posting", [j["job_id"] for j in tiger] == ["tiger:AB12"] and tiger[0]["location"] == "Chennai, Tamil Nadu, India", str(tiger))

capgemini = one(cp.read_capgemini(FakeSite({"job-search": {"count": 1, "data": [{
    "id": "547166-en_GB", "title": "Python Developer", "location": "Bangalore", "contract_type": "Permanent", "updated_at": "2026-11-16T11:14:22.000Z",
    "apply_job_url": "https://careers.capgemini.com/job/x/1/", "description": "<p>Experience: 2-4 years</p><ul><li>Python</li></ul>",
}]}})))
check("Capgemini: place, date and text", (capgemini.get("location"), capgemini.get("posted_time")) == ("Bangalore, India", "2026-11-16")
      and core.location_is_eligible(capgemini) and "Experience: 2-4 years" in capgemini.get("description", ""), str(capgemini))

jpmc_site = FakeSite({
    "recruitingCEJobRequisitions?": {"items": [{"requisitionList": [{
        "Id": "210810804", "Title": "Software Engineer II - Python", "PostedDate": "2026-11-20", "PrimaryLocation": "Hyderabad, Telangana, India",
        "secondaryLocations": [{"Name": "Bengaluru, Karnataka, India"}]}]}]},
    "recruitingCEJobRequisitionDetails": {"items": [{"ExternalDescriptionStr": "<p>Build services in Python.</p>",
                                                    "ExternalQualificationsStr": "<ul><li>2+ years applied experience</li></ul>"}]},
})
jpmc = one(cp.read_jpmc(jpmc_site))
fill = jpmc.pop("_detail", None)
check("JPMorgan Chase: listed without text, both places", jpmc.get("description") == "" and jpmc.get("location") == "Hyderabad, Telangana, India / Bengaluru, Karnataka, India" and fill is not None, str(jpmc))
if fill:
    fill(jpmc_site, jpmc)
check("JPMorgan Chase: the job page supplies the experience", core.parse_experience_requirement(jpmc) == (2.0, None), jpmc.get("description", ""))

NTT_LIST = ('<table><tr class="data-row"><td><span class="jobTitle"><a class="jobTitle-link" href="/job/Bangalore-Python-Developer-KA/1403960000/">'
            'Python Developer</a></span></td><td><span class="jobLocation"> Bangalore, KA, IN </span></td><td><span class="jobDate">Nov 20, 2026 </span></td></tr></table>')
ntt_site = FakeSite({"/search/": NTT_LIST, "/job/": '<span class="jobdescription"><p>Experience: 1-3 years</p><p>Python and Django</p></span>'})
ntt = one(cp.read_ntt_data(ntt_site))
fill = ntt.pop("_detail", None)
check("NTT DATA: id, place spelled out, date", (ntt.get("job_id"), ntt.get("location"), ntt.get("posted_time")) ==
      ("ntt:1403960000", "Bangalore, KA, India", "Nov 20, 2026") and core.location_is_eligible(ntt), str(ntt))
if fill:
    fill(ntt_site, ntt)
check("NTT DATA: the job page supplies the experience", core.parse_experience_requirement(ntt) == (1.0, 3.0), ntt.get("description", ""))

COGNIZANT_LIST = ('<div class="card card-job" data-id="00070617601"><h2 class="card-title"><a class="stretched-link js-view-job" '
                  'href="/global-en/jobs/00070617601/ai-engineer/">AI Engineer</a></h2><ul class="list-inline job-meta">'
                  '<li class="list-inline-item"> Hyderabad, Telangana, India </li><li class="list-inline-item"> Work from Office </li></ul></div>')
COGNIZANT_JOB = ('<div class="job-meta-item">Date published Oct 26 2026</div><div class="job-meta-item">Work model Hybrid</div>'
                 '<div class="cms-content"><p>2 to 4 years of experience</p><p>Python, LangChain</p></div>')
cognizant_site = FakeSite({"/global-en/jobs/?": COGNIZANT_LIST, "/global-en/jobs/0": COGNIZANT_JOB, "/global-en/jobs/": COGNIZANT_LIST})
cognizant = one(cp.read_cognizant(cognizant_site))
fill = cognizant.pop("_detail", None)
check("Cognizant: one posting from the three city searches", cognizant.get("job_id") == "cognizant:00070617601" and cognizant.get("location") == "Hyderabad, Telangana, India", str(cognizant))
if fill:
    fill(FakeSite({"/global-en/jobs/": COGNIZANT_JOB}), cognizant)
check("Cognizant: the job page supplies experience, date and work model", core.parse_experience_requirement(cognizant) == (2.0, 4.0)
      and cognizant.get("posted_time") == "Oct 26 2026" and cognizant.get("workplace_type") == "Hybrid", str(cognizant))
check("the companies asked for are either read or named as not covered",
      set(cp.PORTALS) == {"ntt", "amazon", "cognizant", "tiger", "infor", "jpmc", "capgemini"} and set(cp.NOT_COVERED) == {"EPAM", "Cimpress"})


# --- 4. a scan: what is mailed, and when ----------------------------------------------------------------
work = Path(tempfile.mkdtemp(prefix="jmr_portals_"))
cp.STATE_FILE = work / "company_portal_state.json"
core.DATA_DIR = work
core.REPORT_DIR = work / "reports"
cp.Site = lambda keywords: FakeSite({}, keywords)
sent = {"mail": [], "push": []}
outcome = {"mail": True, "push": True}
core.send_gmail = lambda subject, body, attachment=None: (sent["mail"].append((subject, body)), outcome["mail"])[1]
core.send_push_notification = lambda *a, **k: (sent["push"].append(a), outcome["push"])[1]
listings = {"acme": [], "globex": []}
opened = []


def reader(key):
    def read(site):
        if isinstance(listings[key], Exception):
            raise listings[key]
        return [dict(j) for j in listings[key]]
    return read


cp.PORTALS = {"acme": ("Acme", reader("acme")), "globex": ("Globex", reader("globex"))}


def scan(dry_run=False):
    sent["mail"].clear()
    sent["push"].clear()
    args = argparse.Namespace(candidate_yoe=2.0, min_match=60, dry_run=dry_run, company_list=list(cp.PORTALS), keyword_list=["Python"])
    return cp.run_scan(args)


fits = posting(100)
too_senior = posting(101, description=f"5+ years of experience in {STACK}")
no_years = posting(102, title="Backend Developer", description=NO_YEARS)
elsewhere = posting(103, location="Pune, Maharashtra, India")
listings["acme"] = [fits, too_senior, no_years, elsewhere]

code = scan(dry_run=True)
check("dry run: nothing sent, nothing remembered, preview written", code == 0 and not sent["mail"] and not sent["push"] and not cp.STATE_FILE.exists()
      and (core.REPORT_DIR / "company_portals_preview.html").exists(), f"{code} {sent}")

code = scan()
subject, body = sent["mail"][0] if sent["mail"] else ("", "")
check("first scan: one mail and one push", code == 0 and len(sent["mail"]) == 1 and len(sent["push"]) == 1, f"{code} {len(sent['mail'])} {len(sent['push'])}")
check("first scan: subject counts both tables", "1 new job(s) for 2 years' experience, 1 with no experience stated" in subject, subject)
check("first scan: table 1 has the 2-4 years job, table 2 the one with no years", "/job/100" in body and "/job/102" in body
      and body.index("/job/100") < body.index("2. Experience not stated") < body.index("/job/102"), body[:200])
check("first scan: the 5+ years job and the Pune job are not in the mail", "/job/101" not in body and "/job/103" not in body)
check("first scan: companies not covered are named", "EPAM is not covered" in body and "Cimpress is not covered" in body)

code = scan()
check("same postings again: no mail", code == 0 and not sent["mail"] and not sent["push"], str(sent))

listings["acme"] = [fits, too_senior, no_years, elsewhere, posting(104, title="AI Engineer", description=f"1-3 years of experience in {STACK}", company="Acme")]
code = scan()
body = sent["mail"][0][1] if sent["mail"] else ""
check("one new posting: one mail with only that one", len(sent["mail"]) == 1 and "/job/104" in body and "/job/100" not in body and "/job/102" not in body, body[:200])

listings["acme"] = listings["acme"] + [posting(105, title="AI Engineer", description=f"1-3 years of experience in {STACK}", company="Acme",
                                               location="Chennai, Tamil Nadu, India")]
code = scan()
check("the same opening re-posted for another city is not mailed again", not sent["mail"], str([s[0] for s in sent["mail"]]))

listings["acme"] = cp.PortalError("HTTP 503 from https://careers.acme.example")
listings["globex"] = [posting(200, key="globex", company="Globex", title="Full Stack Developer")]
code = scan()
body = sent["mail"][0][1] if sent["mail"] else ""
check("one site down: the other is still reported, and the mail says which failed", code == 0 and "/job/200" in body
      and "Acme could not be read this run" in body, f"{code} {body[:200]}")
check("one site down: counted", cp.load_state()["failures"].get("acme") == 1, str(cp.load_state()["failures"]))

listings["globex"] = cp.PortalError("HTTP 503 from https://careers.globex.example")
code = scan()
check("every site down: run fails, no mail", code == 1 and not sent["mail"], f"{code} {sent['mail']}")

listings["acme"] = [fits]
listings["globex"] = [posting(201, key="globex", company="Globex", description=f"2+ years of experience in {STACK}")]
outcome.update(mail=False, push=False)
code = scan()
check("mail and push both fail: run fails and the opening stays queued", code == 1 and len(cp.load_state()["queue"]) == 1, f"{code} {cp.load_state()['queue']}")
outcome.update(mail=True, push=True)
code = scan()
check("next scan delivers the queued opening once", code == 0 and len(sent["mail"]) == 1 and "/job/201" in sent["mail"][0][1] and not cp.load_state()["queue"], str(sent["mail"])[:200])
check("a site that works again resets its count", cp.load_state()["failures"] == {"acme": 0, "globex": 0}, str(cp.load_state()["failures"]))


# A posting whose text needs its own request: opened only when title and place fit, and not twice.
def with_page(record, text):
    def fill(site, job):
        opened.append(job["job_id"])
        job["description"] = text
    return dict(record, description="", _detail=fill)


listings["globex"] = [
    with_page(posting(300, key="globex", company="Globex", title="LLM Engineer"), f"2-3 years of experience in {STACK}"),
    with_page(posting(301, key="globex", company="Globex", title="QA Tester"), "anything"),
    with_page(posting(302, key="globex", company="Globex", location="Mumbai, Maharashtra, India"), "anything"),
    with_page(posting(303, key="globex", company="Globex", title="Django Developer"), ""),
]
code = scan()
check("job pages: only the posting with a fitting title and place is opened", opened == ["globex:300", "globex:303"], str(opened))
check("job pages: the opened posting is mailed", len(sent["mail"]) == 1 and "/job/300" in sent["mail"][0][1])
opened.clear()
code = scan()
check("job pages: a page that came back empty is tried again; a judged one is not", opened == ["globex:303"] and not sent["mail"], str(opened))

cp.MAX_DETAIL_PAGES = 2
opened.clear()
listings["acme"] = [with_page(posting(400 + n, title="Python Developer", company=f"Acme {n}"), f"2-4 years of experience in {STACK} Team {n}.") for n in range(5)]
listings["globex"] = []
code = scan()
check("job pages: at most MAX_DETAIL_PAGES per site per run; the mail says the rest are waiting", len(opened) == 2
      and sent["mail"] and "3 posting(s) are still to be opened" in sent["mail"][0][1], f"{opened} {[s[0] for s in sent['mail']]}")
code = scan()
check("job pages: the next run carries on", len(opened) == 4, str(opened))

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
