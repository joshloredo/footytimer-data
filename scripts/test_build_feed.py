import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_feed as bf  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def utc(text):
    return bf.parse_utc(text)


PL = bf.parse_fd(load("fd_pl.json"), "PL")
CL = bf.parse_fd(load("fd_cl.json"), "CL")


def fixture_id(fixtures, label, day):
    return next(f["id"] for f in fixtures if f["label"] == label and f["utc"].strftime("%Y%m%d") == day)


class MatchingTests(unittest.TestCase):
    def test_normalize_strips_club_suffixes_and_accents(self):
        self.assertEqual(bf.normalize("Brighton & Hove Albion FC"), bf.normalize("Brighton & Hove Albion"))
        self.assertEqual(bf.normalize("AFC Bournemouth"), "bournemouth")
        self.assertEqual(bf.normalize("Atlético Madrid"), "atletico madrid")

    def test_display_channels_maps_dedupes_caps_and_flags(self):
        shown, unknown = bf.display_channels(["USA Net", "Tele", "USA Net", "Peacock", "Kanal 7"])
        self.assertEqual(shown, ["USA Network", "Telemundo", "Peacock"])
        self.assertEqual(unknown, ["Kanal 7"])

    def test_assigned_premier_league_day(self):
        matched, unmatched = bf.match_events(PL, bf.parse_espn(load("espn_eng1_20260919.json")))
        self.assertEqual(unmatched, [])
        self.assertEqual(matched[fixture_id(PL, "TOT v AVL", "20260919")], ["USA Net", "Universo"])
        self.assertEqual(matched[fixture_id(PL, "EVE v IPS", "20260919")], ["Peacock"])

    def test_manchester_clubs_resolve(self):
        matched, _ = bf.match_events(PL, bf.parse_espn(load("espn_eng1_20260920.json")))
        self.assertEqual(matched[fixture_id(PL, "FUL v MUN", "20260920")], ["NBC", "Tele"])
        self.assertEqual(matched[fixture_id(PL, "MCI v SUN", "20260920")], ["Peacock"])

    def test_unassigned_day_has_no_listings(self):
        matched, unmatched = bf.match_events(PL, bf.parse_espn(load("espn_eng1_20261010.json")))
        self.assertEqual((matched, unmatched), ({}, []))

    def test_champions_league_on_paramount_plus(self):
        matched, _ = bf.match_events(CL, bf.parse_espn(load("espn_cl_20261013.json")))
        self.assertEqual(matched[fixture_id(CL, "ARS v LIL", "20261013")], ["Paramount+"])

    def test_kickoff_mismatch_is_unmatched_not_guessed(self):
        fixture = {"id": 1, "comp": "PL", "utc": utc("2026-10-10T14:00:00Z"), "status": "TIMED",
                   "home": {"chelsea"}, "away": {"bournemouth"}, "label": "CHE v BOU"}
        event = {"utc": utc("2026-10-10T16:30:00Z"), "home": "chelsea", "away": "bournemouth",
                 "channels": ["NBC"], "name": "BOU @ CHE"}
        self.assertEqual(bf.match_events([fixture], [event]), ({}, ["BOU @ CHE"]))

    def test_same_kickoff_minute_resolves_by_club(self):
        t = utc("2026-10-10T14:00:00Z")
        a = {"id": 1, "comp": "PL", "utc": t, "status": "TIMED", "home": {"chelsea"}, "away": {"bournemouth"}, "label": "CHE v BOU"}
        b = {"id": 2, "comp": "PL", "utc": t, "status": "TIMED", "home": {"sunderland"},
             "away": {"brighton and hove albion"}, "label": "SUN v BHA"}
        event = {"utc": t, "home": "sunderland", "away": "brighton and hove albion", "channels": ["USA Net"], "name": "BHA @ SUN"}
        self.assertEqual(bf.match_events([a, b], [event]), ({2: ["USA Net"]}, []))


class FeedTests(unittest.TestCase):
    NOW = utc("2026-09-18T07:05:00Z")

    def espn_day(self, name):
        return bf.parse_espn(load(name))

    def test_build_feed_from_real_days(self):
        espn = {("PL", "20260919"): self.espn_day("espn_eng1_20260919.json"),
                ("PL", "20260920"): self.espn_day("espn_eng1_20260920.json")}
        feed, stats = bf.build_feed(PL, espn, None, self.NOW)
        self.assertEqual(feed["matches"][str(fixture_id(PL, "TOT v AVL", "20260919"))],
                         {"channels": ["USA Network", "Universo"], "source": "espn"})
        self.assertEqual((feed["version"], feed["region"], feed["generatedAt"]), (1, "US", "2026-09-18T07:05:00Z"))
        self.assertEqual(stats["failed_dates"], [])
        bf.validate(feed, stats, dates_queried=2, fixtures_in_window=9)

    def test_failed_date_keeps_last_nights_listing(self):
        tot = str(fixture_id(PL, "TOT v AVL", "20260919"))
        previous = {"version": 1, "region": "US", "generatedAt": "2026-09-17T07:05:00Z",
                    "matches": {tot: {"channels": ["USA Network"], "source": "espn"}}}
        espn = {("PL", "20260919"): None, ("PL", "20260920"): self.espn_day("espn_eng1_20260920.json")}
        feed, stats = bf.build_feed(PL, espn, previous, self.NOW)
        self.assertEqual(feed["matches"][tot], {"channels": ["USA Network"], "source": "espn"})
        self.assertEqual(stats["failed_dates"], ["PL 20260919"])

    def test_every_date_failing_blocks_publish(self):
        feed, stats = bf.build_feed(PL, {("PL", "20260919"): None}, None, self.NOW)
        with self.assertRaises(bf.FeedError):
            bf.validate(feed, stats, dates_queried=1, fixtures_in_window=5)

    def test_espn_returning_no_events_blocks_publish(self):
        feed, stats = bf.build_feed(PL, {("PL", "20260919"): []}, None, self.NOW)
        with self.assertRaises(bf.FeedError):
            bf.validate(feed, stats, dates_queried=1, fixtures_in_window=5)

    def test_offseason_publishes_an_empty_feed(self):
        feed, stats = bf.build_feed([], {}, {"matches": {"1": {"channels": ["NBC"], "source": "espn"}}}, self.NOW)
        bf.validate(feed, stats, dates_queried=0, fixtures_in_window=0)
        self.assertEqual(feed["matches"], {})

    def test_unassigned_window_publishes_without_listings(self):
        feed, stats = bf.build_feed(PL, {("PL", "20261010"): self.espn_day("espn_eng1_20261010.json")}, None, self.NOW)
        bf.validate(feed, stats, dates_queried=1, fixtures_in_window=6)
        self.assertEqual(feed["matches"], {})

    def test_malformed_entry_is_rejected(self):
        feed = {"version": 1, "region": "US", "generatedAt": "2026-09-18T07:05:00Z", "matches": {"12": {"channels": []}}}
        with self.assertRaises(bf.FeedError):
            bf.validate(feed, {"failed_dates": [], "events": 1}, dates_queried=1, fixtures_in_window=1)


if __name__ == "__main__":
    unittest.main()
