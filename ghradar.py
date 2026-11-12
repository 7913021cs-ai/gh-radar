#!/usr/bin/env python3
"""GitHub 雷达：每天抓取飙升项目、新星项目、老牌项目的重大更新，生成日报。

只用 Python 标准库，无需 pip 安装。可选在 config.json 或环境变量 GITHUB_TOKEN
里放一个 GitHub token 以提高 API 限额（不放也能跑）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout

ROOT = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(ROOT, "reports")
DATA = os.path.join(ROOT, "data")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) gh-radar/1.0"

TODAY = dt.date.today()
NOW = dt.datetime.now(dt.timezone.utc)


def log(*a):
    print(f"[{dt.datetime.now():%H:%M:%S}]", *a, flush=True)


# ---------------------------------------------------------------- HTTP

def http_get(url: str, token: str = "", accept: str = "", retries: int = 3,
             timeout: int = 20) -> str:
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                raise
            if e.code in (403, 429):  # 限流：等一会儿再试
                wait = 10 * (i + 1)
                reset = e.headers.get("x-ratelimit-reset")
                if reset and e.headers.get("x-ratelimit-remaining") == "0":
                    wait = max(1, min(70, int(reset) - int(time.time()) + 1))
                log(f"限流 {e.code}，{wait}s 后重试：{url[:80]}")
                time.sleep(wait)
            else:
                time.sleep(2 * (i + 1))
        except Exception as e:  # 网络抖动
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"请求失败 {url}: {last}")


def gh_search(q: str, token: str, sort: str = "stars", per_page: int = 100) -> list[dict]:
    url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
        {"q": q, "sort": sort, "order": "desc", "per_page": per_page})
    data = json.loads(http_get(url, token, "application/vnd.github+json"))
    return [norm_api(it) for it in data.get("items", [])]


def norm_api(it: dict) -> dict:
    return {
        "repo": it["full_name"],
        "url": it["html_url"],
        "description": (it.get("description") or "").strip(),
        "language": it.get("language") or "",
        "stars": it.get("stargazers_count", 0),
        "forks": it.get("forks_count", 0),
        "created_at": it.get("created_at", ""),
        "pushed_at": it.get("pushed_at", ""),
        "topics": it.get("topics") or [],
        "archived": it.get("archived", False),
    }


# ---------------------------------------------------------------- 数据源 1：Trending

def _num(s: str) -> int:
    s = re.sub(r"[^\d]", "", s or "")
    return int(s) if s else 0


def fetch_trending(language: str = "", since: str = "daily") -> list[dict]:
    url = f"https://github.com/trending/{urllib.parse.quote(language)}?since={since}"
    page = http_get(url)
    out = []
    for art in re.findall(r'<article class="Box-row">(.*?)</article>', page, re.S):
        art = re.sub(r"<svg.*?</svg>", "", art, flags=re.S)
        m = re.search(r'<h2[^>]*>.*?href="/([^"/]+/[^"/]+)"', art, re.S)
        if not m:
            continue
        repo = m.group(1)
        desc = re.search(r'<p class="col-9[^"]*">(.*?)</p>', art, re.S)
        lang = re.search(r'itemprop="programmingLanguage">(.*?)<', art)
        stars = re.search(rf'href="/{re.escape(repo)}/stargazers"[^>]*>\s*([\d,]+)', art)
        forks = re.search(rf'href="/{re.escape(repo)}/forks"[^>]*>\s*([\d,]+)', art)
        gained = re.search(r'([\d,]+)\s+stars?\s+(today|this week|this month)', art)
        out.append({
            "repo": repo,
            "url": f"https://github.com/{repo}",
            "description": html.unescape(re.sub(r"<[^>]+>", "", desc.group(1))).strip() if desc else "",
            "language": html.unescape(lang.group(1)).strip() if lang else "",
            "stars": _num(stars.group(1)) if stars else 0,
            "forks": _num(forks.group(1)) if forks else 0,
            "gained": _num(gained.group(1)) if gained else 0,
            "since": since,
        })
    return out


# ---------------------------------------------------------------- 数据源 2：Releases

PRE_RE = re.compile(r"(?<![a-z])(alpha|beta|rc\d*|pre|preview|nightly|canary|dev|snapshot|insiders)"
                    r"(?![a-z])|\d(a|b)\d+$", re.I)
VER_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def is_prerelease(tag: str) -> bool:
    return bool(PRE_RE.search(tag))


def parse_ver(tag: str):
    m = VER_RE.search(tag.split("@")[-1])
    return tuple(int(x or 0) for x in m.groups()) if m else None


def bump_kind(new: str, older: list[str]) -> tuple[str, str | None]:
    """返回 (类型, 对比的上一版)。按语义化版本判断：x.y.Z(Z>0) 一定是补丁；
    比已发布的最高版本还低的视为旧分支维护版（backport）。"""
    a = parse_ver(new)
    vers = [(parse_ver(t), t) for t in older]
    vers = [(v, t) for v, t in vers if v]
    if not a:
        return "release", (older[0] if older else None)
    if vers and a < max(vers)[0]:
        return "backport", max(vers)[1]
    lower = [(v, t) for v, t in vers if v < a]
    prev = max(lower) if lower else None
    if a[2] > 0:
        return "patch", prev[1] if prev else None
    if prev is None:
        return ("major" if a[1] == 0 and a[0] > 0 else "release"), None
    b = prev[0]
    if a[0] != b[0]:
        return "major", prev[1]
    if a[1] != b[1]:
        return "minor", prev[1]
    return "patch", prev[1]


def strip_html(s: str, n: int = 260) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def fetch_release(repo: str, window_h: int) -> dict | None:
    """读 releases.atom（不占 API 限额），返回窗口期内最新的正式版。"""
    try:
        xml = http_get(f"https://github.com/{repo}/releases.atom", retries=1, timeout=10)
        root = ET.fromstring(xml)
    except Exception:
        return None
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = []
    for e in root.findall("a:entry", ns):
        link = e.find("a:link", ns)
        href = link.get("href") if link is not None else ""
        tag = urllib.parse.unquote(href.rsplit("/tag/", 1)[-1]) if "/tag/" in href else ""
        entries.append({
            "tag": tag,
            "title": (e.findtext("a:title", "", ns) or "").strip(),
            "updated": e.findtext("a:updated", "", ns),
            "url": href,
            "notes": strip_html(e.findtext("a:content", "", ns)),
        })
    # 只保留像版本号的正式版（过滤 tip / deployed / 提交哈希 之类的 CI 标签）
    stable = [x for x in entries if x["tag"] and not is_prerelease(x["tag"])
              and parse_ver(x["tag"]) and not re.search(r"[0-9a-f]{12,}", x["tag"])]
    if not stable:
        return None
    latest = stable[0]
    try:
        t = dt.datetime.fromisoformat(latest["updated"].replace("Z", "+00:00"))
    except ValueError:
        return None
    if NOW - t > dt.timedelta(hours=window_h):
        return None
    # 同一个 monorepo 可能有多个包，前一个版本优先找同前缀的 tag
    prefix = re.split(r"\d", latest["tag"], 1)[0]
    older = [x["tag"] for x in stable[1:] if re.split(r"\d", x["tag"], 1)[0] == prefix]
    latest["bump"], latest["prev"] = bump_kind(latest["tag"], older)
    # 发版节奏：距上一个“同级”正式版多少天。天天发 minor 的项目算例行更新，排后面
    latest["gap_days"] = None
    for x in stable[1:]:
        if x["tag"] == latest["prev"]:
            try:
                pt = dt.datetime.fromisoformat(x["updated"].replace("Z", "+00:00"))
                latest["gap_days"] = (t - pt).total_seconds() / 86400
            except ValueError:
                pass
            break
    latest["routine"] = (latest["bump"] in ("minor", "release")
                         and latest["gap_days"] is not None and latest["gap_days"] < 7)
    latest["published"] = t
    return latest


# ---------------------------------------------------------------- 历史库

def open_db() -> sqlite3.Connection:
    os.makedirs(DATA, exist_ok=True)
    db = sqlite3.connect(os.path.join(DATA, "history.sqlite"))
    db.executescript("""
        CREATE TABLE IF NOT EXISTS stars(repo TEXT, day TEXT, stars INT, PRIMARY KEY(repo, day));
        CREATE TABLE IF NOT EXISTS repos(repo TEXT PRIMARY KEY, url TEXT, description TEXT,
                                         language TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS seen(repo TEXT, section TEXT, first_day TEXT,
                                        PRIMARY KEY(repo, section));
    """)
    return db


def save_snapshot(db, repos: dict[str, dict]):
    day = TODAY.isoformat()
    for r in repos.values():
        if not r.get("stars"):
            continue
        db.execute("INSERT OR REPLACE INTO stars VALUES(?,?,?)", (r["repo"], day, r["stars"]))
        db.execute("""INSERT INTO repos VALUES(?,?,?,?,?) ON CONFLICT(repo) DO UPDATE SET
                      url=excluded.url, description=excluded.description,
                      language=excluded.language,
                      created_at=COALESCE(NULLIF(excluded.created_at,''), repos.created_at)""",
                   (r["repo"], r["url"], r.get("description", ""), r.get("language", ""),
                    r.get("created_at", "")))
    db.commit()


def mark_seen(db, section: str, items: list[dict]):
    """标记首次上榜的项目（🆕），方便每天只关注新面孔。"""
    day = TODAY.isoformat()
    for it in items:
        row = db.execute("SELECT first_day FROM seen WHERE repo=? AND section=?",
                         (it["repo"], section)).fetchone()
        if row is None:
            db.execute("INSERT INTO seen VALUES(?,?,?)", (it["repo"], section, day))
            it["is_new"] = True
        else:
            it["is_new"] = row[0] == day
    db.commit()


def star_acceleration(db, min_stars: int, exclude: set[str], limit: int) -> list[dict]:
    day = TODAY.isoformat()
    rows = db.execute("""
        SELECT t.repo, t.stars, p.stars, p.day, r.url, r.description, r.language
        FROM stars t
        JOIN stars p ON p.repo = t.repo AND p.day = (
            SELECT MAX(day) FROM stars WHERE repo = t.repo AND day < ?)
        JOIN repos r ON r.repo = t.repo
        WHERE t.day = ? AND t.stars >= ?""", (day, day, min_stars)).fetchall()
    out = []
    for repo, now_s, prev_s, prev_day, url, desc, lang in rows:
        if repo in exclude:
            continue
        days = max(1, (TODAY - dt.date.fromisoformat(prev_day)).days)
        per_day = (now_s - prev_s) / days
        if per_day <= 0:
            continue
        out.append({"repo": repo, "url": url, "description": desc or "", "language": lang or "",
                    "stars": now_s, "per_day": per_day, "pct": per_day / max(prev_s, 1) * 100,
                    "days": days})
    # 综合绝对增量和相对增速：既不只看大项目，也不被小基数放大
    out.sort(key=lambda x: x["per_day"] * (1 + min(x["pct"], 10)), reverse=True)
    return out[:limit]


# ---------------------------------------------------------------- 主流程

def load_config() -> dict:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["github_token"] = os.environ.get("GITHUB_TOKEN") or cfg.get("github_token", "")
    return cfg


def excluded(r: dict, kws: list[str]) -> bool:
    text = f"{r['repo']} {r.get('description', '')}".lower()
    return any(k.lower() in text for k in kws)


def reachable(url: str) -> bool:
    try:
        http_get(url, retries=1, timeout=10)
        return True
    except Exception as e:
        log(f"连不上 {url}: {e}")
        return False


def collect(cfg: dict) -> dict:
    token = cfg["github_token"]
    kws = cfg.get("exclude_keywords", [])
    all_repos: dict[str, dict] = {}
    errors = []
    # 先探测网络：github.com 网页不通时跳过 Trending 和 release 检查，避免每个请求都等到超时
    web_ok = reachable("https://github.com/trending")
    log(f"网络检测：github.com 网页 {'可用' if web_ok else '不可用'}")
    if not web_ok:
        errors.append("github.com 网页无法访问，跳过了今日飙升和版本更新")

    def remember(r):
        cur = all_repos.get(r["repo"])
        if cur is None:
            all_repos[r["repo"]] = dict(r)
        else:
            for k, v in r.items():
                if v and not cur.get(k):
                    cur[k] = v
            cur["stars"] = max(cur.get("stars", 0), r.get("stars", 0))

    # 1) Trending 今日
    trending: dict[str, dict] = {}
    for lang in (cfg.get("trending_languages", [""]) if web_ok else []):
        try:
            items = fetch_trending(lang, "daily")
            log(f"Trending[{lang or 'all'}] {len(items)} 个")
        except Exception as e:
            errors.append(f"Trending {lang or 'all'}: {e}")
            continue
        for it in items:
            remember(it)
            if not excluded(it, kws) and (it["repo"] not in trending
                                          or it["gained"] > trending[it["repo"]]["gained"]):
                trending[it["repo"]] = it
    trending_list = sorted(trending.values(), key=lambda x: x["gained"],
                           reverse=True)[: cfg.get("trending_limit", 30)]

    weekly = []
    if web_ok:
        try:
            weekly = fetch_trending("", "weekly")
            for it in weekly:
                remember(it)
        except Exception as e:
            errors.append(f"Trending weekly: {e}")

    # 2) 新星：近 N 天创建、星数最多
    since = (TODAY - dt.timedelta(days=cfg["new_star_days"])).isoformat()
    new_stars = []
    try:
        items = gh_search(f"created:>={since} stars:>={cfg['new_star_min_stars']} fork:false",
                          token)
        for it in items:
            remember(it)
            if it["archived"] or excluded(it, kws):
                continue
            age = max(1, (TODAY - dt.date.fromisoformat(it["created_at"][:10])).days)
            it["age_days"] = age
            it["per_day"] = it["stars"] / age
            new_stars.append(it)
        new_stars.sort(key=lambda x: x["per_day"], reverse=True)
        new_stars = new_stars[: cfg["new_star_limit"]]
        log(f"新星 {len(new_stars)} 个")
    except Exception as e:
        errors.append(f"新星搜索: {e}")

    # 3) 老牌项目的重大更新：候选 = 最近有提交的高星项目 + 本周 trending + 关注列表 + 历史库
    yday = (TODAY - dt.timedelta(days=2)).isoformat()
    lo = cfg["release_min_stars"]
    candidates: dict[str, dict] = {}
    for q in (f"stars:{lo}..{lo * 5} pushed:>={yday} fork:false archived:false",
              f"stars:{lo * 5}..{lo * 20} pushed:>={yday} fork:false archived:false",
              f"stars:>{lo * 20} pushed:>={yday} fork:false archived:false"):
        try:
            for it in gh_search(q, token, sort="updated"):
                remember(it)
                candidates[it["repo"]] = it
        except Exception as e:
            errors.append(f"候选搜索: {e}")
    for it in weekly + trending_list:
        if it["stars"] >= lo:
            candidates.setdefault(it["repo"], it)
    for repo in cfg.get("watchlist", []):
        candidates.setdefault(repo, all_repos.get(repo) or
                              {"repo": repo, "url": f"https://github.com/{repo}", "stars": 0})
    if not web_ok:
        candidates = {}
    log(f"检查 {len(candidates)} 个项目的 release …")

    window = cfg["release_window_hours"]
    # 有时间预算：网络慢（例如云端共享出口被限速）时，到点就用已检查完的部分，不会一直卡住
    budget = cfg.get("release_time_budget_sec", 300)
    t0 = time.time()
    rels = []
    ex = ThreadPoolExecutor(8)
    futs = {ex.submit(fetch_release, r, window): r for r in candidates}
    try:
        for i, fut in enumerate(as_completed(futs, timeout=budget), 1):
            rels.append((futs[fut], fut.result()))
            if i % 50 == 0:
                log(f"  已检查 {i}/{len(futs)}（{time.time() - t0:.0f}s）")
    except FuturesTimeout:
        done_n = len(rels)
        errors.append(f"release 检查超出 {budget}s 时间预算，只检查了 {done_n}/{len(futs)} 个项目")
        log(f"release 检查超时，已完成 {done_n}/{len(futs)}")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    releases = []
    rank = {"major": 0, "minor": 1, "release": 2, "patch": 3, "backport": 4}
    for repo, rel in rels:
        if not rel:
            continue
        if rel["bump"] in ("patch", "backport") and not cfg.get("release_include_patch"):
            continue
        base = dict(candidates[repo])
        if excluded(base, kws):
            continue
        base["release"] = rel
        releases.append(base)
    releases.sort(key=lambda x: (x["release"]["routine"], rank[x["release"]["bump"]],
                                 -x.get("stars", 0)))
    releases = releases[: cfg["release_limit"]]
    log(f"重大更新 {len(releases)} 个")

    # 4) 星标加速（基于历史快照）
    db = open_db()
    save_snapshot(db, all_repos)
    accel = star_acceleration(db, cfg["accel_min_stars"], set(trending), cfg["accel_limit"])
    has_history = db.execute("SELECT COUNT(DISTINCT day) FROM stars").fetchone()[0] > 1
    mark_seen(db, "trending", trending_list)
    mark_seen(db, "new", new_stars)
    mark_seen(db, "release", releases)
    mark_seen(db, "accel", accel)
    db.close()

    return {"trending": trending_list, "new": new_stars, "releases": releases,
            "accel": accel, "has_history": has_history, "errors": errors,
            "tracked": len(all_repos)}


# ---------------------------------------------------------------- 报告

BUMP_CN = {"major": "大版本", "minor": "功能版本", "release": "新发布", "patch": "补丁",
           "backport": "旧分支维护"}


def ago(t: dt.datetime) -> str:
    h = (NOW - t).total_seconds() / 3600
    return f"{max(1, int(h))} 小时前" if h < 48 else f"{int(h // 24)} 天前"


def fmt_n(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 10000 else f"{int(n):,}"


def render_md(res: dict) -> str:
    L = [f"# GitHub 雷达 · {TODAY}", ""]
    L.append(f"今日飙升 {len(res['trending'])} · 新星 {len(res['new'])} · "
             f"重大更新 {len(res['releases'])} · 星标加速 {len(res['accel'])}\n")

    def new_tag(it):
        return "🆕 " if it.get("is_new") else ""

    L.append("## 🔥 今日飙升（GitHub Trending）\n")
    for i, it in enumerate(res["trending"], 1):
        L.append(f"{i}. {new_tag(it)}**[{it['repo']}]({it['url']})** · +{fmt_n(it['gained'])} ⭐ 今日"
                 f" · 共 {fmt_n(it['stars'])} · {it['language'] or '—'}  \n   {it['description']}")
    L.append("\n## 🌱 新星项目（近期新建、增长最快）\n")
    for i, it in enumerate(res["new"], 1):
        L.append(f"{i}. {new_tag(it)}**[{it['repo']}]({it['url']})** · {fmt_n(it['stars'])} ⭐"
                 f" · {it['age_days']} 天 · 日均 {fmt_n(it['per_day'])} · {it['language'] or '—'}"
                 f"  \n   {it['description']}")
    L.append("\n## 🚀 老牌项目重大更新（新版本发布）\n")
    for i, it in enumerate(res["releases"], 1):
        r = it["release"]
        prev = f"（上一版 {r['prev']}）" if r.get("prev") else ""
        L.append(f"{i}. **[{it['repo']}]({it['url']})** · [{r['tag']}]({r['url']}) "
                 f"`{BUMP_CN[r['bump']]}`{' `例行`' if r['routine'] else ''} {prev} · {ago(r['published'])} · {fmt_n(it.get('stars', 0))} ⭐  \n"
                 f"   {it.get('description', '')}  \n   > {r['notes'] or r['title']}")
    L.append("\n## 📈 星标加速（不在 Trending 上，但涨得快）\n")
    if not res["has_history"]:
        L.append("_需要至少两天的快照，明天起开始显示。_")
    for i, it in enumerate(res["accel"], 1):
        L.append(f"{i}. {new_tag(it)}**[{it['repo']}]({it['url']})** · +{fmt_n(it['per_day'])}/天"
                 f"（{it['pct']:.2f}%）· 共 {fmt_n(it['stars'])} · {it['language'] or '—'}  \n"
                 f"   {it['description']}")
    if res["errors"]:
        L.append("\n---\n**抓取时的问题：**\n" + "\n".join(f"- {e}" for e in res["errors"]))
    return "\n".join(L) + "\n"


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1f2328;--mut:#656d76;--line:#e5e7eb;--acc:#0969da;
--hot:#cf222e;--new:#1a7f37;--maj:#8250df;--min:#0969da;--rel:#9a6700}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--card:#161b22;--fg:#e6edf3;--mut:#8d96a0;
--line:#30363d;--acc:#4493f8;--hot:#f85149;--new:#3fb950;--maj:#a371f7;--min:#4493f8;--rel:#d29922}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:26px;margin:0 0 4px}.sub{color:var(--mut);margin-bottom:18px}
nav a{display:inline-block;margin:0 8px 8px 0;padding:4px 12px;border:1px solid var(--line);
border-radius:999px;color:var(--fg);text-decoration:none;background:var(--card);font-size:14px}
h2{font-size:19px;margin:30px 0 10px;padding-top:6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:8px 0}
.top{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.rank{color:var(--mut);font-variant-numeric:tabular-nums;min-width:20px}
.name{font-weight:600;color:var(--acc);text-decoration:none;font-size:16px}
.meta{color:var(--mut);font-size:13px;margin-left:auto;white-space:nowrap}
.big{color:var(--hot);font-weight:600}
.desc{margin:4px 0 0 30px}.notes{margin:6px 0 0 30px;color:var(--mut);font-size:13px;
border-left:3px solid var(--line);padding-left:10px}
.b{font-size:12px;padding:1px 8px;border-radius:999px;border:1px solid currentColor;font-weight:600}
.b-new{color:var(--new)}.b-major{color:var(--maj)}.b-minor{color:var(--min)}.b-release{color:var(--rel)}
.b-patch,.b-backport{color:var(--mut)}.empty{color:var(--mut);font-style:italic}
.err{color:var(--hot);font-size:13px}a.tag{color:var(--acc)}
"""


def render_html(res: dict) -> str:
    e = html.escape

    def badge_new(it):
        return '<span class="b b-new">NEW</span>' if it.get("is_new") else ""

    def card(i, it, meta, extra=""):
        return (f'<div class="card"><div class="top"><span class="rank">{i}</span>'
                f'<a class="name" href="{e(it["url"])}" target="_blank">{e(it["repo"])}</a>'
                f'{badge_new(it)}{extra}<span class="meta">{meta}</span></div>'
                f'<div class="desc">{e(it.get("description") or "")}</div>')

    def lang(it):
        return e(it.get("language") or "—")

    parts = []
    s = [f'<span class="big">+{fmt_n(it["gained"])} ⭐</span> 今日 · 共 {fmt_n(it["stars"])} · {lang(it)}'
         for it in res["trending"]]
    parts.append(("hot", "🔥 今日飙升", "GitHub Trending 当日榜，按今日新增星数排序",
                  [card(i, it, s[i - 1]) + "</div>" for i, it in enumerate(res["trending"], 1)]))

    parts.append(("new", "🌱 新星项目", "近期新建的项目，按日均增星排序（越靠前越“火”）",
                  [card(i, it, f'<span class="big">{fmt_n(it["stars"])} ⭐</span> · {it["age_days"]} 天 · '
                               f'日均 {fmt_n(it["per_day"])} · {lang(it)}') + "</div>"
                   for i, it in enumerate(res["new"], 1)]))

    rel_cards = []
    for i, it in enumerate(res["releases"], 1):
        r = it["release"]
        prev = f' ← {e(r["prev"])}' if r.get("prev") else ""
        routine = '<span class="b b-patch">例行</span>' if r["routine"] else ""
        extra = (f'<span class="b b-{r["bump"]}">{BUMP_CN[r["bump"]]}</span>{routine}'
                 f'<a class="tag" href="{e(r["url"])}" target="_blank">{e(r["tag"])}</a>'
                 f'<span class="meta" style="margin-left:0">{prev} · {ago(r["published"])}</span>')
        rel_cards.append(card(i, it, f'{fmt_n(it.get("stars", 0))} ⭐ · {lang(it)}', extra)
                         + f'<div class="notes">{e(r["notes"] or r["title"])}</div></div>')
    parts.append(("rel", "🚀 老牌项目重大更新", f"高星项目近期发布的大版本 / 功能版本（已过滤补丁、预发布、旧分支维护版；“例行”= 距上一版不到 7 天）",
                  rel_cards))

    acc = [card(i, it, f'<span class="big">+{fmt_n(it["per_day"])}/天</span> ({it["pct"]:.2f}%) · '
                       f'共 {fmt_n(it["stars"])} · {lang(it)}') + "</div>"
           for i, it in enumerate(res["accel"], 1)]
    if not res["has_history"]:
        acc = ['<p class="empty">需要至少两天的快照，明天起开始显示。</p>']
    parts.append(("acc", "📈 星标加速", "不在今日 Trending 上、但星数增长明显的项目（基于每日快照对比）", acc))

    nav = "".join(f'<a href="#{k}">{t} · {len(c) if c and "empty" not in c[0] else 0}</a>'
                  for k, t, _, c in parts)
    body = []
    for k, t, sub, cards in parts:
        body.append(f'<h2 id="{k}">{t}</h2><div class="sub">{sub}</div>')
        body.append("".join(cards) or '<p class="empty">今天没有符合条件的项目。</p>')
    if res["errors"]:
        body.append('<h2>抓取时的问题</h2>' + "".join(f'<p class="err">{e(x)}</p>' for x in res["errors"]))
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GitHub 雷达 · {TODAY}</title><style>{CSS}</style></head><body><div class="wrap">
<h1>GitHub 雷达 · {TODAY}</h1>
<div class="sub">生成于 {dt.datetime.now():%Y-%m-%d %H:%M} · 本次追踪 {res['tracked']} 个项目 ·
<a href="index.html">历史日报</a> · NEW = 首次上榜</div>
<nav>{nav}</nav>{''.join(body)}</div></body></html>"""


def write_index():
    files = sorted((f for f in os.listdir(REPORTS) if re.match(r"\d{4}-\d{2}-\d{2}\.html$", f)),
                   reverse=True)
    items = "".join(f'<div class="card"><a class="name" href="{f}">{f[:-5]}</a></div>' for f in files)
    with open(os.path.join(REPORTS, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>GitHub 雷达 · 历史日报</title><style>{CSS}</style></head><body><div class="wrap">
<h1>GitHub 雷达 · 历史日报</h1>{items}</div></body></html>""")


def notify(res: dict, path: str):
    top = [it["repo"] for it in res["trending"][:2]] + [it["repo"] for it in res["releases"][:1]]
    msg = (f"飙升 {len(res['trending'])} · 新星 {len(res['new'])} · 更新 {len(res['releases'])}  "
           + "、".join(top))
    msg = msg.replace('"', "'").replace("\\", "")
    subprocess.run(["osascript", "-e",
                    f'display notification "{msg}" with title "GitHub 雷达 {TODAY}"'],
                   check=False)


# ---------------------------------------------------------------- 远程推送（云端运行时用）

def report_url() -> str:
    """日报网址：优先 REPORT_URL，其次按 GitHub Pages 规则推断。"""
    if os.environ.get("REPORT_URL"):
        return os.environ["REPORT_URL"].rstrip("/") + f"/{TODAY}.html"
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/{TODAY}.html"
    return ""


def summary_text(res: dict, n: int = 5) -> str:
    L = []
    for title, key, fmt in (
        ("🔥 今日飙升", "trending", lambda it: f"+{fmt_n(it['gained'])}⭐"),
        ("🌱 新星", "new", lambda it: f"日均 {fmt_n(it['per_day'])}⭐"),
        ("🚀 重大更新", "releases", lambda it: it["release"]["tag"]),
    ):
        items = res[key][:n]
        if not items:
            continue
        L.append(f"{title}")
        for it in items:
            desc = (it.get("description") or "")[:60]
            L.append(f"· {it['repo']}  {fmt(it)}  {desc}")
        L.append("")
    return "\n".join(L).strip()


def _post(url: str, data: dict, as_json: bool = True):
    body = json.dumps(data).encode() if as_json else urllib.parse.urlencode(data).encode()
    ctype = "application/json" if as_json else "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, body, {"Content-Type": ctype, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def push_remote(res: dict, page_html: str):
    """按环境变量里配置了哪些就推哪些；都没配置则什么也不做。"""
    title = f"GitHub 雷达 {TODAY}"
    url = report_url()
    text = summary_text(res) + (f"\n\n完整日报：{url}" if url else "")
    env = os.environ.get
    done, failed = [], []

    def attempt(name, fn):
        try:
            fn()
            done.append(name)
        except Exception as e:  # 推送失败不影响日报本身
            failed.append(f"{name}: {e}")

    if env("SERVERCHAN_KEY"):  # 微信（Server酱 sct.ftqq.com）
        attempt("Server酱", lambda: _post(
            f"https://sctapi.ftqq.com/{env('SERVERCHAN_KEY')}.send",
            {"title": title, "desp": text.replace("\n", "\n\n")}, as_json=False))
    if env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"):
        attempt("Telegram", lambda: _post(
            f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/sendMessage",
            {"chat_id": env("TELEGRAM_CHAT_ID"), "text": f"{title}\n\n{text}",
             "disable_web_page_preview": True}))
    if env("BARK_URL"):  # iPhone（Bark App），形如 https://api.day.app/你的key
        attempt("Bark", lambda: _post(env("BARK_URL").rstrip("/"),
                                      {"title": title, "body": text[:900], "url": url,
                                       "group": "GitHub 雷达"}))
    if env("SMTP_HOST") and env("SMTP_USER") and env("SMTP_PASS") and env("MAIL_TO"):
        def mail():
            import smtplib
            from email.mime.text import MIMEText
            from email.header import Header
            msg = MIMEText(page_html, "html", "utf-8")
            msg["Subject"] = Header(title, "utf-8")
            msg["From"] = env("SMTP_USER")
            msg["To"] = env("MAIL_TO")
            port = int(env("SMTP_PORT") or 465)
            if port == 465:
                srv = smtplib.SMTP_SSL(env("SMTP_HOST"), port, timeout=30)
            else:
                srv = smtplib.SMTP(env("SMTP_HOST"), port, timeout=30)
                srv.starttls()
            srv.login(env("SMTP_USER"), env("SMTP_PASS"))
            srv.sendmail(env("SMTP_USER"), [x.strip() for x in env("MAIL_TO").split(",")],
                         msg.as_string())
            srv.quit()
        attempt("邮件", mail)
    if done:
        log("已推送：", "、".join(done))
    for f in failed:
        log("推送失败：", f)


def main():
    ap = argparse.ArgumentParser(description="GitHub 雷达日报")
    ap.add_argument("--open", action="store_true", help="生成后在浏览器打开")
    ap.add_argument("--no-notify", action="store_true", help="不弹系统通知")
    args = ap.parse_args()

    cfg = load_config()
    os.makedirs(REPORTS, exist_ok=True)
    res = collect(cfg)
    md_path = os.path.join(REPORTS, f"{TODAY}.md")
    html_path = os.path.join(REPORTS, f"{TODAY}.html")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_md(res))
    page = render_html(res)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(page)
    with open(os.path.join(REPORTS, "latest.html"), "w", encoding="utf-8") as f:
        f.write(page)
    write_index()
    log(f"日报已生成：{html_path}")
    if res["errors"]:
        log("部分数据源出错：", *res["errors"])
    push_remote(res, page)
    if cfg.get("notify") and not args.no_notify and sys.platform == "darwin":
        notify(res, html_path)
    if (args.open or cfg.get("open_report")) and sys.platform == "darwin":
        subprocess.run(["open", html_path], check=False)


if __name__ == "__main__":
    main()
