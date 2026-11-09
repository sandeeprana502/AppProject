#!/usr/bin/env python3
"""Science In-Short pipeline: fetch -> parse -> clean -> tag -> publish data/news.json (everything from the last 21 days).

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
from html.parser import HTMLParser
from urllib.parse import urljoin
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).parent
CONFIG = ROOT / "sources.json"
DATA = ROOT / "data"
OUT = DATA / "news.json"            # raw feed: every item of the last 21 days
LOG = DATA / "run_log.json"
WORK = ROOT / "work"               # job-local scratch, never committed
TEXT = WORK / "source_text.json"   # longer source text, used only by summarize_news.py

UA = {"User-Agent": "ScienceInShortBot/0.1 (+https://github.com/sandeeprana502)"}
TIMEOUT = 25
MIN_WORDS, MAX_WORDS, SNIPPET_WORDS = 100, 150, 35
MAX_AGE_DAYS = 21          # keep everything published in the last 21 days
CAP_PER_CATEGORY = 500     # safety limit so the file can never grow without bound
TEXT_WORDS = 700           # how much source text is handed to the AI step


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


def _num(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def best_image(entry):
    """Largest picture a feed entry offers (feeds often list a tiny thumbnail first)."""
    found = []
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key) or []:
            found.append((_num(m.get("width")) or _num(m.get("height")), m.get("url", "")))
    for enc in entry.get("enclosures") or []:
        if str(enc.get("type", "")).startswith("image"):
            found.append((0, enc.get("href", "")))
    found = [f for f in found if f[1]]
    return max(found, key=lambda f: f[0])[1] if found else ""


class _ImageMetaParser(HTMLParser):
    """Extract Open Graph and Twitter image metadata."""

    IMAGE_KEYS = {
        "og:image",
        "og:image:url",
        "og:image:secure_url",
        "twitter:image",
        "twitter:image:src",
    }

    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return

        data = {
            str(k).lower(): v
            for k, v in attrs
            if k and v
        }

        key = (
            data.get("property")
            or data.get("name")
            or ""
        ).lower()

        content = data.get("content", "").strip()

        if key in self.IMAGE_KEYS and content:
            self.images.append(content)


def og_image(url):
    """Extract the webpage's sharing image, if available."""

    if not url:
        return ""

    try:
        response = get(url, tries=1)

        content_type = response.headers.get("Content-Type", "")
        if "html" not in content_type.lower():
            return ""

        parser = _ImageMetaParser()
        parser.feed(response.text[:500000])

        for image_url in parser.images:
            image_url = urljoin(url, html.unescape(image_url))

            if image_url.startswith(("https://", "http://")):
                return image_url

    except Exception:
        return ""

    return ""


class _Paras(HTMLParser):
    """Fallback article reader: collects the text of <p> tags outside menus, footers and scripts."""
    SKIP = {"script", "style", "nav", "footer", "header", "aside", "form", "noscript"}

    def __init__(self):
        super().__init__()
        self.paras, self._buf, self._in_p, self._skip = [], [], False, 0

    def _flush(self):
        if self._in_p:
            self.paras.append(" ".join("".join(self._buf).split()))
        self._in_p, self._buf = False, []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "p" and not self._skip:
            self._flush()
            self._in_p = True

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "p":
            self._flush()

    def handle_data(self, data):
        if self._in_p and not self._skip:
            self._buf.append(data)


def extract_article_text(page_html):
    """Main text of an article page. Uses trafilatura when installed, else a simple reader."""
    try:
        import trafilatura
        text = trafilatura.extract(page_html, include_comments=False, include_tables=False)
        if text and len(text.split()) >= 80:
            return clean_text(text)
    except Exception:
        pass
    p = _Paras()
    p.feed(page_html)
    p._flush()
    return clean_text(" ".join(x for x in p.paras if len(x.split()) >= 8))


def fetch_article_text(url):
    try:
        return extract_article_text(get(url, tries=1).text)
    except Exception:
        return ""


# ---------- fetchers: each returns a list of raw() dicts ----------
def fetch_feed(src):
    response = get(
        src["url"],
        params=src.get("params")
    )

    feed = feedparser.parse(response.content)

    if feed.bozo and not feed.entries:
        raise RuntimeError(
            f"Invalid feed: {feed.bozo_exception}"
        )

    items = []

    for e in feed.entries:
        body = e.get("summary") or (
            e.get("content") or [{}]
        )[0].get("value", "")

        authors = [
            a.get("name", "")
            for a in e.get("authors", [])
        ] or [e.get("author", "")]

        items.append(
            raw(
                e.get("title", ""),
                body,
                e.get("link", ""),
                names(authors),
                to_dt(
                    e.get("published_parsed")
                    or e.get("updated_parsed")
                ),
                best_image(e)
            )
        )

    return items


def fetch_usgs(src):
    data = get(src["url"]).json()
    items = []
    for f in data.get("features", []):
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
    for ev in data.get("events", []):
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
              "resultType": "core", "pageSize": 100, "sort": "P_PDATE_D desc"}
    items = []
    for r in get(src["url"], params).json().get("resultList", {}).get("result", []):
        url = f"https://europepmc.org/article/{r.get('source', 'MED')}/{r.get('id', '')}"
        items.append(raw(r.get("title", ""), r.get("abstractText", ""), url,
                         r.get("authorString", ""), to_dt(r.get("firstPublicationDate"))))
    return items


def fetch_biorxiv(src):
    start, end = date_window(3)  # runs every 3 hours, so a short window is enough
    url = src["url"].replace("{from}", start).replace("{to}", end)
    rows = get(url).json().get("collection", [])
    return [raw(r.get("title", ""), r.get("abstract", ""),
                f"https://www.biorxiv.org/content/{r.get('doi', '')}v{r.get('version', '1')}",
                names([a.strip() for a in (r.get("authors") or "").split(";")]),
                to_dt(r.get("date"))) for r in rows]


def fetch_guardian(src):
    key = os.environ.get(src.get("env_key", ""), "")
    if not key:
        raise RuntimeError("GUARDIAN_API_KEY not set (free key at open-platform.theguardian.com)")
    params = dict(src.get("params", {}), **{"api-key": key, "show-fields": "trailText,byline",
                                            "page-size": 50})
    res = get(src["url"], params).json()["response"]["results"]
    return [raw(r.get("webTitle", ""), (r.get("fields") or {}).get("trailText", ""),
                r.get("webUrl", ""), (r.get("fields") or {}).get("byline", ""),
                to_dt(r.get("webPublicationDate"))) for r in res]


def fetch_sportsdb(src):
    events = get(src["url"], src.get("params")).json().get("events") or []
    items = []
    for e in events:
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
        item = {
            "id": hashlib.sha1(r["url"].encode()).hexdigest()[:12],
            "title": r["title"], "summary": summary, "words": n,
            "category": src["category"], "source": src["name"],
            "author": r["author"], "published": r["published"].isoformat() if r["published"] else "",
            "url": r["url"], "image": r["image"] if src["licence"] == "open" else "", "licence": src["licence"],
            "_text": " ".join(r["text"].split()[:TEXT_WORDS]),
        }
        if not r["published"]:
            item["added"] = datetime.now(timezone.utc).isoformat()  # lets undated items age out
        out.append(item)
    return out


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


def age_key(item):
    """Date used for sorting and ageing: publish date, or the day we first saw it."""
    return item["published"] or item.get("added", "")


def write_if_changed(path, payload):
    """Save JSON only when something other than the 'generated' time changed.
    Keeps the repo free of pointless commits every 3 hours."""
    def core(d):
        return {k: v for k, v in d.items() if k != "generated"}
    try:
        if core(json.loads(path.read_text(encoding="utf-8"))) == core(payload):
            return False
    except (OSError, ValueError):
        pass
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return True


def _int_env(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def run(config_path=CONFIG):
    all_sources = json.loads(Path(config_path).read_text(encoding="utf-8"))["sources"]
    window = {s["name"]: s.get("max_age_days", MAX_AGE_DAYS) for s in all_sources}
    now = datetime.now(timezone.utc)
    previous = load_previous()
    prev_by_id = {p["id"]: p for p in previous}
    fresh, log = [], []
    for src in (s for s in all_sources if s.get("enabled", True)):
        cutoff = now - timedelta(days=window[src["name"]])
        try:
            got = process(src, HANDLERS[src["type"]](src), cutoff)
            log.append({"source": src["name"], "status": "ok", "items": len(got)})
            fresh += got
        except Exception as exc:  # one broken source must never break the run
            kept = sum(1 for p in previous if p["source"] == src["name"])
            log.append({"source": src["name"], "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}"[:300], "kept_previous": kept})

    texts = {}
    for item in fresh:
        texts[item["id"]] = item.pop("_text", "")
        old = prev_by_id.get(item["id"])
        if old and "added" in old and not item["published"]:
            item["added"] = old["added"]  # keep the first-seen day so it can age out
        if old and old.get("image") and not item["image"]:
            item["image"] = old["image"]  # keep a picture found earlier (for example from the article page)

    def still_fresh(p):
        when = to_dt(age_key(p))
        return when is None or when >= now - timedelta(days=window.get(p["source"], MAX_AGE_DAYS))

    merged = dedupe(fresh + [p for p in previous if still_fresh(p)])  # fresh copies win
    merged.sort(key=age_key, reverse=True)
    counts, items = {}, []
    for it in merged:
        n = counts.get(it["category"], 0)
        if n < CAP_PER_CATEGORY:
            items.append(it)
            counts[it["category"]] = n + 1

    by_name = {s["name"]: s for s in all_sources}

   # Pictures:
# 1. Keep RSS/API images.
# 2. If missing, try the article webpage's image when enabled.
# 3. Only retain images for sources marked as open.
og_budget = _int_env("MAX_OG_FETCH", 40)

for it in items:

    # Do not retain images from sources without open image permission.
    if it.get("licence") != "open":
        it["image"] = ""
        continue

    source = by_name.get(it["source"], {})

    # Try the article webpage if RSS/API did not provide an image.
    # This also retries older articles that previously had no image.
    if (
        not it.get("image")
        and source.get("og_image", False)
        and og_budget > 0
    ):
        og_budget -= 1

        image_url = og_image(it["url"])

        if image_url:
            it["image"] = image_url

    # Article text for the AI step. Opt-in (set FETCH_ARTICLE_TEXT=1 on your laptop), open sources
    # only, and only for items the AI has not summarised yet. It is kept in work/, never committed.
    if os.environ.get("FETCH_ARTICLE_TEXT") == "1":
        done = set()
        try:
            summarised = json.loads(OUT.with_name("summarized_news.json").read_text(encoding="utf-8"))
            done = {i["id"] for i in summarised.get("items", []) if i.get("summary_by") == "llm"}
        except (OSError, ValueError):
            pass
        budget = _int_env("MAX_ARTICLE_FETCH", 60)
        for it in items:
            if budget <= 0:
                break
            wanted = by_name.get(it["source"], {}).get("article_text")
            if wanted and it["licence"] == "open" and it["id"] not in done \
                    and len(texts.get(it["id"], "").split()) < 150:
                budget -= 1
                got = fetch_article_text(it["url"])
                if got:
                    texts[it["id"]] = " ".join(got.split()[:TEXT_WORDS])
                time.sleep(1)  # be polite to the site

    ok = sum(1 for entry in log if entry["status"] == "ok")
    stamp = now.isoformat()
    write_if_changed(LOG, {"generated": stamp, "sources_ok": ok, "sources_total": len(log), "sources": log})
    if not items:
        print("No items fetched and none saved earlier; nothing written.", file=sys.stderr)
        return 1
    write_if_changed(OUT, {"generated": stamp, "count": len(items), "items": items})
    WORK.mkdir(exist_ok=True)
    TEXT.write_text(json.dumps({i["id"]: texts[i["id"]] for i in items if i["id"] in texts}), encoding="utf-8")

    print(f"news.json: {len(items)} items ({len(fresh)} fetched this run). Sources ok: {ok}/{len(log)}.")
    for entry in log:
        if entry["status"] != "ok":
            print(f"  FAILED {entry['source']}: {entry['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
