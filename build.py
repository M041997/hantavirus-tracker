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
        "site_url": SITE_URL,
        "analytics_code": ANALYTICS_CODE,
        "vessel": HONDIUS_VESSEL,
        "vessel_position": hondius_pos,
        "vessel_json": json.dumps(
            {**HONDIUS_VESSEL, "position": hondius_pos} if hondius_pos else None
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
        "vessel": {**HONDIUS_VESSEL, "position": hondius_pos},
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
