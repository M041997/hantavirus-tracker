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

### Pointing a custom domain at it

1. Buy the domain (Cloudflare Registrar / Namecheap / Porkbun).
2. Add a `docs/CNAME` file containing the bare domain (e.g. `hantavirus.live`).
3. In the registrar's DNS, set four `A` records on the apex pointing to
   GitHub Pages:
   `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, `185.199.111.153`.
   For `www.<domain>`, add a `CNAME` to `m041997.github.io`.
4. In **Settings → Pages → Custom domain**, paste the domain and wait
   for the HTTPS certificate (a few minutes).
5. In **Settings → Secrets and variables → Actions → Variables**, set
   `SITE_URL=https://yourdomain.tld`. The next build will use it for
   canonical/OG/sitemap URLs.

### Enabling analytics

Sign up at [goatcounter.com](https://www.goatcounter.com/) (free, privacy-
friendly, no cookie banner needed). Pick a code (e.g. `hantavirus`). Add
a repo variable `GOATCOUNTER_CODE=hantavirus`. The next build will inject
the snippet.

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
