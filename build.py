#!/usr/bin/env python3
"""Hantavirus tracker static site builder.

Fetches:
  - Google News RSS (hantavirus query) — primary news firehose
  - ProMED-mail search page (hantavirus) — community outbreak alerts
  - ECDC news RSS (filtered) — EU agency press releases & assessments
  - CDC HPS surveillance summary — slow-moving aggregate stats

Renders templates/index.html.j2 -> docs/index.html for GitHub Pages.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import html as ihtml
import json
import pathlib
import re
import sys
import time
from email.utils import parsedate_to_datetime

import feedparser
import requests
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parent
TEMPLATES = ROOT / "templates"
OUT = ROOT / "docs"

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 hantavirus-tracker/1.0"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
TIMEOUT = 20

# ----- Source: CDC US surveillance snapshot -----
# CDC publishes annual aggregates only; values from
# https://www.cdc.gov/hantavirus/data-research/cases/index.html
# Update by re-reading that page when CDC refreshes (typically once a year).
CDC_SNAPSHOT = {
    "as_of": "2023-12-31",
    "total_cases": 890,
    "hps_cases": 859,
    "non_pulmonary_cases": 31,
    "case_fatality_rate_pct": 35,
    "median_age": 38,
    "pct_male": 62,
    "pct_west_of_mississippi": 94,
    "surveillance_start": 1993,
    "source_url": "https://www.cdc.gov/hantavirus/data-research/cases/index.html",
}


# ----- Outbreak vessel: MV Hondius -----
# Operated by Oceanwide Expeditions; departed Ushuaia 2026-04-01 with the
# Andes-virus cluster. CruiseMapper embeds current AIS position as JSON in
# the page HTML, so we can scrape it without a JS engine.
HONDIUS_VESSEL = {
    "name": "MV Hondius",
    "operator": "Oceanwide Expeditions",
    "imo": "9818709",
    "mmsi": "244327000",
    "flag": "Netherlands",
    "departed_from": "Ushuaia, Argentina",
    "departed_at": "2026-04-01",
    "tracker_url": "https://www.cruisemapper.com/ships/MV-Hondius-1624",
    "vesselfinder_url": "https://www.vesselfinder.com/vessels/details/9818709",
    "marinetraffic_url": (
        "https://www.marinetraffic.com/en/ais/details/ships/"
        "shipid:5873599/mmsi:244327000/imo:9818709/vessel:HONDIUS"
    ),
}


ECDC_OUTBREAK_URL = (
    "https://www.ecdc.europa.eu/en/infectious-disease-topics/"
    "hantavirus-infection/surveillance-and-updates/andes-hantavirus-outbreak"
)
WHO_DON_URL = (
    "https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON599"
)
# Final fallback if WHO scrape fails — figures from WHO DON599 (4 May 2026).
HONDIUS_OUTBREAK_FALLBACK = {
    "cases_total": 7,
    "cases_confirmed": 2,
    "cases_suspected": 5,
    "deaths": 3,
    "critical": 1,
    "on_board": 147,
    "passengers": 88,
    "crew": 59,
    "as_of": "2026-05-04",
    "source": "WHO DON599",
    "source_url": WHO_DON_URL,
}

WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}


def _to_int(s: str) -> int | None:
    s = s.strip().lower()
    if s.isdigit():
        return int(s)
    return WORD_NUMBERS.get(s)


def fetch_ecdc_outbreak_counts() -> dict | None:
    """Scrape ECDC's outbreak page. Returns None on failure.

    ECDC updates the page daily (including weekends), so prefer it over WHO
    DON599 which only republishes when figures change materially.

    The page renders a stats block like
        "Confirmed cases*** 7 Probable cases** 2 Suspected cases* 0
         Number of deaths 3"
    plus an "As of D Month" sentence with no year.
    """
    try:
        r = requests.get(ECDC_OUTBREAK_URL, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"[ecdc] fetch failed: {exc}", file=sys.stderr)
        return None
    text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)

    confirmed = probable = suspected = deaths = None
    m = re.search(r"Confirmed cases\*+\s+(\d+)", text)
    if m: confirmed = int(m.group(1))
    m = re.search(r"Probable cases\*+\s+(\d+)", text)
    if m: probable = int(m.group(1))
    m = re.search(r"Suspected cases\*+\s+(\d+)", text)
    if m: suspected = int(m.group(1))
    m = re.search(r"Number of deaths\s+(\d+)", text)
    if m: deaths = int(m.group(1))

    # Need at least confirmed + deaths to call this a successful parse.
    if confirmed is None or deaths is None:
        print("[ecdc] structured stats block not found", file=sys.stderr)
        return None

    # ECDC distinguishes Probable from Suspected; WHO lumps both as
    # "suspected". To keep the existing template field meaningful, fold
    # probable + suspected into cases_suspected.
    p_total = (probable or 0) + (suspected or 0)
    cases_total = confirmed + p_total

    out = {
        "cases_total":     cases_total,
        "cases_confirmed": confirmed,
        "cases_suspected": p_total,
        "deaths":          deaths,
        # ECDC summary doesn't surface these; carry forward from the WHO
        # fallback so the UI doesn't show blanks.
        "critical":   HONDIUS_OUTBREAK_FALLBACK["critical"],
        "on_board":   HONDIUS_OUTBREAK_FALLBACK["on_board"],
        "passengers": HONDIUS_OUTBREAK_FALLBACK["passengers"],
        "crew":       HONDIUS_OUTBREAK_FALLBACK["crew"],
        "as_of":      HONDIUS_OUTBREAK_FALLBACK["as_of"],
        "source":     "ECDC",
        "source_url": ECDC_OUTBREAK_URL,
    }

    # Parse "As of 11 May" — ECDC omits the year. Default to current UTC
    # year; sanity-check that the resulting date isn't in the future.
    m = re.search(
        r"[Aa]s of\s+(\d{1,2})\s+"
        r"(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)"
        r"(?:\s+(\d{4}))?",
        text,
    )
    if m:
        day, month = m.group(1), m.group(2)
        year = m.group(3) or str(dt.datetime.now(dt.timezone.utc).year)
        try:
            d = dt.datetime.strptime(f"{day} {month} {year}", "%d %B %Y")
            if d.date() <= dt.datetime.now(dt.timezone.utc).date():
                out["as_of"] = d.strftime("%Y-%m-%d")
        except ValueError:
            pass

    out["fetched_at"] = dt.datetime.now(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return out


def fetch_hondius_outbreak_counts() -> dict:
    """Scrape WHO DON599 for current Hondius cluster counts.

    Returns the parsed dict, or HONDIUS_OUTBREAK_FALLBACK if scraping or
    parsing fails (so the site never shows blank counters).
    """
    try:
        r = requests.get(WHO_DON_URL, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"[who] fetch failed, using fallback: {exc}", file=sys.stderr)
        return HONDIUS_OUTBREAK_FALLBACK
    text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
    out = dict(HONDIUS_OUTBREAK_FALLBACK)

    # "seven cases (two laboratory confirmed cases of hantavirus and five
    #  suspected cases) have been identified, including three deaths,
    #  one critically ill patient ..."
    m = re.search(
        r"([A-Za-z]+|\d+)\s+cases?\s*\(\s*([A-Za-z]+|\d+)\s+laboratory\s+"
        r"confirmed[^()]*?([A-Za-z]+|\d+)\s+suspected",
        text,
        re.IGNORECASE,
    )
    if m:
        total = _to_int(m.group(1))
        conf = _to_int(m.group(2))
        susp = _to_int(m.group(3))
        if total is not None: out["cases_total"] = total
        if conf is not None:  out["cases_confirmed"] = conf
        if susp is not None:  out["cases_suspected"] = susp

    m = re.search(
        r"including\s+([A-Za-z]+|\d+)\s+deaths?",
        text, re.IGNORECASE,
    )
    if m:
        d = _to_int(m.group(1))
        if d is not None: out["deaths"] = d

    m = re.search(
        r"([A-Za-z]+|\d+)\s+critically\s+ill",
        text, re.IGNORECASE,
    )
    if m:
        c = _to_int(m.group(1))
        if c is not None: out["critical"] = c

    m = re.search(
        r"total of\s+(\d+)\s+individuals?,?\s+including\s+(\d+)\s+passengers"
        r"\s+and\s+(\d+)\s+crew",
        text, re.IGNORECASE,
    )
    if m:
        out["on_board"]   = int(m.group(1))
        out["passengers"] = int(m.group(2))
        out["crew"]       = int(m.group(3))

    # "As of 4 May 2026" — extract date
    m = re.search(
        r"[Aa]s of (\d{1,2})\s+"
        r"(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+(\d{4})",
        text,
    )
    if m:
        try:
            d = dt.datetime.strptime(
                f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y"
            )
            out["as_of"] = d.strftime("%Y-%m-%d")
        except ValueError:
            pass

    out["fetched_at"] = dt.datetime.now(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return out


def fetch_hondius_position() -> dict | None:
    """Scrape CruiseMapper for the Hondius's current AIS position.

    Returns a dict {lat, lng, heading_deg, fetched_at} or None if the page
    layout changed and we can't parse it.
    """
    url = HONDIUS_VESSEL["tracker_url"]
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"[hondius] fetch failed: {exc}", file=sys.stderr)
        return None
    m = re.search(
        r'"shipCurrentPositionMap"\s*:\s*\{([^}]+)\}', r.text
    )
    if not m:
        print("[hondius] position blob not found in HTML", file=sys.stderr)
        return None
    blob = "{" + m.group(1) + "}"
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        print(f"[hondius] JSON decode failed: {exc}", file=sys.stderr)
        return None
    lat = data.get("lat")
    lng = data.get("lon")
    rotation = data.get("rotation")  # radians
    if lat is None or lng is None:
        return None
    heading_deg = None
    if rotation is not None:
        import math
        heading_deg = round((math.degrees(float(rotation)) + 360) % 360, 1)
    return {
        "lat": float(lat),
        "lng": float(lng),
        "heading_deg": heading_deg,
        "fetched_at": dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }


# ----- Hondius track persistence -----
# The site itself is the persistent store: each build pulls the previously
# deployed track over HTTP, appends a new fix if movement is meaningful,
# and writes the updated file back into docs/ for the next run to read.
TRACK_FETCH_URL = "https://hantavirusonline.org/hondius_track.json"
TRACK_MAX_DAYS = 14
TRACK_MAX_POINTS = 400
TRACK_MIN_MOVE_DEG = 0.05      # ~3 nautical miles
TRACK_MIN_AGE_MINUTES = 30     # always record at least once per ~30 min


def load_existing_track(local_path: pathlib.Path) -> list[dict]:
    """Prefer the deployed site — it's always the freshest copy because CI
    doesn't commit generated files back. Fall back to the local file only
    when the network is unavailable (e.g. local dev offline, first build).
    """
    try:
        r = requests.get(TRACK_FETCH_URL, headers=HEADERS, timeout=TIMEOUT)
        if r.ok:
            data = r.json()
            if isinstance(data, list):
                return data
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        pass
    if local_path.exists():
        try:
            return json.loads(local_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _parse_iso(s: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def update_track(track: list[dict], pos: dict | None) -> list[dict]:
    if not pos:
        return track
    entry = {"lat": pos["lat"], "lng": pos["lng"],
             "fetched_at": pos["fetched_at"]}
    if track:
        last = track[-1]
        last_dt = _parse_iso(last.get("fetched_at", ""))
        cur_dt = _parse_iso(entry["fetched_at"]) or dt.datetime.now(dt.timezone.utc)
        moved = (abs(entry["lat"] - last["lat"]) > TRACK_MIN_MOVE_DEG
                 or abs(entry["lng"] - last["lng"]) > TRACK_MIN_MOVE_DEG)
        old_enough = (last_dt is None
                      or (cur_dt - last_dt).total_seconds() / 60
                      >= TRACK_MIN_AGE_MINUTES)
        if not (moved or old_enough):
            return track
    track.append(entry)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=TRACK_MAX_DAYS)
    pruned = [e for e in track
              if (e_dt := _parse_iso(e.get("fetched_at", ""))) and e_dt >= cutoff]
    return pruned[-TRACK_MAX_POINTS:]


@dataclasses.dataclass
class Item:
    title: str
    url: str
    source: str
    published: dt.datetime | None
    summary: str = ""

    @property
    def stable_id(self) -> str:
        return hashlib.sha1(f"{self.source}|{self.url}".encode()).hexdigest()[:12]

    @property
    def published_iso(self) -> str:
        return self.published.strftime("%Y-%m-%d") if self.published else ""

    @property
    def published_human(self) -> str:
        if not self.published:
            return ""
        delta = dt.datetime.now(dt.timezone.utc) - self.published
        days = delta.days
        if days <= 0:
            hours = max(1, delta.seconds // 3600)
            return f"{hours}h ago"
        if days == 1:
            return "1 day ago"
        if days < 30:
            return f"{days} days ago"
        return self.published.strftime("%b %d, %Y")


# ----- Other active disease outbreaks (sidebar) -----
# Lightweight side-panel that surfaces unrelated outbreaks the user might be
# tracking in parallel. Each entry drives one Google News query; the UI lets
# visitors hide the whole panel via a localStorage toggle, so this stays
# opt-in for casual visitors but available for anyone who wants it.
OTHER_DISEASES: list[dict] = [
    {
        "key": "ebola-drc-2026",
        "name": "Ebola",
        "region": "DR Congo · Ituri province",
        "query": "ebola outbreak Congo",
        # Africa CDC confirmation on 2026-05-15; figures will be scraped via
        # Google News titles only — no structured counter source yet.
        "started": "2026-05-15",
        "blurb": "17th Congo outbreak. Africa CDC confirmed in Ituri province.",
        # Violet so it reads clearly against the red hantavirus pulses.
        "color": "#c084fc",
        "locations": [
            {"name": "Mongwalu (Ituri)",  "lat":  1.95, "lng": 30.06},
            {"name": "Rwampara (Ituri)",  "lat":  1.60, "lng": 30.20},
        ],
    },
]


def fetch_other_outbreaks(limit_each: int = 6) -> list[dict]:
    """Fetch a few news items per configured other-disease outbreak."""
    out: list[dict] = []
    for cfg in OTHER_DISEASES:
        news_items = fetch_google_news(query=cfg["query"], limit=limit_each)
        out.append({
            "key": cfg["key"],
            "name": cfg["name"],
            "region": cfg["region"],
            "started": cfg["started"],
            "blurb": cfg["blurb"],
            "color": cfg.get("color", "#c084fc"),
            "locations": cfg.get("locations", []),
            "news": [
                {
                    "title": i.title,
                    "url": i.url,
                    "source": i.source,
                    "published": i.published_human,
                }
                for i in news_items
            ],
        })
    return out


# ----- Source: Google News RSS -----
def fetch_google_news(query: str = "hantavirus", limit: int = 25) -> list[Item]:
    from urllib.parse import quote_plus
    url = (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    parsed = feedparser.parse(url, request_headers=HEADERS)
    items: list[Item] = []
    for e in parsed.entries[:limit]:
        published = None
        if getattr(e, "published", None):
            try:
                published = parsedate_to_datetime(e.published)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=dt.timezone.utc)
            except Exception:
                published = None
        # Google News titles end with " - Source" — split off.
        title = e.title or ""
        outlet = ""
        if " - " in title:
            title, outlet = title.rsplit(" - ", 1)
        items.append(
            Item(
                title=title.strip(),
                url=e.link,
                source=f"Google News · {outlet}" if outlet else "Google News",
                published=published,
                summary="",
            )
        )
    return items


# ----- Source: ECDC news RSS -----
# ECDC has no dedicated hantavirus RSS, but the general news taxonomy feed
# covers press releases + news items for current outbreaks. Filter by title
# the same way we filter ProMED.
ECDC_RSS_URL = "https://www.ecdc.europa.eu/en/taxonomy/term/1307/feed"


def fetch_ecdc(query: str = "hantavirus", limit: int = 15) -> list[Item]:
    parsed = feedparser.parse(ECDC_RSS_URL, request_headers=HEADERS)
    items: list[Item] = []
    for e in parsed.entries:
        title = (e.title or "").strip()
        if query.lower() not in title.lower():
            continue
        published = None
        if getattr(e, "published", None):
            try:
                published = parsedate_to_datetime(e.published)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=dt.timezone.utc)
            except Exception:
                published = None
        items.append(
            Item(
                title=title,
                url=e.link,
                source="ECDC",
                published=published,
                summary="",
            )
        )
        if len(items) >= limit:
            break
    return items


# ----- Source: ProMED-mail scraper -----
# ProMED renders search results as a Tailwind-styled <table>. Rows are
# clickable via JS, so individual post URLs are not in the HTML — we link
# items back to the search page as the best public surface.
PROMED_DATE_RE = re.compile(
    r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+\d{4}$"
)


def fetch_promed(query: str = "hantavirus", limit: int = 20) -> list[Item]:
    search_url = f"https://www.promedmail.org/?s={query}"
    try:
        r = requests.get(search_url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"[promed] fetch failed: {exc}", file=sys.stderr)
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    items: list[Item] = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 2:
            continue
        date_text = tds[0].get_text(" ", strip=True)
        if not PROMED_DATE_RE.match(date_text):
            continue
        title_div = tds[1].find("div", class_=lambda c: c and "font-medium" in c)
        if not title_div:
            continue
        title = title_div.get_text(" ", strip=True)
        if query.lower() not in title.lower():
            continue
        try:
            published = dt.datetime.strptime(date_text, "%a %b %d %Y").replace(
                tzinfo=dt.timezone.utc
            )
        except ValueError:
            published = None
        items.append(
            Item(
                title=title,
                url=search_url,
                source="ProMED-mail",
                published=published,
                summary="",
            )
        )
        if len(items) >= limit:
            break
    return items


# ----- Aggregation helpers -----
def dedupe_by_title(items: list[Item]) -> list[Item]:
    """Remove near-duplicate titles across sources."""
    seen: set[str] = set()
    out: list[Item] = []
    for item in items:
        # Normalize: lowercase, strip punctuation, collapse whitespace.
        key = re.sub(r"[^a-z0-9 ]+", " ", item.title.lower())
        key = re.sub(r"\s+", " ", key).strip()[:120]
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def sort_by_date(items: list[Item]) -> list[Item]:
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return sorted(items, key=lambda i: i.published or epoch, reverse=True)


# Approximate centroids for places we cluster on. Mix of countries and US
# states — for the cruise-outbreak coverage, US-state granularity matters
# because individual states (Texas, Nebraska, New Jersey, …) show up in
# news as quarantine and monitoring sites.
# "Georgia (US)" disambiguates from the country Georgia.
COUNTRY_CENTROIDS: dict[str, tuple[float, float]] = {
    "Argentina":      (-38.4, -63.6),
    "Chile":          (-35.7, -71.5),
    "Bolivia":        (-16.3, -63.6),
    "Brazil":         (-14.2, -51.9),
    "Paraguay":       (-23.4, -58.4),
    "Uruguay":        (-32.5, -55.8),
    "Peru":           (-9.2,  -75.0),
    "Panama":         ( 8.5,  -80.8),
    "Mexico":         (23.6, -102.5),
    "USA":            (37.1,  -95.7),
    "United States":  (37.1,  -95.7),
    "Canada":         (56.1, -106.3),
    "China":          (35.9,  104.2),
    "Korea":          (35.9,  127.8),
    "South Korea":    (35.9,  127.8),
    "Russia":         (61.5,  105.3),
    "Germany":        (51.2,   10.4),
    "Finland":        (61.9,   25.7),
    "Sweden":         (60.1,   18.6),
    "Taiwan":         (23.7,  121.0),
    "Japan":          (36.2,  138.3),
    "Spain":          (40.4,   -3.7),
    "France":         (46.6,    2.2),
    # The Canaries are politically Spain but ~1,000 km away off NW Africa;
    # disembarkation news points here, not Madrid, so keep a separate dot.
    "Canary Islands": (28.3,  -15.5),
    "South Africa":   (-30.6,  22.9),
    # US states currently relevant to the Hondius / hantavirus coverage.
    "Texas":          (31.5,  -99.3),
    "Nebraska":       (41.5,  -99.8),
    "New Jersey":     (40.2,  -74.5),
    "Georgia (US)":   (32.6,  -83.4),
    "California":     (36.8, -119.4),
    "Arizona":        (34.2, -111.7),
    "Virginia":       (37.9,  -78.0),
    "Florida":        (27.7,  -81.5),
    "Colorado":       (39.0, -105.5),
    "New Mexico":     (34.5, -106.0),
    "Nevada":         (38.8, -116.4),
    "Utah":           (39.3, -111.6),
    "Oregon":         (44.0, -120.5),
}

# Hardcoded "spread arcs" the news has reported clearly. Format:
# (origin, dest, label). Both endpoints must be in COUNTRY_CENTROIDS.
SPREAD_ARCS: list[tuple[str, str, str]] = [
    ("Argentina", "Texas",      "TX residents on cruise, returned"),
    ("Argentina", "Nebraska",   "Federal quarantine — Nebraska Medicine"),
    ("Argentina", "New Jersey", "NJDOH monitoring air-travel contacts"),
    ("Argentina", "California", "Passengers monitored on return"),
    ("Argentina", "Arizona",    "Passengers monitored on return"),
    ("Argentina", "Chile",          "Cruise stopover"),
    ("Argentina", "Brazil",         "Cruise stopover"),
    ("Argentina", "Canary Islands", "Hondius port of disembarkation"),
    ("Argentina", "South Africa",   "Contacts monitored on stopover"),
    ("Argentina", "France",         "Critical patient on ECMO"),
    ("Argentina", "Oregon",         "Physician case in isolation"),
]


# Alias terms that imply a specific place in news titles. Order matters:
# state-specific aliases come BEFORE generic US terms so a headline like
# "United States, Texas" tags Texas, not the generic USA bucket.
COUNTRY_ALIASES: list[tuple[str, str]] = [
    # US state aliases — match first.
    ("California",    "California"),
    ("Florida",       "Florida"),
    ("Texas",         "Texas"),
    ("Texan",         "Texas"),
    ("Arizona",       "Arizona"),
    ("New Mexico",    "New Mexico"),
    ("Colorado",      "Colorado"),
    ("Nevada",        "Nevada"),
    ("Utah",          "Utah"),
    ("Virginia",      "Virginia"),
    ("Oregon",        "Oregon"),
    ("Yosemite",      "California"),
    ("San Francisco", "California"),
    ("Bay Area",      "California"),
    ("San Mateo",     "California"),
    ("SFO",           "California"),
    ("Nebraska",      "Nebraska"),
    ("Omaha",         "Nebraska"),
    # UNMC is the federal quarantine + biocontainment hub for returning
    # Hondius passengers; headlines often name the institution, not the state.
    ("UNMC",                                  "Nebraska"),
    ("University of Nebraska Medical Center", "Nebraska"),
    ("Nebraska Medicine",                     "Nebraska"),
    ("Nebraska Medical Center",               "Nebraska"),
    ("Georgia",       "Georgia (US)"),
    ("Atlanta",       "Georgia (US)"),
    # Emory University in Atlanta receives transfer cases (symptomatic).
    ("Emory University", "Georgia (US)"),
    ("Emory",            "Georgia (US)"),
    ("New Jersey",    "New Jersey"),
    ("NJDOH",         "New Jersey"),
    # Generic US terms — fall through to country-level USA bucket only when
    # no specific state matched first.
    ("United States", "USA"),
    ("U.S.",          "USA"),
    # Canary Islands (Tenerife disembarkation).
    ("Canary Islands", "Canary Islands"),
    ("Tenerife",       "Canary Islands"),
    ("Gran Canaria",   "Canary Islands"),
    ("Las Palmas",     "Canary Islands"),
    ("France",         "France"),
    ("French",         "France"),
    ("Paris",          "France"),
    # Other-country aliases.
    ("South Korea",   "Korea"),
    ("Republic of Korea", "Korea"),
]


def cluster_by_country(items: list[Item]) -> dict[str, list[Item]]:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=60)
    clusters: dict[str, list[Item]] = {}
    # Build lookup: term -> canonical country (centroid key)
    term_to_country: list[tuple[str, str]] = []
    for term, canon in COUNTRY_ALIASES:
        term_to_country.append((term, canon))
    for c in COUNTRY_CENTROIDS:
        canon = "USA" if c == "United States" else c
        term_to_country.append((c, canon))
    for item in items:
        if not item.published or item.published < cutoff:
            continue
        # An article can mention several places (e.g. roundup posts naming
        # multiple US states). Count it once per distinct canonical place
        # so each state pin reflects all relevant coverage, but don't double
        # within one article.
        seen: set[str] = set()
        for term, canon in term_to_country:
            if canon in seen:
                continue
            if re.search(rf"\b{re.escape(term)}\b", item.title, re.IGNORECASE):
                clusters.setdefault(canon, []).append(item)
                seen.add(canon)
    return clusters


def detect_active_outbreaks(clusters: dict[str, list[Item]]) -> list[dict]:
    out = []
    for country, cl in sorted(
        clusters.items(), key=lambda kv: len(kv[1]), reverse=True
    ):
        if len(cl) < 2:
            continue
        latest = max(cl, key=lambda i: i.published or dt.datetime.min)
        out.append(
            {
                "country": country,
                "count": len(cl),
                "latest_title": latest.title,
                "latest_url": latest.url,
                "latest_date": latest.published_human,
            }
        )
    return out


def build_country_index(clusters: dict[str, list[Item]]) -> list[dict]:
    """One entry per country with markers + recent items for the map."""
    out = []
    for country, cl in clusters.items():
        if country not in COUNTRY_CENTROIDS:
            continue
        lat, lng = COUNTRY_CENTROIDS[country]
        cl_sorted = sorted(
            cl, key=lambda i: i.published or dt.datetime.min, reverse=True
        )
        latest = cl_sorted[0]
        out.append(
            {
                "country": country,
                "lat": lat,
                "lng": lng,
                "count": len(cl),
                "latest_title": latest.title,
                "latest_url": latest.url,
                "latest_date": latest.published_human,
                "items": [
                    {
                        "title": i.title,
                        "url": i.url,
                        "source": i.source,
                        "published": i.published_human,
                    }
                    for i in cl_sorted[:6]
                ],
            }
        )
    out.sort(key=lambda c: c["count"], reverse=True)
    return out


def build_spread_arcs(country_index: list[dict]) -> list[dict]:
    """Return arc dicts only for spread routes whose origin has activity."""
    active = {c["country"] for c in country_index}
    arcs: list[dict] = []
    for origin, dest, label in SPREAD_ARCS:
        if origin not in active:
            continue
        if origin not in COUNTRY_CENTROIDS or dest not in COUNTRY_CENTROIDS:
            continue
        o_lat, o_lng = COUNTRY_CENTROIDS[origin]
        d_lat, d_lng = COUNTRY_CENTROIDS[dest]
        arcs.append(
            {
                "from": origin,
                "to": dest,
                "from_latlng": [o_lat, o_lng],
                "to_latlng": [d_lat, d_lng],
                "label": label,
            }
        )
    return arcs


# ----- Site config -----
# Site URL for canonical/OG/sitemap. Override via SITE_URL env var when DNS
# moves to a custom domain (e.g. https://hantavirus.live).
import os
SITE_URL = os.environ.get(
    "SITE_URL", "https://m041997.github.io/hantavirus-tracker"
).rstrip("/")
# GoatCounter analytics code; set GOATCOUNTER_CODE env var after signing up
# at goatcounter.com to enable the snippet.
ANALYTICS_CODE = os.environ.get("GOATCOUNTER_CODE", "").strip()


# ----- Render -----
def render(template_name: str, context: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["unescape"] = ihtml.unescape
    tmpl = env.get_template(template_name)
    return tmpl.render(**context)


# ----- OG share image -----
def _load_font(size: int) -> ImageFont.ImageFont:
    """Try a few common system fonts; fall back to default."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/arialbd.ttf",
    ]
    for path in candidates:
        if pathlib.Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_og_image(country_index: list[dict], outbreaks: list[dict],
                     reports_today: int) -> Image.Image:
    """Render a 1200x630 PNG share card matching the site's hero look."""
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), (10, 14, 20))
    draw = ImageDraw.Draw(img, "RGBA")

    # Diagonal gradient panel
    for y in range(H):
        c = int(10 + 8 * (y / H))
        draw.line([(0, y), (W, y)], fill=(c, c + 4, c + 10))

    # Faint world-map silhouette band — abstract, evocative
    band_y = H // 2 - 40
    draw.rectangle([(0, band_y), (W, band_y + 120)],
                   fill=(20, 32, 48, 90))

    # Pulse dots representing active countries (max 5)
    palette_red = (255, 59, 59)
    for i, c in enumerate(sorted(
        country_index, key=lambda c: c["count"], reverse=True
    )[:6]):
        # Map lng (-180..180) -> x (60..W-60), lat (90..-90) -> y in band
        x = int(60 + (c["lng"] + 180) * (W - 120) / 360)
        y = int(band_y + 60 + (-c["lat"] + 0) * 0.7)
        r = max(8, min(28, 6 + c["count"] * 2))
        # Glow
        for rr, alpha in [(r * 2, 40), (int(r * 1.4), 90)]:
            draw.ellipse([x - rr, y - rr, x + rr, y + rr],
                         fill=(255, 59, 59, alpha))
        draw.ellipse([x - r, y - r, x + r, y + r], fill=palette_red)

    # Pulse marker for the title
    dot_x, dot_y, dot_r = 80, 100, 14
    draw.ellipse([dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r],
                 fill=palette_red)
    draw.ellipse([dot_x - dot_r - 8, dot_y - dot_r - 8,
                  dot_x + dot_r + 8, dot_y + dot_r + 8],
                 outline=(255, 59, 59, 120), width=4)

    # Title
    title_font = _load_font(72)
    sub_font = _load_font(28)
    stat_v_font = _load_font(58)
    stat_k_font = _load_font(20)

    draw.text((dot_x + 40, dot_y - 38), "HANTAVIRUS TRACKER",
              font=title_font, fill=(232, 238, 245))
    draw.text((dot_x + 42, dot_y + 36),
              "Live global outbreak & news aggregator",
              font=sub_font, fill=(138, 153, 171))

    # Stat tiles (bottom)
    stats = [
        (f"{len(country_index)}", "ACTIVE COUNTRIES"),
        (f"{reports_today}", "REPORTS TODAY"),
        (f"{len(outbreaks)}", "CLUSTERS"),
        ("35%", "US CFR · CDC"),
    ]
    tile_w, tile_h, gap = 240, 110, 24
    total_w = len(stats) * tile_w + (len(stats) - 1) * gap
    start_x = (W - total_w) // 2
    y0 = H - tile_h - 70
    for i, (v, k) in enumerate(stats):
        x0 = start_x + i * (tile_w + gap)
        draw.rounded_rectangle(
            [x0, y0, x0 + tile_w, y0 + tile_h],
            radius=14, fill=(20, 28, 38, 220),
            outline=(40, 50, 62), width=1,
        )
        # Center the value horizontally in the tile
        bbox = draw.textbbox((0, 0), v, font=stat_v_font)
        vw = bbox[2] - bbox[0]
        draw.text((x0 + (tile_w - vw) // 2, y0 + 12), v,
                  font=stat_v_font,
                  fill=(255, 59, 59) if i == 0 else (255, 184, 77)
                  if i == 1 else (232, 238, 245))
        bbox = draw.textbbox((0, 0), k, font=stat_k_font)
        kw = bbox[2] - bbox[0]
        draw.text((x0 + (tile_w - kw) // 2, y0 + 78), k,
                  font=stat_k_font, fill=(138, 153, 171))

    # Footer URL
    foot_font = _load_font(22)
    draw.text((60, H - 40),
              SITE_URL.replace("https://", "").replace("http://", ""),
              font=foot_font, fill=(138, 153, 171))

    return img


def write_og_image(path: pathlib.Path, country_index: list[dict],
                   outbreaks: list[dict], reports_today: int) -> None:
    img = render_og_image(country_index, outbreaks, reports_today)
    img.save(path, "PNG", optimize=True)


# ----- Sitemap + robots -----
def write_sitemap(path: pathlib.Path, last_mod_iso: str) -> None:
    pages = ["", "about.html"]
    urls = "\n".join(
        f"  <url><loc>{SITE_URL}/{p}</loc>"
        f"<lastmod>{last_mod_iso[:10]}</lastmod></url>"
        for p in pages
    )
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n",
        encoding="utf-8",
    )


def write_robots(path: pathlib.Path) -> None:
    path.write_text(
        "User-agent: *\n"
        "Allow: /\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n",
        encoding="utf-8",
    )


def main() -> int:
    print("[build] fetching Google News...", file=sys.stderr)
    news = fetch_google_news()
    print(f"[build]   got {len(news)} items", file=sys.stderr)

    print("[build] fetching ProMED...", file=sys.stderr)
    promed = fetch_promed()
    print(f"[build]   got {len(promed)} items", file=sys.stderr)

    print("[build] fetching ECDC...", file=sys.stderr)
    ecdc = fetch_ecdc()
    print(f"[build]   got {len(ecdc)} items", file=sys.stderr)

    print("[build] fetching other-disease outbreaks...", file=sys.stderr)
    other_outbreaks = fetch_other_outbreaks()
    print(
        f"[build]   got {sum(len(o['news']) for o in other_outbreaks)} "
        f"items across {len(other_outbreaks)} outbreaks",
        file=sys.stderr,
    )

    track_path = OUT / "hondius_track.json"

    print("[build] fetching MV Hondius position...", file=sys.stderr)
    hondius_pos = fetch_hondius_position()
    if hondius_pos:
        print(
            f"[build]   Hondius @ {hondius_pos['lat']:.3f}, "
            f"{hondius_pos['lng']:.3f}",
            file=sys.stderr,
        )
    else:
        print("[build]   Hondius position unavailable", file=sys.stderr)

    hondius_track = update_track(load_existing_track(track_path), hondius_pos)
    print(f"[build]   track length: {len(hondius_track)} points", file=sys.stderr)

    print("[build] fetching ECDC outbreak counts...", file=sys.stderr)
    hondius_counts = fetch_ecdc_outbreak_counts()
    if hondius_counts is None:
        print("[build]   ECDC unavailable, falling back to WHO DON599",
              file=sys.stderr)
        hondius_counts = fetch_hondius_outbreak_counts()
    print(
        f"[build]   cases={hondius_counts['cases_total']} "
        f"deaths={hondius_counts['deaths']} "
        f"source={hondius_counts['source']} "
        f"as_of={hondius_counts['as_of']}",
        file=sys.stderr,
    )

    promed = sort_by_date(promed)
    news = sort_by_date(news)
    ecdc = sort_by_date(ecdc)
    all_items = sort_by_date(dedupe_by_title(promed + ecdc + news))
    clusters = cluster_by_country(all_items)
    outbreaks = detect_active_outbreaks(clusters)
    country_index = build_country_index(clusters)
    spread_arcs = build_spread_arcs(country_index)

    today = dt.datetime.now(dt.timezone.utc).date()
    reports_today = sum(
        1 for i in all_items if i.published and i.published.date() == today
    )

    now = dt.datetime.now(dt.timezone.utc)
    context = {
        "build_time_iso": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "build_time_human": now.strftime("%Y-%m-%d %H:%M UTC"),
        "promed_items": promed[:15],
        "ecdc_items": ecdc[:10],
        "news_items": news[:25],
        "all_items": all_items[:30],
        "outbreaks": outbreaks,
        "country_index": country_index,
        "spread_arcs": spread_arcs,
        "country_index_json": json.dumps(country_index),
        "spread_arcs_json": json.dumps(spread_arcs),
        "cdc": CDC_SNAPSHOT,
        "promed_count": len(promed),
        "news_count": len(news),
        "reports_today": reports_today,
        "active_country_count": len(country_index),
        "site_url": SITE_URL,
        "analytics_code": ANALYTICS_CODE,
        "vessel": HONDIUS_VESSEL,
        "vessel_position": hondius_pos,
        "vessel_counts": hondius_counts,
        "other_outbreaks": other_outbreaks,
        "other_outbreaks_json": json.dumps(other_outbreaks),
        "vessel_json": json.dumps(
            {**HONDIUS_VESSEL,
             "position": hondius_pos,
             "counts": hondius_counts,
             "track": hondius_track} if hondius_pos else None
        ),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "index.html").write_text(
        render("index.html.j2", context), encoding="utf-8"
    )
    (OUT / "about.html").write_text(
        render("about.html.j2", context), encoding="utf-8"
    )
    write_og_image(OUT / "og.png", country_index, outbreaks, reports_today)
    write_sitemap(OUT / "sitemap.xml", context["build_time_iso"])
    write_robots(OUT / "robots.txt")
    # Also write a JSON dump alongside for anyone who wants the raw data.
    payload = {
        "generated_at": context["build_time_iso"],
        "outbreaks": outbreaks,
        "country_index": country_index,
        "spread_arcs": spread_arcs,
        "vessel": {
            **HONDIUS_VESSEL,
            "position": hondius_pos,
            "counts": hondius_counts,
        },
        "cdc_snapshot": CDC_SNAPSHOT,
        "items": [
            {
                "title": i.title,
                "url": i.url,
                "source": i.source,
                "published": i.published_iso,
            }
            for i in all_items[:50]
        ],
    }
    (OUT / "data.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    track_path.write_text(json.dumps(hondius_track), encoding="utf-8")
    print(f"[build] wrote {OUT/'index.html'} and {OUT/'data.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
