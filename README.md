# Science In-Short: backend (AppProject)

GitHub Actions runs this every 3 hours and on demand. It collects science news, adds AI summaries, and saves JSON files that the Flutter app downloads. No server, no hosting bill.

```
sources.json --> fetch_news.py --> data/news.json ------> summarize_news.py --> data/summarized_news.json --> app
                                                              |        ^
                                                              v        |
                                                      Gemini API   data/ai_summaries.json (cache)
```

## Files

| File | Purpose |
|---|---|
| `sources.json` | Source list and settings. Edit this to add, remove or fix a source or category |
| `fetch_news.py` | Fetch, clean, tag, remove duplicates, keep the last 21 days. Works as is |
| `summarize_news.py` | Read article text, ask the AI, validate, cache, build the app feed |
| `requirements.txt` | Python libraries (`requests`, `feedparser`, `trafilatura`) |
| `.github/workflows/*.yml` | Schedule (`0 */3 * * *`) and the manual **Run workflow** button |
| `data/news.json` | Original news. Keep it as the untouched source feed |
| `data/ai_summaries.json` | Cache of accepted AI results. Do not delete it |
| `data/summarized_news.json` | **What the app reads first** |
| `data/run_log.json` | Each source `ok` or `failed`, with the error |
| `merge_sources.py`, `sources_additions.json` | Optional: add a prepared batch of sources safely |

## Sources and categories

Each entry in `sources.json` has `name`, `category`, `type`, `url`, `licence` (`open` or `check`) and `full_summary`. Optional: `enabled`, `max_age_days`, `article_text`, `og_image`, `note`.

- **Categories are free text.** The fetcher copies the `category` to each article. The app builds its chips from the categories in the downloaded news, so a new category appears after the next refresh. Keep the spelling consistent, because `Health` and `health` count as two topics.
- **Types the fetcher knows:** `feed`, `usgs`, `eonet`, `apod`, `europepmc`, `biorxiv`, `guardian`, `sportsdb`. A new kind of API needs a new function in `fetch_news.py`.
- **`open` sources** get a 100 to 150 word summary when the article text is readable. **`check` sources** keep a short snippet and the link until their terms are confirmed.
- A broken source never stops the others. Its address is the first thing to check in `run_log.json`.

### Add or fix a source
1. Edit `sources.json`.
2. **Actions → Fetch News → Run workflow**.
3. Open `data/run_log.json`. Fix any `failed` source (usually the `url`) and run again.

## AI summaries

| Setting | Name |
|---|---|
| GitHub secret | `GEMINI_API_KEY` |
| GitHub variable | `GEMINI_MODEL` (overrides the script's default: keep them consistent) |

Per run: up to 10 new articles, in batches of 5. Accepted results are saved in `data/ai_summaries.json`, so an article is only summarised once. If the AI fails or the answer is rejected, the article keeps its original text (`summary_by: extract`). A publisher block (HTTP 403 or 429) is not a Gemini failure: the script falls back to the feed text.

To check what you got, look at `summary_by` on each article, not at `provider` at the top of the file.

### Using a local model (Ollama) instead
Free, and it does not use the Gemini quota. Run `summarize_news.py` on your laptop with an Ollama model. Run `git pull` first, and do not run it at the same time as the Actions job: both write `ai_summaries.json`, and the uploads can clash.

## Article fields

`id, title, summary, category, source, url, published, image`, plus after AI processing (some may be missing):
`original_title`, `original_summary`, `summary_by` (`gemini` or `extract`), `content_source` (`article` or `rss`), `flow` (3 to 5 short steps).

## Rules to keep

1. Keep API keys in GitHub secrets, never in the code.
2. Do not delete `ai_summaries.json` without accepting the cost of re-summarising everything.
3. Show a summary and a link, never a full article. Show pictures only from open sources.
4. After a big change, read the Actions log and `run_log.json`.

## Feed addresses used by the app (in this order)

1. `https://raw.githubusercontent.com/sandeeprana502/AppProject/main/data/summarized_news.json`
2. `https://raw.githubusercontent.com/sandeeprana502/AppProject/main/data/news.json`

The app then falls back to the copy saved on the phone. GitHub caches these files for a few minutes.

## Next checks

- Run the fetch workflow and open `data/run_log.json`.
- Confirm new categories appear in `summarized_news.json` or `news.json`.
- `flutter analyze`, test on a phone, then `flutter build apk --release`.
