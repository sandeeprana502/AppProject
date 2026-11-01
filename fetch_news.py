#!/usr/bin/env python3
"""Science In-Short pipeline: fetch -> parse -> clean -> tag -> publish data/news.json.

Run locally:  python fetch_news.py
Runs on a schedule in GitHub Actions (see .github/workflows/fetch.yml).
"""
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).parent
CONFIG = ROOT / "sources.json"
OUT = ROOT / "data" / "news.json"
LOG = ROOT / "data" / "run_log.json"

UA = {"User-Agent": "ScienceInShortBot/0.1 (+https://github.com/sandeeprana502)"}
TIMEOUT = 25
MIN_WORDS, MAX_WORDS, SNIPPET_WORDS = 100, 150, 35
PER_SOURCE, MAX_ITEMS, MAX_AGE_DAYS = 6, 60, 21


# ---------- helpers ----------
def get(url, params=None, tries=3):
    """GET with a few retries for network errors and 5xx server errors."""
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
            if r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code} Server Error for url: {url}", response=r)
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = exc
        except requests.HTTPError as exc:
            last = exc
            if exc.response is not None and exc.response.status_code < 500:
                raise  # 403/404 etc. will not fix themselves by retrying
        if attempt < tries - 1:
            time.sleep(2 * (attempt + 1))
    raise last


def clean_text(raw):
    """Remove HTML tags and entities, collapse whitespace."""
    text = html.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def summarise(text, full):
    """Take whole sentences from the start of the text.

    full=True  -> aim for MIN_WORDS..MAX_WORDS (open sources)
    full=False -> short snippet of about SNIPPET_WORDS (terms not confirmed)
    Returns (summary, word_count). Never pads: short input gives a short summary.
    """
    limit = MAX_WORDS if full else SNIPPET_WORDS
    target = MIN_WORDS if full else SNIPPET_WORDS
    out, n = [], 0
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        w = len(sentence.split())
        if not w:
            continue
        if n + w > limit:
            if not out:
                out.append(" ".join(sentence.split()[:limit]) + "\u2026")
                n = limit
            break
        out.append(sentence)
        n += w
        if n >= target:
            break
    return " ".join(out), n


def to_dt(value):
    """Parse a feedparser time struct, ISO string or epoch ms into aware UTC datetime."""
    if value is None:
        return None
    try:
        if hasattr(value, "tm_year"):
            return datetime(*value[:6], tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def names(authors, limit=3):
    authors = [a for a in authors if a]
    if not authors:
        return ""
    s = ", ".join(authors[:limit])
    return s + " et al." if len(authors) > limit else s


def raw(title, text, url, author="", published=None, image=""):
    return {"title": clean_text(title), "text": clean_text(text), "url": url,
            "author": author, "published": published, "image": image}


def date_window(days=7):
    end = datetime.now(timezone.utc).date()
    return (end - timedelta(days=days)).isoformat(), end.isoformat()


# ---------- fetchers: each returns a list of raw() dicts ----------
def fetch_feed(src):
    feed = feedparser.parse(get(src["url"]).content)
    items = []
    for e in feed.entries[:PER_SOURCE * 2]:
        body = e.get("summary") or (e.get("content") or [{}])[0].get("value", "")
        authors = [a.get("name", "") for a in e.get("authors", [])] or [e.get("author", "")]
        image = ""
        for key in ("media_content", "media_thumbnail"):
            if e.get(key):
                image = e[key][0].get("url", "")
                break
        if not image and e.get("enclosures"):
            image = e["enclosures"][0].get("href", "")
        items.append(raw(e.get("title", ""), body, e.get("link", ""), names(authors),
                         to_dt(e.get("published_parsed") or e.get("updated_parsed")), image))
    return items


def fetch_usgs(src):
    data = get(src["url"]).json()
    items = []
    for f in data.get("features", [])[:PER_SOURCE * 2]:
        p, g = f.get("properties", {}), f.get("geometry", {})
        when = to_dt(p.get("time"))
        depth = (g.get("coordinates") or [0, 0, None])[2]
        text = (f"A magnitude {p.get('mag')} earthquake was recorded {p.get('place')}. "
                f"The event happened on {when:%d %B %Y at %H:%M} UTC"
                + (f", at a depth of about {depth:.0f} km. " if depth is not None else ". "))
        if p.get("tsunami"):
            text += "The agency has flagged a possible tsunami threat for this event. "
        if p.get("felt"):
            text += f"About {p['felt']} people reported feeling it. "
        text += ("Magnitude and location can change as the agency reviews the data. "
                 "The agency event page has the map, updates and safety information.")
        items.append(raw(p.get("title", "Earthquake"), text, p.get("url", ""),
                         "USGS", when))
    return items


def fetch_eonet(src):
    data = get(src["url"]).json()
    items = []
    for ev in data.get("events", [])[:PER_SOURCE * 2]:
        cat = (ev.get("categories") or [{}])[0].get("title", "natural event")
        geo = (ev.get("geometry") or [{}])[-1]
        link = (ev.get("sources") or [{}])[0].get("url") or ev.get("link", "")
        text = (f"NASA\u2019s Earth Observatory Natural Event Tracker lists an open event in the "
                f"category {cat.lower()}. {ev.get('description') or ''} "
                "The tracker collects reports from official agencies and updates them as an event develops. "
                "Open the source page for the latest details and maps.")
        items.append(raw(ev.get("title", ""), text, link, "NASA EONET", to_dt(geo.get("date"))))
    return items


def fetch_apod(src):
    key = os.environ.get(src.get("env_key", ""), "") or "DEMO_KEY"
    d = get(src["url"], {"api_key": key}).json()
    day = d.get("date", "")
    url = f"https://apod.nasa.gov/apod/ap{day[2:4]}{day[5:7]}{day[8:10]}.html" if day else ""
    image = d.get("url", "") if d.get("media_type") == "image" else ""
    author = ("Image credit: " + d["copyright"].strip()) if d.get("copyright") else "NASA APOD"
    return [raw(d.get("title", ""), d.get("explanation", ""), url, author, to_dt(day), image)]


def fetch_europepmc(src):
    start, end = date_window()
    params = {"query": f"OPEN_ACCESS:y AND FIRST_PDATE:[{start} TO {end}]", "format": "json",
              "resultType": "core", "pageSize": PER_SOURCE * 2, "sort": "P_PDATE_D desc"}
    items = []
    for r in get(src["url"], params).json().get("resultList", {}).get("result", []):
        url = f"https://europepmc.org/article/{r.get('source', 'MED')}/{r.get('id', '')}"
        items.append(raw(r.get("title", ""), r.get("abstractText", ""), url,
                         r.get("authorString", ""), to_dt(r.get("firstPublicationDate"))))
    return items


def fetch_biorxiv(src):
    start, end = date_window()
    url = src["url"].replace("{from}", start).replace("{to}", end)
    rows = get(url).json().get("collection", [])[-PER_SOURCE * 2:]
    return [raw(r.get("title", ""), r.get("abstract", ""),
                f"https://www.biorxiv.org/content/{r.get('doi', '')}v{r.get('version', '1')}",
                names([a.strip() for a in (r.get("authors") or "").split(";")]),
                to_dt(r.get("date"))) for r in rows]


def fetch_guardian(src):
    key = os.environ.get(src.get("env_key", ""), "")
    if not key:
        raise RuntimeError("GUARDIAN_API_KEY not set (free key at open-platform.theguardian.com)")
    params = dict(src.get("params", {}), **{"api-key": key, "show-fields": "trailText,byline",
                                            "page-size": PER_SOURCE * 2})
    res = get(src["url"], params).json()["response"]["results"]
    return [raw(r.get("webTitle", ""), (r.get("fields") or {}).get("trailText", ""),
                r.get("webUrl", ""), (r.get("fields") or {}).get("byline", ""),
                to_dt(r.get("webPublicationDate"))) for r in res]


def fetch_sportsdb(src):
    events = get(src["url"], src.get("params")).json().get("events") or []
    items = []
    for e in events[-PER_SOURCE * 2:]:
        text = (f"{e.get('strHomeTeam')} played {e.get('strAwayTeam')} in {e.get('strLeague')}"
                f" on {e.get('dateEvent')}. The final score was {e.get('intHomeScore')} to {e.get('intAwayScore')}. "
                "Open the source page for the full match details.")
        items.append(raw(e.get("strEvent", ""), text,
                         f"https://www.thesportsdb.com/event/{e.get('idEvent', '')}",
                         "TheSportsDB", to_dt(e.get("dateEvent"))))
    return items


HANDLERS = {"feed": fetch_feed, "usgs": fetch_usgs, "eonet": fetch_eonet, "apod": fetch_apod,
            "europepmc": fetch_europepmc, "biorxiv": fetch_biorxiv,
            "guardian": fetch_guardian, "sportsdb": fetch_sportsdb}


# ---------- pipeline ----------
def process(src, rows, cutoff):
    """Clean, summarise, tag and trim one source's raw rows."""
    out = []
    for r in rows:
        if not r["title"] or not r["url"]:
            continue
        if r["published"] and r["published"] < cutoff:
            continue
        summary, n = summarise(r["text"], bool(src.get("full_summary")))
        out.append({
            "id": hashlib.sha1(r["url"].encode()).hexdigest()[:12],
            "title": r["title"], "summary": summary, "words": n,
            "category": src["category"], "source": src["name"],
            "author": r["author"], "published": r["published"].isoformat() if r["published"] else "",
            "url": r["url"], "image": r["image"], "licence": src["licence"],
        })
    out.sort(key=lambda i: i["published"], reverse=True)
    return out[:PER_SOURCE]


def dedupe(items):
    seen, result = set(), []
    for i in items:
        keys = {i["url"], re.sub(r"\W+", " ", i["title"].lower()).strip()}
        if keys & seen:
            continue
        seen |= keys
        result.append(i)
    return result


def load_previous():
    try:
        return json.loads(OUT.read_text(encoding="utf-8")).get("items", [])
    except (OSError, ValueError):
        return []


def run(config_path=CONFIG):
    sources = [s for s in json.loads(Path(config_path).read_text(encoding="utf-8"))["sources"]
               if s.get("enabled", True)]
    previous, items, log = load_previous(), [], []
    for src in sources:
        cutoff = datetime.now(timezone.utc) - timedelta(days=src.get("max_age_days", MAX_AGE_DAYS))
        try:
            got = process(src, HANDLERS[src["type"]](src), cutoff)
            log.append({"source": src["name"], "status": "ok", "items": len(got)})
            items += got
        except Exception as exc:  # one broken source must not stop the run
            kept = [p for p in previous if p["source"] == src["name"]]
            items += kept
            log.append({"source": src["name"], "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}"[:300], "kept_previous": len(kept)})
    items = dedupe(sorted(items, key=lambda i: i["published"], reverse=True))[:MAX_ITEMS]
    ok = sum(1 for entry in log if entry["status"] == "ok")
    LOG.parent.mkdir(exist_ok=True)
    LOG.write_text(json.dumps({"generated": datetime.now(timezone.utc).isoformat(),
                               "sources_ok": ok, "sources_total": len(log), "sources": log},
                              indent=1), encoding="utf-8")
    if not items:
        print("No items fetched; leaving the previous news.json untouched.", file=sys.stderr)
        return 1
    OUT.write_text(json.dumps({"generated": datetime.now(timezone.utc).isoformat(),
                               "count": len(items), "items": items}, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    print(f"Wrote {len(items)} items. Sources ok: {ok}/{len(log)}.")
    for entry in log:
        if entry["status"] != "ok":
            print(f"  FAILED {entry['source']}: {entry['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
