import datetime as dt
import pathlib

import build
from build import Item


def recent(days: int = 0) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)


def recent_iso(minutes: int = 0) -> str:
    """ISO timestamp `minutes` ago — used so track tests never age out of the
    TRACK_MAX_DAYS pruning window as real time passes."""
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_clean_summary_strips_feed_html():
    raw = "<p>Cases reported in <strong>Argentina</strong>&nbsp;today.</p>"

    assert build.clean_summary(raw) == "Cases reported in Argentina today."


def test_dedupe_by_title_normalizes_case_and_punctuation():
    items = [
        Item("Hantavirus outbreak in Argentina!", "url1", "src1", recent()),
        Item("hantavirus outbreak in argentina", "url2", "src2", recent()),
        Item("Hantavirus outbreak in Chile", "url3", "src3", recent()),
    ]

    deduped = build.dedupe_by_title(items)

    assert [i.url for i in deduped] == ["url1", "url3"]


def test_cluster_by_country_uses_summary_and_ignores_stale_items():
    items = [
        Item(
            "Cruise passenger monitored",
            "url1",
            "src1",
            recent(),
            summary="The passenger returned from Argentina.",
        ),
        Item("Outbreak in Texas", "url2", "src2", recent()),
        Item("Old hantavirus note in Chile", "url3", "src3", recent(90)),
    ]

    clusters = build.cluster_by_country(items)

    assert "Argentina" in clusters
    assert "Texas" in clusters
    assert "Chile" not in clusters


def test_update_track_skips_tiny_recent_movement():
    track = [{"lat": 1.0, "lng": 2.0, "fetched_at": recent_iso(10)}]
    pos = {"lat": 1.01, "lng": 2.01, "fetched_at": recent_iso(0)}

    assert build.update_track(track.copy(), pos) == track


def test_update_track_appends_after_minimum_age():
    track = [{"lat": 1.0, "lng": 2.0, "fetched_at": recent_iso(31)}]
    pos = {"lat": 1.01, "lng": 2.01, "fetched_at": recent_iso(0)}

    updated = build.update_track(track.copy(), pos)

    assert len(updated) == 2
    assert updated[-1]["lat"] == 1.01


def test_other_diseases_entries_have_required_shape():
    required = {"key", "name", "region", "query", "started", "blurb", "color", "locations"}

    assert build.OTHER_DISEASES, "OTHER_DISEASES should not be empty if sidebar is wired"
    for entry in build.OTHER_DISEASES:
        missing = required - entry.keys()
        assert not missing, f"{entry.get('key')} missing keys: {missing}"
        for loc in entry["locations"]:
            assert isinstance(loc.get("lat"), (int, float))
            assert isinstance(loc.get("lng"), (int, float))
            assert -90 <= loc["lat"] <= 90
            assert -180 <= loc["lng"] <= 180


def test_started_human_renders_relative_days():
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    yesterday = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    a_week_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).strftime("%Y-%m-%d")

    assert "today" in build._started_human(today)
    assert "1d ago" in build._started_human(yesterday)
    assert "7d ago" in build._started_human(a_week_ago)
    assert build._started_human("not-a-date") == "not-a-date"


def test_data_age_classifies_freshness():
    fresh = build.data_age(recent(3).strftime("%Y-%m-%d"))
    stale = build.data_age(recent(30).strftime("%Y-%m-%d"))
    dormant = build.data_age(recent(120).strftime("%Y-%m-%d"))

    assert fresh["status"] == "fresh"
    assert stale["status"] == "stale"
    assert dormant["status"] == "dormant"
    assert "ago" in dormant["human"]

    # Unparseable / missing dates degrade to 'unknown', never crash.
    assert build.data_age(None)["status"] == "unknown"
    assert build.data_age("not-a-date")["status"] == "unknown"


def test_cyclospora_counts_fall_back_when_source_blocked(monkeypatch):
    """CDC bot-blocks scrapers; a refused fetch must yield the dated fallback,
    never a blank/None counter."""
    def blocked(url):
        raise build.requests.RequestException("403 Forbidden")

    monkeypatch.setattr(build, "http_get", blocked)
    counts = build.fetch_cyclospora_counts()

    for key in ("confirmed", "pending", "hospitalized", "deaths", "as_of", "source_url"):
        assert counts.get(key) is not None, f"missing {key}"
    assert counts["confirmed"] == build.CYCLOSPORA_FALLBACK["confirmed"]


def test_cyclospora_locations_are_valid_coords():
    assert build.CYCLOSPORA_LOCATIONS, "hero needs US-state map markers"
    for loc in build.CYCLOSPORA_LOCATIONS:
        assert -90 <= loc["lat"] <= 90
        assert -180 <= loc["lng"] <= 180


def test_load_existing_track_falls_back_to_local_file(monkeypatch, tmp_path):
    def fail_fetch(url):
        raise build.requests.RequestException("offline")

    track_path = pathlib.Path(tmp_path) / "hondius_track.json"
    track_path.write_text('[{"lat": 1, "lng": 2, "fetched_at": "2026-05-14T00:00:00Z"}]')
    monkeypatch.setattr(build, "http_get", fail_fetch)

    assert build.load_existing_track(track_path) == [
        {"lat": 1, "lng": 2, "fetched_at": "2026-05-14T00:00:00Z"}
    ]
