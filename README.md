# Science In-Short feed (backend pipeline)

A scheduled job fetches open science sources, cleans the text, trims it to a short summary and
saves `data/news.json`. The Flutter app downloads that one file. There is no server to pay for.

```
sources.json -> fetch_news.py -> data/news.json -> app (Flutter) -> local cache of 10 items
              (GitHub Actions, every 3 hours)
```

## Set up (about 10 minutes)

1. On GitHub: **New repository**, name it `science-in-short-feed`, set it to **Public**.
2. Upload all files in this folder (keep the `.github/workflows` folder path exactly).
3. Optional keys, under **Settings > Secrets and variables > Actions > New repository secret**:
   - `NASA_API_KEY`: free at api.nasa.gov. Without it the script uses NASA's shared DEMO_KEY, which is rate limited.
   - `GUARDIAN_API_KEY`: free developer key at open-platform.theguardian.com. Without it that one source is skipped.
4. **Actions** tab > "Fetch news" > **Run workflow**. Wait about a minute.
5. Open `data/run_log.json` in the repo. It lists every source as `ok` or `failed` with the error.
6. Your feed URL for the app is:
   `https://raw.githubusercontent.com/sandeeprana502/science-in-short-feed/main/data/news.json`

## Summary length and licences

- Sources marked `"licence": "open"` and `"full_summary": true` get 100 to 150 words, taken as whole
  sentences from the start of the source text. If the source text is shorter, the summary is shorter. Nothing is invented or padded.
- Sources marked `"licence": "check"` keep only a snippet of about 35 words plus the link, until you read the publisher's terms and
  set `"full_summary": true` for that source in `sources.json`.
- The app shows the summary, the source name and a link to the exact article. It never shows the full article.

## Things to check

- The feed addresses in `sources.json` were written from memory and have not been run against the live internet. Expect a few to fail
  on the first run. `data/run_log.json` names them. Fix the `url` and run again.
- A failed source keeps its previous items, so the app never loses cards because one source is down.
- Run the tests on your computer with `python -m unittest discover tests` (no internet needed).

## Output format (`data/news.json`)

`generated`, `count`, and `items[]` with: `id, title, summary, words, category, source, author, published, url, image, licence`.
