#!/usr/bin/env python3
"""Scheduled job collector, skills-overlap scorer, and GitHub Issue notifications.

The runner scores collected jobs, keeps a seen-job history, writes a two-sheet
Excel report, and creates an assigned GitHub Issue for new 60%+ matches.
Features a built-in LinkedIn Guest-Endpoint Scraper with anti-blocking behavior,
rotating browser headers, exponential backoff with jitter, proxy support, and
configurable hours / posted-before freshness filter (e.g., 1 hour ago).
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import random
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple
from urllib.parse import quote_plus, urlparse
from zoneinfo import ZoneInfo

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    requests = None
    BeautifulSoup = None

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False


LOG = logging.getLogger("job_match_runner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
STATE_FILE = DATA_DIR / "seen_job_ids.json"
REPORT_DIR = ROOT / "reports"

# ---------------------------------------------------------------------------
# Anti-Blocking & Browser Emulation
# ---------------------------------------------------------------------------
USER_AGENTS: List[str] = [
    # Chrome Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Chrome macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Edge Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Safari macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    # Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
]


def _get_browser_headers() -> Dict[str, str]:
    """Generates realistic modern browser headers for anti-blocking."""
    ua = random.choice(USER_AGENTS)
    is_windows = "Windows" in ua
    platform = '"Windows"' if is_windows else ('"macOS"' if "Macintosh" in ua else '"Linux"')
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


class LinkedInBackoff:
    """Manages exponential backoff with jitter and Retry-After header parsing."""
    def __init__(self, base_delay: float = 3.0, max_delay: float = 60.0, max_retries: int = 5):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.failures = 0

    def wait_seconds(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after:
            try:
                val = float(retry_after)
                if 0 < val <= self.max_delay:
                    return val + random.uniform(0.5, 1.5)
            except ValueError:
                pass
        w = min(self.max_delay, self.base_delay * (2 ** attempt))
        return w + random.uniform(1.0, 3.0)

    def record_failure(self) -> None:
        self.failures += 1

    def reset(self) -> None:
        self.failures = 0

    @property
    def exhausted(self) -> bool:
        return self.failures >= self.max_retries


def _resolve_proxy(proxy_arg: str | None = None) -> str | None:
    """Resolves proxy configuration from arguments or standard environment variables."""
    p = (
        proxy_arg
        or os.environ.get("LINKEDIN_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("ALL_PROXY")
    )
    if not p or not p.strip():
        return None
    p = p.strip()
    # Guard against dummy or invalid placeholder strings
    if "host:port" in p or "example.com" in p or p.lower() in ("none", "null", "false", "undefined", "empty"):
        return None
    if not (p.startswith("http://") or p.startswith("https://") or p.startswith("socks5://")):
        p = f"http://{p}"
    return p


# ---------------------------------------------------------------------------
# Candidate Profile & Skill Vocabulary
# ---------------------------------------------------------------------------
# Based on the candidate profile supplied for this job-search workflow.
# Canonical names are used for scoring; aliases are normalised below.
CANDIDATE_SKILLS = {
    # Languages and application development
    "python", "javascript", "sql", "django", "django rest framework", "fastapi",
    "rest api", "react", "html5", "css3", "responsive ui",
    # AI / LLM engineering (as stated in the uploaded resume)
    "azure openai", "openai api", "gpt-4o", "langchain", "langgraph",
    "ai agents", "crewai", "llamaindex", "prompt engineering", "function calling",
    "structured outputs", "rag", "embedding search", "vector search", "pgvector",
    "faiss", "azure ai search", "document chunking", "fine-tuning",
    # Backend, data, and asynchronous processing
    "microservices", "websockets", "rabbitmq", "celery", "postgresql",
    "sql server", "mongodb", "redis",
    # Security, delivery, and engineering tools
    "jwt", "rbac", "session management", "multi-tenant", "api security",
    "burp suite", "docker", "nginx", "ci/cd", "git", "github", "postman",
    "power bi",
}

# Job-description vocabulary. A skill is scored once even if repeated many times.
SKILL_PATTERNS: Dict[str, Tuple[str, ...]] = {
    "python": (r"\bpython\b",),
    "azure openai": (r"\bazure openai\b",),
    "openai api": (r"\bopenai api\b", r"\bopenai platform api\b"),
    "gpt-4o": (r"\bgpt[- ]?4o\b",),
    "langchain": (r"\blangchain\b",),
    "langgraph": (r"\blanggraph\b",),
    "ai agents": (r"\bai agents?\b", r"\bagentic ai\b"),
    "crewai": (r"\bcrewai\b",),
    "llamaindex": (r"\bllamaindex\b", r"\blamaindex\b"),
    "prompt engineering": (r"\bprompt engineering\b",),
    "function calling": (r"\bfunction calling\b", r"\btool calling\b"),
    "structured outputs": (r"\bstructured outputs?\b",),
    "pgvector": (r"\bpgvector\b",),
    "faiss": (r"\bfaiss\b",),
    "azure ai search": (r"\bazure ai search\b",),
    "document chunking": (r"\bdocument chunking\b", r"\bchunking\b"),
    "fine-tuning": (r"\bfine[- ]tuning\b", r"\bfine tuned\b"),
    "html5": (r"\bhtml5?\b",),
    "css3": (r"\bcss3?\b",),
    "responsive ui": (r"\bresponsive (?:ui|user interface|web design)\b",),
    "postman": (r"\bpostman\b",),
    "power bi": (r"\bpower bi\b",),
    "github": (r"\bgithub\b",),
    "django": (r"\bdjango\b",),
    "django rest framework": (r"\bdjango rest framework\b", r"\bdrf\b"),
    "fastapi": (r"\bfastapi\b",),
    "flask": (r"\bflask\b",),
    "javascript": (r"\bjavascript\b",),
    "typescript": (r"\btypescript\b",),
    "react": (r"\breact(?:\.js)?\b",),
    "node.js": (r"\bnode(?:\.js)?\b",),
    "rest api": (r"\brest(?:ful)? apis?\b", r"\brest api development\b"),
    "graphql": (r"\bgraphql\b",),
    "sql": (r"\bsql\b",),
    "postgresql": (r"\bpostgres(?:ql)?\b",),
    "sql server": (r"\bsql server\b", r"\bmssql\b"),
    "mysql": (r"\bmysql\b",),
    "mongodb": (r"\bmongodb\b",),
    "redis": (r"\bredis\b",),
    "rabbitmq": (r"\brabbitmq\b",),
    "celery": (r"\bcelery\b",),
    "docker": (r"\bdocker\b",),
    "kubernetes": (r"\bkubernetes\b", r"\bk8s\b"),
    "nginx": (r"\bnginx\b",),
    "ci/cd": (r"\bci\s*/\s*cd\b", r"\bcontinuous integration\b", r"\bcontinuous deployment\b"),
    "git": (r"\bgit\b", r"\bgithub\b"),
    "aws": (r"\baws\b", r"\bamazon web services\b"),
    "azure": (r"\bazure\b",),
    "gcp": (r"\bgcp\b", r"\bgoogle cloud platform\b"),
    "jwt": (r"\bjwt\b", r"\bjson web tokens?\b"),
    "rbac": (r"\brbac\b", r"\brole[- ]based access control\b"),
    "session management": (r"\bsession management\b",),
    "multi-tenant": (r"\bmulti[- ]tenant\b",),
    "api security": (r"\bapi security\b", r"\bapi authentication\b", r"\bapi authorization\b"),
    "burp suite": (r"\bburp suite\b",),
    "rag": (r"\brag\b", r"\bretrieval[- ]augmented generation\b"),
    "embedding search": (r"\bembedding(?:s)?\b", r"\bsemantic search\b"),
    "vector search": (r"\bvector search\b", r"\bvector database\b", r"\bvector db\b"),
    "llm": (r"\bllms?\b", r"\blarge language models?\b"),
    "langchain": (r"\blangchain\b",),
    "websockets": (r"\bwebsockets?\b",),
    "microservices": (r"\bmicroservices?\b",),
    "linux": (r"\blinux\b",),
    "kafka": (r"\bkafka\b",),
    "pandas": (r"\bpandas\b",),
    "pytorch": (r"\bpytorch\b",),
    "tensorflow": (r"\btensorflow\b",),
}

# Current search preferences supplied by the candidate:
# Strictly: Hyderabad, Bengaluru/Bangalore, Chennai (any work mode: onsite, hybrid, in-office, remote) and India Remote.
# Worldwide remote and other cities (e.g. Pune, Noida, Gurgaon, Mumbai) are strictly excluded.
TARGET_CITIES = (
    "hyderabad", "secunderabad",
    "bengaluru", "bangalore", "bgl",
    "chennai", "madras",
)
ALLOWED_LOCAL_WORK_MODES = {"onsite", "on-site", "in-office", "inoffice", "hybrid", "remote"}

REMOTE_INDIA_PATTERNS = (
    r"\bindia\b", r"\bindia[- ]eligible\b", r"\bopen to applicants in india\b",
)
REMOTE_ONLY_OUTSIDE_INDIA_PATTERNS = (
    r"\b(?:united states|usa|us only|canada only|uk only|united kingdom only|"
    r"australia only|europe only|emea only|germany only|france only|singapore only)\b",
)

SEARCH_TERMS = [
    "AI Engineer",
    "Software Engineer",
    "Python Backend",
    "Python Full Stack",
    "Python Developer",
    "Python",
]

# Target role titles explicitly requested by candidate:
# "ai engineer, software engineer, python backend, python full stack, python jobs only"
ALLOWED_TITLE_PATTERNS = (
    # AI Engineer & GenAI
    r"\b(?:ai|a\.i\.|artificial intelligence|genai|gen ai|generative ai|llm|machine learning|ml)\b",
    # Software Engineer & SDE
    r"\bsoftware\s+(?:development\s+)?engineer\b",
    r"\bsoftware\s+developer\b",
    r"\bsde\b",
    r"\bsde[- ]?[123iIvV]+\b",
    r"\bmember of technical staff\b",
    r"\bmts\b",
    # Python Backend
    r"\b(?:backend|back[- ]end)\b",
    # Python Full Stack
    r"\b(?:full[- ]stack|fullstack)\b",
    # Python jobs
    r"\bpython\b",
    r"\bdjango\b",
    r"\bfastapi\b",
    r"\bflask\b",
)

EXCLUDED_TITLE_KEYWORDS = (
    r"\bqa\b",
    r"\bquality assurance\b",
    r"\btester\b",
    r"\btesting\b",
    r"\btest engineer\b",
    r"\bsdet\b",
    r"\bsystem performance\b",
    r"\bperformance engineer\b",
    r"\bhardware\b",
    r"\bfpga\b",
    r"\basic\b",
    r"\bmechanical\b",
    r"\bcivil\b",
    r"\belectrical\b",
    r"\bnetwork engineer\b",
    r"\bsalesforce\b",
    r"\bsap\b",
    r"\bscrum master\b",
    r"\bproduct manager\b",
    r"\bproject manager\b",
    r"\btechnical writer\b",
    r"\brecruiter\b",
    r"\bhr\b",
    # Full-time only: exclude internships, trainees, contractors, evaluators, part-time
    r"\bintern\b",
    r"\binternship\b",
    r"\binterns\b",
    r"\btrainee\b",
    r"\bapprentice\b",
    r"\bpart[- ]time\b",
    r"\bcontract\b",
    r"\bcontractor\b",
    r"\bfreelance\b",
    r"\bevaluator\b",
    r"\brater\b",
    r"\bannotat(?:or|ion)\b",
)


def employment_is_eligible(job: Dict[str, Any]) -> bool:
    """Ensure the role is strictly full-time (no internships, contract, or part-time)."""
    emp = str(job.get("employment_type") or "").lower().strip()
    if emp:
        disallowed = ("intern", "part-time", "contract", "temporary", "volunteer")
        if any(d in emp for d in disallowed):
            return False
    return True



def title_is_eligible(job: Dict[str, Any]) -> bool:
    """Validate that the job title strictly matches one of the candidate's target roles:
    AI Engineer, Software Engineer, Python Backend, Python Full Stack, or Python jobs.
    Excludes QA, testing, hardware, performance, and non-software roles.
    """
    title = str(job.get("title") or "").strip().lower()
    if not title:
        return False
    if any(re.search(pat, title, re.I) for pat in EXCLUDED_TITLE_KEYWORDS):
        return False
    return any(re.search(pat, title, re.I) for pat in ALLOWED_TITLE_PATTERNS)



def canonical_skill_hits(text: str) -> List[str]:
    blob = (text or "").lower()
    hits = []
    for skill, patterns in SKILL_PATTERNS.items():
        if any(re.search(pattern, blob, re.I) for pattern in patterns):
            hits.append(skill)
    # Collapse broad SQL when a more specific SQL engine is identified.
    if "postgresql" in hits or "sql server" in hits or "mysql" in hits:
        hits = [x for x in hits if x != "sql"]
    return sorted(set(hits))


def score_job(job: Dict[str, Any]) -> Dict[str, Any]:
    title = str(job.get("title") or "")
    description = str(job.get("description") or "")
    skills = canonical_skill_hits(title + "\n" + description)
    matched = sorted(set(skills) & CANDIDATE_SKILLS)
    missing = sorted(set(skills) - CANDIDATE_SKILLS)
    percentage = round((len(matched) / len(skills)) * 100) if skills else 0
    job["job_skills"] = skills
    job["matched_skills"] = matched
    job["missing_skills"] = missing
    job["skill_match_pct"] = percentage
    return job


def load_seen_ids() -> set[str]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps({"seen_job_ids": [], "updated_at": datetime.now(timezone.utc).isoformat()}, indent=2), encoding="utf-8")
        return set()
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(str(x) for x in payload.get("seen_job_ids", []))
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        LOG.warning("Could not read job history; starting with empty history: %s", exc)
        return set()


def save_seen_ids(ids: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Keep the state bounded to the latest 25,000 ids.
    serialised = sorted(ids)[-25000:]
    STATE_FILE.write_text(
        json.dumps({"seen_job_ids": serialised, "updated_at": datetime.now(timezone.utc).isoformat()}, indent=2),
        encoding="utf-8",
    )


def _canonical_linkedin_job_url(raw_url: Any, job_id: Any = None) -> str:
    """Accept LinkedIn job URLs only and normalize them to the canonical job page."""
    raw = str(raw_url or "").strip()
    if raw:
        try:
            parsed = urlparse(raw)
            host = (parsed.hostname or "").lower()
            if host != "linkedin.com" and not host.endswith(".linkedin.com"):
                return ""
            match = re.search(r"/(?:comm/)?jobs/(?:view|collections)/(\d+)", parsed.path, re.I)
            if match:
                return f"https://www.linkedin.com/jobs/view/{match.group(1)}/"
        except ValueError:
            return ""
    candidate_id = str(job_id or "").strip()
    if re.fullmatch(r"\d{6,20}", candidate_id):
        return f"https://www.linkedin.com/jobs/view/{candidate_id}/"
    return ""


def clean_job_url(job: Dict[str, Any]) -> str:
    return _canonical_linkedin_job_url(
        job.get("apply_url") or job.get("job_url") or job.get("url"),
        job.get("job_id") or job.get("id"),
    )


def _parse_linkedin_age(raw: Any) -> float | None:
    """Parse LinkedIn relative labels such as '35 minutes ago' or '2h ago'."""
    if raw in (None, ""):
        return None
    text = str(raw).strip().lower()
    text = re.sub(r"^(?:posted|reposted)\s+", "", text)
    if re.search(r"\b(?:just now|moments ago|right now)\b", text):
        return 0.0
    match = re.search(
        r"\b(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)\s*(?:ago)?\b",
        text,
        re.I,
    )
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith(("second", "sec")) or unit == "s":
        return amount / 3600.0
    if unit.startswith(("minute", "min")) or unit == "m":
        return amount / 60.0
    if unit.startswith(("hour", "hr")) or unit == "h":
        return amount
    if unit.startswith("day") or unit == "d":
        return amount * 24.0
    return amount * 168.0


def _parse_linkedin_timestamp(raw: Any) -> datetime | None:
    """Parse timezone-aware ISO timestamps or Unix timestamps; reject date-only strings."""
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        stamp = float(raw)
        if stamp > 10_000_000_000:
            stamp /= 1000.0
        if stamp < 1_000_000_000:
            return None
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    value = str(raw).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _verified_posting_age(job: Dict[str, Any]) -> float | None:
    """Compute current age from a precise posting timestamp or captured age + capture time."""
    for key in ("posted_iso", "posted_at", "datePosted", "published_at"):
        posted = _parse_linkedin_timestamp(job.get(key))
        if posted is not None:
            age = (datetime.now(timezone.utc) - posted).total_seconds() / 3600.0
            return None if age < -0.25 else max(0.0, age)

    capture = _parse_linkedin_timestamp(job.get("scraped_at") or job.get("captured_at"))
    if capture is None:
        return None
    elapsed = max(0.0, (datetime.now(timezone.utc) - capture).total_seconds() / 3600.0)

    base_age = _as_float(job.get("age_hours"))
    if base_age is None:
        for key in ("posted_time", "posted_label", "freshness_badge", "time_ago"):
            base_age = _parse_linkedin_age(job.get(key))
            if base_age is not None:
                break
    if base_age is None or base_age < 0:
        return None
    return base_age + elapsed


def _extract_experience_range(job: Dict[str, Any]) -> None:
    """Populate minimum/maximum experience from an explicit range if not already supplied."""
    if job.get("min_experience_years") is not None or job.get("max_experience_years") is not None:
        return
    blob = " ".join(str(job.get(k) or "") for k in (
        "title", "description", "experience_requirement", "experience",
    ))
    range_match = re.search(
        r"\b(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(\d+(?:\.\d+)?)\s*(?:\+\s*)?(?:years?|yrs?)\b",
        blob, re.I,
    )
    if range_match:
        job["min_experience_years"] = float(range_match.group(1))
        job["max_experience_years"] = float(range_match.group(2))
        return
    minimum_match = re.search(
        r"\b(?:minimum|min\.?|at least)?\s*(\d+(?:\.\d+)?)\s*\+\s*(?:years?|yrs?)\b",
        blob, re.I,
    )
    if minimum_match:
        job["min_experience_years"] = float(minimum_match.group(1))


# ---------------------------------------------------------------------------
# LinkedIn Live Guest-Endpoint Scraper & Async JD Enrichment
# ---------------------------------------------------------------------------
def scrape_linkedin_guest_jobs(
    hours: float,
    target_count: int,
    delay: float = 1.0,
    proxy: str | None = None,
    keywords_list: Sequence[str] | None = None,
    locations_list: Sequence[str] | None = None,
) -> List[Dict[str, Any]]:
    """
    Direct live guest-endpoint scraper for LinkedIn jobs.
    Queries https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
    with dynamic f_TPR, rotating browser headers, backoff, and optional proxy.
    """
    if requests is None or BeautifulSoup is None:
        LOG.error("requests or beautifulsoup4 not installed. Cannot run live LinkedIn scraper.")
        return []

    active_proxy = _resolve_proxy(proxy)
    session = requests.Session()
    if active_proxy:
        session.proxies.update({"http": active_proxy, "https": active_proxy})
        LOG.info("LinkedIn Guest Scraper active with proxy: %s", active_proxy)

    backoff = LinkedInBackoff()
    terms = list(keywords_list) if keywords_list else SEARCH_TERMS
    locs = list(locations_list) if locations_list else ["India"]

    # Calculate dynamic f_TPR
    tpr_seconds = max(3600, int(float(hours) * 3600))
    f_tpr = f"r{tpr_seconds}"

    # Allow scraping as many fresh jobs as possible (up to 100 per term)
    per_term_cap = target_count if target_count > 0 else 100
    collected: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()

    LOG.info(
        "Starting LinkedIn live guest scraping: %d keywords, %d locations, window=%.1fh (f_TPR=%s)",
        len(terms), len(locs), hours, f_tpr,
    )

    for term in terms:
        for loc in locs:
            if target_count > 0 and len(collected) >= target_count:
                break

            term_jobs = 0
            start = 0
            consecutive_empty = 0

            is_remote_loc = loc.strip().lower() == "remote"
            query_loc = "India" if is_remote_loc else loc
            remote_param = "&f_WT=2" if is_remote_loc else ""

            encoded_kw = quote_plus(term)
            encoded_loc = quote_plus(query_loc)

            while term_jobs < per_term_cap and consecutive_empty < 2:
                url = (
                    f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
                    f"?keywords={encoded_kw}&location={encoded_loc}&f_TPR={f_tpr}&f_JT=F{remote_param}&start={start}"
                )

                resp = None
                for attempt in range(backoff.max_retries):
                    try:
                        r = session.get(url, headers=_get_browser_headers(), timeout=15)
                        if r.status_code == 200:
                            backoff.reset()
                            resp = r
                            break
                        elif r.status_code in (429, 403):
                            wait = backoff.wait_seconds(attempt, r.headers.get("Retry-After"))
                            backoff.record_failure()
                            LOG.warning("Rate limit (HTTP %d). Waiting %.1fs before retry...", r.status_code, wait)
                            time.sleep(wait)
                            if backoff.exhausted:
                                LOG.error("Backoff exhausted on LinkedIn guest API. Skipping term %s.", term)
                                break
                        else:
                            LOG.warning("LinkedIn returned HTTP %d for %s", r.status_code, term)
                            time.sleep(2.0)
                    except Exception as err:
                        LOG.warning("Network error querying LinkedIn: %s", err)
                        time.sleep(backoff.wait_seconds(attempt))

                if not resp or not resp.text.strip():
                    consecutive_empty += 1
                    start += 10
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                cards = soup.find_all("li")
                if not cards:
                    consecutive_empty += 1
                    start += 10
                    continue

                consecutive_empty = 0
                for card in cards:
                    try:
                        t_el = card.find("h3", class_="base-search-card__title") or card.find("h3")
                        co_el = card.find("h4", class_="base-search-card__subtitle") or card.find("h4")
                        lo_el = card.find("span", class_="job-search-card__location")
                        ti_el = card.find("time")
                        li_el = card.find("a", class_="base-card__full-link") or card.find("a", href=True)

                        if not (t_el and li_el):
                            continue

                        title = t_el.get_text(" ", strip=True)
                        company = co_el.get_text(" ", strip=True) if co_el else "Unknown Company"
                        job_loc = lo_el.get_text(" ", strip=True) if lo_el else query_loc

                        raw_href = li_el.get("href", "")
                        clean_href = raw_href.split("?")[0]
                        m = re.search(r"(\d{8,12})", clean_href)
                        job_id = m.group(1) if m else clean_href.split("-")[-1].split("/")[-1]

                        if not job_id or job_id in seen_ids:
                            continue
                        seen_ids.add(job_id)

                        canonical_url = f"https://www.linkedin.com/jobs/view/{job_id}/"

                        posted_str = ""
                        posted_iso = ""
                        if ti_el:
                            posted_str = ti_el.get_text(strip=True)
                            posted_iso = ti_el.get("datetime", "")

                        age = _parse_linkedin_age(posted_str)
                        if age is None and posted_iso:
                            parsed_dt = _parse_linkedin_timestamp(posted_iso)
                            if parsed_dt:
                                age = max(0.0, (datetime.now(timezone.utc) - parsed_dt).total_seconds() / 3600.0)

                        if age is not None and age >= float(hours):
                            continue

                        age_val = round(age, 3) if age is not None else 0.0
                        badge = f"<1h ago ({max(1, round(age_val * 60))}m)" if age_val < 1 else f"{age_val:.1f}h ago"

                        workplace = "Remote" if is_remote_loc else "Not specified"

                        rec = {
                            "job_id": job_id,
                            "source": "LinkedIn",
                            "job_url": canonical_url,
                            "apply_url": canonical_url,
                            "title": title,
                            "company": company,
                            "location": job_loc,
                            "description": "",
                            "workplace_type": workplace,
                            "posted_time": posted_str,
                            "posted_iso": posted_iso,
                            "scraped_at": datetime.now(timezone.utc).isoformat(),
                            "age_hours": age_val,
                            "freshness_badge": badge,
                        }
                        _extract_experience_range(rec)
                        collected.append(rec)
                        term_jobs += 1

                        if target_count > 0 and len(collected) >= target_count:
                            break
                    except Exception as card_err:
                        LOG.debug("Error parsing card: %s", card_err)
                        continue

                start += max(len(cards), 10)
                time.sleep(delay + random.uniform(0.5, 1.2))

    LOG.info("Guest scraper harvested %d initial LinkedIn job cards.", len(collected))
    return collected


async def _async_fetch_jd(
    session: "aiohttp.ClientSession",
    job: Dict[str, Any],
    sem: asyncio.Semaphore,
    proxy: str | None = None,
    hours: float = 24.0,
) -> Dict[str, Any]:
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job['job_id']}"
    headers = _get_browser_headers()
    async with sem:
        for _ in range(2):
            try:
                async with session.get(url, headers=headers, proxy=proxy, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 200:
                        text = await r.text()
                        _parse_jd_html_into_job(job, text, hours)
                        return job
                    elif r.status in (429, 403):
                        await asyncio.sleep(2.0 + random.uniform(1.0, 2.0))
            except Exception as e:
                LOG.debug("Async JD error for %s: %s", job["job_id"], e)
        return job


def _parse_jd_html_into_job(job: Dict[str, Any], html_text: str, hours: float = 24.0) -> None:
    if not html_text:
        return
    soup = BeautifulSoup(html_text, "html.parser")

    # Company name / URL
    comp_link = soup.find("a", class_=lambda c: c and "topcard__org-name-link" in c)
    if comp_link:
        if comp_link.get("href"):
            job["company_url"] = comp_link["href"].split("?")[0]
        if not job.get("company") or job["company"] == "Unknown Company":
            job["company"] = comp_link.get_text(strip=True)

    # Criteria items
    for item in soup.find_all("li", class_="description__job-criteria-item"):
        h = item.find("h3")
        v = item.find("span")
        if not (h and v):
            continue
        h_txt = h.get_text().strip().lower()
        v_txt = v.get_text().strip()
        if "seniority" in h_txt:
            job["seniority_level"] = v_txt
        elif "employment" in h_txt:
            job["employment_type"] = v_txt
        elif "workplace" in h_txt:
            job["workplace_type"] = v_txt
        elif "industr" in h_txt:
            job["industries"] = v_txt

    # Full description
    desc_el = (
        soup.find("div", class_="show-more-less-html__markup")
        or soup.find("div", class_="description__text")
        or soup.find("section", class_="description")
    )
    if desc_el:
        job["description"] = desc_el.get_text(separator="\n", strip=True)

    # Timestamp verification from detail page
    t_ago = soup.find(class_=lambda c: c and "posted-time-ago" in c)
    time_tag = soup.find("time")
    detail_posted = (t_ago.get_text(strip=True) if t_ago else "") or (time_tag.get_text(strip=True) if time_tag else "")
    detail_iso = time_tag.get("datetime", "") if time_tag else ""

    for script in soup.find_all("script", type=lambda t: t and "ld+json" in t.lower()):
        try:
            data = json.loads(script.get_text(strip=True))
            if isinstance(data, dict) and data.get("datePosted"):
                detail_iso = data["datePosted"]
        except Exception:
            pass

    if detail_posted or detail_iso:
        age = _parse_linkedin_age(detail_posted)
        if age is None and detail_iso:
            parsed_dt = _parse_linkedin_timestamp(detail_iso)
            if parsed_dt:
                age = max(0.0, (datetime.now(timezone.utc) - parsed_dt).total_seconds() / 3600.0)
        if age is not None:
            job["age_hours"] = round(age, 3)
            job["posted_time"] = detail_posted or job.get("posted_time", "")
            job["posted_iso"] = detail_iso or job.get("posted_iso", "")
            if age < 1:
                job["freshness_badge"] = f"<1h ago ({max(1, round(age * 60))}m)"
            else:
                job["freshness_badge"] = f"{age:.1f}h ago"

    _extract_experience_range(job)


def enrich_linkedin_jds(
    jobs: List[Dict[str, Any]],
    proxy: str | None = None,
    max_concurrency: int = 5,
    hours: float = 24.0,
) -> List[Dict[str, Any]]:
    """Enriches job records with complete job descriptions and metadata."""
    to_enrich = [j for j in jobs if not j.get("description")]
    if not to_enrich:
        return jobs

    LOG.info("Enriching %d jobs with full job descriptions and criteria...", len(to_enrich))
    active_proxy = _resolve_proxy(proxy)

    if HAS_AIOHTTP and aiohttp:
        async def _run():
            sem = asyncio.Semaphore(max_concurrency)
            connector = aiohttp.TCPConnector(limit=10, ssl=False)
            async with aiohttp.ClientSession(connector=connector) as sess:
                tasks = [_async_fetch_jd(sess, j, sem, active_proxy, hours) for j in to_enrich]
                await asyncio.gather(*tasks)

        try:
            asyncio.run(_run())
            return jobs
        except Exception as exc:
            LOG.warning("Async JD fetch encountered exception: %s. Falling back to sync.", exc)

    # Synchronous fallback
    session = requests.Session()
    if active_proxy:
        session.proxies.update({"http": active_proxy, "https": active_proxy})

    for i, j in enumerate(to_enrich):
        url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{j['job_id']}"
        try:
            r = session.get(url, headers=_get_browser_headers(), timeout=12)
            if r.status_code == 200:
                _parse_jd_html_into_job(j, r.text, hours)
            time.sleep(random.uniform(0.6, 1.2))
        except Exception as err:
            LOG.debug("Sync JD fetch error for %s: %s", j["job_id"], err)
        if (i + 1) % 10 == 0:
            LOG.info("  Enriched %d/%d JDs...", i + 1, len(to_enrich))

    return jobs


# ---------------------------------------------------------------------------
# Core Collection Pipeline (Supports Live Guest Scraping & Local JSON)
# ---------------------------------------------------------------------------
def collect_jobs(
    hours: float,
    target_count: int,
    delay: float,
    source: str = "auto",
    proxy: str | None = None,
    keywords: str | None = None,
    location: str | None = None,
    no_jd: bool = False,
) -> List[Dict[str, Any]]:
    """Collect LinkedIn job records via live guest scraping and/or local JSON intake.

    source choices:
      - 'auto'  : Loads records from local JSON if present; also runs live guest scraper
                  if file is empty or missing.
      - 'guest' : Strictly runs live guest-endpoint scraper.
      - 'file'  : Only reads from local JSON file (LINKEDIN_JOBS_JSON).
    """
    accepted: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    # 1. Local JSON Intake
    input_path = Path(os.environ.get("LINKEDIN_JOBS_JSON", str(DATA_DIR / "linkedin_jobs.json")))
    file_records: List[Dict[str, Any]] = []

    if source in ("auto", "file") and input_path.exists():
        try:
            payload = json.loads(input_path.read_text(encoding="utf-8"))
            raw_list = payload.get("jobs", []) if isinstance(payload, dict) else payload
            if isinstance(raw_list, list):
                file_records = [r for r in raw_list if isinstance(r, dict)]
                LOG.info("Loaded %d records from local JSON file %s", len(file_records), input_path)
        except Exception as exc:
            LOG.warning("Could not read local JSON at %s: %s", input_path, exc)

    # 2. Live LinkedIn Guest Scraping
    guest_records: List[Dict[str, Any]] = []
    should_scrape = (source == "guest") or (source == "auto" and len(file_records) == 0)

    if should_scrape:
        kw_list = [k.strip() for k in keywords.split(",") if k.strip()] if keywords else None
        loc_list = [l.strip() for l in location.split(",") if l.strip()] if location else None
        guest_records = scrape_linkedin_guest_jobs(
            hours=hours,
            target_count=target_count,
            delay=delay,
            proxy=proxy,
            keywords_list=kw_list,
            locations_list=loc_list,
        )

    all_raw = file_records + guest_records

    # 3. Normalization and Verified Posting Age Check
    for raw_item in all_raw:
        job = dict(raw_item)
        job_id = str(job.get("job_id") or job.get("id") or "").strip()
        raw_url = job.get("job_url") or job.get("apply_url") or job.get("url") or ""
        canonical_url = _canonical_linkedin_job_url(raw_url, job_id)
        if not canonical_url:
            LOG.debug("Skipping record without a valid LinkedIn job URL: %s", job.get("title", "Untitled"))
            continue
        id_match = re.search(r"/jobs/view/(\d+)", canonical_url)
        if id_match:
            job_id = id_match.group(1)
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)

        age = _verified_posting_age(job)
        if age is None or age >= float(hours):
            LOG.debug(
                "Skipping %s: posting age is unverified or outside the %.1fh window.",
                job.get("title", "Untitled"), hours,
            )
            continue

        job["job_id"] = job_id
        job["source"] = "LinkedIn"
        job["job_url"] = canonical_url
        job["apply_url"] = canonical_url
        job["title"] = str(job.get("title") or job.get("job_title") or "").strip()
        job["company"] = str(job.get("company") or job.get("company_name") or "Unknown Company").strip()
        job["location"] = str(job.get("location") or job.get("job_location") or "").strip()
        job["description"] = str(
            job.get("description") or job.get("job_description") or job.get("full_description") or ""
        ).strip()
        job["workplace_type"] = str(
            job.get("workplace_type") or job.get("workplaceType") or "Not specified"
        ).strip()
        _extract_experience_range(job)

        if not job["title"] or not job["location"]:
            LOG.debug("Skipping LinkedIn record missing title or location (%s).", job_id)
            continue

        job["age_hours"] = round(age, 3)
        if age < 1:
            job["freshness_badge"] = f"<1h ago ({max(1, round(age * 60))}m)"
        else:
            job["freshness_badge"] = f"{age:.1f}h ago"
        accepted.append(job)

    # 4. Enrich full JD if descriptions are missing (prioritize target candidate roles)
    if not no_jd:
        eligible_target = [j for j in accepted if title_is_eligible(j) and location_is_eligible(j)]
        other_jobs = [j for j in accepted if j not in eligible_target]
        to_enrich = eligible_target + other_jobs[:50]
        enrich_linkedin_jds(to_enrich, proxy=proxy, max_concurrency=8, hours=hours)

    accepted.sort(key=lambda item: float(item.get("age_hours", 999999)))
    if target_count > 0:
        accepted = accepted[:target_count]

    LOG.info("Accepted %d verified LinkedIn records matching <= %.1fh window.", len(accepted), hours)
    return accepted


def _as_float(value: Any) -> float | None:
    if value in (None, "", "N/A", "Not specified"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def experience_is_eligible(job: Dict[str, Any]) -> bool:
    """Keep roles whose stated experience band contains 2 years, or is flexible/unspecified."""
    target_years = 2.0
    minimum = _as_float(job.get("min_experience_years"))
    maximum = _as_float(job.get("max_experience_years"))
    blob = " ".join(str(job.get(k) or "") for k in (
        "title", "description", "experience_requirement", "experience",
    )).lower()

    if minimum is not None and maximum is not None:
        if minimum <= target_years <= maximum:
            return True
        flexible_phrases = (
            "any years of experience", "all experience levels", "experience is flexible",
            "flexible on experience", "open to candidates with 2 years",
            "consider candidates with 2 years", "less experience will be considered",
            "equivalent practical experience", "equivalent hands-on experience",
        )
        return any(phrase in blob for phrase in flexible_phrases)
    if minimum is not None:
        if minimum <= target_years:
            return True
        flexible_phrases = (
            "any years of experience", "all experience levels", "experience is flexible",
            "flexible on experience", "open to candidates with 2 years",
            "consider candidates with 2 years", "less experience will be considered",
            "equivalent practical experience", "equivalent hands-on experience",
        )
        return any(phrase in blob for phrase in flexible_phrases)
    if maximum is not None:
        return maximum >= target_years

    # Parse common explicit ranges in the posting, e.g. "1-3 years" or "1 to 3 yrs".
    range_match = re.search(
        r"\b(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(\d+(?:\.\d+)?)\s*(?:\+\s*)?(?:years?|yrs?)\b",
        blob, re.I,
    )
    if range_match:
        lo, hi = float(range_match.group(1)), float(range_match.group(2))
        if lo <= target_years <= hi:
            return True
        flexible_phrases = (
            "any years of experience", "all experience levels", "experience is flexible",
            "flexible on experience", "open to candidates with 2 years",
            "consider candidates with 2 years", "less experience will be considered",
        )
        return any(phrase in blob for phrase in flexible_phrases)

    # Explicit minimum-only requirements above 2 years need a flexibility statement.
    min_match = re.search(
        r"(?:minimum|min\.?|at least)\s*(\d+(?:\.\d+)?)\s*(?:\+\s*)?(?:years?|yrs?)",
        blob, re.I,
    )
    if not min_match:
        min_match = re.search(r"\b(\d+(?:\.\d+)?)\s*\+\s*(?:years?|yrs?)\b", blob, re.I)
    if not min_match:
        min_match = re.search(
            r"\b(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\s+(?:of\s+)?experience\b",
            blob, re.I,
        )
    if min_match and float(min_match.group(1)) > target_years:
        flexible_phrases = (
            "any years of experience", "all experience levels", "experience is flexible",
            "flexible on experience", "open to candidates with 2 years",
            "consider candidates with 2 years", "less experience will be considered",
        )
        return any(phrase in blob for phrase in flexible_phrases)
    return True  # No usable experience restriction was supplied.


def location_is_eligible(job: Dict[str, Any]) -> bool:
    """Strict candidate location requirements:
    1. Hyderabad, Bengaluru/Bangalore, Chennai: All work modes allowed (On-site, In-office, Hybrid, Remote).
    2. India Remote: Remote positions specifically based in India or open to applicants in India.
    3. Strictly excludes: Worldwide remote, outside-India remote, and onsite/hybrid in other cities.
    """
    location = str(job.get("location") or "").lower()
    workplace = str(job.get("workplace_type") or "").lower().replace("_", "-")
    title = str(job.get("title") or "").lower()
    description = str(job.get("description") or "").lower()
    blob = f"{title}\n{location}\n{workplace}\n{description}"

    # 1. Strictly exclude roles explicitly restricted to countries outside India
    if any(re.search(pat, location, re.I) for pat in REMOTE_ONLY_OUTSIDE_INDIA_PATTERNS):
        return False

    # 2. Check if job is in the 3 target cities: Hyderabad, Bengaluru/Bangalore, or Chennai
    # For these 3 cities, on-site, in-office, hybrid, and remote are all accepted.
    city_match = any(re.search(rf"\b{re.escape(city)}\b", location, re.I) for city in TARGET_CITIES)
    if city_match:
        return True

    # 3. Check for India Remote:
    # Must be remote AND based in India (not worldwide/global remote)
    is_remote = (
        any(term in workplace for term in ("remote", "work from home", "wfh", "distributed"))
        or bool(re.search(r"\b(remote|work from home|wfh)\b", location))
        or bool(re.search(r"\b(?:remote|wfh)\b", title))
    )

    if is_remote:
        # Check if explicitly worldwide/global remote without India base
        is_worldwide_only = bool(re.search(r"\b(?:worldwide|global(?:ly)? remote|anywhere in the world)\b", blob, re.I))
        if is_worldwide_only and "india" not in location:
            return False

        # Must have an explicit India connection
        has_india = (
            "india" in location
            or bool(re.search(r"\bindia\b", blob, re.I))
            or bool(re.search(r"\bopen to applicants in india\b", blob, re.I))
        )
        if has_india:
            return True

    # Non-target cities (e.g. Pune, Noida, Gurgaon, Mumbai) that are onsite/hybrid are excluded
    return False


def experience_label(job: Dict[str, Any]) -> str:
    minimum = _as_float(job.get("min_experience_years"))
    maximum = _as_float(job.get("max_experience_years"))
    if minimum is not None and maximum is not None:
        return f"{minimum:g}-{maximum:g} years"
    if minimum is not None:
        return f"{minimum:g}+ years"
    if maximum is not None:
        return f"Up to {maximum:g} years"
    return str(job.get("experience_requirement") or job.get("experience") or "Not specified")


def source_is_allowed(job: Dict[str, Any]) -> bool:
    source = str(job.get("source") or "").lower()
    url = clean_job_url(job).lower()
    # This monitor is intentionally LinkedIn-only; no other job sources are accepted.
    return ("linkedin" in source or bool(url)) and url.startswith("https://www.linkedin.com/jobs/view/")


def filter_job_preferences(jobs: Sequence[Dict[str, Any]], enforce_title: bool = True) -> List[Dict[str, Any]]:
    eligible = []
    for job in jobs:
        if (
            source_is_allowed(job)
            and location_is_eligible(job)
            and experience_is_eligible(job)
            and employment_is_eligible(job)
            and (not enforce_title or title_is_eligible(job))
        ):
            eligible.append(job)
    return eligible


def html_table(jobs: Sequence[Dict[str, Any]], include_match: bool = True) -> str:
    headers = ["Match %", "Job title", "Company", "Location", "Work mode", "Posted", "Experience", "Matched skills", "Missing skills", "Direct job link"]
    rows = []
    if not jobs:
        return '<p style="color:#666;margin:8px 0 18px">No jobs in this section for this run.</p>'
    for j in jobs:
        title = html.escape(str(j.get("title") or "Untitled role"))
        url = html.escape(clean_job_url(j), quote=True)
        link = f'<a href="{url}">Open job / apply</a>' if url.startswith("https://") else "Link unavailable"
        vals = [
            f"{int(j.get('skill_match_pct', 0))}%",
            title,
            html.escape(str(j.get("company") or "Unknown")),
            html.escape(str(j.get("location") or "Not specified")),
            html.escape(str(j.get("workplace_type") or "Not specified")),
            html.escape(str(j.get("freshness_badge") or j.get("posted_time") or "Unknown")),
            html.escape(experience_label(j)),
            html.escape(", ".join(j.get("matched_skills", [])) or "None detected"),
            html.escape(", ".join(j.get("missing_skills", [])) or "None detected"),
            link,
        ]
        rows.append("<tr>" + "".join(f"<td>{v}</td>" for v in vals) + "</tr>")
    th = "".join(f"<th>{h}</th>" for h in headers)
    return (
        '<div style="overflow-x:auto"><table cellpadding="7" cellspacing="0" '
        'style="border-collapse:collapse;width:100%;font-size:12px">'
        f'<thead><tr style="background:#17365d;color:#fff">{th}</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def markdown_table(jobs: Sequence[Dict[str, Any]]) -> str:
    headers = ["Match %", "Job title", "Company", "Location", "Work mode", "Posted", "Experience", "Matched skills", "Missing skills", "Direct application link"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    if not jobs:
        return "_No jobs in this section for this run._"
    def cell(value: Any) -> str:
        return str(value if value not in (None, "") else "Not specified").replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    for j in jobs:
        url = clean_job_url(j)
        link = f"[Open job / apply]({url})" if url.startswith("https://") else "Link unavailable"
        experience = experience_label(j)
        row = [
            f"{int(j.get('skill_match_pct', 0))}%", cell(j.get("title") or "Untitled role"),
            cell(j.get("company") or "Unknown"), cell(j.get("location")),
            cell(j.get("workplace_type")), cell(j.get("freshness_badge") or j.get("posted_time")),
            cell(experience), cell(", ".join(j.get("matched_skills", [])) or "None detected"),
            cell(", ".join(j.get("missing_skills", [])) or "None detected"), link,
        ]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def build_github_issue_body(matched: Sequence[Dict[str, Any]], all_scraped: Sequence[Dict[str, Any]]) -> str:
    run_time = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
    actor = os.environ.get("GITHUB_ACTOR", "SUKRUTH81").strip() or "SUKRUTH81"
    mention_header = f"cc @{actor}\n\n"
    return f"""# Job Match Report\n\n{mention_header}**Run time:** {run_time}  
**Skill score:** matching candidate skills / distinct technical skills detected in the job description × 100.  
**Notification rule:** this issue is created only when at least one new job scores 60% or higher.\n\n## 1. Jobs matching 60% or more ({len(matched)})\n\nSorted by match percentage, highest first.\n\n{markdown_table(matched)}\n\n## 2. All newly discovered jobs scraped ({len(all_scraped)})\n\nIncludes every new unique job collected in this run, including jobs below 60%. Sorted highest first.\n\n{markdown_table(all_scraped)}\n\n---\nPlease verify eligibility and posting details on the employer's application page.\n"""


def create_github_issue(title: str, body: str) -> None:
    import subprocess

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    actor = os.environ.get("GITHUB_ACTOR", "").strip()
    if not token or not repo:
        LOG.warning("GITHUB_TOKEN or GITHUB_REPOSITORY not set. Skipping GitHub Issue creation.")
        return

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    body_path = REPORT_DIR / "github_issue_body.md"
    body_path.write_text(body, encoding="utf-8")
    cmd = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body-file", str(body_path)]
    if actor:
        cmd.extend(["--assignee", actor])
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)
        LOG.info("GitHub notification issue created: %s", result.stdout.strip())
    except Exception as exc:
        LOG.error("Failed to create GitHub Issue via gh CLI: %s", exc)


DEFAULT_RECIPIENTS = ["sukruth1435@gmail.com", "akhilarohithayepuri@gmail.com"]


def build_email_html(matched: Sequence[Dict[str, Any]], all_scraped: Sequence[Dict[str, Any]]) -> str:
    run_time = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
    return f"""<!doctype html><html><body style="font-family:Arial,sans-serif;color:#202124">
    <h2>LinkedIn Job Match Monitor Report</h2>
    <p>Run time: <b>{run_time}</b></p>
    <p>Skill score = matching candidate skills ÷ distinct technical skills detected in the job title and description × 100.</p>
    <h3>1. Jobs matching 60% or more ({len(matched)})</h3>
    <p>Sorted by skill match percentage, highest first.</p>
    {html_table(matched)}
    <h3 style="margin-top:28px">2. All newly discovered jobs scraped ({len(all_scraped)})</h3>
    <p>This table includes every new job collected in this run, including jobs below 60%. Sorted highest first.</p>
    {html_table(all_scraped)}
    <p style="font-size:11px;color:#666;margin-top:22px">Automated report. Verify job requirements and eligibility on the employer's application page.</p>
    </body></html>"""


def send_gmail(subject: str, html_body: str, attachment: Path | None = None) -> None:
    sender = os.environ.get("GMAIL_ADDRESS", "").strip()
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    recipients_raw = os.environ.get("JOB_ALERT_TO", "").strip()
    if recipients_raw:
        recipients = [r.strip() for r in re.split(r"[,;]+", recipients_raw) if r.strip()]
    else:
        recipients = DEFAULT_RECIPIENTS

    if not sender or not app_password:
        LOG.warning(
            "GMAIL_ADDRESS or GMAIL_APP_PASSWORD secret not set in GitHub repository. "
            "Direct email delivery to %s skipped. Add these secrets in Settings -> Secrets and variables -> Actions.",
            ", ".join(recipients),
        )
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content("Your email client does not support HTML. Please view this message in an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")
    if attachment and attachment.exists():
        data = attachment.read_bytes()
        msg.add_attachment(
            data,
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=attachment.name,
        )

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(sender, app_password)
            server.send_message(msg)
        LOG.info("Gmail match report successfully sent to: %s", ", ".join(recipients))
    except Exception as exc:
        LOG.error("Failed to send email to %s: %s", ", ".join(recipients), exc)


def create_excel(matched: Sequence[Dict[str, Any]], all_scraped: Sequence[Dict[str, Any]]) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"job_match_report_{stamp}.xlsx"
    wb = Workbook()
    wb.remove(wb.active)
    columns = ["Match %", "Job Title", "Company", "Location", "Workplace", "Posted", "Min Experience", "Matched Skills", "Missing Skills", "Job URL"]
    for sheet_name, data in (("60%+ Matches", matched), ("All Scraped", all_scraped)):
        ws = wb.create_sheet(sheet_name)
        ws.append(columns)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="17365D")
            cell.alignment = Alignment(horizontal="center")
        for j in data:
            ws.append([
                int(j.get("skill_match_pct", 0)), j.get("title", ""), j.get("company", ""),
                j.get("location", ""), j.get("workplace_type", ""),
                j.get("freshness_badge", j.get("posted_time", "")),
                experience_label(j), ", ".join(j.get("matched_skills", [])),
                ", ".join(j.get("missing_skills", [])), clean_job_url(j),
            ])
            cell = ws.cell(ws.max_row, 10)
            if cell.value and str(cell.value).startswith("https://"):
                cell.hyperlink = cell.value
                cell.style = "Hyperlink"
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        widths = [10, 32, 24, 25, 14, 18, 16, 38, 38, 55]
        for idx, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = width
    wb.save(path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Scheduled LinkedIn job scraping, skills matching, and GitHub notifications")
    parser.add_argument("--hours", type=float, default=1.0, help="Only consider jobs posted within this many hours (e.g., 1 for 1 hour ago, 24 for 24h)")
    parser.add_argument("--target-count", type=int, default=0, help="Maximum jobs to collect (0 for all fresh / scrape as many as possible)")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between search pages in seconds")
    parser.add_argument("--min-match", type=int, default=60, help="Minimum skill match percentage to notify (default 60)")
    parser.add_argument("--source", choices=["auto", "guest", "file"], default="auto",
                        help="Intake source: 'auto' (live guest scraper + file fallback), 'guest' (live scraper), or 'file' (JSON only)")
    parser.add_argument("--proxy", type=str, default=None, help="HTTP/HTTPS/SOCKS proxy URL (or set via LINKEDIN_PROXY / HTTPS_PROXY env)")
    parser.add_argument("--keywords", type=str, default=None, help="Optional comma-separated search terms override")
    parser.add_argument("--location", type=str, default=None, help="Optional comma-separated search locations override")
    parser.add_argument("--no-jd", action="store_true", help="Skip full JD enrichment (listing cards only)")
    parser.add_argument("--force-notify", action="store_true", help="Force report and notification generation even if jobs were previously seen")
    args = parser.parse_args()

    collected = collect_jobs(
        hours=args.hours,
        target_count=args.target_count,
        delay=args.delay,
        source=args.source,
        proxy=args.proxy,
        keywords=args.keywords,
        location=args.location,
        no_jd=args.no_jd,
    )
    all_found = filter_job_preferences(collected)
    LOG.info("Collected %d jobs; %d meet location, 2-year experience, full-time, and target title eligibility.", len(collected), len(all_found))
    for job in all_found:
        score_job(job)
        job["job_url"] = clean_job_url(job)
        job["apply_url"] = job["job_url"]

    # Keep only first-time discoveries, so the same job is not reported repeatedly.
    seen_ids = load_seen_ids()
    new_jobs = [j for j in all_found if str(j.get("job_id")) not in seen_ids]
    LOG.info("Collected %d unique jobs; %d are newly discovered.", len(all_found), len(new_jobs))

    if not new_jobs:
        if args.force_notify and all_found:
            LOG.info("--force-notify active: generating report with all eligible matching jobs.")
            new_jobs = list(all_found)
        else:
            LOG.info("No new jobs. No GitHub notification created.")
            save_seen_ids(seen_ids)
            return 0

    new_jobs.sort(key=lambda j: (int(j.get("skill_match_pct", 0)), str(j.get("title", "")).lower()), reverse=True)
    matched = [j for j in new_jobs if int(j.get("skill_match_pct", 0)) >= args.min_match]
    matched.sort(key=lambda j: (int(j.get("skill_match_pct", 0)), str(j.get("title", "")).lower()), reverse=True)
    all_scraped = sorted(new_jobs, key=lambda j: (int(j.get("skill_match_pct", 0)), str(j.get("title", "")).lower()), reverse=True)

    # Always generate the report for new LinkedIn records; only 60%+ matches notify.
    report_path = create_excel(matched, all_scraped)
    run_stamp = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %I:%M %p IST")
    title = f"LinkedIn job matches: {len(matched)} at 60%+ | {run_stamp}"
    if not matched:
        LOG.info("New LinkedIn jobs were found, but none reached %d%%. Excel report created; no GitHub Issue.", args.min_match)
    else:
        create_github_issue(title, build_github_issue_body(matched, all_scraped))

    # Send email notification (to akhilarohithayepuri@gmail.com) with attached Excel report
    send_gmail(title, build_email_html(matched, all_scraped), attachment=report_path)

    seen_ids.update(str(j.get("job_id")) for j in new_jobs)
    save_seen_ids(seen_ids)
    LOG.info("Excel report saved: %s", report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
