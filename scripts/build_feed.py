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
USER_AGENT = "FootyTimerFeed/1.0 (+https://footytimer.joshloredo.com)"

CHANNEL_NAMES = {"USA Net": "USA Network", "Tele": "Telemundo", "CBSSN": "CBS Sports Network"}
KNOWN_CHANNELS = {"NBC", "Peacock", "USA Network", "Telemundo", "Universo", "NBCSN", "CNBC",
                  "Paramount+", "CBS", "CBS Sports Network", "TUDN", "UniMás", "ViX"}
ALIASES: Dict[str, str] = {}  # normalised ESPN club name -> normalised football-data name, only when they differ

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
