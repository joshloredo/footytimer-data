#!/usr/bin/env python3
"""FootyTimer broadcast feed (spec: footytimer docs/plans/2026-09-26-broadcast-feed-design.md).

Nightly on the Mac mini: football-data.org PL/CL fixtures + ESPN's US listings -> docs/broadcasts.json.
A match ESPN hasn't assigned gets no entry and the app shows "TBD" -- nothing is ever guessed.
Standard library only; runs under /usr/bin/python3 (3.9).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
COMPETITIONS = {"PL": "eng.1", "CL": "uefa.champions"}  # football-data code -> ESPN league slug
FD_WINDOW = timedelta(days=90)
ESPN_WINDOW = timedelta(days=21)
FD_RETRY_DELAYS = (10, 30, 90)  # first try + 3 retries
ESPN_RETRY_DELAYS = (5,)        # first try + 1 retry
STALE_LOCK = timedelta(hours=2)
LOG_MAX_BYTES = 1_000_000

CHANNEL_NAMES = {"USA Net": "USA Network", "Tele": "Telemundo", "CBSSN": "CBS Sports Network"}
KNOWN_CHANNELS = {"NBC", "Peacock", "USA Network", "Telemundo", "Universo", "NBCSN", "CNBC",
                  "Paramount+", "CBS", "CBS Sports Network", "TUDN", "UniMás", "ViX"}
ALIASES: Dict[str, str] = {  # normalised ESPN club name -> normalised football-data name, only when they differ
    # found by scanning PL/CL fixtures against ESPN on 2026-09-26
    "aek athens": "pae aek",
    "atletico madrid": "club atletico de madrid",
    "bayern munich": "bayern munchen",
    "bodo glimt": "fk bod glimt",
    "como": "como 1907",
    "internazionale": "internazionale milano",
    "lens": "racing club de lens",
    "psv eindhoven": "psv",
    "shakhtar donetsk": "fk shakhtar donetsk",
    "slavia prague": "sk slavia praha",
    "slovan bratislava": "sk slovan bratislava",
}

Fetch = Callable[[str, Dict[str, str]], dict]


class FeedError(Exception):
    """The new feed must not be published."""


# ---------- parsing and matching (pure) ----------

def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def normalize(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().replace("&", " and ")
    words = [w for w in re.sub(r"[^a-z0-9]+", " ", text).split() if w not in {"fc", "afc", "cf", "sc"}]
    joined = " ".join(words)
    return ALIASES.get(joined, joined)


def parse_fd(payload: dict, comp: str) -> List[dict]:
    fixtures = []
    for m in payload.get("matches", []):
        try:
            home, away = m["homeTeam"], m["awayTeam"]
            fixture = {
                "id": int(m["id"]),
                "comp": comp,
                "utc": parse_utc(m["utcDate"]),
                "status": m.get("status") or "",
                "home": {normalize(n) for n in (home.get("name"), home.get("shortName")) if n},
                "away": {normalize(n) for n in (away.get("name"), away.get("shortName")) if n},
                "label": f"{home.get('tla') or '?'} v {away.get('tla') or '?'}",
            }
        except (KeyError, TypeError, ValueError, AttributeError):
            continue  # e.g. knockout ties whose teams aren't known yet
        if fixture["home"] and fixture["away"]:
            fixtures.append(fixture)
    return fixtures


def parse_espn(payload: dict) -> List[dict]:
    events = []
    for event in payload.get("events", []):
        try:
            comp = event["competitions"][0]
            clubs = {c["homeAway"]: normalize(c["team"]["displayName"]) for c in comp["competitors"]}
            channels = [n for b in comp.get("broadcasts", []) if b.get("market") == "national" for n in b.get("names", [])]
            events.append({"utc": parse_utc(event["date"]), "home": clubs["home"], "away": clubs["away"],
                           "channels": channels, "name": event.get("shortName", "?")})
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return events


def match_events(fixtures: List[dict], events: List[dict]) -> Tuple[Dict[int, List[str]], List[str]]:
    """({fixture id: raw ESPN channel names}, [unmatched ESPN event names]).

    Never guesses: the kickoff minute must be identical and at least one club must agree on the
    same side, with exactly one candidate fixture. Anything else is reported as unmatched.
    """
    matched: Dict[int, List[str]] = {}
    unmatched: List[str] = []
    for event in events:
        candidates = [f for f in fixtures if f["utc"] == event["utc"]
                      and (event["home"] in f["home"] or event["away"] in f["away"])]
        if len(candidates) != 1:
            unmatched.append(event["name"])
        elif event["channels"]:
            matched[candidates[0]["id"]] = event["channels"]
    return matched, unmatched


def display_channels(raw: List[str]) -> Tuple[List[str], List[str]]:
    """(up to 3 display names in ESPN's order, names not in KNOWN_CHANNELS)."""
    shown: List[str] = []
    unknown: List[str] = []
    for name in raw:
        label = CHANNEL_NAMES.get(name.strip(), name.strip())
        if label and label not in shown:
            shown.append(label)
            if label not in KNOWN_CHANNELS:
                unknown.append(label)
    return shown[:3], unknown


# ---------- building and validating the feed (pure) ----------

def build_feed(fixtures: List[dict], espn: Dict[Tuple[str, str], Optional[List[dict]]],
               previous: Optional[dict], now: datetime) -> Tuple[dict, dict]:
    """espn maps (competition, YYYYMMDD) -> parsed events, or None when that date's request failed."""
    prev_matches = (previous or {}).get("matches", {})
    entries: Dict[str, dict] = {}
    stats: dict = {"unmatched": [], "unknown": [], "failed_dates": [], "events": 0}
    for (comp, day), events in sorted(espn.items()):
        day_fixtures = [f for f in fixtures if f["comp"] == comp and f["utc"].strftime("%Y%m%d") == day]
        if events is None:
            stats["failed_dates"].append(f"{comp} {day}")
            for f in day_fixtures:  # keep last night's listing for this date
                if str(f["id"]) in prev_matches:
                    entries[str(f["id"])] = prev_matches[str(f["id"])]
            continue
        stats["events"] += len(events)
        matched, unmatched = match_events(day_fixtures, events)
        stats["unmatched"] += unmatched
        for fid, raw in matched.items():
            channels, unknown = display_channels(raw)
            stats["unknown"] += [u for u in unknown if u not in stats["unknown"]]
            if channels:
                entries[str(fid)] = {"channels": channels, "source": "espn"}
    feed = {
        "version": 1,
        "region": "US",
        "generatedAt": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "matches": dict(sorted(entries.items(), key=lambda kv: int(kv[0]))),
    }
    return feed, stats


def validate(feed: dict, stats: dict, dates_queried: int, fixtures_in_window: int) -> None:
    if feed.get("version") != 1 or feed.get("region") != "US":
        raise FeedError("bad feed header")
    for mid, entry in feed.get("matches", {}).items():
        channels = entry.get("channels")
        if (not mid.isdigit() or not isinstance(channels, list) or not 1 <= len(channels) <= 3
                or not all(isinstance(c, str) and c for c in channels)):
            raise FeedError(f"bad entry for match {mid}")
    if dates_queried and len(stats["failed_dates"]) == dates_queried:
        raise FeedError("ESPN failed for every date")
    if fixtures_in_window and dates_queried and stats["events"] == 0:
        raise FeedError("ESPN returned no events for dates with fixtures")


# ---------- reporting ----------

def snapshot(fixtures: List[dict]) -> Dict[str, dict]:
    return {str(f["id"]): {"utc": f["utc"].strftime("%Y-%m-%dT%H:%M:%SZ"), "status": f["status"], "label": f["label"]}
            for f in fixtures}


def fmt_et(when: datetime) -> str:
    return when.astimezone(ET).strftime("%a %H:%M")


def diff_fixtures(previous: Dict[str, dict], current: Dict[str, dict], now: datetime) -> List[str]:
    """Changes in the next 21 days since last night; silent on the first night."""
    if not previous:
        return []
    horizon = now + ESPN_WINDOW
    changes: List[str] = []
    for fid, cur in sorted(current.items(), key=lambda kv: kv[1]["utc"]):
        when = parse_utc(cur["utc"])
        if not now <= when <= horizon:
            continue
        old = previous.get(fid)
        if old is None:
            changes.append(f"new: {cur['label']} {fmt_et(when)}")
        elif old["utc"] != cur["utc"]:
            changes.append(f"{cur['label']} → {fmt_et(when)}")
        elif old["status"] != cur["status"] and cur["status"] in {"POSTPONED", "CANCELLED", "SUSPENDED"}:
            changes.append(f"{cur['label']} {cur['status'].lower()}")
    for fid, old in sorted(previous.items(), key=lambda kv: kv[1]["utc"]):
        if fid not in current and now <= parse_utc(old["utc"]) <= horizon:
            changes.append(f"removed: {old['label']}")
    return changes


def status_line(feed: dict, stats: dict, fixtures_in_window: int, changes: List[str]) -> str:
    on_tv = len(feed["matches"])
    if stats["failed_dates"]:
        count = len(stats["failed_dates"])
        line = (f"⚽ FootyTimer ⚠️ ESPN failed for {count} date{'s' if count != 1 else ''} (kept last known)"
                f" · {fixtures_in_window} fixtures · {on_tv} on TV")
    else:
        line = f"⚽ FootyTimer ✓ {fixtures_in_window} fixtures · {on_tv} on TV · {max(fixtures_in_window - on_tv, 0)} TBD"
    if changes:
        line += f" · {len(changes)} changed: " + "; ".join(changes[:3])
    if stats["unmatched"]:
        line += f" · {len(stats['unmatched'])} unmatched"
    if stats["unknown"]:
        line += " · new channel: " + ", ".join(stats["unknown"])
    return line


def fail(root: Path, dry_run: bool, reason: str, previous: Optional[dict], now: datetime) -> int:
    age = "none published yet"
    if previous and previous.get("generatedAt"):
        days = (now - parse_utc(previous["generatedAt"])).days
        age = f"{days} day{'s' if days != 1 else ''} old"
    line = f"⚽ FootyTimer ❌ {reason}, kept last good feed ({age})"
    if not dry_run:
        (root / "state" / "status.line").write_text(line + "\n", encoding="utf-8")
    print(f"{now:%Y-%m-%d %H:%M:%S}Z {line}")
    return 1

# ---------- I/O ----------

def http_json(url: str, headers: Dict[str, str]) -> dict:
    # ESPN's CDN answers unfamiliar User-Agents with 403; urllib's own default identity is accepted
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def with_retries(call: Callable[[], dict], delays, sleep) -> dict:
    for delay in delays:
        try:
            return call()
        except Exception:
            sleep(delay)
    return call()


def fd_url(code: str, now: datetime) -> str:
    return (f"https://api.football-data.org/v4/competitions/{code}/matches"
            f"?dateFrom={now:%Y-%m-%d}&dateTo={now + FD_WINDOW:%Y-%m-%d}")


def espn_url(comp: str, day: str) -> str:
    return f"https://site.api.espn.com/apis/site/v2/sports/soccer/{COMPETITIONS[comp]}/scoreboard?dates={day}"


def read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_env(path: Path) -> Dict[str, str]:
    pairs = (line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
             if "=" in line and not line.lstrip().startswith("#"))
    return {key.strip(): value.strip() for key, value in pairs}


def trim_log(path: Path) -> None:
    try:
        if path.stat().st_size > LOG_MAX_BYTES:
            path.write_bytes(path.read_bytes()[-LOG_MAX_BYTES:])
    except OSError:
        pass


def last_scheduled_run(now: datetime) -> datetime:
    """The most recent 03:05 America/New_York at or before now, in UTC."""
    local = now.astimezone(ET)
    boundary = local.replace(hour=3, minute=5, second=0, microsecond=0)
    if boundary > local:
        boundary -= timedelta(days=1)
    return boundary.astimezone(timezone.utc)


def already_ran(root: Path, now: datetime) -> bool:
    """True if a run already succeeded since the last 03:05 ET, so a reboot's catch-up is skipped."""
    try:
        last = parse_utc((root / "state" / "last-success").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return last >= last_scheduled_run(now)


def take_lock(root: Path) -> bool:
    lock = root / "state" / "lock"
    try:
        lock.mkdir()
        return True
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < STALE_LOCK.total_seconds():
            return False
        lock.rmdir()  # left behind by a crashed run
        lock.mkdir()
        return True


def publish(root: Path, now: datetime) -> None:
    git = ["git", "-C", str(root)]
    # Fast-forward to commits pushed from elsewhere (alias or code fixes) so our push stays a fast-forward.
    # Best effort: when offline or diverged, the push below fails and reports it.
    subprocess.run(git + ["pull", "-q", "--ff-only", "origin", "main"], capture_output=True)
    subprocess.run(git + ["add", "docs/broadcasts.json"], check=True, capture_output=True)
    subprocess.run(git + ["commit", "-q", "-m", f"feed: {now.astimezone(ET):%Y-%m-%d}"], check=True, capture_output=True)
    subprocess.run(git + ["push", "-q", "origin", "HEAD:main"], check=True, capture_output=True)


def run(root: Path, dry_run: bool, fetch: Fetch, now: datetime, sleep) -> int:
    feed_path = root / "docs" / "broadcasts.json"
    previous = read_json(feed_path)
    try:
        key = read_env(root / ".env")["FOOTBALL_DATA_API_KEY"]
        fixtures: List[dict] = []
        for code in COMPETITIONS:
            payload = with_retries(lambda: fetch(fd_url(code, now), {"X-Auth-Token": key}), FD_RETRY_DELAYS, sleep)
            fixtures += parse_fd(payload, code)
    except Exception as exc:  # network, HTTP, missing .env/key
        return fail(root, dry_run, f"football-data failed ({type(exc).__name__}: {exc})", previous, now)

    window = [f for f in fixtures if now - timedelta(hours=3) <= f["utc"] <= now + ESPN_WINDOW]
    espn: Dict[Tuple[str, str], Optional[List[dict]]] = {}
    for comp, day in sorted({(f["comp"], f["utc"].strftime("%Y%m%d")) for f in window}):
        try:
            espn[(comp, day)] = parse_espn(with_retries(lambda: fetch(espn_url(comp, day), {}), ESPN_RETRY_DELAYS, sleep))
        except Exception:
            espn[(comp, day)] = None
        sleep(1)

    feed, stats = build_feed(fixtures, espn, previous, now)
    try:
        validate(feed, stats, dates_queried=len(espn), fixtures_in_window=len(window))
    except FeedError as exc:
        return fail(root, dry_run, str(exc), previous, now)

    current = snapshot(fixtures)
    changes = diff_fixtures(read_json(root / "state" / "fixtures-last.json") or {}, current, now)
    line = status_line(feed, stats, len(window), changes)
    if dry_run:
        print(json.dumps(feed, indent=2, ensure_ascii=False))
        print(line)
        return 0

    write_json_atomic(feed_path, feed)
    try:
        publish(root, now)
    except subprocess.CalledProcessError as exc:
        return fail(root, False, f"push failed (git exit {exc.returncode})", previous, now)
    write_json_atomic(root / "state" / "fixtures-last.json", current)
    (root / "state" / "last-success").write_text(now.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n", encoding="utf-8")
    (root / "state" / "status.line").write_text(line + "\n", encoding="utf-8")
    print(f"{now:%Y-%m-%d %H:%M:%S}Z {line}")
    return 0


def main(argv: List[str], root: Path = Path(__file__).resolve().parent.parent, fetch: Fetch = http_json,
         now: Optional[datetime] = None, sleep=time.sleep) -> int:
    dry_run = "--dry-run" in argv
    now = now or datetime.now(timezone.utc)
    (root / "state").mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    trim_log(root / "logs" / "feed.log")
    if not dry_run and already_ran(root, now):
        print(f"{now:%Y-%m-%d %H:%M:%S}Z skip: already published since the last 03:05 ET run")
        return 0
    if not take_lock(root):
        print(f"{now:%Y-%m-%d %H:%M:%S}Z skip: another run holds the lock")
        return 0
    try:
        return run(root, dry_run, fetch, now, sleep)
    finally:
        (root / "state" / "lock").rmdir()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
