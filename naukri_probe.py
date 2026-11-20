#!/usr/bin/env python3
"""Test only: how Naukri's public search answers a real browser on a GitHub runner, with no login.

Opens a few public search pages the way a visitor would and reports what came back: how many jobs,
in what order, and which address the page itself asked for. It signs in to nothing, solves no
CAPTCHA and sends nothing.

Usage: xvfb-run -a python naukri_probe.py
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

BASE = "https://www.naukri.com/"
PAGES = [
    ("default order", "python-developer-jobs-in-bangalore?experience=2&jobAge=1"),
    ("sort=f", "python-developer-jobs-in-bangalore?experience=2&jobAge=1&sort=f"),
    ("page 2", "software-engineer-jobs-in-bangalore-2?experience=2&jobAge=1"),
    ("remote", "python-developer-jobs?experience=2&jobAge=1&wfhType=2"),
]


def main() -> int:
    from playwright.sync_api import sync_playwright

    answers = []

    def on_response(response) -> None:
        if "/jobapi/v3/search" not in response.url:
            return
        try:
            answers.append((response.url, response.status, response.json()))
        except Exception as exc:
            answers.append((response.url, response.status, {"error": type(exc).__name__}))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata", viewport={"width": 1366, "height": 900}).new_page()
        page.on("response", on_response)
        for name, path in PAGES:
            answers.clear()
            try:
                answer = page.goto(BASE + path, wait_until="domcontentloaded", timeout=60000)
                status = answer.status if answer else None
            except Exception as exc:
                print(f"\n## {name}: opening failed: {type(exc).__name__}")
                continue
            for _ in range(20):
                if answers:
                    break
                page.wait_for_timeout(1000)
            page.wait_for_timeout(1500)
            print(f"\n## {name}: page HTTP {status}, title {page.title()[:90]!r}, final {page.url[:110]}")
            for url, code, data in answers:
                jobs = data.get("jobDetails") or [] if isinstance(data, dict) else []
                print(f"  asked: {urlsplit(url).query[:420]}")
                print(f"  HTTP {code}, total {data.get('noOfJobs') if isinstance(data, dict) else '-'}, on this page {len(jobs)}")
                stamps = [int(j.get("createdDate") or 0) for j in jobs]
                print("  newest first:", stamps == sorted(stamps, reverse=True), "| posted:",
                      [datetime.fromtimestamp(s / 1000, tz=timezone.utc).strftime("%d %H:%M") for s in stamps[:8]])
                places = [next((x.get("label") for x in j.get("placeholders") or [] if x.get("type") == "location"), "") for j in jobs]
                print("  places:", places[:8])
                print("  ranges:", [j.get("experienceText") for j in jobs[:8]], "| labels:", sorted({str(j.get("footerPlaceholderLabel")) for j in jobs}))
                lengths = [len(re.sub(r"<[^>]+>", " ", str(j.get("jobDescription") or ""))) for j in jobs]
                print("  description length (chars): min", min(lengths or [0]), "max", max(lengths or [0]))
                if not jobs:
                    print("  body:", json.dumps(data)[:300])
            page.wait_for_timeout(2500)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
