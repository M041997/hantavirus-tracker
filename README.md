# Outbreak Monitor

Hourly-updated outbreak tracker that publishes an **alert feed** — RSS, email,
and Bluesky — plus a browsable map. Aggregates ProMED-mail, ECDC, Google News,
WHO Disease Outbreak News, and CDC surveillance.

Live: https://hantavirusonline.org/ · Feed: https://hantavirusonline.org/feed.xml

The map is the shop window; the feed is the product. Search traffic for any
single disease is spiky and owned by CDC/WHO/Wikipedia, so the site is built to
be **subscribed to once** rather than found repeatedly.

## How it works

`build.py` fetches:

- **Google News RSS** for the query `hantavirus` — primary news firehose.
- **ProMED-mail** search page (`/?s=hantavirus`) — official outbreak alerts.
  Server-rendered HTML, scraped with BeautifulSoup.
- **ECDC hantavirus RSS + outbreak page** — EU agency updates, including
  structured MV Hondius counters when available.
- **WHO Disease Outbreak News DON599** — fallback source for MV Hondius
  counters when ECDC's structured page is unavailable.
- **CDC HPS surveillance** — slow-moving aggregate stats, snapshotted in
  `CDC_SNAPSHOT` in `build.py`. Refresh by re-reading
  https://www.cdc.gov/hantavirus/data-research/cases/index.html when CDC
  publishes a new annual update (typically once a year).

Items are deduped by normalized title, sorted by published date, and grouped
into a country-cluster heuristic (`detect_active_outbreaks`).

Output: `docs/index.html`, `docs/data.json`, `docs/feed.xml`, `docs/alerts.json`.

## The alert layer

Every build flattens the current picture into a snapshot (`snapshot_state`),
diffs it against the previously deployed snapshot, and appends any material
change to an append-only ledger in `docs/alerts.json`. That ledger backs both
`feed.xml` and the Bluesky bot, so an event fires exactly once regardless of how
many surfaces render it.

Alerts fire on:

- **`new_outbreak`** — an outbreak enters the tracker.
- **`new_cluster`** — reports start clustering in a country that had none.
- **`count_change`** — cases rise by at least `ALERT_MIN_CASE_DELTA` *and*
  `ALERT_MIN_CASE_PCT`, or deaths rise at all.

Deliberately *not* alerted: downward revisions (agencies reconcile counts
routinely), sub-threshold drift, and raw news items. Builds run hourly; a feed
that fires hourly gets unsubscribed from.

### Persistence without a database

CI never commits generated files back, so **the deployed site is the database**.
`load_ledger` pulls `alerts.json` off the live origin, falling back to the local
file for offline dev. Same trick as `load_existing_track`. A ledger with no
`state` key means "never run" — `update_ledger` seeds it silently rather than
flushing every already-tracked outbreak into the feed at once.

### Enabling the Bluesky bot

1. Create the bot account, then **Settings → Privacy and security → App
   passwords** and generate one.
2. Repo **Settings → Secrets and variables → Actions**:
   - Variable `BLUESKY_HANDLE=yourbot.bsky.social`
   - Secret `BLUESKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx`

With no credentials the poster no-ops and the build still succeeds, so local
runs and forks are unaffected. Posting is capped at
`BLUESKY_MAX_POSTS_PER_RUN` per build and skips alerts older than
`BLUESKY_MAX_ALERT_AGE_HOURS` — that bounds the damage if a deploy fails after
a post and the ledger's `posted_bluesky` flag is lost.

### Enabling email

`feed.xml` is a normal RSS feed, so any RSS-to-email bridge works and there's no
mail-sending code to maintain. [Buttondown](https://buttondown.com) free tier is
the path of least resistance: create a newsletter, point its RSS-to-email
automation at `https://hantavirusonline.org/feed.xml`, then set the repo
variable `NEWSLETTER_URL` to the embed endpoint
(`https://buttondown.com/api/emails/embed-subscribe/<username>`). The subscribe
form renders only when that variable is set, so the page never shows a form that
posts nowhere.

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

### Repo variables at a glance

| Name | Kind | Effect when unset |
| --- | --- | --- |
| `SITE_URL` | variable | Falls back to the github.io URL |
| `GOATCOUNTER_CODE` | variable | No analytics snippet |
| `NEWSLETTER_URL` | variable | Subscribe form hidden |
| `BLUESKY_HANDLE` | variable | Follow link hidden |
| `BLUESKY_APP_PASSWORD` | **secret** | Bot no-ops; build still succeeds |

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
- Not a case registry. Counts and locations are only as current as the public
  sources above.
- Not broadly WHO-indexed. WHO's Disease Outbreak News listing does not expose
  a verified RSS feed here; the tracker uses the relevant DON599 page directly
  for Hondius fallback counts.
- Not an early-warning system. Alerts fire when a *public source* publishes a
  change, which is downstream of the event itself.
