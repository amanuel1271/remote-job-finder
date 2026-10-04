#!/usr/bin/env python3
"""Daily remote-job finder: only jobs a Korea-based backend engineer can actually take.

Sources:
  - Himalayas search API with country=South Korea (structured eligibility: worldwide or Korea-tagged)
  - We Work Remotely RSS (kept only if region is worldwide/Asia AND the country list, if any, includes Korea)

Self-correcting:
  - Every job's full description is scanned for location-lock phrases ("must reside in the US", ...) and
    AI-interviewer funnels ("AI interview", ...) and dropped. micro1 and Mercor are blocked outright.
  - After the list, type the numbers of bad jobs. Their company is blocked, and you can add a phrase
    from the posting (e.g. "US hours"); both go to rules.json and filter every future run.

Usage:  python3 jobs.py          -> new jobs since last run
        python3 jobs.py --all    -> everything that matches (ignores seen list)
        python3 jobs.py --ui     -> local web UI (http://127.0.0.1:8765)
        python3 jobs.py --digest -> score new jobs vs resume.txt (local Qwen) and email them (launchd runs this)
        python3 jobs.py --test   -> self-check of the filters
"""
import email.utils, hashlib, json, os, re, subprocess, sys, tempfile, threading, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

COUNTRY = "South Korea"  # Himalayas country filter
COUNTRY_WORD = "Korea"   # must appear in a WWR country list, when the posting has one
MAX_AGE_DAYS = 30
HIMALAYAS_QUERIES = ["backend", "python", "node.js", "typescript", "full stack", "fastapi"]
HIMALAYAS_PAGES = 5  # 20 jobs/page; results are relevance-sorted so later pages are mostly noise
WWR_FEEDS = ["remote-back-end-programming-jobs", "remote-full-stack-programming-jobs", "remote-devops-sysadmin-jobs"]

TITLE_OK = re.compile(r"back.?end|software engineer|software developer|platform|full.?stack|\bapi\b|python|node|typescript|server|infrastructure|blockchain|web3|devops|site reliability|\bsre\b|golang|\bgo (?:developer|engineer)\b|\brust\b", re.I)
# Word boundaries matter: "intern" must not hit "International", "test" must not hit "Latest".
TITLE_BAD = re.compile(r"\bintern(?:s|ship)?\b|\bjunior\b|\bjr\b|\bgraduate\b|\bprincipal\b|\bdirector\b|\bmanager\b|\bhead of\b|front.?end|\bios\b|\bandroid\b|\bmobile\b|\bjava\b|\.net\b|c#|c\+\+|\bphp\b|\bruby\b|\brails\b|\bqa\b|\btest(?:ing|er)?\b|\bsdet\b|data scien|machine learning|\bml\b|\bsales\b|customer support|technical support|\bsupport (?:engineer|specialist|agent)\b|\bdesigner\b|\brecruit", re.I)
# Himalayas sometimes tags region-locked jobs as worldwide; the title gives them away.
TITLE_REGION_LOCK = re.compile(r"(?i:\b(?:europe|latin america|north america|americas|united states|canada|brazil|mexico|india)\b|desarrollador)|\b(?:US|USA|EU|UK|EMEA|LATAM)\b|\bU\.S\.(?!\w)")
# Phrases in a description that mean "not open to someone in Korea". rules.json adds learned ones.
DEFAULT_LOCK_PHRASES = [
    "must reside in the us", "must reside in the united states", "must be located in the us",
    "must be located in the united states", "must be based in the us", "must be based in the united states",
    "authorized to work in the united states", "authorized to work in the us", "us citizen", "u.s. citizen",
    "green card", "security clearance", "us hours", "u.s. hours", "eu only", "europe only", "latam only",
    "within the united states", "us-based candidates", "residents of the us",
    "european timezone", "european time zone", "timezones only", "cet timezone", "(m/w/d)", "deutschkenntnisse", "fließend deutsch",
]
# Hiring funnels run by an AI interviewer (user doesn't want these).
AI_INTERVIEW_PHRASES = [
    "ai interview", "ai-led interview", "ai-powered interview", "ai interviewer", "ai-driven interview",
    "interview with our ai", "ai recruiter", "ai screening", "ai-based screening", "ai video interview",
]
DEFAULT_BLOCKED_COMPANIES = []  # add yours in rules.json (or with the Block button)

HERE = Path(__file__).parent
SEEN_FILE, RULES_FILE = HERE / "seen.json", HERE / "rules.json"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) personal-job-search"}


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save(path, obj, indent=None):
    """Atomic write: a crash mid-write can't leave truncated JSON that breaks every later run."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(obj, indent=indent))
    os.replace(tmp, path)


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20).read()


def region_locked(title):
    # only an explicit worldwide marker overrides ("Global Payments ... (US only)" is still locked)
    return bool(TITLE_REGION_LOCK.search(title)) and not re.search(r"worldwide|anywhere|\bglobally\b|global remote|remote\W{1,3}global", title, re.I)


NEGATION = re.compile(r"\b(?:no|not|don't|do not|never|without|isn't|aren't|nor)\b[^.;!?]*$")


def lock_phrase(text, phrases):
    """First lock phrase found as whole words and not negated ("no US citizenship required" doesn't count), or None."""
    text = re.sub(r"<[^>]+>|&nbsp;", " ", text).lower()
    for p in phrases:
        for m in re.finditer(r"(?<!\w)" + re.escape(p) + r"(?!\w)", text):
            if not NEGATION.search(text[max(0, m.start() - 40):m.start()]):
                return p
    return None


def himalayas():
    for q in HIMALAYAS_QUERIES:
        for page in range(1, HIMALAYAS_PAGES + 1):
            url = "https://himalayas.app/jobs/api/search?" + urllib.parse.urlencode({"q": q, "country": COUNTRY, "page": page})
            jobs = json.loads(get(url)).get("jobs", [])
            for j in jobs:
                try:
                    countries = j.get("locationRestrictions") or []
                    where = "Worldwide" if not countries else ", ".join(countries) if len(countries) <= 3 else f"{len(countries)} countries incl. {COUNTRY_WORD}"
                    job = (j["title"], j["companyName"], where, int(j["pubDate"]), j["applicationLink"], j.get("description") or "")
                except (KeyError, TypeError, ValueError) as e:
                    print(f"[Fetch] ITEM_SKIP source=Himalayas err={type(e).__name__}: {e}", file=sys.stderr)
                    continue
                yield job
            if len(jobs) < 20:
                break
            time.sleep(0.5)  # be polite


def wwr():
    for feed in WWR_FEEDS:
        root = ET.fromstring(get(f"https://weworkremotely.com/categories/{feed}.rss"))
        for item in root.iter("item"):
            region = item.findtext("region") or ""
            countries = item.findtext("country") or ""
            # "Anywhere in the World" with a country list attached is often EU-only, so check the list too
            if not re.search(r"anywhere|worldwide|asia|apac", region, re.I) or (countries and COUNTRY_WORD not in countries):
                continue
            full_title = item.findtext("title") or ""
            company, _, title = full_title.partition(": ") if ": " in full_title else ("", "", full_title)
            try:  # RFC 822 date with timezone; handles single-digit days
                posted = int(email.utils.parsedate_to_datetime(item.findtext("pubDate")).timestamp())
            except (TypeError, ValueError) as e:
                print(f"[Fetch] ITEM_SKIP source=WWR title={full_title[:40]!r} err={type(e).__name__}", file=sys.stderr)
                continue
            yield title, company, region, posted, item.findtext("link"), item.findtext("description") or ""


def learn(jobs, rules):
    """Ask which listed jobs were bad; block their company and optionally a phrase from the posting."""
    if not jobs or not sys.stdin.isatty():
        return
    answer = input("\nBad jobs? Numbers separated by spaces (Enter to skip): ").split()
    for n in answer:
        if not n.isdigit() or not 1 <= int(n) <= len(jobs):
            continue
        title, company = jobs[int(n) - 1][1:3]
        phrase = input(f"  #{n} {title} @ {company}\n     phrase from the posting to block everywhere (Enter = just block {company}): ").strip().lower()
        rules["companies"] = sorted(set(rules["companies"]) | {company.lower()})
        if phrase:
            rules["phrases"] = sorted(set(rules["phrases"]) | {phrase})
    save(RULES_FILE, rules, indent=2)
    print(f"Saved. Now blocking {len(rules['companies'])} companies and {len(rules['phrases'])} learned phrases.")


# ---------- Resume match scoring: local Qwen via Ollama, keyword fallback ----------
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:3b"
RESUME = (HERE / "resume.txt").read_text() if (HERE / "resume.txt").exists() else ""
SCORES_FILE = HERE / "scores.json"
# Tech words a job might ask for. MY_SKILLS are the ones on the resume; the rest count as "missing".
MY_SKILLS = ["Python", "FastAPI", "Node.js", "NestJS", "TypeScript", "JavaScript", "PostgreSQL", "Redis", "WebSocket",
             "gRPC", "REST", "AWS", "Docker", "Kubernetes", "nginx", "CI/CD", "React", "Solidity", "web3", "smart contract",
             "microservices", "payments", "blockchain", "LLM", "Jest", "SQL"]
OTHER_TECH = ["Go", "Java", "Kotlin", "Rust", "Ruby", "Rails", "PHP", "C#", ".NET", "Scala", "Elixir", "Django",
              "Flask", "Spring", "Kafka", "GraphQL", "MongoDB", "MySQL", "Vue", "Angular", "Terraform", "GCP", "Azure",
              "Elasticsearch", "Spark", "Airflow", "Swift", "C++"]


# Terms that are also English words get a stricter, case-sensitive pattern ("we react swiftly", "Go above and beyond").
TECH_PATTERNS = {
    "Go": r"\bGolang\b|(?<=[(,/] )Go\b|(?<=[(,/])Go\b|\bGo(?=\s*[,/)]|\s+(?:and|or)\s+[A-Z])",
    "React": r"\bReact\b(?!ive)", "Rust": r"\bRust\b(?!-)", "Spring": r"\bSpring\b", "Swift": r"\bSwift\b",
    "Flask": r"\bFlask\b", "Spark": r"\bSpark\b", "Rails": r"\bRails\b", "SQL": r"\bSQL\b",
    "REST": r"\bREST(?:ful)?\b|(?i:\brestful\b|\brest api)", "Node.js": r"(?i:\bnode\.?js\b)",
    "C++": r"(?<![\w+])C\+\+", ".NET": r"\.NET\b",
}


def tech_regex(term):
    if term in TECH_PATTERNS:
        return re.compile(TECH_PATTERNS[term])
    return re.compile(r"(?<![\w.])" + re.escape(term) + r"(?![\w])", re.I)


TECH = {t: tech_regex(t) for t in MY_SKILLS + OTHER_TECH}


# A job must mention enough of the resume to be shown at all (tune these two).
MIN_SKILL_MATCHES = 3
CORE_SKILLS = ["Python", "FastAPI", "Node.js", "NestJS", "TypeScript"]  # at least one must appear


def resume_overlap(title, description):
    """Resume skills the posting mentions."""
    text = title + " " + re.sub(r"<[^>]+>", " ", description)
    return [t for t in MY_SKILLS if TECH[t].search(text)]


def weak_match(title, description):
    matched = resume_overlap(title, description)
    return len(matched) < MIN_SKILL_MATCHES or not any(s in matched for s in CORE_SKILLS)


def keyword_score(title, description):
    text = title + " " + re.sub(r"<[^>]+>", " ", description)
    matched = [t for t in MY_SKILLS if TECH[t].search(text)]
    missing = [t for t in OTHER_TECH if TECH[t].search(text)]
    # half "few missing skills", half "how much of the resume it mentions" (8+ matches = full marks)
    fit = len(matched) / (len(matched) + len(missing)) if matched or missing else 0.5
    depth = min(1, len(matched) / 8)
    score = round(100 * (0.5 * fit + 0.5 * depth))
    why = f"Keyword match: {', '.join(matched[:6]) or 'no stack overlap found'}"
    return {"score": score, "why": why, "missing": missing[:6], "engine": "keywords"}


def ai_score(title, description):
    """Ask local Qwen to compare the resume with the job. Returns None on any failure (caller falls back)."""
    jd = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", description))[:5000]
    prompt = f"""You screen jobs for one candidate. Compare the RESUME with the JOB and reply with JSON only:
{{"score": <0-100 integer, how well the candidate fits the REQUIRED qualifications>,
  "why": "<one short sentence: the main reason for the score>",
  "missing": ["<required skill or requirement the candidate lacks>", ...up to 4]}}
Scoring guide: 80+ strong fit (stack and seniority match), 60-79 decent (most requirements met),
40-59 stretch (key requirement missing), below 40 poor fit. "Nice to have" items must not lower the score much.

RESUME:
{RESUME}

JOB TITLE: {title}
JOB DESCRIPTION:
{jd}"""
    body = json.dumps({"model": OLLAMA_MODEL, "prompt": prompt, "format": "json", "stream": False,
                       "options": {"temperature": 0}}).encode()
    try:
        req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
        out = json.loads(json.loads(urllib.request.urlopen(req, timeout=90).read())["response"])
        raw = out["score"]
        if isinstance(raw, bool):
            raise ValueError("score is a boolean")
        score = max(0, min(100, round(float(str(raw).strip().rstrip("%")))))  # 85, "85", "85%", 85.5
        missing = out.get("missing") or []
        missing = [missing] if isinstance(missing, str) else missing  # "Kafka" must not become K, a, f, k
        missing = [str(m) for m in missing if isinstance(m, (str, int, float))][:4]
        return {"score": score, "why": str(out.get("why") or "")[:200], "missing": missing, "engine": "qwen"}
    except Exception as e:  # Ollama down, slow, or bad JSON -> keyword fallback
        print(f"[Score] AI_FAIL title={title[:40]!r} err={type(e).__name__}", file=sys.stderr)
        return None


_cache_lock = threading.Lock()  # UI handler threads can score at the same time


def score(key, title, description, ai=True):
    """Cached AI score if still valid; otherwise compute it (ai=True) or return the instant keyword score."""
    fp = hashlib.sha1((RESUME + description).encode()).hexdigest()[:12]  # resume or posting changed -> rescore
    with _cache_lock:
        cached = load(SCORES_FILE, {}).get(key)
    if cached and cached.get("fp", fp) == fp:  # entries from before fingerprints existed are kept
        return cached
    result = ai_score(title, description) if ai and RESUME else None  # slow: outside the lock
    if result:
        with _cache_lock:
            cache = load(SCORES_FILE, {})
            cache[key] = {**result, "fp": fp}
            save(SCORES_FILE, cache)
        return result
    return keyword_score(title, description)


def fetch_all():
    """Raw jobs from every source: (title, company, where, posted, link, description, source). Sets fetch_all.failed."""
    raw, fetch_all.failed = [], []
    for source, fetch in (("Himalayas", himalayas), ("WWR", wwr)):
        try:
            raw += [(*job, source) for job in fetch()]
        except Exception as e:  # one dead source shouldn't kill the run
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [Fetch] SOURCE_FAIL source={source} err={type(e).__name__}: {e}", file=sys.stderr)
            fetch_all.failed.append(source)
    return raw


def apply_filters(raw, rules):
    """Keep jobs worth showing. Returns ({key: job}, {drop reason: count})."""
    phrases = DEFAULT_LOCK_PHRASES + AI_INTERVIEW_PHRASES + rules["phrases"]
    blocked = set(DEFAULT_BLOCKED_COMPANIES) | set(rules["companies"])
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    found, dropped, handled = {}, {}, set()
    for title, company, where, posted, link, description, source in raw:
        key = f"{company}|{title}".lower()
        if key in handled or posted < cutoff or not TITLE_OK.search(title) or TITLE_BAD.search(title):
            continue
        handled.add(key)
        reason = ("blocked company" if company.lower() in blocked
                  else "region in title" if region_locked(title)
                  else lock_phrase(title + " " + description, phrases)
                  or ("weak resume match" if weak_match(title, description) else None))
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
        else:
            found[key] = (posted, title, company, where, link, source, description)
    return found, dropped


def main():
    show_all = "--all" in sys.argv
    seen = set(load(SEEN_FILE, []))
    rules = load(RULES_FILE, {"companies": [], "phrases": []})
    found, dropped = apply_filters(fetch_all(), rules)
    shown = sorted((v for k, v in found.items() if show_all or k not in seen), reverse=True)
    for i, (posted, title, company, where, link, source, _) in enumerate(shown, 1):
        print(f"{i:>3}. {time.strftime('%m-%d', time.localtime(posted))}  {title}  @ {company}  [{where}]  ({source})\n       {link}")
    print(f"\n{len(shown)} {'matching' if show_all else 'new'} jobs. Auto-dropped {sum(dropped.values())}:",
          ", ".join(f"{r} ×{n}" for r, n in sorted(dropped.items(), key=lambda x: -x[1])[:8]) or "none")
    save(SEEN_FILE, sorted(seen | found.keys()))
    learn(shown, rules)


# ---------- Email digest (run by launchd at 9:00 and 18:00) ----------
EMAIL = os.environ.get("JOB_FINDER_EMAIL", "")  # your Resend account address; set in the LaunchAgent plist
EMAILED_FILE = HERE / "emailed.json"
DIGEST_TOP = 40
MAX_EMAILS_PER_DAY = 2  # hard cap (Resend free quota): manual runs or wake-up catch-ups can't exceed it
EMAIL_LOG_FILE = HERE / "email_log.json"
KEYCHAIN_SERVICE = "job-finder-resend"  # Resend API key lives in macOS Keychain, never in a file


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [Digest] {msg}", flush=True)


def send_email(subject, body, idempotency_key):
    """Send via Resend's API. The free sandbox sender can only mail the account owner, which is all we need."""
    try:
        key = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", EMAIL, "-w"],
                             capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"KEYCHAIN_FAIL rc={e.returncode} stderr={e.stderr.strip()}") from None
    payload = json.dumps({"from": "Job Finder <onboarding@resend.dev>", "to": [EMAIL], "subject": subject, "text": body}).encode()
    req = urllib.request.Request("https://api.resend.com/emails", data=payload, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "job-finder/1.0",
        "Idempotency-Key": idempotency_key})  # a retry of the same digest can't deliver twice
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            log(f"EMAIL_OK id={json.loads(resp.read()).get('id')}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"EMAIL_FAIL status={e.code} body={e.read()[:500]!r}") from None


def digest_text(scored, total_new):
    lines = [f"{total_new} new remote jobs open to {COUNTRY}, sorted by resume match.", ""]
    for result, (posted, title, company, where, link, source, _) in scored[:DIGEST_TOP]:
        mark = "AI" if result["engine"] == "qwen" else "kw"
        lines += [f"[{result['score']:>3} {mark}] {title} @ {company}",
                  f"         {where} · {time.strftime('%b %d', time.localtime(posted))} · {source}",
                  f"         {result['why']}"]
        if result["missing"]:
            lines.append(f"         Missing: {', '.join(result['missing'])}")
        lines += [f"         {link}", ""]
    if total_new > DIGEST_TOP:
        lines.append(f"+{total_new - DIGEST_TOP} more: run  python3 ~/job-search/jobs.py --ui")
    lines.append("[kw] = quick keyword match. For a detailed AI check, open  python3 ~/job-search/jobs.py --ui  and click ✨ AI match.")
    return "\n".join(lines)


def digest():
    try:
        _digest()
    except Exception as e:  # log every failure with a tag so digest.log explains what happened
        log(f"FAIL err={type(e).__name__}: {e}")
        raise


def _digest():
    if not EMAIL:
        raise RuntimeError("JOB_FINDER_EMAIL is not set")
    today = time.strftime("%Y-%m-%d")
    sent_today = [s for s in load(EMAIL_LOG_FILE, []) if s.startswith(today)]
    if len(sent_today) >= MAX_EMAILS_PER_DAY:
        log(f"SKIP reason=daily_cap sent_today={len(sent_today)}")
        return
    rules = load(RULES_FILE, {"companies": [], "phrases": []})
    raw = fetch_all()
    if fetch_all.failed and not raw:
        log(f"SKIP reason=all_sources_failed sources={fetch_all.failed}")  # e.g. run on wake before Wi-Fi is up
        return
    found, dropped = apply_filters(raw, rules)
    emailed = set(load(EMAILED_FILE, []))
    new = {k: v for k, v in found.items() if k not in emailed}
    log(f"FETCHED found={len(found)} new={len(new)} dropped={sum(dropped.values())} failed_sources={fetch_all.failed}")
    if not new:
        log("SKIP reason=no_new_jobs")
        return
    scored = sorted(((score(k, v[1], v[6], ai=False), k, v) for k, v in new.items()), key=lambda x: -x[0]["score"])
    listed = scored[:DIGEST_TOP]  # only these get marked emailed; the rest roll over to the next digest
    best = listed[0]
    subject = f"{len(new)} new remote jobs · top match {best[0]['score']}: {best[2][1]} @ {best[2][2]}"
    keys = sorted(k for _, k, _ in listed)
    # Reserve the cap slot first: if the send times out after Resend accepted it, it still counts (fail closed).
    save(EMAIL_LOG_FILE, sent_today + [time.strftime("%Y-%m-%d %H:%M:%S")])
    send_email(subject, digest_text([(r, v) for r, _, v in listed], len(new)),
               idempotency_key=hashlib.sha256("\n".join(keys).encode()).hexdigest()[:48])
    save(EMAILED_FILE, sorted(emailed | set(keys)))
    log(f"SENT to={EMAIL} listed={len(listed)} of_new={len(new)} top_score={best[0]['score']}")


def ui(port=8765):
    """Local web UI on http://127.0.0.1:8765 — list, search, block company/phrase."""
    import threading, webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state = {"raw": [], "loading": True}
    state_lock = threading.Lock()

    def refresh():
        state["loading"] = True
        state["raw"] = fetch_all()
        state["loading"] = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal quiet
            pass

        def send(self, body, content_type="application/json"):
            body = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(body)

        def trusted(self):
            """Only our own page may talk to this server: blocks DNS rebinding (Host) and cross-site requests (Origin)."""
            allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
            origin = self.headers.get("Origin")
            if self.headers.get("Host") not in allowed or (origin and origin.split("//", 1)[-1] not in allowed):
                self.send_error(403)
                return False
            return True

        def do_GET(self):
            if not self.trusted():
                return
            if self.path == "/":
                return self.send((HERE / "ui.html").read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/jobs":
                if state["loading"]:
                    return self.send({"loading": True})
                rules = load(RULES_FILE, {"companies": [], "phrases": []})
                seen = set(load(SEEN_FILE, []))
                found, dropped = apply_filters(state["raw"], rules)
                jobs = [{"key": k, "posted": p, "title": t, "company": c, "where": w, "link": l, "source": s, "new": k not in seen,
                         **score(k, t, d, ai=False)}  # cached AI score if ready, else instant keyword score
                        for k, (p, t, c, w, l, s, d) in sorted(found.items(), key=lambda kv: -kv[1][0])]
                return self.send({"jobs": jobs, "dropped": dropped, "rules": rules})
            self.send_error(404)

        def do_POST(self):
            # JSON only: a cross-site "simple" request (text/plain, no preflight) can't get through
            if not self.trusted():
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self.send_error(415, "Content-Type must be application/json")
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 <= length <= 100_000:
                    raise ValueError("bad Content-Length")
                data = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("body must be a JSON object")
            except ValueError as e:
                return self.send_error(400, str(e))
            if self.path == "/api/score":  # {"key": ...} -> AI match for one job, on demand (slow, so no lock)
                found, _ = apply_filters(state["raw"], load(RULES_FILE, {"companies": [], "phrases": []}))
                job = found.get(data.get("key"))
                if not job:
                    return self.send({"error": "This job is no longer in the list (blocked or refreshed). Reload the page."})
                return self.send(score(data["key"], job[1], job[6]))
            if self.path == "/api/refresh":
                if not state["loading"]:  # one refresh at a time
                    threading.Thread(target=refresh, daemon=True).start()
                return self.send({"ok": True})
            with state_lock:  # load-change-save must not interleave between two quick clicks
                if self.path == "/api/block":
                    rules = load(RULES_FILE, {"companies": [], "phrases": []})
                    for field, param in (("companies", "company"), ("phrases", "phrase")):
                        value = str(data.get(param) or "").strip().lower()
                        if len(value) >= 2:  # a 1-letter phrase would hide everything
                            rules[field] = sorted(set(rules[field]) | {value})
                    save(RULES_FILE, rules, indent=2)
                    return self.send({"ok": True})
                if self.path == "/api/unblock":
                    if data.get("field") not in ("companies", "phrases"):
                        return self.send_error(400, "field must be companies or phrases")
                    rules = load(RULES_FILE, {"companies": [], "phrases": []})
                    rules[data["field"]] = [v for v in rules[data["field"]] if v != data.get("value")]
                    save(RULES_FILE, rules, indent=2)
                    return self.send({"ok": True})
                if self.path == "/api/seen":  # {"keys": [...], "seen": true|false}
                    keys = data.get("keys")
                    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
                        return self.send_error(400, "keys must be a list of strings")
                    seen = set(load(SEEN_FILE, []))
                    seen = seen | set(keys) if data.get("seen", True) else seen - set(keys)
                    save(SEEN_FILE, sorted(seen))
                    return self.send({"ok": True})
            self.send_error(404)

    threading.Thread(target=refresh, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)  # localhost only
    print(f"Job finder UI on http://127.0.0.1:{port}  (Ctrl+C to stop)")
    webbrowser.open(f"http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def test():
    assert region_locked("Contract Golang Software Engineer (Remote Europe)")
    assert region_locked("Backend Engineer (US Only)") and region_locked("Backend Engineer, LATAM")
    assert not region_locked("Backend Engineer - Wallets (100% Remote Worldwide)")
    assert not region_locked("Backend Engineer (Remote US or Worldwide)")
    assert not region_locked("Join us: Backend Engineer")  # lowercase "us" is not a region
    assert lock_phrase("<p>You <b>must reside in the US</b>.</p>", DEFAULT_LOCK_PHRASES) == "must reside in the us"
    assert lock_phrase("We hire from anywhere in the world.", DEFAULT_LOCK_PHRASES) is None
    assert lock_phrase("Senior Backend Developer (Go) — Full Remote, European Timezones Only", DEFAULT_LOCK_PHRASES)
    assert lock_phrase("<p>Step 1: a 20-minute <b>AI interview</b></p>", AI_INTERVIEW_PHRASES) == "ai interview"
    assert lock_phrase("Senior Backend Developer (Go) — Full Remote, European Timezones Only", DEFAULT_LOCK_PHRASES)
    k = keyword_score("Backend Engineer", "<p>Python, FastAPI and PostgreSQL. Go and Kafka required. Let's go!</p>")
    assert k["score"] == 49 and k["missing"] == ["Go", "Kafka"], k  # 3 matched, 2 missing
    # regressions from the correctness review
    L = DEFAULT_LOCK_PHRASES + AI_INTERVIEW_PHRASES
    for ok in ["Protect your focus hours", "plus hours of bonus hours", "No US citizenship required", "We don't use AI interviews"]:
        assert lock_phrase(ok, L) is None, ok
    assert lock_phrase("Candidates must overlap US hours.", L) == "us hours"
    for good in ["Internal Platform Engineer", "International Payments Backend Engineer", "Latest Tech Backend Engineer", "Support Platform Engineer"]:
        assert not TITLE_BAD.search(good), good
    for bad in ["Backend Intern", "QA Test Engineer", "Engineering Manager", "Technical Support Engineer"]:
        assert TITLE_BAD.search(bad), bad
    for title in ["DevOps Engineer", "Site Reliability Engineer", "Senior Golang Engineer", "Go Developer", "Rust Developer"]:
        assert TITLE_OK.search(title), title
    assert region_locked("Backend Engineer (Remote U.S.)") and region_locked("Backend Engineer, North America")
    assert region_locked("Global Payments Backend Engineer (US only)")
    k = keyword_score("Backend", "We react swiftly; in the spring we Go above and beyond. Rust-free. Go-to person.")
    assert k["missing"] == [] and "React" not in k["why"], k
    k = keyword_score("Backend", "C++17, ASP.NET, NodeJS and RESTful APIs, rest api")
    assert "C++" in k["missing"] and ".NET" in k["missing"] and "Node.js" in k["why"] and "REST" in k["why"], k
    assert weak_match("Software Engineer", "We use JavaScript.")  # 1 skill
    assert weak_match("Engineer", "AWS, Docker, Kubernetes, PostgreSQL")  # 4 skills but no core language
    assert not weak_match("Backend Engineer", "Python, PostgreSQL and Redis")  # 3 skills incl. core
    assert TITLE_BAD.search("Senior Java Developer") and not TITLE_BAD.search("Senior Backend Engineer")
    print("ok")


if __name__ == "__main__":
    if "--test" in sys.argv:
        test()
    elif "--ui" in sys.argv:
        ui()
    elif "--digest" in sys.argv:
        digest()
    else:
        main()
