"""QuickJobs: щоденна добірка вакансій (Adzuna + Reed) у Telegram-канал.

Налаштування беруться з config.yaml, секрети - зі змінних середовища:
BOT_TOKEN, CHAT_ID, ADZUNA_APP_ID, ADZUNA_APP_KEY, REED_API_KEY.
Достатньо мати ключі хоча б одного з джерел (Adzuna або Reed).
DRY_RUN=1 - лише показати результат у логу, нічого не надсилати.
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
SEEN_PATH = ROOT / "seen_jobs.json"

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
ADZUNA_ID = os.getenv("ADZUNA_APP_ID")
ADZUNA_KEY = os.getenv("ADZUNA_APP_KEY")
REED_KEY = os.getenv("REED_API_KEY")
DRY_RUN = os.getenv("DRY_RUN") == "1"

MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "25"))  # скільки вакансій максимум за один запуск
MAX_AGE_DAYS = 7  # не показувати вакансії старші за N днів
SEEN_KEEP_DAYS = 60  # скільки днів пам'ятати вже надіслані
TIMEOUT = 20

AGENT_EMOJI = {
    "shops": "🛒",
    "premium_retail": "👗",
    "cafe_restaurants": "☕",
    "warehouse": "📦",
    "easy_jobs": "🎟",
}

HOURLY_RE = re.compile(
    r"£\s?(\d{1,3}(?:\.\d{1,2})?)"
    r"(?:\s*(?:-|–|—|to)\s*£?\s?(\d{1,3}(?:\.\d{1,2})?))?"
    r"\s*(?:per\s*hour|an\s*hour|/\s*(?:hr|hour|h)\b|p/?h\b|hourly)",
    re.I,
)


# ---------- допоміжне ----------

def log(msg: str):
    print(msg, flush=True)


def clean(text) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def norm_words(text: str) -> str:
    """'Sainsbury's' -> 'sainsburys', 'M&S' -> 'm and s', 'Co-op' -> 'co op'."""
    text = (text or "").lower().replace("&", " and ").replace("'", "").replace("’", "")
    return " " + re.sub(r"[^a-z0-9]+", " ", text).strip() + " "


def fingerprint(job: dict) -> str:
    return "|".join(norm_words(job[k]).strip() for k in ("title", "company", "location"))


def parse_hourly(*texts):
    for t in texts:
        m = HOURLY_RE.search(t or "")
        if m:
            value = max(float(m.group(1)), float(m.group(2) or m.group(1)))
            if 5 <= value <= 100:
                return value
    return None


def salary_info(lo, hi, predicted, title, snippet):
    """Повертає (hourly | None, текст для показу)."""
    hourly = parse_hourly(title, snippet)
    if hourly is None and lo and not predicted:
        top = float(hi or lo)
        if top < 100:  # схоже на погодинну ставку
            hourly = top
    if hourly is not None:
        return hourly, f"£{hourly:.2f}/h".replace(".00/h", "/h")
    if lo and not predicted:
        lo_i, hi_i = int(lo), int(hi or lo)
        if lo_i != hi_i:
            return None, f"£{lo_i:,}–£{hi_i:,}/yr"
        return None, f"£{lo_i:,}/yr"
    return None, ""


# ---------- джерела ----------

def fetch_adzuna(keyword: str, cfg: dict) -> list[dict]:
    if not (ADZUNA_ID and ADZUNA_KEY):
        return []
    loc = cfg["location"]
    params = {
        "app_id": ADZUNA_ID,
        "app_key": ADZUNA_KEY,
        "what": keyword,
        "where": loc["home"],
        "distance": max(1, round(loc.get("radius_miles", 5) * 1.609)),  # Adzuna: кілометри
        "max_days_old": MAX_AGE_DAYS,
        "results_per_page": 50,
        "sort_by": "date",
        "content-type": "application/json",
    }
    if "part-time" in [e.lower() for e in cfg.get("employment", [])]:
        params["part_time"] = 1
    r = requests.get("https://api.adzuna.com/v1/api/jobs/gb/search/1", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("results", []):
        title, snippet = clean(j.get("title")), clean(j.get("description"))
        hourly, sal = salary_info(
            j.get("salary_min"), j.get("salary_max"),
            str(j.get("salary_is_predicted", "0")) == "1", title, snippet,
        )
        posted = None
        try:
            posted = datetime.fromisoformat(j["created"].replace("Z", "+00:00"))
        except Exception:
            pass
        jobs.append({
            "key": f"adzuna:{j['id']}", "title": title,
            "company": clean((j.get("company") or {}).get("display_name")),
            "location": clean((j.get("location") or {}).get("display_name")),
            "url": j.get("redirect_url", ""), "hourly": hourly, "salary": sal, "posted": posted,
        })
    return jobs


def fetch_reed(keyword: str, cfg: dict) -> list[dict]:
    if not REED_KEY:
        return []
    loc = cfg["location"]
    params = {
        "keywords": keyword,
        "locationName": loc["home"],
        "distanceFromLocation": loc.get("radius_miles", 5),  # Reed: милі
        "resultsToTake": 50,
    }
    if "part-time" in [e.lower() for e in cfg.get("employment", [])]:
        params["partTime"] = "true"
    r = requests.get("https://www.reed.co.uk/api/1.0/search", params=params, auth=(REED_KEY, ""), timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("results", []):
        title, snippet = clean(j.get("jobTitle")), clean(j.get("jobDescription"))
        hourly, sal = salary_info(j.get("minimumSalary"), j.get("maximumSalary"), False, title, snippet)
        posted = None
        try:
            posted = datetime.strptime(j["date"], "%d/%m/%Y").replace(tzinfo=timezone.utc)
        except Exception:
            pass
        jobs.append({
            "key": f"reed:{j['jobId']}", "title": title,
            "company": clean(j.get("employerName")), "location": clean(j.get("locationName")),
            "url": j.get("jobUrl", ""), "hourly": hourly, "salary": sal, "posted": posted,
        })
    return jobs


def collect(cfg: dict) -> list[dict]:
    """Збирає вакансії з усіх увімкнених агентів і джерел, без дублікатів."""
    agents = {n: a for n, a in (cfg.get("agents") or {}).items() if a.get("enabled")}
    brands = {n: [norm_words(c) for c in a.get("companies", [])] for n, a in agents.items()}
    all_brands = [b for lst in brands.values() for b in lst]

    found: dict[str, dict] = {}
    calls = errors = 0
    for name, agent in agents.items():
        for keyword in agent.get("keywords", []):
            for fetch in (fetch_adzuna, fetch_reed):
                try:
                    jobs = fetch(keyword, cfg)
                except Exception as e:  # не друкуємо текст помилки: у ньому може бути URL із ключем
                    status = getattr(getattr(e, "response", None), "status_code", "")
                    log(f"  ! {fetch.__name__} '{keyword}': {type(e).__name__} {status}")
                    errors += 1
                    jobs = []
                else:
                    calls += 1
                time.sleep(0.3)
                for job in jobs:
                    fp = fingerprint(job)
                    if fp in found:
                        found[fp]["keys"].add(job["key"])
                        continue
                    job["keys"] = {job["key"]}
                    job["fp"] = fp
                    job["agent"] = name
                    company_n = norm_words(job["company"])
                    job["brand"] = any(b in company_n for b in all_brands)
                    found[fp] = job
    log(f"Запитів виконано: {calls}, помилок: {errors}, унікальних вакансій: {len(found)}")
    if calls == 0:
        raise SystemExit("Жодне джерело не відповіло. Перевірте ключі ADZUNA_* / REED_API_KEY у Secrets.")
    return list(found.values())


# ---------- фільтри та сортування ----------

def is_excluded(title: str, words: list[str]) -> bool:
    t = norm_words(title)
    return any(norm_words(w) in t for w in words)


def select(jobs: list[dict], cfg: dict, seen: dict) -> list[dict]:
    exclude = cfg.get("exclude_keywords", [])
    prio = cfg.get("salary", {}).get("priority_from", 15)
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    out = []
    for j in jobs:
        if is_excluded(j["title"], exclude):
            continue
        if j["posted"] and j["posted"] < cutoff:
            continue
        if j["fp"] in seen or any(k in seen for k in j["keys"]):
            continue
        j["top"] = bool(j["hourly"] and j["hourly"] >= prio)
        out.append(j)
    out.sort(key=lambda j: (not j["top"], not j["brand"], -(j["hourly"] or 0)))
    return out


# ---------- Telegram ----------

def esc(text: str) -> str:
    return html.escape(text, quote=False)


def format_job(j: dict, cfg: dict) -> str:
    high = cfg.get("salary", {}).get("highlight_from", 14)
    emoji = AGENT_EMOJI.get(j["agent"], "💼")
    lines = [f"{emoji} <b>{esc(j['title'])}</b>"]
    who = esc(j["company"]) if j["company"] else "—"
    if j["brand"]:
        who += " ⭐"
    lines.append(f"🏢 {who}")
    if j["location"]:
        lines.append(f"📍 {esc(j['location'])}")
    if j["salary"]:
        flag = " 🔥" if j["top"] else (" ✅" if j["hourly"] and j["hourly"] >= high else "")
        lines.append(f"💷 {esc(j['salary'])}{flag}")
    if j["url"]:
        lines.append(f'🔗 <a href="{html.escape(j["url"], quote=True)}">Відкрити вакансію</a>')
    return "\n".join(lines)


def send(text: str) -> bool:
    if DRY_RUN:
        log("----- DRY RUN -----\n" + text + "\n-------------------")
        return True
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    for _ in range(3):
        r = requests.post(url, json=payload, timeout=TIMEOUT)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if r.status_code != 200:
            log(f"Telegram {r.status_code}: {r.text}")
            return False
        return True
    return False


def post_jobs(jobs: list[dict], cfg: dict) -> list[dict]:
    """Надсилає вакансії пачками. Повертає ті, що точно надіслано."""
    sent, batch, batch_jobs, size = [], [], [], 0
    today = datetime.now().strftime("%d %b")
    header = f"📋 <b>QuickJobs · {today}</b> · нових: {len(jobs)}\n"

    def flush():
        nonlocal batch, batch_jobs, size
        if not batch:
            return
        text = (header + "\n" if not sent and not flushed[0] else "") + "\n\n".join(batch)
        flushed[0] = True
        if send(text):
            sent.extend(batch_jobs)
        batch, batch_jobs, size = [], [], 0
        time.sleep(1)

    flushed = [False]
    for j in jobs:
        block = format_job(j, cfg)
        if size + len(block) > 3500 or len(batch) >= 8:
            flush()
        batch.append(block)
        batch_jobs.append(j)
        size += len(block) + 2
    flush()
    return sent


# ---------- збережені (вже надіслані) вакансії ----------

def load_seen() -> dict:
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_seen(seen: dict):
    if DRY_RUN:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_KEEP_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")


# ---------- main ----------

def main():
    if not DRY_RUN and not (BOT_TOKEN and CHAT_ID):
        raise SystemExit("Немає BOT_TOKEN або CHAT_ID (додайте їх у GitHub Secrets).")
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    seen = load_seen()

    jobs = select(collect(cfg), cfg, seen)
    log(f"Нових вакансій після фільтрів: {len(jobs)}")
    if not jobs:
        log("Нічого нового, повідомлення не надсилаю.")
        save_seen(seen)
        return

    to_send = jobs[:MAX_PER_RUN]
    sent = post_jobs(to_send, cfg)
    now = datetime.now(timezone.utc).isoformat()
    for j in sent:
        seen[j["fp"]] = now
        for k in j["keys"]:
            seen[k] = now
    save_seen(seen)
    log(f"Надіслано: {len(sent)} з {len(to_send)} (залишилось на наступні дні: {len(jobs) - len(to_send)})")
    if len(sent) < len(to_send):
        sys.exit(1)


if __name__ == "__main__":
    main()
