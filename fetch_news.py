
#!/usr/bin/env python3
"""Science In-Short pipeline: fetch -> parse -> clean -> tag -> publish data/news.json.

Keeps articles from the last 21 days (or source-specific max_age_days).

Run locally: python fetch_news.py
Runs on a schedule in GitHub Actions.
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


# ---------- paths and settings ----------

ROOT = Path(__file__).parent
CONFIG = ROOT / "sources.json"
DATA = ROOT / "data"
OUT = DATA / "news.json"
LOG = DATA / "run_log.json"

WORK = ROOT / "work"
TEXT = WORK / "source_text.json"

UA = {
    "User-Agent": "ScienceInShortBot/0.1 (+https://github.com/sandeeprana502)"
}

TIMEOUT = 25

MIN_WORDS = 100
MAX_WORDS = 150
SNIPPET_WORDS = 35

MAX_AGE_DAYS = 21
CAP_PER_CATEGORY = 500
TEXT_WORDS = 700


# ---------- helpers ----------

def get(url, params=None, tries=3):
    """GET with retries for network errors and server errors."""

    last = None

    for attempt in range(tries):
        try:
            response = requests.get(
                url,
                params=params,
                headers=UA,
                timeout=TIMEOUT
            )

            if response.status_code >= 500:
                raise requests.HTTPError(
                    f"{response.status_code} Server Error for url: {url}",
                    response=response
                )

            response.raise_for_status()
            return response

        except (requests.ConnectionError, requests.Timeout) as exc:
            last = exc

        except requests.HTTPError as exc:
            last = exc

            if (
                exc.response is not None
                and exc.response.status_code < 500
            ):
                raise

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
    """Create a short feed summary from complete sentences."""

    limit = MAX_WORDS if full else SNIPPET_WORDS
    target = MIN_WORDS if full else SNIPPET_WORDS

    out = []
    count = 0

    for sentence in re.split(r"(?<=[.!?])\s+", text):

        words = len(sentence.split())

        if not words:
            continue

        if count + words > limit:

            if not out:
                out.append(
                    " ".join(sentence.split()[:limit]) + "\u2026"
                )
                count = limit

            break

        out.append(sentence)
        count += words

        if count >= target:
            break

    return " ".join(out), count


def to_dt(value):
    """Parse feed time, ISO string or epoch milliseconds into UTC."""

    if value is None:
        return None

    try:

        if hasattr(value, "tm_year"):
            return datetime(*value[:6], tzinfo=timezone.utc)

        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(
                value / 1000,
                tz=timezone.utc
            )

        value = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)

        return (
            dt if dt.tzinfo
            else dt.replace(tzinfo=timezone.utc)
        )

    except (ValueError, TypeError, OverflowError):
        return None


def names(authors, limit=3):

    authors = [a for a in authors if a]

    if not authors:
        return ""

    result = ", ".join(authors[:limit])

    return result + " et al." if len(authors) > limit else result


def raw(title, text, url, author="", published=None, image=""):

    return {
        "title": clean_text(title),
        "text": clean_text(text),
        "url": url,
        "author": author,
        "published": published,
        "image": image
    }


def date_window(days=7):

    end = datetime.now(timezone.utc).date()

    return (
        (end - timedelta(days=days)).isoformat(),
        end.isoformat()
    )


def _num(value):

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------- image extraction ----------

def best_image(entry):
    """Find the largest image offered by an RSS feed entry."""

    found = []

    for key in ("media_content", "media_thumbnail"):

        for media in entry.get(key) or []:

            found.append((
                _num(media.get("width"))
                or _num(media.get("height")),
                media.get("url", "")
            ))

    for enclosure in entry.get("enclosures") or []:

        if str(enclosure.get("type", "")).startswith("image"):

            found.append((
                0,
                enclosure.get("href", "")
            ))

    found = [
        item for item in found
        if item[1]
    ]

    return (
        max(found, key=lambda item: item[0])[1]
        if found else ""
    )


class _ImageMetaParser(HTMLParser):
    """Extract Open Graph and Twitter image metadata."""

    IMAGE_KEYS = {
        "og:image",
        "og:image:url",
        "og:image:secure_url",
        "twitter:image",
        "twitter:image:src"
    }

    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):

        if tag.lower() != "meta":
            return

        data = {
            str(key).lower(): value
            for key, value in attrs
            if key and value
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
    """Extract an article webpage's Open Graph image, if available."""

    if not url:
        return ""

    try:

        response = get(url, tries=1)

        content_type = response.headers.get("Content-Type", "")

        if "html" not in content_type.lower():
            return ""

        parser = _ImageMetaParser()

        parser.feed(response.text[:500000])

        # Use the final URL after redirects as the relative URL base.
        page_url = response.url or url

        for image_url in parser.images:

            image_url = urljoin(
                page_url,
                html.unescape(image_url)
            )

            if image_url.startswith(("https://", "http://")):
                return image_url

    except Exception:
        return ""

    return ""


# ---------- article text extraction ----------

class _Paras(HTMLParser):
    """Fallback article reader collecting paragraph text."""

    SKIP = {
        "script",
        "style",
        "nav",
        "footer",
        "header",
        "aside",
        "form",
        "noscript"
    }

    def __init__(self):

        super().__init__()

        self.paras = []
        self._buf = []
        self._in_p = False
        self._skip = 0

    def _flush(self):

        if self._in_p:

            self.paras.append(
                " ".join("".join(self._buf).split())
            )

        self._in_p = False
        self._buf = []

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
    """Extract main article content."""

    try:

        import trafilatura

        text = trafilatura.extract(
            page_html,
            include_comments=False,
            include_tables=False
        )

        if text and len(text.split()) >= 80:
            return clean_text(text)

    except Exception:
        pass

    parser = _Paras()

    parser.feed(page_html)
    parser._flush()

    return clean_text(
        " ".join(
            paragraph
            for paragraph in parser.paras
            if len(paragraph.split()) >= 8
        )
    )


def fetch_article_text(url):

    try:
        response = get(url, tries=1)
        return extract_article_text(response.text)

    except Exception:
        return ""


# ---------- fetchers ----------

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

    for entry in feed.entries:

        body = (
            entry.get("summary")
            or (entry.get("content") or [{}])[0].get("value", "")
        )

        authors = [
            author.get("name", "")
            for author in entry.get("authors", [])
        ] or [entry.get("author", "")]

        items.append(
            raw(
                entry.get("title", ""),
                body,
                entry.get("link", ""),
                names(authors),
                to_dt(
                    entry.get("published_parsed")
                    or entry.get("updated_parsed")
                ),
                best_image(entry)
            )
        )

    return items


def fetch_usgs(src):

    data = get(src["url"]).json()

    items = []

    for feature in data.get("features", []):

        properties = feature.get("properties", {})
        geometry = feature.get("geometry", {})

        when = to_dt(properties.get("time"))

        depth = (
            geometry.get("coordinates") or [0, 0, None]
        )[2]

        text = (
            f"A magnitude {properties.get('mag')} earthquake "
            f"was recorded {properties.get('place')}. "
        )

        if when:

            text += (
                f"The event happened on "
                f"{when:%d %B %Y at %H:%M} UTC. "
            )

        if depth is not None:

            text += (
                f"It occurred at a depth of about "
                f"{depth:.0f} km. "
            )

        if properties.get("tsunami"):
            text += "The agency has flagged a possible tsunami threat. "

        if properties.get("felt"):

            text += (
                f"About {properties['felt']} people "
                f"reported feeling it. "
            )

        text += (
            "Magnitude and location can change as the agency reviews "
            "the data. The agency event page has the map, updates "
            "and safety information."
        )

        items.append(
            raw(
                properties.get("title", "Earthquake"),
                text,
                properties.get("url", ""),
                "USGS",
                when
            )
        )

    return items


def fetch_eonet(src):

    data = get(src["url"]).json()

    items = []

    for event in data.get("events", []):

        category = (
            event.get("categories") or [{}]
        )[0].get("title", "natural event")

        geometry = (
            event.get("geometry") or [{}]
        )[-1]

        link = (
            (event.get("sources") or [{}])[0].get("url")
            or event.get("link", "")
        )

        text = (
            f"NASA's Earth Observatory Natural Event Tracker "
            f"lists an open event in the category {category.lower()}. "
            f"{event.get('description') or ''} "
            "The tracker collects reports from official agencies "
            "and updates them as an event develops. "
            "Open the source page for the latest details and maps."
        )

        items.append(
            raw(
                event.get("title", ""),
                text,
                link,
                "NASA EONET",
                to_dt(geometry.get("date"))
            )
        )

    return items


def fetch_apod(src):

    key = (
        os.environ.get(src.get("env_key", ""), "")
        or "DEMO_KEY"
    )

    data = get(
        src["url"],
        {"api_key": key}
    ).json()

    day = data.get("date", "")

    url = (
        f"https://apod.nasa.gov/apod/ap"
        f"{day[2:4]}{day[5:7]}{day[8:10]}.html"
        if day else ""
    )

    image = (
        data.get("url", "")
        if data.get("media_type") == "image"
        else ""
    )

    author = (
        "Image credit: " + data["copyright"].strip()
        if data.get("copyright")
        else "NASA APOD"
    )

    return [
        raw(
            data.get("title", ""),
            data.get("explanation", ""),
            url,
            author,
            to_dt(day),
            image
        )
    ]


def fetch_europepmc(src):

    start, end = date_window()

    params = {
        "query": (
            f"OPEN_ACCESS:y AND FIRST_PDATE:[{start} TO {end}]"
        ),
        "format": "json",
        "resultType": "core",
        "pageSize": 100,
        "sort": "P_PDATE_D desc"
    }

    items = []

    results = (
        get(src["url"], params)
        .json()
        .get("resultList", {})
        .get("result", [])
    )

    for result in results:

        url = (
            f"https://europepmc.org/article/"
            f"{result.get('source', 'MED')}/"
            f"{result.get('id', '')}"
        )

        items.append(
            raw(
                result.get("title", ""),
                result.get("abstractText", ""),
                url,
                result.get("authorString", ""),
                to_dt(result.get("firstPublicationDate"))
            )
        )

    return items


def fetch_biorxiv(src):

    start, end = date_window(3)

    url = (
        src["url"]
        .replace("{from}", start)
        .replace("{to}", end)
    )

    rows = get(url).json().get("collection", [])

    return [
        raw(
            row.get("title", ""),
            row.get("abstract", ""),
            (
                f"https://www.biorxiv.org/content/"
                f"{row.get('doi', '')}v{row.get('version', '1')}"
            ),
            names([
                author.strip()
                for author in (row.get("authors") or "").split(";")
            ]),
            to_dt(row.get("date"))
        )
        for row in rows
    ]


def fetch_guardian(src):

    key = os.environ.get(src.get("env_key", ""), "")

    if not key:

        raise RuntimeError(
            "GUARDIAN_API_KEY not set "
            "(free key at open-platform.theguardian.com)"
        )

    params = dict(
        src.get("params", {}),
        **{
            "api-key": key,
            "show-fields": "trailText,byline",
            "page-size": 50
        }
    )

    results = (
        get(src["url"], params)
        .json()["response"]["results"]
    )

    return [
        raw(
            result.get("webTitle", ""),
            (result.get("fields") or {}).get("trailText", ""),
            result.get("webUrl", ""),
            (result.get("fields") or {}).get("byline", ""),
            to_dt(result.get("webPublicationDate"))
        )
        for result in results
    ]


def fetch_sportsdb(src):

    events = (
        get(src["url"], src.get("params"))
        .json()
        .get("events") or []
    )

    items = []

    for event in events:

        text = (
            f"{event.get('strHomeTeam')} played "
            f"{event.get('strAwayTeam')} in "
            f"{event.get('strLeague')} on "
            f"{event.get('dateEvent')}. "
            f"The final score was "
            f"{event.get('intHomeScore')} to "
            f"{event.get('intAwayScore')}. "
            "Open the source page for the full match details."
        )

        items.append(
            raw(
                event.get("strEvent", ""),
                text,
                (
                    "https://www.thesportsdb.com/event/"
                    f"{event.get('idEvent', '')}"
                ),
                "TheSportsDB",
                to_dt(event.get("dateEvent"))
            )
        )

    return items


# ---------- handler mapping ----------

HANDLERS = {
    "feed": fetch_feed,
    "usgs": fetch_usgs,
    "eonet": fetch_eonet,
    "apod": fetch_apod,
    "europepmc": fetch_europepmc,
    "biorxiv": fetch_biorxiv,
    "guardian": fetch_guardian,
    "sportsdb": fetch_sportsdb
}


# ---------- pipeline ----------

def process(src, rows, cutoff):
    """Clean, summarise, tag and trim one source's raw rows."""

    out = []

    for row in rows:

        if not row["title"] or not row["url"]:
            continue

        if row["published"] and row["published"] < cutoff:
            continue

        summary, count = summarise(
            row["text"],
            bool(src.get("full_summary"))
        )

        item = {
            "id": hashlib.sha1(
                row["url"].encode()
            ).hexdigest()[:12],

            "title": row["title"],
            "summary": summary,
            "words": count,

            "category": src["category"],
            "source": src["name"],

            "author": row["author"],

            "published": (
                row["published"].isoformat()
                if row["published"]
                else ""
            ),

            "url": row["url"],

            "image": (
                row["image"]
                if src["licence"] == "open"
                else ""
            ),

            "licence": src["licence"],

            "_text": " ".join(
                row["text"].split()[:TEXT_WORDS]
            )
        }

        if not row["published"]:

            item["added"] = datetime.now(
                timezone.utc
            ).isoformat()

        out.append(item)

    return out


def dedupe(items):

    seen = set()
    result = []

    for item in items:

        keys = {
            item["url"],
            re.sub(
                r"\W+",
                " ",
                item["title"].lower()
            ).strip()
        }

        if keys & seen:
            continue

        seen |= keys
        result.append(item)

    return result


def load_previous():

    try:

        return json.loads(
            OUT.read_text(encoding="utf-8")
        ).get("items", [])

    except (OSError, ValueError):
        return []


def age_key(item):

    return (
        item.get("published")
        or item.get("added", "")
    )


def write_if_changed(path, payload):
    """Save JSON only when data changes."""

    def core(data):

        return {
            key: value
            for key, value in data.items()
            if key != "generated"
        }

    try:

        existing = json.loads(
            path.read_text(encoding="utf-8")
        )

        if core(existing) == core(payload):
            return False

    except (OSError, ValueError):
        pass

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    tmp = path.with_suffix(".tmp")

    tmp.write_text(
        json.dumps(
            payload,
            indent=1,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    tmp.replace(path)

    return True


def _int_env(name, default):

    try:
        return int(os.environ.get(name) or default)

    except (TypeError, ValueError):
        return default


# ---------- main run ----------

def run(config_path=CONFIG):

    # Load source configuration.
    all_sources = json.loads(
        Path(config_path).read_text(encoding="utf-8")
    )["sources"]

    window = {
        source["name"]: source.get(
            "max_age_days",
            MAX_AGE_DAYS
        )
        for source in all_sources
    }

    now = datetime.now(timezone.utc)

    previous = load_previous()

    prev_by_id = {
        item["id"]: item
        for item in previous
        if item.get("id")
    }

    fresh = []
    log = []

    # Fetch each enabled source independently.
    for src in (
        source
        for source in all_sources
        if source.get("enabled", True)
    ):

        cutoff = now - timedelta(
            days=window[src["name"]]
        )

        try:

            rows = HANDLERS[src["type"]](src)

            got = process(
                src,
                rows,
                cutoff
            )

            log.append({
                "source": src["name"],
                "status": "ok",
                "items": len(got)
            })

            fresh += got

        except Exception as exc:

            kept = sum(
                1
                for item in previous
                if item.get("source") == src["name"]
            )

            log.append({
                "source": src["name"],
                "status": "failed",
                "error": (
                    f"{type(exc).__name__}: {exc}"
                )[:300],
                "kept_previous": kept
            })

    # Extract longer source text for the AI summarization pipeline.
    texts = {}

    for item in fresh:

        texts[item["id"]] = item.pop("_text", "")

        old = prev_by_id.get(item["id"])

        if old and "added" in old and not item["published"]:

            item["added"] = old["added"]

        if old and old.get("image") and not item["image"]:

            item["image"] = old["image"]

    # Keep previous articles that are still within their source's age limit.
    def still_fresh(item):

        when = to_dt(age_key(item))

        if when is None:
            return False

        cutoff = now - timedelta(
            days=window.get(
                item.get("source"),
                MAX_AGE_DAYS
            )
        )

        return when >= cutoff

    merged = dedupe(
        fresh + [
            item
            for item in previous
            if still_fresh(item)
        ]
    )

    merged.sort(
        key=age_key,
        reverse=True
    )

    # Apply category safety limit.
    counts = {}
    items = []

    for item in merged:

        count = counts.get(
            item["category"],
            0
        )

        if count < CAP_PER_CATEGORY:

            items.append(item)

            counts[item["category"]] = count + 1

    # ---------- image fallback ----------

    by_name = {
        source["name"]: source
        for source in all_sources
    }

    # Priority:
    # 1. Existing RSS/API image.
    # 2. Open Graph image, if enabled for the source.
    # 3. Empty image (Flutter displays its fallback).
    #
    # Images are retained only for sources marked as open.

    og_budget = _int_env(
        "MAX_OG_FETCH",
        40
    )

    for item in items:

        if item.get("licence") != "open":

            item["image"] = ""
            continue

        source = by_name.get(
            item["source"],
            {}
        )

        if (
            not item.get("image")
            and source.get("og_image", False)
            and og_budget > 0
        ):

            og_budget -= 1

            image_url = og_image(
                item["url"]
            )

            if image_url:
                item["image"] = image_url

    # ---------- optional article text extraction ----------

    # Enable with FETCH_ARTICLE_TEXT=1.
    # Extract only from open sources.
    # Do not repeatedly fetch articles already summarised by AI.

    if os.environ.get("FETCH_ARTICLE_TEXT") == "1":

        done = set()

        summary_file = OUT.with_name(
            "summarized_news.json"
        )

        try:

            summarised = json.loads(
                summary_file.read_text(
                    encoding="utf-8"
                )
            )

            done = {
                entry["id"]
                for entry in summarised.get("items", [])
                if (
                    entry.get("id")
                    and entry.get("summary_by")
                    in {"llm", "gemini", "ollama"}
                )
            }

        except (OSError, ValueError):
            pass

        budget = _int_env(
            "MAX_ARTICLE_FETCH",
            60
        )

        for item in items:

            if budget <= 0:
                break

            source = by_name.get(
                item["source"],
                {}
            )

            wanted = source.get(
                "article_text",
                False
            )

            if (
                wanted
                and item.get("licence") == "open"
                and item["id"] not in done
                and len(
                    texts.get(item["id"], "").split()
                ) < 150
            ):

                budget -= 1

                article_text = fetch_article_text(
                    item["url"]
                )

                if article_text:

                    texts[item["id"]] = " ".join(
                        article_text.split()[:TEXT_WORDS]
                    )

                time.sleep(1)

    # ---------- save output ----------

    ok = sum(
        1
        for entry in log
        if entry["status"] == "ok"
    )

    stamp = now.isoformat()

    write_if_changed(
        LOG,
        {
            "generated": stamp,
            "sources_ok": ok,
            "sources_total": len(log),
            "sources": log
        }
    )

    if not items:

        print(
            "No items fetched and none saved earlier; nothing written.",
            file=sys.stderr
        )

        return 1

    write_if_changed(
        OUT,
        {
            "generated": stamp,
            "count": len(items),
            "items": items
        }
    )

    WORK.mkdir(
        parents=True,
        exist_ok=True
    )

    TEXT.write_text(
        json.dumps(
            {
                item["id"]: texts[item["id"]]
                for item in items
                if item["id"] in texts
            },
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    print(
        f"news.json: {len(items)} items "
        f"({len(fresh)} fetched this run). "
        f"Sources ok: {ok}/{len(log)}."
    )

    for entry in log:

        if entry["status"] != "ok":

            print(
                f"  FAILED {entry['source']}: "
                f"{entry['error']}"
            )

    return 0


# ---------- entry point ----------

if __name__ == "__main__":
    sys.exit(run())
