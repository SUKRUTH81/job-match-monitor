# LinkedIn Job Match Monitor — GitHub Actions

Every hour this searches LinkedIn for jobs posted in the last hour, keeps the ones that fit the candidate (location, years of experience, full-time, Python developer role), scores them against the candidate's skills, and sends one email. New 60%+ matches also get a GitHub Issue and a phone push.

## How a run works

1. **Timer.** `hourly-timer.yml` starts a run on the hour (see Schedule below).
2. **Search workers.** Five parallel jobs search LinkedIn (Hyderabad, Bengaluru, Chennai, and two for India-remote) and read the description of every job whose title and location could fit.
3. **Report.** One job merges the workers' results, applies the filters, scores, removes duplicates and already-reported openings, and sends the email, issue and push.

The filters are tested offline by `tests/` (run `python tests/test_experience.py`, `python tests/test_role_fit.py`, `python tests/test_pipeline.py`, `python tests/test_fast_alert.py`); the Tests workflow runs them on every change.

## Hyderabad fast alert

Beside the hourly report, `hyderabad-fast-alert.yml` scans Hyderabad **every 5 minutes** and notifies at once, so a good opening is seen minutes after it is posted instead of up to an hour later.

An alert goes out when a new opening is all of:

- in **Hyderabad**, whatever the work mode (on-site, hybrid or remote);
- **full-time**;
- a skill match of **80% or more**;
- asking for experience that **includes your years** (2 by default: 0-2, 1-3, 2-4, 2+ ...). A posting that states no experience never starts an alert.

It must also pass every filter of the hourly report (target title, Python developer role).

What you get: a phone push (ntfy, urgent priority, opens the job), a mail named **`Hyderabad (<work mode>) jobs apply fast`** to the same recipients as the hourly mail, and a GitHub Issue. The mail and the issue hold two tables:

1. **Apply now** - the openings that met every rule above.
2. **Also qualified, but the posting states no experience** - Hyderabad, full-time, 80%+, with no years stated. These never start an alert by themselves; they are listed with the next alert, if one goes out within 24 hours of their posting.

The work mode in the mail name and in the tables is read from the posting's own words ("Work mode: Hybrid", "work from office", "(Remote)" in the title). LinkedIn's public pages have no work-mode field, so when the posting does not say, the alert says `work mode not stated`.

An opening is alerted once: the watcher remembers what it sent (also when the same opening is re-posted under a new LinkedIn id). The hourly report is unchanged and still lists these jobs with all the others.

**How it keeps running.** Each run scans for 25 minutes (6 scans) and then starts the next run, so one "Hyderabad Fast Alert" run is always in progress, each on a fresh runner. `timer-watchdog.yml` restarts it if it ever stops. To stop the alerts, disable the **Hyderabad Fast Alert** workflow on the Actions tab; to start them again, enable it and run it once. `INTERVAL_MINUTES` in the workflow and the `watch_minutes` input set the pace; the `min_match` and `candidate_yoe` inputs set the rule.

To see what an alert would look like without sending anything: `python job_match_runner.py --fast-alert --source guest --dry-run` (writes `reports/hyderabad_alert_preview.html`).

## Notification behavior

- **Every run sends one email** through Gmail SMTP (secrets `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, optional `JOB_ALERT_TO`): the match report, a "nothing new" mail, a "no eligible jobs" mail, or a failure mail.
- A new GitHub Issue is created only when at least one newly discovered job scores at or above 60%.
- The issue is assigned to the repository owner, which triggers a GitHub assignment notification. Check GitHub notification settings and watch the repository for Issues.
- Issue table 1: new jobs scoring 60%+, sorted by match percentage descending.
- Issue table 2: all newly discovered jobs in that run, including below-60% jobs, sorted descending.
- Every row includes a direct application link, score, location/work mode, experience estimate, matched skills, and other detected technical skills.
- An Excel workbook is uploaded as a workflow artifact with `60%+ Matches` and `All Scraped` sheets.

## Target Job Roles, Strict Title & Full-Time Filtering

The monitor strictly searches for and filters by the candidate's target job titles and full-time employment:
- **AI Engineer**: AI Engineer, GenAI Engineer, LLM Engineer, Machine Learning Engineer, Artificial Intelligence Engineer.
- **Software Engineer**: Software Engineer, Software Developer, SDE (I/II/III), Member of Technical Staff.
- **Python Backend**: Python Backend Developer, Backend Engineer (Python).
- **Python Full Stack**: Python Full Stack Developer, Full Stack Engineer (Python).
- **Python Jobs**: Python Developer, Python Programmer, Django/FastAPI Developer.

**Strict Filtering Rules:**
- **Full-Time Only**: Internships, trainees, apprentices, contractors, freelancers, part-time, and task evaluators/raters are strictly excluded.
- **Location Restrictions**: Strictly **Hyderabad**, **Bengaluru/Bangalore**, and **Chennai** (any work mode: Onsite, In-Office, Hybrid, Remote) and **India Remote**. Worldwide remote, international remote, and onsite/hybrid roles in other cities (Pune, Noida, Gurgaon, Mumbai) are strictly excluded.
- **Non-Software Roles Excluded**: QA, testing (SDET, QA Engineer), system performance, hardware, and administrative roles are excluded.

## Schedule

The job monitor runs **every hour at :00 IST** (minute 30 UTC), day and night.

GitHub's `schedule` cron is best-effort and was skipping most hourly slots, so the monitor is not started by a cron. Instead:

- **`.github/workflows/hourly-timer.yml`** keeps time. Each run waits 55 minutes in the `hourly-timer` environment (a wait timer, which uses no runner minutes), sleeps until minute 30 UTC, starts the job monitor, and then starts the next timer run. There is always one "Hourly Timer" run in the *Waiting* state - that is normal.
- **`.github/workflows/timer-watchdog.yml`** is a safety net. If the timer chain ever stops, it starts the monitor once and restarts the chain.

To start the chain by hand, run the **Timer Watchdog** workflow from the Actions tab. To stop the hourly runs, cancel the waiting **Hourly Timer** run and disable the **Timer Watchdog** workflow. To change the minute, edit `TARGET_MINUTE` in `hourly-timer.yml`.

Each run scans jobs posted within the **last 1 hour** (`--hours 1`), deduplicates against previously seen job IDs, creates a GitHub Issue when matches reach 60%+, and optionally emails the HTML report with the attached Excel workbook to **`sukruth1435@gmail.com`** and **`akhilarohithayepuri@gmail.com`**.

## Email notifications (Gmail)

To enable email delivery to `sukruth1435@gmail.com` and `akhilarohithayepuri@gmail.com`:
1. In your GitHub repository, go to **Settings → Secrets and variables → Actions → New repository secret**.
2. Add:
   - `GMAIL_ADDRESS`: your Gmail address (e.g. `yourname@gmail.com`).
   - `GMAIL_APP_PASSWORD`: a 16-character Google App Password (generated at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)).
   - *(Optional)* `JOB_ALERT_TO`: Defaults to `sukruth1435@gmail.com, akhilarohithayepuri@gmail.com`.
3. If these secrets are not configured, the runner will still generate the Excel artifact and create the assigned GitHub Issue without errors.

## LinkedIn input and automation boundary

This repository is **LinkedIn-only**. `job_match_runner.py` includes a built-in **Live LinkedIn Guest-Endpoint Scraper** with anti-blocking behavior, modern browser emulation, exponential backoff with jitter, proxy routing, and dynamic freshness filtering (`f_TPR=r{seconds}`). It can also ingest records from `data/linkedin_jobs.json` when available.

### Live Scraping & Anti-Blocking Features

- **Guest-Endpoint Search:** Queries `https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search` directly without needing LinkedIn login credentials.
- **Configurable Freshness:** Supports filtering jobs posted within any number of hours (e.g. `--hours 1` for past 1 hour, `--hours 24` for past 24 hours) using dynamic LinkedIn `f_TPR=r{seconds}` query parameters combined with strict client-side timestamp verification.
- **Anti-Blocking Engine:** Rotates modern desktop User-Agents (Windows/macOS Chrome, Edge, Safari, Firefox) with realistic Sec-Ch-Ua, Accept-Language, and Sec-Fetch headers.
- **Exponential Backoff:** Automatically handles HTTP 429 (Rate Limit) and HTTP 403 responses by parsing the `Retry-After` header and backing off with randomized jitter.
- **Proxy Support:** Supports HTTP, HTTPS, and SOCKS5 proxies via `--proxy` or environment variables `LINKEDIN_PROXY`, `HTTPS_PROXY`, `HTTP_PROXY`.
- **Async High-Speed JD Fetching:** Leverages `aiohttp` with bounded semaphore concurrency (5 workers) to fetch full job descriptions, criteria, and applicants within seconds.
- **Dual-Mode Intake (`--source`):**
  - `auto` (default): Runs the live scraper when no local file records exist; seamlessly merges local JSON records if present.
  - `guest`: Scrapes live LinkedIn guest endpoints directly.
  - `file`: Only reads from local `data/linkedin_jobs.json`.

## Add LinkedIn job records

1. In the repository root, create `data/linkedin_jobs.json` (the example file is `data/linkedin_jobs.example.json`).
2. Add records using this shape:

```json
{
  "jobs": [
    {
      "job_id": "4488888888",
      "job_url": "https://www.linkedin.com/jobs/view/4488888888/",
      "title": "Full Stack Python Developer",
      "company": "Example Company",
      "location": "Hyderabad, Telangana, India",
      "workplace_type": "Hybrid",
      "description": "Python, Django, React, PostgreSQL, REST APIs; 1-3 years of experience.",
      "posted_iso": "2026-11-14T16:00:00+05:30"
    }
  ]
}
```

Use a timezone-aware `posted_iso` timestamp when available. Alternatively, include `posted_time` such as `35 minutes ago` together with `scraped_at` as a timezone-aware ISO timestamp. The age is recalculated from the capture time on every run. Do not use a date-only value or an unanchored relative label. The example values are placeholders, not real vacancies.

Committing `data/linkedin_jobs.json` to the default branch triggers a run. The scheduled workflow also runs at its configured interval, but it does not refresh the input data from LinkedIn.

## Deploy

1. Create a repository on GitHub. For free five-minute runs using GitHub-hosted runners, a **public** repository is the zero-minute-cost option; remember that the code and candidate skill list become public. A private repository is safer for personal information but the 5-minute schedule will exceed the included free minutes.
2. Upload the package contents to the repository root. Keep the workflow path exactly `.github/workflows/hourly-job-monitor.yml`.
3. Open **Settings → Actions → General** and ensure Actions are enabled. Under **Workflow permissions**, allow the workflow to read and write repository contents only if your settings require it; the workflow itself declares `issues: write` and `contents: read`.
4. Ensure Issues are enabled: **Settings → General → Features → Issues**.
5. Create and populate `data/linkedin_jobs.json` using the schema above. The workflow accepts LinkedIn URLs only; it does not automatically scrape LinkedIn.
6. Commit the workflow and input JSON to the repository's **default branch**. Scheduled workflows only run from the default branch.
7. Go to **Actions → Job Match Monitor → Run workflow** to process the current JSON input manually.
8. Go to **Settings → Notifications** in GitHub and enable issue assignment / repository notifications. Watch the repository (or configure custom notifications for Issues) so assigned job-alert issues reach your GitHub inbox/mobile app.
9. Check **Actions → Job Match Monitor** to inspect run logs. Download the Excel workbook from a successful run's **Artifacts** section.

## Notifications (Push, Email & Issues)

1. **Instant Phone Push Notifications (ntfy.sh):**
   - Free, zero-setup, instant lock-screen push alerts with sound.
   - Install the free **ntfy** app from Google Play Store or Apple App Store (or open [https://ntfy.sh/sukruth_job_alerts](https://ntfy.sh/sukruth_job_alerts) in any browser).
   - Subscribe to topic: `sukruth_job_alerts`.
   - Whenever matching roles are found, your phone buzzes with an instant notification containing the job title, company, match score, Easy Apply tag, and clickable link!
2. **GitHub Mobile Push Notifications:**
   - Install the GitHub Mobile app on your phone and enable notifications.
   - Every issue automatically mentions and assigns `@SUKRUTH81`, delivering an immediate push alert.
3. **Email Notifications:**
   - GitHub automatically sends notification emails from `notifications@github.com` (found under the **Updates** tab or **Spam** folder).
   - Direct Gmail SMTP delivery is used when the `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` secrets are set. **Every run sends one email**, whatever it finds: the match report when there are eligible jobs, a short "no eligible jobs" mail listing what was scanned and why each job was filtered out when there are none, and a "scan FAILED" mail if the run crashes. A missing hourly mail therefore means the run did not start. If Gmail refuses the mail three times in a row, the run is marked failed (red) on the Actions tab.

## Permissions and secrets

No Gmail credentials or custom personal access token are required. GitHub Actions supplies `GITHUB_TOKEN`; the workflow grants it only `issues: write` and `contents: read`.

If you wish to route LinkedIn requests through a dedicated proxy, add your proxy URL to GitHub repository secrets (**Settings → Secrets and variables → Actions → New repository secret**) with name `LINKEDIN_PROXY` (e.g. `http://user:pass@proxy.example.com:8080` or `socks5://...`). When set, the workflow automatically routes all LinkedIn guest API queries and JD fetching through the proxy.

## Search preferences

- **Locations:** Strictly Hyderabad, Bengaluru ("bgl"), Chennai (all work modes: Onsite, In-Office, Hybrid, Remote) and India Remote. Worldwide remote and other cities (Pune, Noida, Mumbai) are strictly excluded.
- **Employment type:** Strictly full-time jobs only (excludes internships, trainees, contractors, evaluators, and part-time).
- **Apply type:** every search is run twice, once for Easy Apply (`f_AL=true`) and once for everything else, and both passes read all results. Each row is labelled Easy Apply or Normal Apply.
- **Experience (YOE):** A job is kept when the candidate's 2 years fall inside the experience the posting asks for. A range is accepted when it contains 2 (0-2, 1-2, 0-3, 1-3, 2-4, 2-10); a minimum is accepted when it is 2 or less (1+, 2+); an upper limit is accepted when it is 2 or more. Everything else is excluded (0-1 fresher only, 3-5, 3+, 4+, 5+). A figure in the job title wins over the description; "preferred" figures do not count when a required one exists; company age, contract length, notice period and similar durations are ignored. A job that states no experience is kept. A job whose description LinkedIn did not return cannot be checked, so it is not counted as a match and is listed separately in the email as "Not checked". Senior titles (Staff, Principal, Architect, Director, VP, SDE 3/4/5) are excluded.
- **Posting age:** Strictly jobs posted within 1 hour (<= 1.0h freshness window).
- **Search:** each keyword (AI Engineer, Generative AI, LLM Engineer, Software Engineer, Python Developer, Django, Full Stack Developer, Backend Developer) is searched separately in Hyderabad, Bengaluru, Chennai and India-remote (`SEARCH_TERMS` and `SEARCH_LOCATIONS` in `job_match_runner.py`).
- **Role fit (Python developer roles only):** a posting is removed when (1) its title names another stack or job family (Java, Kotlin, .NET, C++, Go, Node.js, Angular, mobile, Salesforce/SAP, embedded, DevOps, data engineering, marketing, support, ...), (2) the posting never names Python or a Python-only library such as Django, FastAPI or LangChain (on hand-checked samples, postings that talk about AI but never name Python were Node, DevOps, C or product-manager jobs six times out of seven), (3) its description asks for another programming language outright, or its frameworks are another language's (Spring, Hibernate, ASP.NET), or (4) the description itself says the job is something else (data pipelines only, an ERP platform throughout, "seeking a Product Manager"). Titles with no developer word (consultant, analyst, specialist, scientist, researcher) are removed. A language that is only offered as an alternative ("Python, Java or Go") or only listed under nice-to-have does not remove the posting. Removed postings are listed at the bottom of the email with the reason (`role_mismatch` in `job_match_runner.py`).
- **Skill score:** skills the candidate has divided by the skills the posting asks for. Skills that appear only in the company blurb are ignored, nice-to-have skills count half, a language offered only as an alternative to Python is not counted as missing, and a posting that names fewer than 5 skills is scored out of 5 (so "Azure, Git, SQL" is 60%, not 100%).
- **Duplicates:** one opening posted several times (once per city, or re-posted under a new LinkedIn id) is shown once, with the other cities named in the Location column, and is not reported again in a later hour.
- **Parallel search workers:** on GitHub the search runs as five parallel jobs (one per city, two for India-remote), each on its own runner, so LinkedIn's rate limit is spread over five addresses. Each worker hands over its jobs and the list of searches it covered. The report job merges them and runs any search no worker covered itself, saying so in the email, so a failed worker delays the report but never shrinks it.
- **Senior titles:** a Senior / Sr / Lead title is excluded when the posting states no years; when it states years, the figure decides.
- **Target roles:** AI Engineer, Software Engineer, Python Backend, Python Full Stack, Python Developer, and Python engineering jobs.

## Candidate profile source

The matching skills in `job_match_runner.py` are based on the resume attached in this conversation. The profile uses the resume's stated **2+ years** and its listed Python/Django/React, AI/LLM, backend, database, security, and DevOps skills. Search eligibility now accepts any experience band that includes 2 years (such as 1–3), and any explicitly flexible/all-levels posting. The resume PDF and personal contact details are not included in this package.

## Matching formula

`match % = distinct candidate technical skills detected in the job / all distinct technical skills detected in the job × 100`

This is a transparent keyword-overlap score, not an LLM judgement or a guarantee of interview fit. Missing/unknown job requirements must not be invented.

## Local syntax check

```bash
python -m py_compile job_match_runner.py linkedin_scraper.py
```
