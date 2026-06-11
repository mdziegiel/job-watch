import asyncio
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from docx import Document
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

DB_PATH = os.getenv("DATABASE_PATH", "/data/job-watch.sqlite")
RESUME_BUILDER_DB = os.getenv("RESUME_BUILDER_DB", "/resume-builder-data/resume-builder.sqlite")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_TOKEN") or ""
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_HOME_CHANNEL") or os.getenv("TELEGRAM_CHAT_ID", "")
INDEED_MCP_URL = os.getenv("INDEED_MCP_URL", "https://mcp.indeed.com/claude/mcp")
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "1") == "1"
ALERT_BATCH_LIMIT = int(os.getenv("ALERT_BATCH_LIMIT", "10"))
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://10.10.10.237:8085").rstrip("/")

TARGET_TITLES = [
    "Network Administrator",
    "Systems Administrator",
    "IT Administrator",
    "Endpoint Engineer",
    "Desktop Engineer",
    "M365 Engineer",
]
STATUSES = ["New", "Saved", "Applied", "Interview", "Rejected", "Offer"]


class StatusIn(BaseModel):
    status: str


class NoteIn(BaseModel):
    notes: str = ""


class GenerateIn(BaseModel):
    kind: str


class ManualJob(BaseModel):
    source: str = "Manual"
    title: str
    company: str = ""
    location: str = ""
    salary: str = ""
    url: str = ""
    description: str = ""
    remote_type: str = "unknown"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source TEXT NOT NULL,
              title TEXT NOT NULL,
              company TEXT,
              location TEXT,
              salary TEXT,
              url TEXT,
              description TEXT,
              remote_type TEXT DEFAULT 'unknown',
              status TEXT DEFAULT 'New',
              match_score INTEGER DEFAULT 0,
              fit_summary TEXT DEFAULT '',
              score_breakdown TEXT DEFAULT '{}',
              ideal_match INTEGER DEFAULT 0,
              fingerprint TEXT UNIQUE,
              first_seen TEXT,
              last_seen TEXT,
              alerted INTEGER DEFAULT 0,
              notes TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS generated_docs(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id INTEGER,
              kind TEXT,
              title TEXT,
              content TEXT,
              created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_seen ON jobs(first_seen);
            CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(match_score);
            CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url);
            CREATE INDEX IF NOT EXISTS idx_jobs_exact_duplicate ON jobs(title, company, location);
            """
        )
        # Existing rows predate the strict first-insert notification contract.
        # Mark them notified at startup so deploy/restart cannot resurrect old jobs as alerts.
        c.execute("UPDATE jobs SET alerted=1 WHERE alerted=0 AND first_seen IS NOT NULL")


def dedupe_jobs() -> int:
    """Remove duplicate jobs, keeping the newest row. Duplicate means same URL or same title/company/location."""
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM jobs ORDER BY COALESCE(last_seen, first_seen, '') DESC, COALESCE(first_seen, '') DESC, id DESC"
        ).fetchall()
        seen_urls: set[str] = set()
        seen_identities: set[tuple[str, str, str]] = set()
        delete_ids = []
        for row in rows:
            url_key = canonical_url(row["url"])
            id_key = identity_key(row)
            duplicate = bool(url_key and url_key in seen_urls) or id_key in seen_identities
            if duplicate:
                delete_ids.append(row["id"])
                continue
            if url_key:
                seen_urls.add(url_key)
            seen_identities.add(id_key)
        if delete_ids:
            placeholders = ",".join("?" for _ in delete_ids)
            c.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", delete_ids)
        removed = len(delete_ids)
        if removed:
            print(f"dedupe removed {removed} duplicate job rows", flush=True)
        return removed


def backfill_linkedin_companies() -> int:
    """Best-effort repair for old LinkedIn rows scraped before company extraction existed."""
    with connect() as c:
        rows = c.execute(
            "SELECT id, url FROM jobs WHERE source='LinkedIn' AND COALESCE(company, '')='' AND COALESCE(url, '')!=''"
        ).fetchall()
        fixed = 0
        for row in rows:
            company = linkedin_company_from_url(row["url"])
            if company:
                c.execute("UPDATE jobs SET company=? WHERE id=?", (company, row["id"]))
                fixed += 1
        malformed = c.execute(
            "SELECT id, title, location FROM jobs WHERE source='LinkedIn' AND COALESCE(company, '')='' AND title LIKE '%' || char(10) || '%'"
        ).fetchall()
        for row in malformed:
            lines = [x.strip() for x in (row["title"] or "").splitlines() if x.strip()]
            if len(lines) >= 2:
                title, company = lines[0], clean_company(lines[1])
                location = row["location"] or (lines[2] if len(lines) >= 3 else "")
                if company:
                    c.execute("UPDATE jobs SET title=?, company=?, location=COALESCE(NULLIF(?, ''), location) WHERE id=?", (title, company, location, row["id"]))
                    fixed += 1
        if fixed:
            print(f"backfilled {fixed} LinkedIn company names", flush=True)
        return fixed


def rowdict(row):
    d = dict(row)
    try:
        d["score_breakdown"] = json.loads(d.get("score_breakdown") or "{}")
    except Exception:
        d["score_breakdown"] = {}
    d["ideal_match"] = bool(d.get("ideal_match"))
    return d


def norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def canonical_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlsplit(raw)
        if not parsed.scheme or not parsed.netloc:
            return raw.split("?")[0].split("#")[0].rstrip("/").lower()
        path = re.sub(r"/+", "/", urllib.parse.unquote(parsed.path or "")).rstrip("/")
        return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))
    except Exception:
        return raw.split("?")[0].split("#")[0].rstrip("/").lower()


def field(job, name: str, default=""):
    if hasattr(job, "get"):
        return job.get(name, default)
    try:
        return job[name]
    except Exception:
        return default


def identity_key(job: dict) -> tuple[str, str, str]:
    return (norm(field(job, "title")), norm(field(job, "company")), norm(field(job, "location")))


def fingerprint(job: dict) -> str:
    url = canonical_url(job.get("url", ""))
    if url:
        return hashlib.sha256(("url:" + url).encode()).hexdigest()
    title, company, location = identity_key(job)
    return hashlib.sha256(("tcl:" + title + "|" + company + "|" + location).encode()).hexdigest()


def find_existing_job(c: sqlite3.Connection, job: dict, fp: str):
    old = c.execute("SELECT * FROM jobs WHERE fingerprint=?", (fp,)).fetchone()
    if old:
        return old

    url = canonical_url(job.get("url", ""))
    if url:
        for row in c.execute("SELECT * FROM jobs WHERE COALESCE(url, '') != ''"):
            if canonical_url(row["url"]) == url:
                return row

    key = identity_key(job)
    for row in c.execute("SELECT * FROM jobs"):
        if identity_key(row) == key:
            return row
    return None


def salary_floor(text: str) -> int:
    nums = []
    for match in re.finditer(r"\$?\s*(\d{2,3})(?:[,\.]?(\d{3}))?\s*([kK])?", text or ""):
        n = int(match.group(1) + (match.group(2) or ""))
        if match.group(3) or n < 1000:
            n *= 1000
        nums.append(n)
    return max(nums) if nums else 0


def infer_remote_type(title="", location="", description="") -> str:
    blob = " ".join([title or "", location or "", description or ""]).lower()
    if "hybrid" in blob:
        return "hybrid"
    if "remote" in blob or "work from home" in blob:
        return "remote"
    if "onsite" in blob or "on-site" in blob or "on site" in blob:
        return "onsite"
    return "unknown"


def estimate_distance(location: str) -> int:
    loc = (location or "").lower()
    if "lowell" in loc:
        return 0
    near = {
        "chelmsford": 5,
        "billerica": 10,
        "andover": 15,
        "bedford": 17,
        "nashua": 17,
        "woburn": 22,
        "cambridge": 28,
        "waltham": 28,
        "boston": 31,
        "manchester": 38,
        "worcester": 45,
    }
    for city, miles in near.items():
        if city in loc:
            return miles
    if "remote" in loc:
        return 999
    if " ma" in " " + loc or "massachusetts" in loc or " nh" in " " + loc or "new hampshire" in loc:
        return 50
    return 999


def heuristic_score(job: dict):
    title = job.get("title", "")
    desc = job.get("description", "")
    loc = job.get("location", "")
    salary = job.get("salary", "")
    blob = " ".join([title, desc]).lower()
    score = 35
    bits = {}

    title_hit = 1 if any(t.lower() in title.lower() for t in TARGET_TITLES) else 0
    bits["title_match"] = 20 * title_hit
    score += bits["title_match"]

    terms = ["azure", "intune", "endpoint", "m365", "microsoft 365", "active directory", "entra", "network", "firewall", "hyper-v", "desktop", "systems"]
    hits = sum(1 for t in terms if t in blob)
    bits["skill_alignment"] = min(25, hits * 3)
    score += bits["skill_alignment"]

    work_mode = job.get("remote_type") or infer_remote_type(title, loc, desc)
    distance = estimate_distance(loc)
    loc_score = 20 if work_mode == "hybrid" and distance <= 40 else 15 if work_mode == "remote" else 10 if distance <= 50 else 0
    bits["location_work_mode"] = loc_score
    score += loc_score

    sf = salary_floor(salary)
    sal_score = 15 if sf >= 115000 else 8 if sf >= 100000 else -8 if sf else 0
    bits["salary"] = sal_score
    score += sal_score

    score = max(0, min(100, score))
    ideal = work_mode == "hybrid" and distance <= 40 and sf >= 115000
    summary = "Strong fit" if score >= 80 else "Possible fit" if score >= 60 else "Below target"
    if sf and sf < 115000:
        summary += "; salary appears below target"
    if work_mode not in ["remote", "hybrid"] and distance > 40:
        summary += "; location/work mode may be weak"
    return score, summary, bits, ideal


def claude_json(prompt: str) -> Optional[dict]:
    if not ANTHROPIC_API_KEY:
        return None
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": ANTHROPIC_MODEL, "max_tokens": 1400, "messages": [{"role": "user", "content": prompt}]},
            timeout=45,
        )
        r.raise_for_status()
        text = "\n".join(part.get("text", "") for part in r.json().get("content", []))
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def score_job(job: dict):
    score, summary, breakdown, ideal = heuristic_score(job)
    prompt = (
        "Return JSON only with keys score integer 0-100, summary string, breakdown object, ideal_match boolean. "
        "Score this job for Michael Dziegiel: Senior Network Administrator, 20+ years, Azure, Intune, Hyper-V, endpoint, M365, networking, security. "
        "Target salary $115k-$120k. Ideal is hybrid within 40 miles of Lowell MA, $115k+, 10% bonus potential. "
        "Do not reject jobs below criteria; flag weaknesses. Job: " + json.dumps(job)[:7000]
    )
    ai = claude_json(prompt)
    if ai:
        score = int(max(0, min(100, ai.get("score", score))))
        summary = str(ai.get("summary") or summary)[:500]
        if isinstance(ai.get("breakdown"), dict):
            breakdown = ai["breakdown"]
        ideal = bool(ai.get("ideal_match", ideal))
    return score, summary, breakdown, ideal


def upsert_job(job: dict):
    job = {k: (v or "") for k, v in job.items()}
    if not job.get("remote_type") or job.get("remote_type") == "unknown":
        job["remote_type"] = infer_remote_type(job.get("title"), job.get("location"), job.get("description"))
    fp = fingerprint(job)
    score, summary, breakdown, ideal = score_job(job)
    with connect() as c:
        old = find_existing_job(c, job, fp)
        if old:
            c.execute(
                "UPDATE jobs SET last_seen=?, company=COALESCE(NULLIF(company, ''), NULLIF(?, ''), company), location=COALESCE(NULLIF(location, ''), NULLIF(?, ''), location), salary=COALESCE(NULLIF(?, ''), salary), url=COALESCE(NULLIF(?, ''), url), description=COALESCE(NULLIF(?, ''), description), match_score=?, fit_summary=?, score_breakdown=?, ideal_match=?, fingerprint=COALESCE(NULLIF(fingerprint, ''), ?) WHERE id=?",
                (utcnow(), job.get("company", ""), job.get("location", ""), job.get("salary", ""), job.get("url", ""), job.get("description", ""), score, summary, json.dumps(breakdown), int(ideal), fp, old["id"]),
            )
            return rowdict(c.execute("SELECT * FROM jobs WHERE id=?", (old["id"],)).fetchone()), False
        c.execute(
            "INSERT INTO jobs(source,title,company,location,salary,url,description,remote_type,match_score,fit_summary,score_breakdown,ideal_match,fingerprint,first_seen,last_seen,alerted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (job.get("source"), job.get("title"), job.get("company"), job.get("location"), job.get("salary"), job.get("url"), job.get("description"), job.get("remote_type"), score, summary, json.dumps(breakdown), int(ideal), fp, utcnow(), utcnow()),
        )
        rid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        return rowdict(c.execute("SELECT * FROM jobs WHERE id=?", (rid,)).fetchone()), True


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def clean_company(text: str) -> str:
    company = clean_text(text)
    company = re.sub(r"\s*(?:logo|company logo)$", "", company, flags=re.I).strip(" -·|\t\n\r")
    noise = {"", "promoted", "be an early applicant", "actively hiring", "view job"}
    return "" if company.lower() in noise else company[:160]


def linkedin_company_from_url(url: str) -> str:
    """Extract company from LinkedIn public slugs like title-at-acme-corp-123456."""
    path = urllib.parse.unquote(urllib.parse.urlsplit(url or "").path).rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    m = re.search(r"-at-(?P<company>.+?)(?:-\d+)?$", slug, re.I)
    if not m:
        return ""
    company = re.sub(r"[^\w\s&.+-]+", " ", m.group("company").replace("-", " "), flags=re.UNICODE)
    return clean_company(company.title())


def html_attr(tag: str, attr: str):
    m = re.search(rf'{attr}=["\']([^"\']+)["\']', tag or "", re.I)
    return html.unescape(m.group(1)) if m else ""


def extract_linkedin_company(card_html: str) -> str:
    patterns = [
        r'<h4[^>]*class=["\'][^"\']*base-search-card__subtitle[^"\']*["\'][^>]*>(?P<value>.*?)</h4>',
        r'<a[^>]*class=["\'][^"\']*hidden-nested-link[^"\']*["\'][^>]*>(?P<value>.*?)</a>',
        r'<a[^>]*data-tracking-control-name=["\'][^"\']*job_card_company[^"\']*["\'][^>]*>(?P<value>.*?)</a>',
        r'<span[^>]*class=["\'][^"\']*job-card-container__primary-description[^"\']*["\'][^>]*>(?P<value>.*?)</span>',
    ]
    for pattern in patterns:
        m = re.search(pattern, card_html or "", re.S | re.I)
        if m:
            company = clean_company(m.group("value"))
            if company:
                return company
    alt = re.search(r'<img[^>]+alt=["\'](?P<value>[^"\']+)["\'][^>]*>', card_html or "", re.I)
    if alt:
        company = clean_company(alt.group("value"))
        if company:
            return company
    return ""


def extract_linkedin_title(card_html: str, fallback: str) -> str:
    for pattern in [
        r'<h3[^>]*class=["\'][^"\']*base-search-card__title[^"\']*["\'][^>]*>(?P<value>.*?)</h3>',
        r'<a[^>]+href=["\']https://www\.linkedin\.com/jobs/view/[^"\']+["\'][^>]*>(?P<value>.*?)</a>',
    ]:
        m = re.search(pattern, card_html or "", re.S | re.I)
        if m:
            title = clean_text(m.group("value"))
            if title:
                return title
    return fallback


def extract_linkedin_location(card_html: str) -> str:
    m = re.search(r'<span[^>]*class=["\'][^"\']*job-search-card__location[^"\']*["\'][^>]*>(?P<value>.*?)</span>', card_html or "", re.S | re.I)
    return clean_text(m.group("value")) if m else "Lowell MA / Remote"


def parse_linkedin_jobs(page_html: str, fallback_title: str) -> list[dict]:
    jobs = []
    seen = set()
    cards = re.findall(r'<(?:li|div)[^>]*(?:base-card|job-search-card)[^>]*>.*?</(?:li|div)>', page_html or "", re.S | re.I)
    if not cards:
        cards = [m.group(0) for m in re.finditer(r'.{0,2500}https://www\.linkedin\.com/jobs/view/[^"#?<]+.{0,2500}', page_html or "", re.S | re.I)]
    for card in cards:
        url_m = re.search(r'https://www\.linkedin\.com/jobs/view/[^?"#<\s]+', card, re.I)
        if not url_m:
            continue
        url = html.unescape(url_m.group(0)).rstrip('/')
        if url in seen:
            continue
        seen.add(url)
        jobs.append({
            "source": "LinkedIn",
            "title": extract_linkedin_title(card, fallback_title),
            "company": extract_linkedin_company(card) or linkedin_company_from_url(url),
            "location": extract_linkedin_location(card),
            "salary": "",
            "url": url,
            "description": "LinkedIn public listing",
            "remote_type": "unknown",
        })
    return jobs


def fetch(url: str) -> str:
    return requests.get(url, headers={"User-Agent": "Mozilla/5.0 JobWatchAssistant/1.0", "Accept-Language": "en-US,en;q=0.9"}, timeout=20).text


def scrape_linkedin(title: str):
    q = urllib.parse.quote(title)
    url = f"https://www.linkedin.com/jobs/search?keywords={q}&location=Lowell%2C%20Massachusetts%2C%20United%20States&distance=50"
    try:
        return parse_linkedin_jobs(fetch(url), title)[:15]
    except Exception:
        return []


def scrape_dice(title: str):
    q = urllib.parse.quote(title)
    url = f"https://www.dice.com/jobs?q={q}&location=Lowell,%20MA&radius=50&radiusUnit=mi&page=1&pageSize=20"
    jobs = []
    try:
        text = fetch(url)
        pattern = r'"title"\s*:\s*"(?P<title>[^"]+)".*?"companyName"\s*:\s*"(?P<company>[^"]*)".*?"jobLocation"\s*:\s*"(?P<loc>[^"]*)".*?"detailUrl"\s*:\s*"(?P<url>[^"]+)"'
        for m in re.finditer(pattern, text, re.S):
            jobs.append({"source": "Dice", "title": m.group("title"), "company": m.group("company"), "location": m.group("loc"), "salary": "", "url": m.group("url").replace("\\/", "/"), "description": "Dice public listing", "remote_type": "unknown"})
    except Exception:
        pass
    return jobs[:15]


def scrape_ziprecruiter(title: str):
    q = urllib.parse.quote(title)
    url = f"https://www.ziprecruiter.com/jobs-search?search={q}&location=Lowell%2C+MA&radius=50"
    jobs = []
    try:
        text = fetch(url)
        for m in re.finditer(r'<a[^>]+href="(?P<url>https://www\.ziprecruiter\.com/[^"#]+)"[^>]*>(?P<title>[^<]{5,120})</a>', text, re.S):
            jobs.append({"source": "ZipRecruiter", "title": clean_text(m.group("title")), "company": "", "location": "Lowell MA / Remote", "salary": "", "url": m.group("url"), "description": "ZipRecruiter public listing", "remote_type": "unknown"})
    except Exception:
        pass
    return jobs[:15]


def scrape_indeed_mcp(title: str):
    jobs = []
    try:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_jobs", "arguments": {"query": title, "location": "Lowell, MA", "radius": 50, "remote": True}}}
        r = requests.post(INDEED_MCP_URL, json=payload, headers={"Accept": "application/json"}, timeout=25)
        if r.ok:
            data = r.json()
            text = json.dumps(data.get("result", data))
            for obj in re.findall(r"\{[^{}]*(?:jobTitle|title)[^{}]*\}", text):
                try:
                    j = json.loads(obj)
                    jobs.append({"source": "Indeed", "title": j.get("jobTitle") or j.get("title") or title, "company": j.get("company") or j.get("companyName") or "", "location": j.get("location") or "", "salary": j.get("salary") or "", "url": j.get("url") or j.get("jobUrl") or "", "description": j.get("description") or "", "remote_type": "unknown"})
                except Exception:
                    pass
    except Exception:
        pass
    return jobs[:15]


def scrape_all():
    found = []
    for title in TARGET_TITLES:
        for fn in [scrape_indeed_mcp, scrape_linkedin, scrape_dice, scrape_ziprecruiter]:
            found.extend(fn(title))
        for fn in [scrape_linkedin, scrape_dice, scrape_ziprecruiter]:
            found.extend(fn(title + " remote"))
    new_jobs = []
    for raw in found:
        if not raw.get("title"):
            continue
        saved, is_new = upsert_job(raw)
        if is_new:
            new_jobs.append(saved)
    alert_new_jobs(new_jobs)
    return {"found": len(found), "new": len(new_jobs)}


def send_telegram(job: dict):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        text = f"New job: {job['title']}\n{job.get('company','')} · {job.get('location','')} · {job.get('salary','')}\nScore: {job.get('match_score')} · {job.get('fit_summary')}\n{job.get('url','')}"
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": False}, timeout=10)
    except Exception:
        pass


def send_telegram_summary(count: int):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        text = f"{count} new jobs found — check the dashboard"
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": f"{text}\n{DASHBOARD_URL}", "disable_web_page_preview": True}, timeout=10)
    except Exception:
        pass


def alert_new_jobs(jobs):
    ids = [int(j["id"]) for j in jobs if j.get("id")]
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    with connect() as c:
        unalerted = [rowdict(r) for r in c.execute(f"SELECT * FROM jobs WHERE id IN ({placeholders}) AND alerted=0", ids).fetchall()]
        if len(unalerted) > ALERT_BATCH_LIMIT:
            send_telegram_summary(len(unalerted))
        else:
            for j in unalerted:
                send_telegram(j)
        c.execute(f"UPDATE jobs SET alerted=1 WHERE id IN ({placeholders})", ids)


def load_resume_text() -> str:
    p = Path(RESUME_BUILDER_DB)
    fallback = "Michael Dziegiel: Senior Network Administrator, 20+ years, Azure, Intune, Hyper-V, endpoint, Microsoft 365, Active Directory, networking, security."
    if not p.exists():
        return fallback
    try:
        rc = sqlite3.connect(str(p))
        rc.row_factory = sqlite3.Row
        row = rc.execute("SELECT data_json FROM resumes ORDER BY updated_at DESC LIMIT 1").fetchone()
        if not row:
            return fallback
        data = json.loads(row["data_json"])
        parts = [json.dumps(data.get("contact", {})), data.get("summary", "")]
        parts.extend(json.dumps(x) for x in data.get("experience", []))
        parts.append(json.dumps(data.get("skills", [])))
        return "\n".join(parts)[:10000]
    except Exception:
        return fallback


def generate_content(job: dict, kind: str) -> str:
    resume = load_resume_text()
    if kind == "cover_letter":
        fallback = f"Dear Hiring Manager,\n\nI am writing to express interest in the {job['title']} role at {job.get('company') or 'your organization'}. My background as a Senior Network Administrator with 20+ years across Microsoft 365, Azure, Intune, endpoint management, Hyper-V, networking, and security aligns well with the role requirements.\n\nI would welcome the opportunity to discuss how my infrastructure and endpoint experience can help your team deliver secure, reliable IT operations.\n\nSincerely,\nMichael Dziegiel"
        prompt = "Return JSON only as {\"content\":\"...\"}. Write a professional tailored cover letter for Michael Dziegiel. Resume/profile: " + resume + " Job: " + json.dumps(job)[:8000]
    else:
        fallback = f"Michael Dziegiel\nSenior Network Administrator\n\nSummary\nSenior Network Administrator with 20+ years of experience in Azure, Intune, Hyper-V, endpoint engineering, Microsoft 365, Active Directory, networking, and IT operations. Tailored target: {job['title']} at {job.get('company') or 'target employer'}.\n\nCore Skills\nAzure, Intune, Microsoft 365, Endpoint Management, Hyper-V, Active Directory, Networking, Security, Troubleshooting."
        prompt = "Return JSON only as {\"content\":\"...\"}. Create an ATS-optimized resume version for Michael Dziegiel. Keep it truthful. Resume/profile: " + resume + " Job: " + json.dumps(job)[:8000]
    out = claude_json(prompt)
    return (out or {}).get("content") or fallback


async def cron_loop():
    await asyncio.sleep(8)
    while True:
        try:
            await asyncio.to_thread(scrape_all)
        except Exception as exc:
            print("scrape failed", exc, flush=True)
        await asyncio.sleep(6 * 60 * 60)


@asynccontextmanager
async def lifespan(app):
    init_db()
    dedupe_jobs()
    backfill_linkedin_companies()
    task = asyncio.create_task(cron_loop()) if SCHEDULER_ENABLED else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Job Watch Assistant", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/api/health")
def health():
    with connect() as c:
        total = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    return {"status": "ok", "database": DB_PATH, "jobs": total, "scheduler": SCHEDULER_ENABLED, "sources": ["Indeed MCP", "LinkedIn", "Dice", "ZipRecruiter"]}


@app.get("/api/dashboard")
def dashboard():
    today = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with connect() as c:
        total = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        new_today = c.execute("SELECT COUNT(*) FROM jobs WHERE first_seen>=?", (today,)).fetchone()[0]
        applied = c.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('Applied','Interview','Offer')").fetchone()[0]
        statuses = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
        dist = {r["bucket"]: r["n"] for r in c.execute("SELECT CASE WHEN match_score>=80 THEN '80-100' WHEN match_score>=60 THEN '60-79' WHEN match_score>=40 THEN '40-59' ELSE '0-39' END bucket, COUNT(*) n FROM jobs GROUP BY bucket")}
    return {"new_today": new_today, "total": total, "applied": applied, "statuses": statuses, "score_distribution": dist}


@app.get("/api/jobs")
def jobs(status: str = "", q: str = "", min_score: int = 0, remote_type: str = "", source: str = ""):
    where, args = [], []
    if status:
        where.append("status=?"); args.append(status)
    if q:
        where.append("(title LIKE ? OR company LIKE ? OR location LIKE ?)"); args += [f"%{q}%"] * 3
    if min_score:
        where.append("match_score>=?"); args.append(min_score)
    if remote_type:
        where.append("remote_type=?"); args.append(remote_type)
    if source:
        where.append("source=?"); args.append(source)
    sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ideal_match DESC, match_score DESC, first_seen DESC LIMIT 500"
    with connect() as c:
        return [rowdict(r) for r in c.execute(sql, args).fetchall()]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    with connect() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not r:
            raise HTTPException(404, "job not found")
        docs = [dict(x) for x in c.execute("SELECT * FROM generated_docs WHERE job_id=? ORDER BY created_at DESC", (job_id,)).fetchall()]
    d = rowdict(r); d["generated_docs"] = docs
    return d


@app.post("/api/jobs")
def add_job(payload: ManualJob):
    saved, _ = upsert_job(payload.model_dump())
    return saved


@app.post("/api/jobs/{job_id}/status")
def set_status(job_id: int, payload: StatusIn):
    if payload.status not in STATUSES:
        raise HTTPException(400, "bad status")
    with connect() as c:
        c.execute("UPDATE jobs SET status=? WHERE id=?", (payload.status, job_id))
    return get_job(job_id)


@app.post("/api/jobs/{job_id}/notes")
def set_notes(job_id: int, payload: NoteIn):
    with connect() as c:
        c.execute("UPDATE jobs SET notes=? WHERE id=?", (payload.notes, job_id))
    return get_job(job_id)


@app.post("/api/scrape")
def run_scrape():
    return scrape_all()


@app.post("/api/jobs/{job_id}/generate")
def generate(job_id: int, payload: GenerateIn):
    j = get_job(job_id)
    kind = "cover_letter" if payload.kind == "cover_letter" else "tailored_resume"
    content = generate_content(j, kind)
    title = ("Cover Letter" if kind == "cover_letter" else "Tailored Resume") + " - " + j["title"]
    with connect() as c:
        c.execute("INSERT INTO generated_docs(job_id,kind,title,content,created_at) VALUES(?,?,?,?,?)", (job_id, kind, title, content, utcnow()))
        doc_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    return {"id": doc_id, "job_id": job_id, "kind": kind, "title": title, "content": content}


@app.get("/api/docs/{doc_id}/export/{fmt}")
def export_doc(doc_id: int, fmt: str):
    with connect() as c:
        d = c.execute("SELECT * FROM generated_docs WHERE id=?", (doc_id,)).fetchone()
    if not d:
        raise HTTPException(404, "doc not found")
    title, content = d["title"], d["content"]
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", title).strip("-")[:80] or "document"
    if fmt == "docx":
        out = io.BytesIO(); doc = Document(); doc.add_heading(title, 0)
        for para in content.split("\n"):
            doc.add_paragraph(para.strip())
        doc.save(out)
        return Response(out.getvalue(), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers={"Content-Disposition": f'attachment; filename="{safe}.docx"'})
    if fmt == "pdf":
        out = io.BytesIO(); pdf = SimpleDocTemplate(out, pagesize=letter); styles = getSampleStyleSheet(); story = [Paragraph(html.escape(title), styles["Title"]), Spacer(1, 12)]
        for para in content.split("\n"):
            story.append(Paragraph(html.escape(para) or " ", styles["BodyText"])); story.append(Spacer(1, 6))
        pdf.build(story)
        return Response(out.getvalue(), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{safe}.pdf"'})
    raise HTTPException(400, "fmt must be docx or pdf")


frontend = Path("/app/frontend-dist")
if frontend.exists():
    app.mount("/", StaticFiles(directory=str(frontend), html=True), name="frontend")
