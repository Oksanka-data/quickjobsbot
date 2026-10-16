"""QuickJobs: щоденна добірка вакансій (Reed, Jooble, Adzuna) у Telegram-канал.

Налаштування беруться з config.yaml, секрети - зі змінних середовища:
BOT_TOKEN, CHAT_ID, REED_API_KEY, JOOBLE_API_KEY, ADZUNA_APP_ID, ADZUNA_APP_KEY.
Достатньо мати ключ хоча б одного з джерел; джерела без ключа пропускаються.
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
JOOBLE_KEY = os.getenv("JOOBLE_API_KEY")
DRY_RUN = os.getenv("DRY_RUN") == "1"

MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "25"))  # скільки вакансій максимум за один запуск
MAX_AGE_DAYS = 7  # не показувати вакансії старші за N днів
SEEN_KEEP_DAYS = 60  # скільки днів пам'ятати вже надіслані
TIMEOUT = 20

REMOTE_WORDS = (" remote ", " work from home ", " home based ", " homeworking ", " home working ", " wfh ")

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
    """Повертає (hourly | None, текст для показу, оцінка_за_годину_з_річної | None)."""
    hourly = parse_hourly(title, snippet)
    if hourly is None and lo and not predicted:
        top = float(hi or lo)
        if top < 100:  # схоже на погодинну ставку
            hourly = top
    if hourly is not None:
        return hourly, f"£{hourly:.2f}/h".replace(".00/h", "/h"), None
    if lo and not predicted:
        lo_i, hi_i = int(lo), int(hi or lo)
        top = max(lo_i, hi_i)
        rng = f"£{lo_i:,}–£{hi_i:,}" if lo_i != hi_i else f"£{lo_i:,}"
        if top >= 8000:  # річна зарплата (повний робочий тиждень)
            est = top / 1950 if top <= 200000 else None
            return None, f"{rng}/yr", est
        return None, rng, None  # незрозуміла одиниця (наприклад, ставка за день)
    return None, "", None


SAL_NUM_RE = re.compile(r"£\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(\s*k\b)?", re.I)


def salary_from_text(raw: str, title: str, snippet: str):
    """Зарплата, записана рядком ("£12 - £14 per hour", "£25,000 a year"). Повертає як salary_info."""
    hourly = parse_hourly(raw, title, snippet)
    if hourly is not None:
        return hourly, f"£{hourly:.2f}/h".replace(".00/h", "/h"), None
    nums = []
    for m in SAL_NUM_RE.finditer(raw or ""):
        value = float(m.group(1).replace(",", ""))
        nums.append(value * 1000 if m.group(2) else value)
    if nums:
        return salary_info(min(nums), max(nums), False, "", "")
    return None, "", None


# ---------- джерела ----------

def fetch_adzuna(keyword: str, cfg: dict, agent: dict, remote: bool = False) -> list[dict]:
    if not (ADZUNA_ID and ADZUNA_KEY):
        return []
    loc = cfg["location"]
    radius = agent.get("radius_miles", loc.get("radius_miles", 5))
    params = {
        "app_id": ADZUNA_ID,
        "app_key": ADZUNA_KEY,
        "what": keyword + (" remote" if remote else ""),
        "max_days_old": MAX_AGE_DAYS,
        "results_per_page": 50,
        "sort_by": "date",
        "content-type": "application/json",
    }
    if not remote:
        params["where"] = loc["home"]
        params["distance"] = max(1, round(radius * 1.609))  # Adzuna: кілометри
    if "part-time" in [e.lower() for e in cfg.get("employment", [])]:
        params["part_time"] = 1
    r = requests.get("https://api.adzuna.com/v1/api/jobs/gb/search/1", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("results", []):
        title, snippet = clean(j.get("title")), clean(j.get("description"))
        hourly, sal, est = salary_info(
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
            "url": j.get("redirect_url", ""), "hourly": hourly, "est": est, "salary": sal,
            "posted": posted, "snippet": snippet,
        })
    return jobs


def fetch_reed(keyword: str, cfg: dict, agent: dict, remote: bool = False) -> list[dict]:
    if not REED_KEY:
        return []
    loc = cfg["location"]
    radius = agent.get("radius_miles", loc.get("radius_miles", 5))
    params = {"keywords": keyword + (" remote" if remote else ""), "resultsToTake": 50}
    if not remote:
        params["locationName"] = loc["home"]
        params["distanceFromLocation"] = radius  # Reed: милі
    if "part-time" in [e.lower() for e in cfg.get("employment", [])]:
        params["partTime"] = "true"
    r = requests.get("https://www.reed.co.uk/api/1.0/search", params=params, auth=(REED_KEY, ""), timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("results", []):
        title, snippet = clean(j.get("jobTitle")), clean(j.get("jobDescription"))
        hourly, sal, est = salary_info(j.get("minimumSalary"), j.get("maximumSalary"), False, title, snippet)
        posted = None
        try:
            posted = datetime.strptime(j["date"], "%d/%m/%Y").replace(tzinfo=timezone.utc)
        except Exception:
            pass
        jobs.append({
            "key": f"reed:{j['jobId']}", "title": title,
            "company": clean(j.get("employerName")), "location": clean(j.get("locationName")),
            "url": j.get("jobUrl", ""), "hourly": hourly, "est": est, "salary": sal,
            "posted": posted, "snippet": snippet,
        })
    return jobs


def fetch_jooble(keyword: str, cfg: dict, agent: dict, remote: bool = False) -> list[dict]:
    """Jooble API: POST https://jooble.org/api/<ключ> з JSON-тілом."""
    loc = cfg["location"]
    radius = agent.get("radius_miles", loc.get("radius_miles", 5))
    part_time = "part-time" in [e.lower() for e in cfg.get("employment", [])]
    body = {"keywords": f"{keyword}{' remote' if remote else ''}{' part time' if part_time else ''}", "page": "1"}
    if not remote:
        body["location"] = loc["home"]
        body["radius"] = str(max(1, round(radius * 1.609)))  # Jooble: кілометри
    r = requests.post(f"https://jooble.org/api/{JOOBLE_KEY}", json=body, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict) or "jobs" not in data:
        log(f"  ! Jooble: неочікувана відповідь (ключі: {list(data)[:5] if isinstance(data, dict) else type(data).__name__})")
        return []
    jobs = []
    for j in data["jobs"]:
        title, snippet = clean(j.get("title")), clean(j.get("snippet"))
        link = j.get("link", "")
        hourly, sal, est = salary_from_text(clean(j.get("salary")), title, snippet)
        posted = None
        try:
            posted = datetime.fromisoformat(str(j["updated"])[:19]).replace(tzinfo=timezone.utc)
        except Exception:
            pass
        jobs.append({
            "key": f"jooble:{j.get('id') or abs(hash(link))}", "title": title,
            "company": clean(j.get("company")), "location": clean(j.get("location")),
            "url": link, "hourly": hourly, "est": est, "salary": sal,
            "posted": posted, "snippet": snippet,
        })
    return jobs


def title_ok(title: str, agent: dict) -> bool:
    """Додатковий фільтр за назвою посади для конкретної категорії (title_match / title_exclude у config.yaml)."""
    t = norm_words(title)
    match = agent.get("title_match")
    if match and not any(norm_words(w) in t for w in match):
        return False
    return not any(norm_words(w) in t for w in agent.get("title_exclude", []))


def remote_ok(job: dict) -> bool:
    blob = norm_words(" ".join([job["title"], job["location"], job.get("snippet", "")]))
    return any(w in blob for w in REMOTE_WORDS)


def collect(cfg: dict) -> list[dict]:
    """Збирає вакансії з усіх увімкнених агентів і джерел, без дублікатів."""
    agents = {n: a for n, a in (cfg.get("agents") or {}).items() if a.get("enabled")}
    brands = {n: [norm_words(c) for c in a.get("companies", [])] for n, a in agents.items()}
    all_brands = [b for lst in brands.values() for b in lst]

    sources = [f for f, key in (
        (fetch_reed, REED_KEY), (fetch_jooble, JOOBLE_KEY), (fetch_adzuna, ADZUNA_ID and ADZUNA_KEY),
    ) if key]
    if not sources:
        raise SystemExit("Немає жодного ключа джерела. Додайте REED_API_KEY і/або JOOBLE_API_KEY у GitHub Secrets.")
    log("Джерела: " + ", ".join(f.__name__.replace("fetch_", "") for f in sources))

    found: dict[str, dict] = {}
    calls = errors = 0
    for name, agent in agents.items():
        modes = [False] + ([True] if agent.get("remote") else [])  # remote: додатковий пошук без прив'язки до району
        for keyword in agent.get("keywords", []):
            for remote in modes:
                for fetch in sources:
                    try:
                        jobs = fetch(keyword, cfg, agent, remote)
                    except Exception as e:  # не друкуємо текст помилки: у ньому може бути URL із ключем
                        status = getattr(getattr(e, "response", None), "status_code", "")
                        log(f"  ! {fetch.__name__} '{keyword}': {type(e).__name__} {status}")
                        errors += 1
                        jobs = []
                    else:
                        calls += 1
                    time.sleep(0.3)
                    for job in jobs:
                        if not title_ok(job["title"], agent):
                            continue  # не підходить цій категорії - може підійти іншій
                        is_remote = remote_ok(job)
                        if remote and not is_remote:
                            continue  # у remote-пошуку лишаємо тільки справжній віддалений формат
                        fp = fingerprint(job)
                        if fp in found:
                            found[fp]["keys"].add(job["key"])
                            continue
                        job["keys"] = {job["key"]}
                        job["fp"] = fp
                        job["agent"] = name
                        job["remote"] = is_remote
                        company_n = norm_words(job["company"])
                        job["brand"] = any(b in company_n for b in all_brands)
                        found[fp] = job
    log(f"Запитів виконано: {calls}, помилок: {errors}, унікальних вакансій: {len(found)}")
    if calls == 0:
        raise SystemExit("Жодне джерело не відповіло. Перевірте ключі в GitHub Secrets.")
    return list(found.values())


# ---------- фільтри та сортування ----------

def is_excluded(title: str, words: list[str]) -> bool:
    t = norm_words(title)
    return any(norm_words(w) in t for w in words)


def select(jobs: list[dict], cfg: dict, seen: dict) -> list[dict]:
    exclude = cfg.get("exclude_keywords", [])
    prio = cfg.get("salary", {}).get("priority_from", 15)
    order = {n: i for i, n in enumerate(cfg.get("agents") or {})}
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    out = []
    for j in jobs:
        if is_excluded(j["title"], exclude):
            continue
        if j["posted"] and j["posted"] < cutoff:
            continue
        if j["fp"] in seen or any(k in seen for k in j["keys"]):
            continue
        j["rate"] = j["hourly"] or j["est"] or 0
        j["top"] = j["rate"] >= prio
        out.append(j)
    # спочатку категорії за порядком у config.yaml (tech - перша), всередині - краща оплата та знайомі бренди
    out.sort(key=lambda j: (order.get(j["agent"], 99), not j["top"], not j["brand"], -j["rate"]))
    return out


# ---------- Telegram ----------

def esc(text: str) -> str:
    return html.escape(text, quote=False)


def plural_jobs(n: int, new: bool = True) -> str:
    """Українська множина: 1 нова вакансія / 2 нові вакансії / 5 нових вакансій."""
    if n == 0:
        return "0 вакансій"
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return f"{n} {'нова ' if new else ''}вакансія"
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return f"{n} {'нові ' if new else ''}вакансії"
    return f"{n} {'нових ' if new else ''}вакансій"


def category_list(cfg: dict) -> list[tuple[str, str, str]]:
    """[(ключ, емодзі, назва)] у порядку з config.yaml."""
    out = []
    for name, a in (cfg.get("agents") or {}).items():
        if a.get("enabled"):
            out.append((name, a.get("emoji", "💼"), a.get("title") or name.replace("_", " ").upper()))
    return out


def format_job(j: dict, cfg: dict) -> str:
    high = cfg.get("salary", {}).get("highlight_from", 14)
    lines = [f"<b>{esc(j['title'])}</b>"]
    who = esc(j["company"]) if j["company"] else "—"
    if j["brand"]:
        who += " ⭐"
    lines.append(f"🏢 {who}")
    place = esc(j["location"]) if j["location"] else ""
    if j.get("remote"):
        place = (place + " · " if place else "") + "🏠 remote/WFH"
    if place:
        lines.append(f"📍 {place}")
    if j["salary"]:
        flag = " 🔥" if j["top"] else (" ✅" if j["rate"] >= high else "")
        est = f" (≈£{j['est']:.0f}/h)" if j.get("est") else ""
        lines.append(f"💷 {esc(j['salary'])}{est}{flag}")
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


def build_summary(by: dict, cats: list, jobs: list[dict], cfg: dict, remaining: int) -> str:
    prio = cfg.get("salary", {}).get("priority_from", 15)
    lines = [f"📋 <b>QuickJobs · {datetime.now().strftime('%d %b')}</b>", ""]
    for name, emoji, title in cats:
        lines.append(f"{emoji} <b>{esc(title)}</b>")
        lines.append(plural_jobs(len(by.get(name, []))))
    n_high = sum(1 for j in jobs if j["top"])
    lines.append("💰 <b>HIGH PAY</b>")
    lines.append(plural_jobs(n_high, new=False) + (f" від £{prio}/год · позначені 🔥" if n_high else ""))
    if remaining:
        lines += ["", f"➕ Ще {remaining} - покажу в наступному запуску"]
    return "\n".join(lines)


def post_jobs(jobs: list[dict], cfg: dict, remaining: int = 0) -> list[dict]:
    """Надсилає підсумок, потім вакансії за категоріями. Повертає ті, що точно надіслано."""
    cats = category_list(cfg)
    by: dict[str, list[dict]] = {}
    for j in jobs:
        by.setdefault(j["agent"], []).append(j)

    if not send(build_summary(by, cats, jobs, cfg, remaining)):
        return []  # без підсумку нічого не позначаємо як надіслане - завтра спробуємо знову
    time.sleep(1)

    sent: list[dict] = []
    msg: list[str] = []
    msg_jobs: list[dict] = []
    size = 0

    def flush():
        nonlocal msg, msg_jobs, size
        if msg and send("\n\n".join(msg)):
            sent.extend(msg_jobs)
        msg, msg_jobs, size = [], [], 0
        time.sleep(1)

    for name, emoji, title in cats:
        items = by.get(name, [])
        if not items:
            continue
        head = f"{emoji} <b>{esc(title)}</b> · {len(items)}"
        head_open = False
        for j in items:
            block = format_job(j, cfg)
            extra = len(block) + 2 + (0 if head_open else len(head) + 2)
            if msg and size + extra > 3500:
                flush()
                head, head_open = f"{emoji} <b>{esc(title)}</b> (далі)", False
                extra = len(block) + len(head) + 4
            if not head_open:
                msg.append(head)
                head_open = True
            msg.append(block)
            msg_jobs.append(j)
            size += extra
        # між категоріями - окреме повідомлення, щоб читалося як розділи
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
    sent = post_jobs(to_send, cfg, len(jobs) - len(to_send))
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
