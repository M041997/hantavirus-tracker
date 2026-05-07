#!/usr/bin/env python3
"""Hantavirus tracker static site builder.

Fetches:
  - Google News RSS (hantavirus query) — primary news firehose
  - ProMED-mail search page (hantavirus) — official outbreak alerts
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


# ----- Source: Google News RSS -----
def fetch_google_news(query: str = "hantavirus", limit: int = 25) -> list[Item]:
    url = (
        "https://news.google.com/rss/search?"
        f"q={query}&hl=en-US&gl=US&ceid=US:en"
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


# Approximate population-weighted centroids for countries we cluster on.
# Used both for cluster detection and for placing map markers.
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
}

# Hardcoded "spread arcs" the news has reported clearly. Format:
# (origin_country, dest_country, label). Origin must be in COUNTRY_CENTROIDS.
SPREAD_ARCS: list[tuple[str, str, str]] = [
    ("Argentina", "USA",   "Cruise passengers monitored on return"),
    ("Argentina", "Chile", "Cruise stopover"),
    ("Argentina", "Brazil","Cruise stopover"),
]


# Alias terms that imply a specific country in news titles. Order matters
# only for matching efficiency; first match wins.
COUNTRY_ALIASES: list[tuple[str, str]] = [
    ("United States", "USA"),
    ("U.S.",          "USA"),
    ("California",    "USA"),
    ("Florida",       "USA"),
    ("Texas",         "USA"),
    ("Arizona",       "USA"),
    ("New Mexico",    "USA"),
    ("Colorado",      "USA"),
    ("Nevada",        "USA"),
    ("Utah",          "USA"),
    ("Yosemite",      "USA"),
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
        for term, canon in term_to_country:
            if re.search(rf"\b{re.escape(term)}\b", item.title, re.IGNORECASE):
                clusters.setdefault(canon, []).append(item)
                break
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


# ----- Render -----
def render(context: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["unescape"] = ihtml.unescape
    tmpl = env.get_template("index.html.j2")
    return tmpl.render(**context)


def main() -> int:
    print("[build] fetching Google News...", file=sys.stderr)
    news = fetch_google_news()
    print(f"[build]   got {len(news)} items", file=sys.stderr)

    print("[build] fetching ProMED...", file=sys.stderr)
    promed = fetch_promed()
    print(f"[build]   got {len(promed)} items", file=sys.stderr)

    all_items = sort_by_date(dedupe_by_title(promed + news))
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
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "index.html").write_text(render(context), encoding="utf-8")
    # Also write a JSON dump alongside for anyone who wants the raw data.
    payload = {
        "generated_at": context["build_time_iso"],
        "outbreaks": outbreaks,
        "country_index": country_index,
        "spread_arcs": spread_arcs,
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
    print(f"[build] wrote {OUT/'index.html'} and {OUT/'data.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
