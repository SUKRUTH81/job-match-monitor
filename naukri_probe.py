#!/usr/bin/env python3
"""Test only: can a real browser on a GitHub runner read Naukri's public job search, with no login?

Opens one public search page the way a visitor would and reports what came back. It signs in to
nothing, solves no CAPTCHA and sends nothing. If Naukri answers with a CAPTCHA or a block page,
that is the result: this script reports it and stops.

Usage: python naukri_probe.py [--headed]
"""
from __future__ import annotations

import argparse
import json
import re

SEARCH_URL = "https://www.naukri.com/python-developer-jobs-in-hyderabad?experience=2&jobAge=1"
CARD_SELECTORS = (".srp-jobtuple-wrapper", ".cust-job-tuple", "article.jobTuple", "[data-job-id]")
BLOCK_WORDS = ("captcha", "access denied", "unusual traffic", "are you a human", "verify you are", "blocked", "forbidden")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="Open a full browser window (needs a display, e.g. xvfb-run)")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    api_answers = []

    def on_response(response) -> None:
        if "/jobapi/" not in response.url:
            return
        entry = {"url": response.url.split("?")[0], "status": response.status, "jobs": None, "body": ""}
        try:
            data = response.json()
            jobs = data.get("jobDetails") if isinstance(data, dict) else None
            if isinstance(jobs, list):
                entry["jobs"] = len(jobs)
                entry["total"] = data.get("noOfJobs")
                entry["sample"] = jobs[:2]
            else:
                entry["body"] = json.dumps(data)[:300]
        except Exception as exc:  # not JSON, or the page went away
            entry["body"] = f"({type(exc).__name__})"
        api_answers.append(entry)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata", viewport={"width": 1366, "height": 900})
        page = context.new_page()
        page.on("response", on_response)
        status = None
        try:
            answer = page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
            status = answer.status if answer else None
        except Exception as exc:
            print(f"Opening the page failed: {type(exc).__name__}: {str(exc)[:200]}")
        for _ in range(25):
            if any(a.get("jobs") for a in api_answers) or any(page.locator(s).count() for s in CARD_SELECTORS):
                break
            page.wait_for_timeout(1000)
        page.wait_for_timeout(2000)
        text = re.sub(r"\s+", " ", page.inner_text("body")) if page.locator("body").count() else ""
        cards = {s: page.locator(s).count() for s in CARD_SELECTORS}
        print(f"mode: {'headed' if args.headed else 'headless'}")
        print(f"browser: {page.evaluate('navigator.userAgent')}")
        print(f"page: HTTP {status}, final address {page.url.split('?')[0]}, title {page.title()!r}")
        print(f"job cards on the page: {cards}")
        print(f"block words on the page: {[w for w in BLOCK_WORDS if w in text.lower()]}")
        print(f"page text starts: {text[:300]!r}")
        print(f"search-service answers: {len(api_answers)}")
        for entry in api_answers:
            print(f"  {entry['url']}: HTTP {entry['status']}, jobs {entry.get('jobs')}, total {entry.get('total')} {entry['body']}")
        listed = next((a for a in api_answers if a.get("jobs")), None)
        if listed:
            first = listed["sample"][0]
            print("job fields:", sorted(first))
            for job in listed["sample"]:
                print("sample job:", json.dumps({k: job.get(k) for k in (
                    "jobId", "title", "companyName", "placeholders", "tagsAndSkills", "footerPlaceholderLabel",
                    "createdDate", "jdURL", "experienceText", "minimumExperience", "maximumExperience")}, ensure_ascii=True)[:900])
                print("sample description:", re.sub(r"\s+", " ", str(job.get("jobDescription") or ""))[:400].encode("ascii", "replace").decode())
        elif any(cards.values()):
            selector = next(s for s, n in cards.items() if n)
            print("first card text:", re.sub(r"\s+", " ", page.locator(selector).first.inner_text())[:500].encode("ascii", "replace").decode())
        readable = bool(listed) or any(cards.values())
        print("RESULT:", "readable" if readable else "not readable")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
