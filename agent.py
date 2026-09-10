#!/usr/bin/env python3
"""
Daily job agent.

Pulls finance roles from company ATS APIs and Singapore job portals, filters them,
scores them against profile.yaml with Claude, and emails a digest.

Usage:
    python agent.py                      normal daily run
    python agent.py --dry-run            run everything, print the digest, send nothing,
                                         save nothing
    python agent.py --check-sources      test every configured source, print pass/fail
    python agent.py --discover "Company" find which ATS a company uses
    python agent.py --no-score           fetch and filter only, zero API cost
    python agent.py --since-days 7       ignore the seen-list, look back N days instead
"""

import argparse
import datetime as dt
import hashlib
import html as htmllib
import json
import os
import re
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
DIGEST_DIR = ROOT / "digests"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) job-alert/1.0"}
TIMEOUT = 25
API_URL = "https://api.anthropic.com/v1/messages"
SGT = dt.timezone(dt.timedelta(hours=8))


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def load_local_env():
    """Read secrets.env sitting next to this script, if present.

    Lets you keep keys in a plain text file instead of wrestling with
    environment variables. Real environment variables always win, so this
    is safe to leave in place when running on GitHub Actions.
    """
    path = ROOT / "secrets.env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and val and key not in os.environ:
            os.environ[key] = val


def log(msg):
    print(f"[{dt.datetime.now(SGT):%H:%M:%S}] {msg}", flush=True)


def http(method, url, **kw):
    """One HTTP call with two retries. Returns Response or raises."""
    kw.setdefault("timeout", TIMEOUT)
    headers = dict(UA)
    headers.update(kw.pop("headers", {}))
    last = None
    for attempt in range(3):
        try:
            r = requests.request(method, url, headers=headers, **kw)
            if r.status_code in (429, 502, 503, 504):
                time.sleep(2 * (attempt + 1))
                last = requests.HTTPError(f"{r.status_code} from {url}")
                continue
            if 400 <= r.status_code < 500:
                raise requests.HTTPError(f"{r.status_code} from {url}")
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            if re.match(r"4\d\d ", str(e)):
                raise
            last = e
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def strip_html(s):
    if not s:
        return ""
    if "<" not in s and "&lt;" in s:          # Greenhouse-style escaped HTML
        s = htmllib.unescape(s)
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmllib.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def job_id(company, title, url):
    clean = re.sub(r"\s+", " ", title or "").strip().lower()
    raw = "|".join([company or "", clean, url or ""])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def norm(company, source, title, location, url, description="",
         posted=None, detail=None, salary=None):
    return {
        "id": job_id(company, title or "", url or ""),
        "company": company,
        "source": source,
        "title": (title or "").strip(),
        "location": (location or "").strip(),
        "url": url or "",
        "description": description or "",
        "posted": posted,
        "detail": detail,      # {"kind": ..., ...} for lazy description fetch
        "salary": salary,
    }


# --------------------------------------------------------------------------
# source adapters — each returns a list of normalised postings
# --------------------------------------------------------------------------

def fetch_greenhouse(entry):
    slug = entry["slug"]
    r = http("GET", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in r.json().get("jobs", []):
        out.append(norm(
            entry["company"], "greenhouse",
            j.get("title"),
            (j.get("location") or {}).get("name", ""),
            j.get("absolute_url"),
            strip_html(j.get("content", "")),
            j.get("updated_at"),
        ))
    return out


def fetch_lever(entry):
    slug = entry["slug"]
    r = http("GET", f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in r.json():
        desc = j.get("descriptionPlain") or strip_html(j.get("description", ""))
        for blk in j.get("lists") or []:
            desc += "\n" + strip_html(blk.get("text", "")) + "\n" + strip_html(blk.get("content", ""))
        posted = j.get("createdAt")
        if isinstance(posted, (int, float)):
            posted = dt.datetime.fromtimestamp(posted / 1000, dt.timezone.utc).isoformat()
        out.append(norm(
            entry["company"], "lever",
            j.get("text"),
            (j.get("categories") or {}).get("location", ""),
            j.get("hostedUrl"), desc, posted,
        ))
    return out


def fetch_ashby(entry):
    slug = entry["slug"]
    r = http("GET", f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    out = []
    for j in r.json().get("jobs", []):
        comp = j.get("compensation") or {}
        summary = comp.get("compensationTierSummary") if isinstance(comp, dict) else None
        out.append(norm(
            entry["company"], "ashby",
            j.get("title"),
            j.get("location") or j.get("locationName", ""),
            j.get("jobUrl") or j.get("applyUrl"),
            j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
            j.get("publishedAt") or j.get("updatedAt"),
            salary=summary,
        ))
    return out


def fetch_smartrecruiters(entry):
    """List is cheap; full descriptions cost one call each, so fetch them lazily."""
    slug = entry["slug"]
    out, offset = [], 0
    while offset < 400:
        r = http("GET",
                 f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                 params={"limit": 100, "offset": offset})
        data = r.json()
        items = data.get("content", [])
        for j in items:
            loc = j.get("location") or {}
            loc_str = ", ".join(x for x in [loc.get("city"), loc.get("country")] if x)
            pid = j.get("id") or j.get("uuid")
            out.append(norm(
                entry["company"], "smartrecruiters",
                j.get("name"), loc_str,
                j.get("applyUrl") or f"https://jobs.smartrecruiters.com/{slug}/{pid}",
                "", j.get("releasedDate"),
                detail={"kind": "smartrecruiters", "slug": slug, "id": pid},
            ))
        if len(items) < 100:
            break
        offset += 100
    return out


def fetch_workday(entry):
    host, tenant, site = entry["host"], entry["tenant"], entry["site"]
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    out = []
    for term in ("finance", "controller"):
        offset = 0
        while offset < 200:
            r = http("POST", f"{base}/jobs",
                     headers={"Content-Type": "application/json", "Accept": "application/json"},
                     json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term})
            data = r.json()
            posts = data.get("jobPostings", [])
            for j in posts:
                path = j.get("externalPath", "")
                out.append(norm(
                    entry["company"], "workday",
                    j.get("title"),
                    j.get("locationsText", ""),
                    f"https://{host}/{site}{path}",
                    "", j.get("postedOn"),
                    detail={"kind": "workday", "base": base, "path": path},
                ))
            if len(posts) < 20:
                break
            offset += 20
    seen, uniq = set(), []
    for j in out:
        if j["id"] not in seen:
            seen.add(j["id"])
            uniq.append(j)
    return uniq


def fetch_mcf(cfg):
    """MyCareersFuture. Endpoint shape has changed before; both forms are tried."""
    out = []
    for q in cfg.get("queries", []):
        data = None
        try:
            r = http("POST", "https://api.mycareersfuture.gov.sg/v2/search",
                     params={"limit": 60, "page": 0},
                     headers={"Content-Type": "application/json"},
                     json={"search": q, "sessionId": "", "sortBy": ["new_posting_date"]})
            data = r.json()
        except Exception:
            try:
                r = http("GET", "https://api.mycareersfuture.gov.sg/v2/jobs",
                         params={"search": q, "limit": 60, "page": 0,
                                 "sortBy": "new_posting_date"})
                data = r.json()
            except Exception as e:  # noqa: BLE001
                log(f"  mcf '{q}' failed: {e}")
                continue

        for j in (data or {}).get("results", []):
            meta = j.get("metadata") or {}
            sal = j.get("salary") or {}
            lo, hi = sal.get("minimum"), sal.get("maximum")
            floor = cfg.get("min_salary_monthly", 0) or 0
            if floor and hi and hi < floor:
                continue
            uuid = meta.get("jobPostId") or j.get("uuid", "")
            addr = j.get("address") or {}
            out.append(norm(
                (j.get("postedCompany") or {}).get("name", "Unknown"),
                "mycareersfuture",
                j.get("title"),
                addr.get("district") or "Singapore",
                f"https://www.mycareersfuture.gov.sg/job/{uuid}",
                strip_html(j.get("description", "")),
                meta.get("newPostingDate"),
                salary=(f"SGD {lo:,}-{hi:,}/mo" if lo and hi else None),
            ))
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
}


def hydrate(job):
    """Fetch the description for sources that need a second call."""
    d = job.get("detail")
    if not d or job.get("description"):
        return job
    try:
        if d["kind"] == "smartrecruiters":
            r = http("GET", f"https://api.smartrecruiters.com/v1/companies/{d['slug']}/postings/{d['id']}")
            j = r.json()
            ad = (j.get("jobAd") or {}).get("sections") or {}
            parts = []
            for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
                parts.append(strip_html(((ad.get(key) or {}).get("text")) or ""))
            job["description"] = "\n\n".join(p for p in parts if p)
        elif d["kind"] == "workday":
            r = http("GET", d["base"] + d["path"],
                     headers={"Accept": "application/json"})
            info = r.json().get("jobPostingInfo") or {}
            job["description"] = strip_html(info.get("jobDescription", ""))
            job["location"] = info.get("location") or job["location"]
    except Exception as e:  # noqa: BLE001
        log(f"  hydrate failed for {job['company']} / {job['title']}: {e}")
    return job


# --------------------------------------------------------------------------
# filtering
# --------------------------------------------------------------------------

def build_filters(profile):
    f = profile["filters"]
    flags = re.I | re.X
    return (
        re.compile(f["title_include"], flags),
        re.compile(f["title_exclude"], flags),
        re.compile(f["location_include"], flags),
    )


def prefilter(jobs, profile):
    inc, exc, loc = build_filters(profile)
    kept = []
    for j in jobs:
        t = j["title"]
        if not t or not inc.search(t) or exc.search(t):
            continue
        l = j["location"]
        if l and not loc.search(l):
            continue
        kept.append(j)
    return kept


def sg_confirm(jobs):
    """Drop anything that never mentions Singapore once we have the full text."""
    out = []
    for j in jobs:
        blob = f"{j['location']} {j['description'][:4000]}"
        if re.search(r"singapore|\bsg\b|apac|asia[- ]pacific|southeast asia", blob, re.I):
            out.append(j)
    return out


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

class ClaudeAuthError(RuntimeError):
    """401/403: the key is wrong or has no access. Never worth retrying."""


def clamp(v, lo, hi, default):
    try:
        return max(lo, min(int(float(v)), hi))
    except (TypeError, ValueError):
        return default


def claude(model, system, user, max_tokens=4096):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ClaudeAuthError("ANTHROPIC_API_KEY is not set")
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
    }
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    last = None
    for attempt in range(4):
        try:
            r = requests.post(API_URL, headers=headers, json=payload, timeout=240)
        except requests.RequestException as e:
            last = e
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code in (401, 403):
            raise ClaudeAuthError(f"Claude API {r.status_code}: {r.text[:200]}")
        if r.status_code in (429, 500, 502, 503, 529):
            last = RuntimeError(f"Claude API {r.status_code}: {r.text[:200]}")
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Claude API {r.status_code}: {r.text[:400]}")
        body = r.json()
        return "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
    raise last


def parse_json(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    m = re.search(r"(\[.*\]|\{.*\})", t, re.S)
    if not m:
        raise ValueError("no JSON found")
    return json.loads(m.group(1))


def system_prompt(profile):
    c = profile["candidate"]
    t = profile["targets"]
    r = profile["rubric"]
    return f"""You screen job postings for one specific candidate. You are blunt and you do not flatter. A false positive wastes his week; say so when a role is weak.

CANDIDATE
Name: {c['name']}
Location: {c['location']}
Status: {c['work_status']}
Available: {c['available_from']}
Languages: {c['languages']}

{c['summary']}

TARGET TITLES
{chr(10).join('- ' + x for x in t['titles_wanted'])}

Geography: {t['geography']}
Industry: {t['industry_preference']}
Compensation: {t['compensation']}

HARD GATES (any one triggers rejection, score 0)
{chr(10).join('- ' + x for x in r['hard_gates'])}

SCORING RUBRIC — weights in the key name, guidance in the text. Total 100.
{yaml.safe_dump(r['weights'], sort_keys=False, width=100)}

RESUME VARIANTS — tag every role with exactly one:
  V1 = FP&A / Finance Business Partner / Commercial Finance / Senior Finance Manager
  V2 = Financial Controller / Assistant or Regional Controller / Finance Operations and Revenue roles
  V3 = Business Controller / Controlling Manager / BU Controller (P&L steering, performance management, governance)

Score 0-100. Be strict. A score above 78 means he should apply this week; reserve it.
Most postings should land between 30 and 65. Output valid JSON only, no prose around it."""


def triage(jobs, profile, model):
    """Cheap first pass. Returns {id: score}."""
    sysmsg = system_prompt(profile)
    scores = {}
    for i in range(0, len(jobs), 12):
        batch = jobs[i:i + 12]
        payload = [{
            "id": j["id"],
            "company": j["company"],
            "title": j["title"],
            "location": j["location"],
            "description": j["description"][:1800],
        } for j in batch]
        user = ("Score each posting 0-100 on overall fit.\n\n"
                "Return a JSON array only: "
                '[{"id":"...","score":0,"reject_reason":null}]\n'
                "reject_reason is a short string if a hard gate fired, otherwise null.\n\n"
                + json.dumps(payload, ensure_ascii=False))
        rows = None
        for attempt in range(2):
            try:
                rows = parse_json(claude(model, sysmsg, user, max_tokens=1500))
                break
            except ClaudeAuthError:
                raise
            except Exception as e:  # noqa: BLE001
                log(f"  triage batch {i // 12 + 1} attempt {attempt + 1} failed: {str(e)[:80]}")
        if rows is None:
            log("  passing batch through at 60")
            for j in batch:
                scores[j["id"]] = 60
        else:
            batch_ids = {j["id"] for j in batch}
            for row in rows:
                if isinstance(row, dict) and row.get("id") in batch_ids:
                    scores[row["id"]] = clamp(row.get("score"), 0, 100, 50)
            missing = [j["id"] for j in batch if j["id"] not in scores]
            for jid in missing:
                scores[jid] = 50
            if missing:
                log(f"  {len(missing)} postings unscored by model, defaulted to 50")
        log(f"  triaged {min(i + 12, len(jobs))}/{len(jobs)}")
    return scores


def deep_review(jobs, profile, model):
    """Full write-up on the shortlist. Returns {id: dict}."""
    sysmsg = system_prompt(profile)
    out = {}
    for i in range(0, len(jobs), 4):
        batch = jobs[i:i + 4]
        payload = [{
            "id": j["id"],
            "company": j["company"],
            "title": j["title"],
            "location": j["location"],
            "salary": j.get("salary"),
            "description": j["description"][:7000],
        } for j in batch]
        caps = dict(profile["rubric"]["weights"])
        schema = ",".join(f'"{k}":0' for k in caps)
        cap_text = ", ".join(f"{k} max {v}" for k, v in caps.items())
        user = (
            "Assess each posting properly. Return a JSON array only:\n"
            '[{"id":"...",'
            '"scores":{' + schema + '},'
            '"variant":"V1|V2|V3",'
            '"seniority":"step up|lateral with more scope|lateral|step down",'
            '"company_tier":"A|B|C|D",'
            '"why_fit":"one or two sentences, concrete, cite what in his background answers this job",'
            '"why_not":"one or two sentences on the weakest part of his case for this role, or what the job wants that he cannot evidence",'
            '"headroom":"one short sentence on whether there is a level above this seat",'
            '"red_flags":["short strings, empty array if none"],'
            '"tailoring_note":"one sentence on what to foreground if he applies"}]\n\n'
            "Each sub-score is capped at its weight: " + cap_text + ". "
            "Score every dimension independently — a strong brand does not lift a weak scope score, "
            "and a weak brand does not lower a strong fit score.\n\n"
            + json.dumps(payload, ensure_ascii=False))
        try:
            for row in parse_json(claude(model, sysmsg, user, max_tokens=4096)):
                if not isinstance(row, dict) or "id" not in row:
                    continue
                s = row.get("scores") or {}
                if isinstance(s, dict) and any(k in s for k in caps):
                    clean = {k: clamp(s.get(k, 0), 0, cap, 0) for k, cap in caps.items()}
                    row["scores"] = clean
                    row["score"] = sum(clean.values())
                else:
                    row.pop("scores", None)
                    row["score"] = None          # caller keeps the triage score
                if row.get("variant") not in ("V1", "V2", "V3"):
                    row["variant"] = "V1"
                if not isinstance(row.get("red_flags"), list):
                    row["red_flags"] = []
                out[row["id"]] = row
        except ClaudeAuthError:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"  deep batch {i // 4 + 1} failed: {e}")
        log(f"  reviewed {min(i + 4, len(jobs))}/{len(jobs)}")
    return out


# --------------------------------------------------------------------------
# publishing for an external scorer (Cowork reads these by URL)
# --------------------------------------------------------------------------

LATEST_DIR_NAME = "latest"
CHUNK = 12               # postings per file, so each file stays well under a fetch limit
DESC_CAP = 1600          # enough of a description to score from


def publish_latest(dump, stamp, stats):
    """Write digests/latest/index.json + part-NN.json, overwritten each run."""
    latest = DIGEST_DIR / LATEST_DIR_NAME
    latest.mkdir(parents=True, exist_ok=True)
    for old in latest.glob("part-*.json"):
        old.unlink()
    parts = 0
    for i in range(0, len(dump), CHUNK):
        parts += 1
        chunk = [{**d, "description": (d.get("description") or "")[:DESC_CAP]} for d in dump[i:i + CHUNK]]
        (latest / f"part-{parts:02d}.json").write_text(json.dumps(chunk, ensure_ascii=False, indent=1))
    (latest / "index.json").write_text(json.dumps({
        "date": stamp,
        "count": len(dump),
        "parts": parts,
        "files": [f"part-{n:02d}.json" for n in range(1, parts + 1)],
        "source_health": ", ".join(f"{k}: {v}" for k, v in stats.items()),
    }, ensure_ascii=False, indent=1))
    log(f"published digests/latest/: {len(dump)} postings in {parts} part(s)")


# --------------------------------------------------------------------------
# applications log
# --------------------------------------------------------------------------

OPEN_STATUSES = ("applied", "screening", "interview", "offer")
CLOSED_STATUSES = ("rejected", "withdrawn", "ghosted", "declined")


def load_applications():
    """Read applications.yaml. Tolerant: bad entries are skipped and logged, never fatal."""
    p = ROOT / "applications.yaml"
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text()) or []
    except yaml.YAMLError as e:
        log(f"applications.yaml is not valid YAML ({str(e)[:60]}); pipeline section skipped")
        return []
    if not isinstance(data, list):
        log("applications.yaml should be a list of entries; pipeline section skipped")
        return []
    out = []
    for i, e in enumerate(data):
        if not isinstance(e, dict) or not e.get("company") or not e.get("title"):
            log(f"applications.yaml entry {i + 1} skipped: needs at least company and title")
            continue
        if str(e.get("company")).strip().lower() == "example co":
            continue
        raw_date = e.get("applied")
        try:
            applied = raw_date if isinstance(raw_date, dt.date) else dt.date.fromisoformat(str(raw_date))
        except (TypeError, ValueError):
            applied = None
        out.append({
            "company": str(e["company"]).strip(),
            "title": str(e["title"]).strip(),
            "applied": applied,
            "status": str(e.get("status") or "applied").strip().lower(),
            "resume": str(e.get("resume") or "").strip(),
            "via": str(e.get("via") or "").strip(),
            "notes": str(e.get("notes") or "").strip(),
            "url": str(e.get("url") or "").strip(),
        })
    return out


def _key(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def mark_applied(jobs, apps):
    """Flag postings that match something already in the log (company + title, or URL)."""
    for j in jobs:
        jc, jt = _key(j["company"]), _key(j["title"])
        for a in apps:
            ac, at = _key(a["company"]), _key(a["title"])
            if a["url"] and a["url"] == j["url"]:
                j["applied_already"] = a
                break
            if ac and (ac in jc or jc in ac) and at and (at in jt or jt in at):
                j["applied_already"] = a
                break
    return jobs


def pipeline_summary(apps, today=None, follow_up_days=7):
    today = today or dt.datetime.now(SGT).date()
    open_ = [a for a in apps if a["status"] not in CLOSED_STATUSES]
    closed = [a for a in apps if a["status"] in CLOSED_STATUSES]
    for a in open_:
        a["days"] = (today - a["applied"]).days if a["applied"] else None
        a["follow_up"] = (a["status"] == "applied" and a["days"] is not None
                          and a["days"] >= follow_up_days)
    open_.sort(key=lambda a: (a["status"] != "offer", a["status"] != "interview",
                              a["status"] != "screening", -(a["days"] or 0)))
    counts = {s: sum(1 for a in apps if a["status"] == s) for s in OPEN_STATUSES}
    return {"open": open_, "closed": closed, "counts": counts, "total": len(apps)}


# --------------------------------------------------------------------------
# digest
# --------------------------------------------------------------------------

DIM_LABELS = {
    "seniority_and_scope": "Level",
    "company_brand": "Brand",
    "headroom_and_growth": "Growth",
    "job_family_fit": "Fit",
    "evidence_overlap": "Chances",
}


def esc(s):
    return htmllib.escape(str(s or ""))


def build_digest(scored, stats, profile, sources, pipeline=None):
    bands = profile["rubric"]["bands"]
    today = dt.datetime.now(SGT).strftime("%a %d %b %Y")
    scored.sort(key=lambda x: -x["score"])

    dup = [j for j in scored if j.get("applied_already")]
    fresh = [j for j in scored if not j.get("applied_already")]
    top = [j for j in fresh if j["score"] >= bands["apply_now"]]
    mid = [j for j in fresh if bands["worth_a_look"] <= j["score"] < bands["apply_now"]]
    rest = [j for j in fresh if j["score"] < bands["worth_a_look"]]

    md = [f"# Job digest — {today}", ""]
    md.append(f"{len(scored)} new postings passed the filter. "
              f"{len(top)} apply now, {len(mid)} worth a look, {len(rest)} logged"
              + (f", {len(dup)} already applied." if dup else "."))
    md.append("")

    DIMS = [(DIM_LABELS.get(k, k.replace("_", " ").title()), k, v)
            for k, v in profile["rubric"]["weights"].items()]

    def breakdown_md(r):
        s = r.get("scores") or {}
        if not s:
            return None
        return " · ".join(f"{label} {s.get(key, 0)}/{cap}" for label, key, cap in DIMS)

    def block(j):
        r = j.get("review") or {}
        lines = [f"### {j['title']} — {j['company']}  ·  {j['score']}/100",
                 f"{j['location']}  ·  {j['source']}"
                 + (f"  ·  {j['salary']}" if j.get("salary") else "")]
        b = breakdown_md(r)
        if b:
            lines.append(f"`{b}`")
        meta = [f"**{r['seniority'].title()}**" if r.get("seniority") else None,
                f"Tier {r['company_tier']}" if r.get("company_tier") else None,
                f"Resume {r['variant']}" if r.get("variant") else None]
        if any(meta):
            lines.append(" · ".join(m for m in meta if m))
        if r.get("why_fit"):
            lines.append(f"**Fit.** {r['why_fit']}")
        if r.get("why_not"):
            lines.append(f"**Gap.** {r['why_not']}")
        if r.get("headroom"):
            lines.append(f"**Headroom.** {r['headroom']}")
        if r.get("red_flags"):
            lines.append(f"**Flags.** {'; '.join(r['red_flags'])}")
        if r.get("tailoring_note"):
            lines.append(f"**If applying.** {r['tailoring_note']}")
        lines.append(f"[Open posting]({j['url']})")
        return "\n\n".join(lines)

    def mdsafe(t):
        return str(t or "").replace("[", "(").replace("]", ")")

    if top:
        md += ["## Apply this week", ""] + [block(j) for j in top] + [""]
    if mid:
        md += ["## Worth a look", ""] + [block(j) for j in mid] + [""]

    if pipeline and pipeline["total"]:
        c = pipeline["counts"]
        md += ["## Your applications", "",
               f"{pipeline['total']} logged · {c['applied']} applied · {c['screening']} screening · "
               f"{c['interview']} interview · {c['offer']} offer · {len(pipeline['closed'])} closed", ""]
        for a in pipeline["open"]:
            age = f"{a['days']}d ago" if a["days"] is not None else "date unknown"
            flag = "  ·  **FOLLOW UP**" if a.get("follow_up") else ""
            md.append(f"- **{a['status'].title()}** · {a['company']} — {a['title']} · {age}"
                      + (f" · {a['resume']}" if a["resume"] else "") + flag)
        if dup:
            md += ["", "Already applied, not re-listed: "
                   + "; ".join(f"{j['company']} — {j['title']}" for j in dup)]
        md.append("")

    if rest:
        md += ["## Also new", ""]
        md += [f"- {j['score']} · [{mdsafe(j['title'])} — {mdsafe(j['company'])}]({j['url']}) · {j['location']}"
               for j in rest] + [""]

    md += ["## Recruiter desks — check these yourself", ""]
    md += [f"- [{s['name']}]({s['url']})" for s in sources.get("search_firms", [])]
    md += ["", "---", "", "**Source health**", ""]
    md += [f"- {k}: {v}" for k, v in stats.items()]
    markdown = "\n".join(md)

    # ---- html ----
    def h_block(j, accent):
        r = j.get("review") or {}
        s = r.get("scores") or {}
        bars = ""
        if s:
            cells = ""
            for label, key, cap in DIMS:
                v = s.get(key, 0)
                pct = int(round(100 * v / cap)) if cap else 0
                shade = "#1a7f37" if pct >= 75 else ("#9a6700" if pct >= 50 else "#b3261e")
                cells += (
                    f"<td style='padding:0 10px 0 0;vertical-align:top'>"
                    f"<div style='font-size:11px;color:#666'>{label} "
                    f"<b style='color:#111'>{v}</b>/{cap}</div>"
                    f"<div style='height:4px;background:#e4e4e4;border-radius:2px;margin-top:3px;width:70px'>"
                    f"<div style='height:4px;width:{pct}%;background:{shade};border-radius:2px'></div>"
                    f"</div></td>")
            bars = f"<table style='border-collapse:collapse;margin:2px 0 10px'><tr>{cells}</tr></table>"
        rows = ""
        for label, key in (("Fit", "why_fit"), ("Gap", "why_not"),
                           ("Headroom", "headroom"), ("If applying", "tailoring_note")):
            if r.get(key):
                rows += (f"<p style='margin:6px 0;font-size:14px;line-height:1.5'>"
                         f"<b>{label}.</b> {esc(r[key])}</p>")
        if r.get("red_flags"):
            rows += (f"<p style='margin:6px 0;font-size:14px;color:#b3261e'>"
                     f"<b>Flags.</b> {esc('; '.join(r['red_flags']))}</p>")
        meta = " · ".join(filter(None, [
            esc(j["location"]), esc(j["source"]), esc(j.get("salary")),
            esc(r.get("seniority", "").title()) or None,
            f"Tier {esc(r.get('company_tier'))}" if r.get("company_tier") else None,
            f"Resume {esc(r.get('variant'))}" if r.get("variant") else None,
        ]))
        return f"""
        <div style="border-left:4px solid {accent};padding:12px 16px;margin:14px 0;background:#fafafa">
          <div style="font-size:17px;font-weight:600">{esc(j['title'])}
            <span style="color:#666;font-weight:400"> — {esc(j['company'])}</span>
            <span style="float:right;color:{accent};font-weight:700">{j['score']}</span>
          </div>
          <div style="font-size:12px;color:#777;margin:4px 0 6px">{meta}</div>
          {bars}
          {rows}
          <a href="{esc(j['url'])}" style="font-size:14px">Open posting →</a>
        </div>"""

    parts = [f"<div style='font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
             f"max-width:680px;margin:0 auto;color:#111'>",
             f"<h2 style='margin-bottom:2px'>Job digest — {today}</h2>",
             f"<p style='color:#666;font-size:14px;margin-top:0'>{len(scored)} new · "
             f"{len(top)} apply now · {len(mid)} worth a look · {len(rest)} logged</p>"]
    if top:
        parts.append("<h3 style='margin-top:26px'>Apply this week</h3>")
        parts += [h_block(j, "#1a7f37") for j in top]
    if mid:
        parts.append("<h3 style='margin-top:26px'>Worth a look</h3>")
        parts += [h_block(j, "#9a6700") for j in mid]
    if pipeline and pipeline["total"]:
        c = pipeline["counts"]
        parts.append("<h3 style='margin-top:26px'>Your applications</h3>"
                     f"<p style='font-size:13px;color:#666;margin:0 0 8px'>{pipeline['total']} logged · "
                     f"{c['applied']} applied · {c['screening']} screening · {c['interview']} interview · "
                     f"{c['offer']} offer · {len(pipeline['closed'])} closed</p>"
                     "<table style='border-collapse:collapse;font-size:13px;width:100%'>")
        for a in pipeline["open"]:
            age = f"{a['days']}d" if a["days"] is not None else "?"
            colour = {"offer": "#1a7f37", "interview": "#1a7f37", "screening": "#9a6700"}.get(a["status"], "#666")
            flag = ("<span style='color:#b3261e;font-weight:600'> follow up</span>" if a.get("follow_up") else "")
            parts.append(f"<tr style='border-bottom:1px solid #eee'>"
                         f"<td style='padding:5px 8px 5px 0;color:{colour};font-weight:600;white-space:nowrap'>{esc(a['status'].title())}</td>"
                         f"<td style='padding:5px 8px 5px 0'>{esc(a['company'])} — {esc(a['title'])}</td>"
                         f"<td style='padding:5px 8px 5px 0;color:#888;white-space:nowrap'>{esc(age)}{' · ' + esc(a['resume']) if a['resume'] else ''}{flag}</td></tr>")
        parts.append("</table>")
        if dup:
            parts.append("<p style='font-size:12px;color:#888;margin-top:8px'>Already applied, not re-listed: "
                         + esc("; ".join(f"{j['company']} — {j['title']}" for j in dup)) + "</p>")
    if rest:
        parts.append("<h3 style='margin-top:26px'>Also new</h3><ul style='font-size:14px'>")
        parts += [f"<li>{j['score']} · <a href='{esc(j['url'])}'>{esc(j['title'])} — "
                  f"{esc(j['company'])}</a> · {esc(j['location'])}</li>" for j in rest]
        parts.append("</ul>")
    parts.append("<h3 style='margin-top:26px'>Recruiter desks</h3><ul style='font-size:14px'>")
    parts += [f"<li><a href='{esc(s['url'])}'>{esc(s['name'])}</a></li>"
              for s in sources.get("search_firms", [])]
    parts.append("</ul>")
    parts.append("<p style='font-size:11px;color:#999;margin-top:30px'>"
                 + esc(" · ".join(f"{k}: {v}" for k, v in stats.items())) + "</p></div>")
    return markdown, "".join(parts)


def send_email(subject, markdown, html_body):
    host = os.environ.get("SMTP_HOST")
    to = os.environ.get("DIGEST_TO")
    if not host or not to:
        log("SMTP not configured; digest saved to file only")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_USER", to)
    msg["To"] = to
    msg.set_content(markdown)
    msg.add_alternative(html_body, subtype="html")
    port = int(os.environ.get("SMTP_PORT", 587))
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=45, context=ctx) as s:
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=45) as s:
                s.starttls(context=ctx)
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                s.send_message(msg)
    except Exception as e:  # noqa: BLE001
        log(f"EMAIL FAILED: {str(e)[:120]}")
        log("state not saved; today's postings will be sent again on the next run")
        raise
    log(f"emailed to {to}")
    return True


# --------------------------------------------------------------------------
# maintenance modes
# --------------------------------------------------------------------------

def setup_check(profile, sources):
    """Plain-English readiness check. Run this before anything else."""
    ok, warn, bad = "  [OK]   ", "  [!]    ", "  [X]   "
    print("\nSETUP CHECK\n" + "=" * 58)

    v = sys.version_info
    print((ok if v >= (3, 9) else bad) + f"Python {v.major}.{v.minor}.{v.micro}")

    try:
        import requests, yaml  # noqa: F401
        print(ok + "Required packages installed")
    except ImportError as e:
        print(bad + f"Missing package: {e.name}. Run: pip install -r requirements.txt")

    n = sum(len(sources.get(k, [])) for k in FETCHERS)
    print(ok + f"profile.yaml and sources.yaml load cleanly ({n} company boards)")

    # --- Anthropic key
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print(warn + "ANTHROPIC_API_KEY not found. Not needed if Claude in Cowork does the scoring.")
        print("        For the GitHub/API path, put it in a file called secrets.env next to this script:")
        print("        ANTHROPIC_API_KEY=sk-ant-...")
    elif not key.startswith("sk-ant"):
        print(warn + "ANTHROPIC_API_KEY found but doesn't look like an Anthropic key")
    else:
        print(ok + f"ANTHROPIC_API_KEY found ({key[:11]}..., {len(key)} chars)")
        try:
            claude(profile["models"]["triage"], "Reply with the single word: ready",
                   "ping", max_tokens=8)
            print(ok + "API key works and has credit")
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "401" in msg or "authentication" in msg.lower():
                print(bad + "API key rejected. Check you copied the whole thing.")
            elif "credit" in msg.lower() or "402" in msg or "billing" in msg.lower():
                print(bad + "API key valid but no credit. Add credit in the console.")
            else:
                print(bad + f"API call failed: {msg[:120]}")

    # --- email
    host, user, pw, to = (os.environ.get(k) for k in
                          ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "DIGEST_TO"))
    if not any([host, user, pw, to]):
        print(warn + "Email not configured. The digest will still be saved to digests/")
    elif not all([host, user, pw, to]):
        missing = [k for k, x in zip(("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "DIGEST_TO"),
                                     (host, user, pw, to)) if not x]
        print(bad + f"Email half-configured. Missing: {', '.join(missing)}")
    else:
        try:
            with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587)), timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(user, pw)
            print(ok + f"Email works, digest will go to {to}")
        except smtplib.SMTPAuthenticationError:
            print(bad + "Email login rejected. Gmail needs an app password, "
                        "not your normal password.")
        except Exception as e:  # noqa: BLE001
            print(bad + f"Email connection failed: {str(e)[:100]}")

    # --- state
    if STATE_PATH.exists():
        seen = len(json.loads(STATE_PATH.read_text()).get("seen", {}))
        print(ok + f"Memory file present, {seen} postings already seen")
    else:
        print(warn + "No memory file yet. First run will treat everything as new.")

    print("=" * 58)
    print("Next: python agent.py --check-sources")
    print("Then: python agent.py --dry-run\n")


def check_sources(sources):
    rows = []
    for kind, fetcher in FETCHERS.items():
        for entry in sources.get(kind, []):
            try:
                jobs = fetcher(entry)
                rows.append((kind, entry["company"], "OK", f"{len(jobs)} postings"))
            except Exception as e:  # noqa: BLE001
                rows.append((kind, entry["company"], "FAIL", str(e)[:90]))
    mcf = sources.get("mycareersfuture", {})
    if mcf.get("enabled"):
        try:
            jobs = fetch_mcf({**mcf, "queries": mcf.get("queries", [])[:1]})
            rows.append(("mycareersfuture", "MCF", "OK", f"{len(jobs)} postings"))
        except Exception as e:  # noqa: BLE001
            rows.append(("mycareersfuture", "MCF", "FAIL", str(e)[:90]))

    if not rows:
        print("\nNo sources configured in sources.yaml.")
        return
    w = max(len(r[1]) for r in rows) + 2
    print(f"\n{'SOURCE':<17}{'COMPANY':<{w}}{'':<7}RESULT")
    print("-" * (30 + w))
    for kind, company, status, note in rows:
        print(f"{kind:<17}{company:<{w}}{status:<7}{note}")
    bad = [r for r in rows if r[2] == "FAIL"]
    print(f"\n{len(rows) - len(bad)} working, {len(bad)} broken.")
    if bad:
        print("Broken entries (paste this back to Claude to get them fixed):")
        for kind, company, _, note in bad:
            print(f"  {kind} / {company}: {note}")


def slug_candidates(name):
    """Plausible ATS slugs for a company name, most likely first."""
    base = re.sub(r"\.(com|io|ai|sg|co)\b", "", name.lower())
    base = re.sub(r"\b(inc|ltd|pte|llc|group|holdings|technologies|technology|labs)\b", " ", base)
    words = [w for w in re.sub(r"[^a-z0-9 ]+", " ", base).split() if w]
    if not words:
        return []
    joined, hyph, first = "".join(words), "-".join(words), words[0]
    cands = [joined, hyph, first, joined + "inc", joined + "careers", first + "inc",
             "".join(words[:2]) if len(words) > 1 else None,
             re.sub(r"[^a-z0-9]+", "", name.lower())]
    out = []
    for c in cands:
        if c and len(c) >= 3 and c not in out:
            out.append(c)
    return out


WORKDAY_SITES = ["External", "external", "Careers", "careers", "External_Careers", "ExternalCareers",
                 "External_Career_Site", "jobs", "Jobs", "Careers_External", "en-US", "Global_Careers"]


def probe_workday(host, tenant, sites=None):
    """Try common Workday site names for a tenant; return the first that lists postings."""
    for site in sites or (WORKDAY_SITES + [tenant, tenant.capitalize(), tenant.upper()]):
        try:
            r = http("POST", f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                     headers={"Content-Type": "application/json", "Accept": "application/json"},
                     json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": "finance"})
            if isinstance(r.json().get("jobPostings"), list):
                return site
        except Exception:  # noqa: BLE001
            continue
    return None


def discover(name, quiet=False):
    """Try every slug guess against every ATS. Returns [(kind, slug, count)]."""
    cands = slug_candidates(name)
    if not quiet:
        print(f"\nTrying {len(cands)} slugs across 4 ATS providers for {name}...\n")
    found = []
    for slug in cands:
        for kind, fetcher in (("greenhouse", fetch_greenhouse), ("lever", fetch_lever),
                              ("ashby", fetch_ashby), ("smartrecruiters", fetch_smartrecruiters)):
            try:
                jobs = fetcher({"company": name, "slug": slug})
                if jobs:
                    found.append((kind, slug, len(jobs)))
                    if not quiet:
                        print(f"  FOUND  {kind:<16} slug='{slug}'  ({len(jobs)} postings)")
                        print(f"         add under {kind}: - {{ company: {name}, slug: {slug} }}\n")
            except Exception:
                pass
    if not found and not quiet:
        print("  Nothing found. The company likely uses Workday, SuccessFactors, or its own\n"
              "  careers site. Open the careers page and read the URL:\n"
              "    *.myworkdayjobs.com/...  -> Workday, add host/tenant/site to sources.yaml\n"
              "  Otherwise leave it out and rely on MyCareersFuture and LinkedIn alerts.")
    return found


def rediscover(sources):
    """Re-test every board; for each broken one, hunt for where the company really lives.
    Prints a block that can be pasted straight into sources.yaml."""
    print("\nREDISCOVER — testing every board, then hunting for the broken ones\n")
    fixes, dead, ok = [], [], 0
    for kind in ("greenhouse", "lever", "ashby", "smartrecruiters"):
        for entry in sources.get(kind, []):
            try:
                if FETCHERS[kind](entry) is not None:
                    ok += 1
                    continue
            except Exception:  # noqa: BLE001
                pass
            found = discover(entry["company"], quiet=True)
            found = [f for f in found if not (f[0] == kind and f[1] == entry["slug"])]
            if found:
                k, slug, n = max(found, key=lambda f: f[2])
                fixes.append((entry["company"], kind, entry["slug"], k, slug, n))
                print(f"  {entry['company']:<20} {kind}/{entry['slug']} -> {k}/{slug}  ({n} postings)")
            else:
                dead.append((entry["company"], kind, entry["slug"]))
                print(f"  {entry['company']:<20} {kind}/{entry['slug']} -> nothing found on any ATS")
    for entry in sources.get("workday", []):
        try:
            if fetch_workday(entry) is not None:
                ok += 1
                continue
        except Exception:  # noqa: BLE001
            pass
        site = probe_workday(entry["host"], entry["tenant"])
        if site:
            fixes.append((entry["company"], "workday", entry["site"], "workday", site, None))
            print(f"  {entry['company']:<20} workday site '{entry['site']}' -> '{site}'")
        else:
            dead.append((entry["company"], "workday", entry["site"]))
            print(f"  {entry['company']:<20} workday: no site name worked")

    print(f"\n{ok} boards already fine, {len(fixes)} fixed, {len(dead)} not found.\n")
    if fixes:
        print("PASTE THIS BLOCK TO CLAUDE (or apply it yourself in sources.yaml):\n")
        for company, okind, oslug, nkind, nslug, n in fixes:
            if nkind == "workday":
                print(f"  workday / {company}: change site to {nslug}")
            else:
                print(f"  {company}: remove from {okind} (slug {oslug}); add under {nkind}: - {{ company: {company}, slug: {nslug} }}")
    if dead:
        print("\nNOT FOUND ON ANY ATS (own careers site, or acquired; drop or rely on MyCareersFuture):")
        for company, kind, slug in dead:
            print(f"  {kind} / {company}")


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

class ConfigError(Exception):
    pass


def load_config(root=None, need_rubric=True):
    root = Path(root) if root else ROOT
    out = []
    for name in ("profile.yaml", "sources.yaml"):
        p = root / name
        if not p.exists():
            raise ConfigError(f"{name} is missing next to agent.py")
        try:
            data = yaml.safe_load(p.read_text())
        except yaml.YAMLError as e:
            raise ConfigError(f"{name} is not valid YAML: {str(e)[:120]}")
        if not isinstance(data, dict):
            raise ConfigError(f"{name} is empty or not a mapping")
        out.append(data)
    profile, sources = out

    required = ("candidate", "targets", "rubric", "filters", "models") if need_rubric else ("filters",)
    for key in required:
        if key not in profile:
            raise ConfigError(f"profile.yaml is missing the '{key}' section")
    for key in ("title_include", "title_exclude", "location_include"):
        pat = profile["filters"].get(key)
        if not pat:
            raise ConfigError(f"profile.yaml filters.{key} is missing")
        try:
            re.compile(pat, re.I | re.X)
        except re.error as e:
            raise ConfigError(f"profile.yaml filters.{key} is not a valid regex: {e}")
    if need_rubric:
        w = profile["rubric"].get("weights", {})
        total = sum(v for v in w.values() if isinstance(v, (int, float)))
        if total != 100:
            raise ConfigError(f"rubric.weights must sum to 100 (currently {total})")
        b = profile["rubric"].get("bands", {})
        if not (b.get("apply_now", 0) > b.get("worth_a_look", 0) > 0):
            raise ConfigError("rubric.bands must satisfy apply_now > worth_a_look > 0")
        for key in ("triage", "deep", "deep_threshold", "max_deep"):
            if key not in profile["models"]:
                raise ConfigError(f"profile.yaml models.{key} is missing")
    profile.setdefault("rubric", {}).setdefault("bands", {"apply_now": 78, "worth_a_look": 62})
    profile["rubric"].setdefault("weights", {})

    for kind in ("greenhouse", "lever", "ashby", "smartrecruiters"):
        for i, e in enumerate(sources.get(kind) or []):
            if not isinstance(e, dict) or not e.get("company") or not e.get("slug"):
                raise ConfigError(f"sources.yaml {kind}[{i}] needs both 'company' and 'slug'")
    for i, e in enumerate(sources.get("workday") or []):
        for k in ("company", "host", "tenant", "site"):
            if not isinstance(e, dict) or not e.get(k):
                raise ConfigError(f"sources.yaml workday[{i}] is missing '{k}'")
    return profile, sources


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def load_state():
    if not STATE_PATH.exists():
        return {"seen": {}}
    try:
        data = json.loads(STATE_PATH.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("seen"), dict):
            raise ValueError("unexpected shape")
        return data
    except Exception as e:  # noqa: BLE001
        backup = STATE_PATH.with_suffix(".corrupt.json")
        STATE_PATH.replace(backup)
        log(f"state.json unreadable ({str(e)[:60]}); moved to {backup.name}, starting fresh")
        return {"seen": {}}


def save_state(state):
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=120)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v > cutoff}
    STATE_PATH.write_text(json.dumps(state, indent=0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--setup-check", action="store_true")
    ap.add_argument("--check-sources", action="store_true")
    ap.add_argument("--discover", metavar="COMPANY")
    ap.add_argument("--rediscover", action="store_true")
    ap.add_argument("--no-score", action="store_true")
    ap.add_argument("--since-days", type=int, default=0)
    args = ap.parse_args()

    load_local_env()
    scoring_run = not (args.no_score or args.check_sources or args.rediscover
                       or args.discover or args.setup_check)
    try:
        profile, sources = load_config(need_rubric=scoring_run)
    except ConfigError as e:
        print(f"\nCONFIG PROBLEM: {e}\n")
        sys.exit(2)

    if args.setup_check:
        return setup_check(profile, sources)
    if args.discover:
        return discover(args.discover)
    if args.check_sources:
        return check_sources(sources)
    if args.rediscover:
        return rediscover(sources)
    if not args.no_score and not os.environ.get("ANTHROPIC_API_KEY"):
        print("\nANTHROPIC_API_KEY is not set. Add it as a repository secret, "
              "or run with --no-score to fetch and filter without scoring.\n")
        sys.exit(2)

    # 1. fetch
    all_jobs, stats = [], {}
    for kind, fetcher in FETCHERS.items():
        ok = 0
        for entry in sources.get(kind, []):
            try:
                jobs = fetcher(entry)
                all_jobs += jobs
                ok += 1
            except Exception as e:  # noqa: BLE001
                log(f"  {kind}/{entry['company']} failed: {str(e)[:80]}")
        stats[kind] = f"{ok}/{len(sources.get(kind, []))} boards"
        log(f"{kind}: {ok}/{len(sources.get(kind, []))} boards, running total {len(all_jobs)}")

    mcf = sources.get("mycareersfuture", {})
    if mcf.get("enabled"):
        try:
            jobs = fetch_mcf(mcf)
            all_jobs += jobs
            stats["mycareersfuture"] = f"{len(jobs)} postings"
        except Exception as e:  # noqa: BLE001
            stats["mycareersfuture"] = f"failed: {str(e)[:50]}"
    log(f"fetched {len(all_jobs)} postings total")

    # 2. filter on title and location
    jobs = prefilter(all_jobs, profile)
    log(f"{len(jobs)} passed the title filter")

    # 3. drop the ones already seen
    state = load_state()
    if not args.since_days:
        jobs = [j for j in jobs if j["id"] not in state["seen"]]
        log(f"{len(jobs)} are new since the last run")

    # 4. fetch missing descriptions, confirm Singapore
    needs = [j for j in jobs if j.get("detail") and not j["description"]]
    if needs:
        log(f"hydrating {len(needs)} descriptions")
        for j in needs:
            hydrate(j)
    jobs = sg_confirm(jobs)
    log(f"{len(jobs)} are Singapore-relevant")

    if not jobs:
        log("nothing new today")
        if not args.dry_run:
            save_state(state)
            if args.no_score:
                DIGEST_DIR.mkdir(exist_ok=True)
                publish_latest([], dt.datetime.now(SGT).strftime("%Y-%m-%d"), stats)
            if profile.get("delivery", {}).get("email_when_empty", True):
                stamp = dt.datetime.now(SGT).strftime("%Y-%m-%d")
                apps = load_applications()
                follow = profile.get("delivery", {}).get("follow_up_after_days", 7)
                pipeline = pipeline_summary(apps, follow_up_days=follow) if apps else None
                markdown, html_body = build_digest([], stats, profile, sources, pipeline)
                try:
                    send_email(f"Job digest {stamp} — nothing new", markdown, html_body)
                except Exception:  # noqa: BLE001
                    pass
        return

    # 5. score
    scored = []
    if args.no_score:
        # Write the full postings so something else (Cowork, a human) can score them.
        DIGEST_DIR.mkdir(exist_ok=True)
        stamp = dt.datetime.now(SGT).strftime("%Y-%m-%d")
        dump = [{"id": j["id"], "company": j["company"], "title": j["title"],
                 "location": j["location"], "url": j["url"], "source": j["source"],
                 "salary": j.get("salary"), "posted": j.get("posted"),
                 "already_applied": bool(j.get("applied_already")),
                 "description": (j["description"] or "")[:3500]} for j in jobs]
        (DIGEST_DIR / f"{stamp}.to-score.json").write_text(json.dumps(dump, ensure_ascii=False, indent=1))
        log(f"wrote {len(dump)} postings to digests/{stamp}.to-score.json for scoring")
        publish_latest(dump, stamp, stats)
        for j in jobs:
            j["score"] = 0
            scored.append(j)
    else:
        m = profile["models"]
        log("triage pass")
        try:
            t_scores = triage(jobs, profile, m["triage"])
            for j in jobs:
                j["score"] = t_scores.get(j["id"], 50)
            shortlist = sorted([j for j in jobs if j["score"] >= m["deep_threshold"]],
                               key=lambda x: -x["score"])[:m["max_deep"]]
            log(f"deep review on {len(shortlist)}")
            reviews = deep_review(shortlist, profile, m["deep"])
        except ClaudeAuthError as e:
            print(f"\nCLAUDE API REJECTED THE KEY: {str(e)[:160]}\n"
                  "Nothing was emailed and nothing was marked as seen. Fix the key and rerun.\n")
            sys.exit(2)
        for j in jobs:
            r = reviews.get(j["id"])
            if r:
                j["review"] = r
                if r.get("score") is not None:
                    j["score"] = int(r["score"])
                else:
                    r["score"] = j["score"]
        scored = jobs

    # 6. digest
    apps = load_applications()
    mark_applied(scored, apps)
    if args.no_score:
        # refresh the dump now that duplicates are marked
        stamp = dt.datetime.now(SGT).strftime("%Y-%m-%d")
        p = DIGEST_DIR / f"{stamp}.to-score.json"
        dump = json.loads(p.read_text())
        flagged = {j["id"] for j in scored if j.get("applied_already")}
        for d in dump:
            d["already_applied"] = d["id"] in flagged
        p.write_text(json.dumps(dump, ensure_ascii=False, indent=1))
        publish_latest(dump, stamp, stats)
    follow = profile.get("delivery", {}).get("follow_up_after_days", 7)
    pipeline = pipeline_summary(apps, follow_up_days=follow) if apps else None
    markdown, html_body = build_digest(scored, stats, profile, sources, pipeline)
    DIGEST_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now(SGT).strftime("%Y-%m-%d")
    (DIGEST_DIR / f"{stamp}.md").write_text(markdown)

    if args.dry_run:
        print("\n" + markdown)
        return

    top_n = len([j for j in scored if j["score"] >= profile["rubric"]["bands"]["apply_now"]])
    send_email(f"Job digest {stamp} — {top_n} to apply, {len(scored)} new", markdown, html_body)

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for j in scored:
        state["seen"][j["id"]] = now
    save_state(state)
    log("done")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
