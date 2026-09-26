import json
import os
import subprocess
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


def fake_fetch(url, headers):
    """Recorded responses for a 2026-09-18 run; any other ESPN date has no events."""
    if "competitions/PL/matches" in url:
        return load("fd_pl.json")
    if "competitions/CL/matches" in url:
        return {"matches": []}
    for day in ("20260919", "20260920"):
        if f"eng.1/scoreboard?dates={day}" in url:
            return load(f"espn_eng1_{day}.json")
    return {"events": []}


class MatchingTests(unittest.TestCase):
    def test_normalize_strips_club_suffixes_and_accents(self):
        self.assertEqual(bf.normalize("Brighton & Hove Albion FC"), bf.normalize("Brighton & Hove Albion"))
        self.assertEqual(bf.normalize("AFC Bournemouth"), "bournemouth")
        self.assertEqual(bf.normalize("Málaga CF"), "malaga")

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

    def test_aliases_join_espn_and_football_data_names(self):
        pairs = [
            ("AEK Athens", "PAE AEK"),
            ("Atlético Madrid", "Club Atlético de Madrid"),
            ("Bayern Munich", "FC Bayern München"),
            ("Bodo/Glimt", "FK Bodø/Glimt"),
            ("Como", "Como 1907"),
            ("Internazionale", "FC Internazionale Milano"),
            ("Lens", "Racing Club de Lens"),
            ("PSV Eindhoven", "PSV"),
            ("Shakhtar Donetsk", "FK Shakhtar Donetsk"),
            ("Slavia Prague", "SK Slavia Praha"),
            ("Slovan Bratislava", "ŠK Slovan Bratislava"),
        ]
        for espn_name, fd_name in pairs:
            with self.subTest(espn_name=espn_name):
                self.assertEqual(bf.normalize(espn_name), bf.normalize(fd_name))

    def test_both_sides_renamed_still_matches_via_alias(self):
        t = utc("2026-10-14T19:00:00Z")
        shd = {"id": 7, "comp": "CL", "utc": t, "status": "TIMED",
               "home": {bf.normalize("FK Shakhtar Donetsk"), bf.normalize("Shaktar")},
               "away": {bf.normalize("PAE AEK")}, "label": "SHD v AEK"}
        other = {"id": 8, "comp": "CL", "utc": t, "status": "TIMED",
                 "home": {bf.normalize("AS Roma")}, "away": {bf.normalize("Real Madrid CF")}, "label": "ROM v RMA"}
        event = {"utc": t, "home": bf.normalize("Shakhtar Donetsk"), "away": bf.normalize("AEK Athens"),
                 "channels": ["Paramount+"], "name": "AEK @ SHK"}
        self.assertEqual(bf.match_events([shd, other], [event]), ({7: ["Paramount+"]}, []))


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


class ReportTests(unittest.TestCase):
    NOW = utc("2026-10-01T07:05:00Z")

    def snap(self, **changes):
        base = {"utc": "2026-10-10T11:30:00Z", "status": "TIMED", "label": "ARS v LEE"}
        base.update(changes)
        return base

    def test_first_night_is_silent(self):
        self.assertEqual(bf.diff_fixtures({}, {"1": self.snap()}, self.NOW), [])

    def test_moved_new_postponed_and_removed(self):
        previous = {"1": self.snap(), "2": self.snap(label="CHE v BOU", utc="2026-10-10T14:00:00Z"),
                    "3": self.snap(label="SUN v BHA", utc="2026-10-10T14:00:00Z")}
        current = {"1": self.snap(utc="2026-10-11T15:30:00Z"),
                   "2": self.snap(label="CHE v BOU", utc="2026-10-10T14:00:00Z", status="POSTPONED"),
                   "4": self.snap(label="LIV v MCI", utc="2026-10-11T15:30:00Z")}
        self.assertEqual(bf.diff_fixtures(previous, current, self.NOW),
                         ["CHE v BOU postponed", "ARS v LEE → Sun 11:30", "new: LIV v MCI Sun 11:30", "removed: SUN v BHA"])

    def test_status_line_ok(self):
        feed = {"matches": {str(i): {} for i in range(21)}}
        stats = {"failed_dates": [], "unmatched": [], "unknown": []}
        self.assertEqual(bf.status_line(feed, stats, 38, ["ARS v LEE → Sun 11:30", "new: LIV v MCI Sun 11:30"]),
                         "⚽ FootyTimer ✓ 38 fixtures · 21 on TV · 17 TBD · 2 changed: ARS v LEE → Sun 11:30; new: LIV v MCI Sun 11:30")

    def test_status_line_partial_with_flags(self):
        feed = {"matches": {str(i): {} for i in range(19)}}
        stats = {"failed_dates": ["PL 20261010", "PL 20261011"], "unmatched": ["BOU @ CHE"], "unknown": ["Kanal 7"]}
        self.assertEqual(bf.status_line(feed, stats, 38, []),
                         "⚽ FootyTimer ⚠️ ESPN failed for 2 dates (kept last known) · 38 fixtures · 19 on TV · 1 unmatched · new channel: Kanal 7")

    def test_failure_line_reports_feed_age(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "state").mkdir()
            self.assertEqual(bf.fail(root, False, "football-data failed (HTTPError: HTTP Error 503)",
                                     {"generatedAt": "2026-09-30T07:05:00Z"}, self.NOW), 1)
            self.assertEqual((root / "state" / "status.line").read_text(encoding="utf-8").strip(),
                             "⚽ FootyTimer ❌ football-data failed (HTTPError: HTTP Error 503), kept last good feed (1 day old)")


class ScheduleGuardTests(unittest.TestCase):
    def ran_at(self, root, when):
        (root / "state").mkdir(exist_ok=True)
        (root / "state" / "last-success").write_text(when + "\n", encoding="utf-8")

    def test_reboot_after_tonights_run_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.ran_at(root, "2026-10-01T07:06:00Z")                             # 03:06 EDT
            self.assertTrue(bf.already_ran(root, utc("2026-10-01T18:00:00Z")))   # 14:00 EDT reboot

    def test_next_scheduled_run_is_not_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.ran_at(root, "2026-10-01T07:06:00Z")
            self.assertFalse(bf.already_ran(root, utc("2026-10-02T07:05:00Z")))  # next night 03:05 EDT

    def test_boundary_across_dst_fall_back(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.ran_at(root, "2026-10-31T07:06:00Z")                             # Sat 03:06 EDT
            self.assertTrue(bf.already_ran(root, utc("2026-11-01T07:30:00Z")))   # 02:30 EST, before 03:05
            self.assertFalse(bf.already_ran(root, utc("2026-11-01T08:05:00Z")))  # 03:05 EST

    def test_lock_blocks_a_second_run_and_expires(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "state").mkdir()
            self.assertTrue(bf.take_lock(root))
            self.assertFalse(bf.take_lock(root))
            old = time.time() - 3 * 3600
            os.utime(root / "state" / "lock", (old, old))
            self.assertTrue(bf.take_lock(root))


class DryRunTests(unittest.TestCase):
    def test_dry_run_builds_feed_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "docs").mkdir()
            (root / ".env").write_text("FOOTBALL_DATA_API_KEY=test\n", encoding="utf-8")
            code = bf.main(["--dry-run"], root=root, fetch=fake_fetch,
                           now=utc("2026-09-18T07:05:00Z"), sleep=lambda seconds: None)
            self.assertEqual(code, 0)
            self.assertFalse((root / "docs" / "broadcasts.json").exists())
            self.assertFalse((root / "state" / "status.line").exists())
            self.assertFalse((root / "state" / "lock").exists())


class PublishTests(unittest.TestCase):
    NOW = utc("2026-09-18T07:05:00Z")

    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()

    def configure(self, repo):
        for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
            self.git(repo, "config", key, value)

    def make_clone(self, d):
        """A bare origin plus a working clone laid out like the mini's ~/footytimer-data."""
        origin, work = Path(d) / "origin.git", Path(d) / "work"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
        self.configure(work)
        (work / "docs").mkdir()
        (work / "docs" / "CNAME").write_text("footytimer.joshloredo.com\n", encoding="utf-8")
        (work / ".gitignore").write_text(".env\nstate/\nlogs/\n", encoding="utf-8")
        (work / ".env").write_text("FOOTBALL_DATA_API_KEY=test\n", encoding="utf-8")
        self.git(work, "add", "-A")
        self.git(work, "commit", "-q", "-m", "init")
        self.git(work, "remote", "add", "origin", str(origin))
        self.git(work, "push", "-q", "origin", "HEAD:main")
        return origin, work

    def run_job(self, work):
        return bf.main([], root=work, fetch=fake_fetch, now=self.NOW, sleep=lambda seconds: None)

    def test_publishes_feed_and_records_success(self):
        with tempfile.TemporaryDirectory() as d:
            origin, work = self.make_clone(d)
            self.assertEqual(self.run_job(work), 0)
            self.assertEqual(self.git(origin, "log", "-1", "--format=%s", "main"), "feed: 2026-09-18")
            published = json.loads(self.git(origin, "show", "main:docs/broadcasts.json"))
            self.assertEqual(published["generatedAt"], "2026-09-18T07:05:00Z")
            self.assertTrue(published["matches"])
            self.assertEqual((work / "state" / "last-success").read_text(encoding="utf-8").strip(), "2026-09-18T07:05:00Z")
            self.assertTrue((work / "state" / "status.line").read_text(encoding="utf-8").startswith("⚽ FootyTimer ✓"))
            self.assertTrue((work / "state" / "fixtures-last.json").exists())

    def test_picks_up_commits_pushed_from_elsewhere(self):
        with tempfile.TemporaryDirectory() as d:
            origin, work = self.make_clone(d)
            other = Path(d) / "other"
            subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
            self.configure(other)
            (other / "README.md").write_text("alias update\n", encoding="utf-8")
            self.git(other, "add", "README.md")
            self.git(other, "commit", "-q", "-m", "update aliases")
            self.git(other, "push", "-q", "origin", "HEAD:main")
            self.assertEqual(self.run_job(work), 0)
            self.assertEqual(self.git(origin, "log", "-2", "--format=%s", "main").splitlines(),
                             ["feed: 2026-09-18", "update aliases"])

    def test_push_failure_keeps_file_and_reports(self):
        with tempfile.TemporaryDirectory() as d:
            origin, work = self.make_clone(d)
            self.git(work, "remote", "set-url", "origin", str(Path(d) / "missing.git"))
            self.assertEqual(self.run_job(work), 1)
            self.assertTrue((work / "docs" / "broadcasts.json").exists())
            self.assertIn("push failed", (work / "state" / "status.line").read_text(encoding="utf-8"))
            self.assertFalse((work / "state" / "last-success").exists())


if __name__ == "__main__":
    unittest.main()
