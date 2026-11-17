# LinkedIn Job Match Monitor — GitHub Actions

This package scores collected jobs against the candidate skill profile, keeps a seen-job history, creates a GitHub Issue for newly discovered 60%+ matches, and creates an Excel workbook with two sheets.

## Notification behavior

- **No Gmail, SMTP, Telegram, or external notification service is used.**
- A new GitHub Issue is created only when at least one newly discovered job scores at or above 60%.
- The issue is assigned to the GitHub user who last modified the scheduled workflow, which normally triggers an assignment notification. Check GitHub notification settings and watch the repository for Issues.
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
- **Apply type ratio:** 80% Easy Apply (`f_AL=true`) and 20% Normal Apply jobs.
- **Experience (YOE):** A job is kept when the candidate's 2 years fall inside the experience the posting asks for. A range is accepted when it contains 2 (0-2, 1-2, 0-3, 1-3, 2-4, 2-10); a minimum is accepted when it is 2 or less (1+, 2+); an upper limit is accepted when it is 2 or more. Everything else is excluded (0-1 fresher only, 3-5, 3+, 4+, 5+). A figure in the job title wins over the description; "preferred" figures do not count when a required one exists; company age, contract length, notice period and similar durations are ignored. A job that states no experience is kept. A job whose description LinkedIn did not return cannot be checked, so it is not counted as a match and is listed separately in the email as "Not checked". Senior titles (Staff, Principal, Architect, Director, VP, SDE 3/4/5) are excluded.
- **Posting age:** Strictly jobs posted within 1 hour (<= 1.0h freshness window).
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
