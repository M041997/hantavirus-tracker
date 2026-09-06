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


# ----- Push layer: alert ledger -----
def hero_state(cases=100, deaths=1):
    return {
        "key": "cyclospora-us-2026",
        "name": "Cyclosporiasis",
        "region": "United States",
        "confirmed": cases,
        "deaths": deaths,
        "outbreak_url": "https://cdc.gov/x",
    }


def test_snapshot_state_ignores_counters_without_a_live_scrape():
    others = [
        {"key": "a", "name": "A", "region": "R", "status": "live",
         "cases": 20, "deaths": 2, "source_url": "https://a"},
        {"key": "b", "name": "B", "region": "R", "status": "no_counter",
         "cases": 99, "deaths": 9, "source_url": "https://b"},
    ]

    state = build.snapshot_state(hero_state(), others, [{"country": "Chile"}])

    assert state["outbreak:a"]["cases"] == 20
    # A stale/absent scrape must read as "no observation", never as a real 99.
    assert state["outbreak:b"]["cases"] is None
    assert state["_countries"] == ["Chile"]


def test_diff_alerts_ignores_small_and_downward_case_moves():
    prev = build.snapshot_state(hero_state(cases=1000), [], [])
    small = build.snapshot_state(hero_state(cases=1005), [], [])
    down = build.snapshot_state(hero_state(cases=900), [], [])
    now = "2026-08-27T12:00:00Z"

    assert build.diff_alerts(prev, small, now) == []
    assert build.diff_alerts(prev, down, now) == []


def test_diff_alerts_fires_on_material_case_move():
    prev = build.snapshot_state(hero_state(cases=1000), [], [])
    cur = build.snapshot_state(hero_state(cases=1200), [], [])

    alerts = build.diff_alerts(prev, cur, "2026-08-27T12:00:00Z")

    assert len(alerts) == 1
    assert alerts[0]["kind"] == "count_change"
    assert "1,200" in alerts[0]["title"]


def test_diff_alerts_fires_on_any_death_increase():
    prev = build.snapshot_state(hero_state(cases=1000, deaths=0), [], [])
    cur = build.snapshot_state(hero_state(cases=1000, deaths=1), [], [])

    alerts = build.diff_alerts(prev, cur, "2026-08-27T12:00:00Z")

    assert len(alerts) == 1
    assert "deaths" in alerts[0]["summary"]


def test_diff_alerts_reports_new_outbreaks_and_clusters():
    prev = build.snapshot_state(hero_state(), [], [{"country": "Chile"}])
    cur = build.snapshot_state(
        hero_state(),
        [{"key": "ebola", "name": "Ebola", "region": "DRC",
          "status": "no_counter", "source_url": "https://who.int"}],
        [{"country": "Chile"}, {"country": "Argentina"}],
    )

    kinds = {a["kind"] for a in build.diff_alerts(prev, cur, "2026-08-27T12:00:00Z")}

    assert kinds == {"new_outbreak", "new_cluster"}


def test_update_ledger_seeds_silently_on_first_run():
    ledger, new = build.update_ledger(
        {"alerts": []}, hero_state(), [], [{"country": "Chile"}],
        "2026-08-27T12:00:00Z",
    )

    # A first run must not dump every already-tracked outbreak into the feed.
    assert new == []
    assert ledger["state"]["_countries"] == ["Chile"]


def test_update_ledger_appends_newest_first():
    seeded, _ = build.update_ledger(
        {"alerts": []}, hero_state(cases=1000), [], [], "2026-08-27T12:00:00Z")

    ledger, new = build.update_ledger(
        seeded, hero_state(cases=1400), [], [], "2026-08-27T13:00:00Z")

    assert len(new) == 1
    assert ledger["alerts"][0]["id"] == new[0]["id"]


def test_alert_ids_are_stable_per_event_but_differ_across_events():
    a = build._alert("count_change", "k", "t", "s", "u", "2026-08-27T12:00:00Z", "10")
    same = build._alert("count_change", "k", "t", "s", "u", "2026-08-27T12:00:00Z", "10")
    later = build._alert("count_change", "k", "t", "s", "u", "2026-08-27T13:00:00Z", "10")

    assert a["id"] == same["id"]
    assert a["id"] != later["id"]


def test_load_ledger_falls_back_to_local_file(monkeypatch, tmp_path):
    def boom(url):
        raise build.requests.RequestException("offline")

    monkeypatch.setattr(build, "http_get", boom)
    path = tmp_path / "alerts.json"
    path.write_text('{"alerts": [{"id": "x"}], "state": {}}')

    assert build.load_ledger(path)["alerts"] == [{"id": "x"}]


def test_load_ledger_returns_empty_when_nothing_is_available(monkeypatch, tmp_path):
    def boom(url):
        raise build.requests.RequestException("offline")

    monkeypatch.setattr(build, "http_get", boom)

    ledger = build.load_ledger(tmp_path / "missing.json")

    assert ledger == {"alerts": []}
    assert "state" not in ledger  # -> bootstrap, not a backlog flush


# ----- Push layer: feed + Bluesky -----
def test_render_feed_escapes_and_marks_guids_non_permalink():
    alerts = [{
        "id": "abc123", "kind": "count_change",
        "title": "Cases rise <fast> & far", "summary": "s",
        "url": "https://example.org/x", "created_at": "2026-08-27T12:00:00Z",
    }]

    xml = build.render_feed(alerts, "2026-08-27T12:00:00Z")

    assert "&lt;fast&gt; &amp; far" in xml
    assert '<guid isPermaLink="false">abc123</guid>' in xml
    assert "<pubDate>Thu, 27 Aug 2026 12:00:00 +0000</pubDate>" in xml


def test_render_feed_is_well_formed_xml():
    import xml.etree.ElementTree as ET

    alerts = [{"id": str(i), "kind": "new_cluster", "title": f"t{i}",
               "summary": "s & s", "url": "https://e.org",
               "created_at": "2026-08-27T12:00:00Z"} for i in range(3)]

    root = ET.fromstring(build.render_feed(alerts, "2026-08-27T12:00:00Z"))

    assert len(root.findall("./channel/item")) == 3


def test_render_feed_caps_item_count():
    alerts = [{"id": str(i), "kind": "new_cluster", "title": "t",
               "summary": "s", "url": "https://e.org",
               "created_at": "2026-08-27T12:00:00Z"}
              for i in range(build.ALERT_FEED_LIMIT + 25)]

    xml = build.render_feed(alerts, "2026-08-27T12:00:00Z")

    assert xml.count("<item>") == build.ALERT_FEED_LIMIT


def test_bsky_facets_use_utf8_byte_offsets():
    url = "https://example.org/x"
    text = f"Ebola · Côte d'Ivoire — cases up\n\n{url}"

    facets = build._bsky_facets(text, url)

    start = facets[0]["index"]["byteStart"]
    blob = text.encode("utf-8")
    # Character offsets would land short of this; bytes are what Bluesky indexes.
    assert blob[start:facets[0]["index"]["byteEnd"]].decode() == url


def test_format_bsky_post_trims_long_titles_but_keeps_the_link():
    alert = {"title": "x" * 400, "url": "https://example.org/x"}

    text, facets = build.format_bsky_post(alert)

    assert len(text) < 300
    assert text.endswith("https://example.org/x")
    assert facets


def test_post_alerts_to_bluesky_noops_without_credentials(monkeypatch):
    monkeypatch.setattr(build, "BLUESKY_HANDLE", "")
    monkeypatch.delenv("BLUESKY_APP_PASSWORD", raising=False)

    assert build.post_alerts_to_bluesky([{"id": "a", "posted_bluesky": False}]) == 0


def test_post_alerts_to_bluesky_skips_posted_and_stale_alerts(monkeypatch):
    monkeypatch.setattr(build, "BLUESKY_HANDLE", "bot.example")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "pw")
    calls = []

    class FakeResp:
        ok = True

        def raise_for_status(self):
            pass

        def json(self):
            return {"accessJwt": "jwt", "did": "did:plc:x"}

    def fake_post(url, **kw):
        calls.append((url, kw))
        return FakeResp()

    monkeypatch.setattr(build.SESSION, "post", fake_post)
    stale = (dt.datetime.now(dt.timezone.utc)
             - dt.timedelta(hours=build.BLUESKY_MAX_ALERT_AGE_HOURS + 2))
    alerts = [
        {"id": "done", "title": "t", "url": "https://e.org",
         "created_at": recent_iso(1), "posted_bluesky": True},
        {"id": "old", "title": "t", "url": "https://e.org",
         "created_at": stale.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "posted_bluesky": False},
        {"id": "fresh", "title": "t", "url": "https://e.org",
         "created_at": recent_iso(5), "posted_bluesky": False},
    ]

    assert build.post_alerts_to_bluesky(alerts) == 1
    assert alerts[2]["posted_bluesky"] is True
    assert alerts[1]["posted_bluesky"] is False
    # One login + exactly one createRecord.
    assert sum("createRecord" in c[0] for c in calls) == 1


def test_post_alerts_to_bluesky_caps_posts_per_run(monkeypatch):
    monkeypatch.setattr(build, "BLUESKY_HANDLE", "bot.example")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "pw")

    class FakeResp:
        ok = True

        def raise_for_status(self):
            pass

        def json(self):
            return {"accessJwt": "jwt", "did": "did:plc:x"}

    monkeypatch.setattr(build.SESSION, "post", lambda url, **kw: FakeResp())
    alerts = [{"id": str(i), "title": "t", "url": "https://e.org",
               "created_at": recent_iso(1), "posted_bluesky": False}
              for i in range(10)]

    assert build.post_alerts_to_bluesky(alerts) == build.BLUESKY_MAX_POSTS_PER_RUN


def test_alert_age_human_buckets():
    assert build.alert_age_human(recent_iso(1)) == "just now"
    assert build.alert_age_human(recent_iso(30)) == "30m ago"
    assert build.alert_age_human(recent_iso(180)) == "3h ago"
    assert build.alert_age_human(None) == ""


def test_diff_alerts_fires_on_big_absolute_jump_below_the_pct_gate():
    # 5,000 -> 5,080 is only 1.6%, but +80 cases is material on any base.
    prev = build.snapshot_state(hero_state(cases=5000), [], [])
    cur = build.snapshot_state(hero_state(cases=5080), [], [])

    alerts = build.diff_alerts(prev, cur, "2026-08-27T12:00:00Z")

    assert len(alerts) == 1
    assert "+80" in alerts[0]["title"]


def test_diff_alerts_still_ignores_drift_under_both_gates():
    prev = build.snapshot_state(hero_state(cases=5000), [], [])
    cur = build.snapshot_state(hero_state(cases=5030), [], [])

    assert build.diff_alerts(prev, cur, "2026-08-27T12:00:00Z") == []
