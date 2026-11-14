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

## Schedule

The workflow is configured with:

```yaml
on:
  schedule:
    - cron: '*/5 * * * *'
```

GitHub's documented minimum scheduled interval is five minutes. Scheduled runs are best-effort: GitHub can delay or drop a scheduled run during periods of high load. The workflow runs on the default branch only. Keep the repository active; scheduled workflows in public repositories can be disabled after 60 days without repository activity.

Each run should search the previous 24 hours to apply the requested posting-age window, then deduplicates by job ID. The workflow uses a concurrency group to prevent simultaneous runs; if a scan takes longer than five minutes, later runs can wait in the queue. Do not assume exact five-minute delivery.

## Free-tier cost reality

Five-minute scheduling creates up to 8,640 runs in a 30-day month. GitHub Free includes 2,000 minutes/month for private repositories; each private-repo run is billed with a minimum rounded-up minute. That cadence will exceed the private-repository free allowance. Standard GitHub-hosted runners are free for public repositories, but a public repository exposes the source code and candidate skill profile. Do not put secrets, personal contact details, resumes, or private job-search data in a public repository. See GitHub's official billing docs: <https://docs.github.com/en/actions/concepts/billing-and-usage>.

If you require a private repository and zero cost, use a self-hosted runner on a computer that remains online; that does not meet the requirement that the computer can be off. Otherwise reduce the cadence or accept possible paid usage.

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

## Permissions and secrets

No Gmail credentials or custom personal access token are required. GitHub Actions supplies `GITHUB_TOKEN`; the workflow grants it only `issues: write` and `contents: read`.

If you wish to route LinkedIn requests through a dedicated proxy, add your proxy URL to GitHub repository secrets (**Settings → Secrets and variables → Actions → New repository secret**) with name `LINKEDIN_PROXY` (e.g. `http://user:pass@proxy.example.com:8080` or `socks5://...`). When set, the workflow automatically routes all LinkedIn guest API queries and JD fetching through the proxy.

## Search preferences

- **India locations:** Hyderabad, Chennai, and Bengaluru. Onsite, hybrid, and remote arrangements are all acceptable in these cities.
- **Remote outside these cities:** Include remote roles explicitly open to applicants in India, plus genuinely worldwide/global remote roles. Exclude remote jobs restricted to another country or region.
- **Experience:** The candidate has 2+ years. Include roles that say 2+ years, 2 years, or a stated range containing 2 years (for example, 1–3 or 2–4 years). Include postings that explicitly accept any experience level or explicitly state that 2 years may be considered. Do not reject a role merely because the top of its stated range is above 2. Exclude a strict minimum above 2 when no flexibility is stated. If experience is not specified, do not exclude solely for that reason.
- Keep the 24-hour posting-age rule, verifiable-posting-time requirement, 60% skill threshold for alerts, and Naukri exclusion from the standing search criteria.

## Candidate profile source

The matching skills in `job_match_runner.py` are based on the resume attached in this conversation. The profile uses the resume's stated **2+ years** and its listed Python/Django/React, AI/LLM, backend, database, security, and DevOps skills. Search eligibility now accepts any experience band that includes 2 years (such as 1–3), and any explicitly flexible/all-levels posting. The resume PDF and personal contact details are not included in this package.

## Matching formula

`match % = distinct candidate technical skills detected in the job / all distinct technical skills detected in the job × 100`

This is a transparent keyword-overlap score, not an LLM judgement or a guarantee of interview fit. Missing/unknown job requirements must not be invented.

## Local syntax check

```bash
python -m py_compile job_match_runner.py linkedin_scraper.py
```
