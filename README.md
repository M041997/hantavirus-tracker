# Hantavirus Tracker

Hourly-updated static site aggregating hantavirus outbreak alerts and news from
ProMED-mail, Google News, and CDC HPS surveillance.

Live: https://m041997.github.io/hantavirus-tracker/

## How it works

`build.py` fetches:

- **Google News RSS** for the query `hantavirus` — primary news firehose.
- **ProMED-mail** search page (`/?s=hantavirus`) — official outbreak alerts.
  Server-rendered HTML, scraped with BeautifulSoup.
- **CDC HPS surveillance** — slow-moving aggregate stats, snapshotted in
  `CDC_SNAPSHOT` in `build.py`. Refresh by re-reading
  https://www.cdc.gov/hantavirus/data-research/cases/index.html when CDC
  publishes a new annual update (typically once a year).

Items are deduped by normalized title, sorted by published date, and grouped
into a country-cluster heuristic (`detect_active_outbreaks`).

Output: `docs/index.html` and `docs/data.json`.

## Hosting

GitHub Pages serves the `docs/` directory of the `main` branch. The Action in
`.github/workflows/build.yml` runs hourly at `:05`, regenerates the site,
commits any changes, and deploys.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python build.py
open docs/index.html
```

## What this is not

- Not real-time. ProMED + news lag actual events by hours to days.
- Not a medical resource. See CDC for clinical guidance.
- Not WHO-sourced. WHO Disease Outbreak News page is JS-rendered and would
  require headless browsing — skipped to keep the build hermetic.
