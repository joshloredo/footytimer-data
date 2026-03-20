#!/usr/bin/env python3
"""
Fetch upcoming FA Cup and Carabao Cup fixtures involving Premier League teams
from SofaScore, map to football-data.org IDs, and output as static JSON.

Output: docs/supplemental-fixtures.json (served by GitHub Pages)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

SCRIPT_DIR = Path(__file__).parent
OUTPUT_DIR = SCRIPT_DIR.parent / "docs"
OUTPUT_FILE = OUTPUT_DIR / "supplemental-fixtures.json"
TEAM_MAPPING_FILE = SCRIPT_DIR / "team_mapping.json"

# SofaScore tournament IDs
TOURNAMENTS = {
    19: {"fd_id": 2055, "name": "FA Cup", "code": "FAC"},
    21: {"fd_id": 2139, "name": "EFL Cup", "code": "ELC"},
}

# SofaScore API endpoints
SOFASCORE_BASE = "https://api.sofascore.com/api/v1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}


def load_team_mapping() -> dict[str, dict]:
    """Load SofaScore ID -> FD ID mapping."""
    with open(TEAM_MAPPING_FILE) as f:
        raw = json.load(f)
    # Remove comment keys
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def fetch_json(url: str) -> dict | None:
    """Fetch JSON from a URL with error handling."""
    try:
        req = Request(url, headers=HEADERS)
        with urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                print(f"  HTTP {resp.status} for {url}", file=sys.stderr)
                return None
            return json.loads(resp.read())
    except (URLError, json.JSONDecodeError, TimeoutError) as e:
        print(f"  Error fetching {url}: {e}", file=sys.stderr)
        return None


def get_current_season_id(tournament_id: int) -> int | None:
    """Get the current season ID for a tournament."""
    url = f"{SOFASCORE_BASE}/unique-tournament/{tournament_id}/seasons"
    data = fetch_json(url)
    if not data or "seasons" not in data:
        return None
    # Seasons are ordered most recent first — just use the latest
    return data["seasons"][0]["id"] if data["seasons"] else None


def generate_match_id(home_id: int, away_id: int, comp_code: str, date_str: str) -> int:
    """Generate a deterministic match ID in the 900000+ range."""
    raw = f"{home_id}-{away_id}-{comp_code}-{date_str}"
    h = int(hashlib.sha256(raw.encode()).hexdigest(), 16)
    return 900000 + (h % 100000)


def map_team(ss_team: dict, team_mapping: dict) -> dict:
    """Map a SofaScore team to our output format."""
    ss_id = str(ss_team["id"])

    if ss_id in team_mapping:
        fd = team_mapping[ss_id]
        fd_id = fd["fd_id"]
        return {
            "id": fd_id,
            "name": fd["name"],
            "shortName": ss_team.get("shortName", ss_team["name"]),
            "tla": ss_team.get("nameCode", ss_team["name"][:3].upper()),
            "crest": f"https://crests.football-data.org/{fd_id}.png",
        }
    else:
        # Lower-league team — use SofaScore data directly
        return {
            "id": ss_team["id"],
            "name": ss_team["name"],
            "shortName": ss_team.get("shortName", ss_team["name"]),
            "tla": ss_team.get("nameCode", ss_team["name"][:3].upper()),
            "crest": f"https://api.sofascore.app/api/v1/team/{ss_team['id']}/image",
        }


def involves_pl_team(event: dict, team_mapping: dict) -> bool:
    """Check if either team in the event is a mapped PL team."""
    home_id = str(event.get("homeTeam", {}).get("id", ""))
    away_id = str(event.get("awayTeam", {}).get("id", ""))
    return home_id in team_mapping or away_id in team_mapping


def sofascore_status_to_fd(status_code: int) -> str:
    """Map SofaScore status codes to football-data.org status strings."""
    # SofaScore: 0=not started, 6=1st half, 7=2nd half, 100=finished, etc.
    mapping = {
        0: "SCHEDULED",
        60: "POSTPONED",
        70: "CANCELLED",
        100: "FINISHED",
    }
    if status_code in mapping:
        return mapping[status_code]
    if 6 <= status_code <= 50:
        return "IN_PLAY"
    return "SCHEDULED"


def fetch_tournament_fixtures(
    tournament_id: int, comp_info: dict, team_mapping: dict
) -> list[dict]:
    """Fetch fixtures for a tournament, filter to PL teams, and map to output format."""
    season_id = get_current_season_id(tournament_id)
    if not season_id:
        print(f"  Could not find season for tournament {tournament_id}", file=sys.stderr)
        return []

    print(f"  Tournament {comp_info['name']}: season {season_id}")

    matches = []
    # Fetch upcoming events (page 0 typically has next ~20 events)
    for page in range(3):  # Check up to 3 pages
        url = f"{SOFASCORE_BASE}/unique-tournament/{tournament_id}/season/{season_id}/events/next/{page}"
        data = fetch_json(url)
        if not data or "events" not in data:
            break

        events = data["events"]
        if not events:
            break

        for event in events:
            if not involves_pl_team(event, team_mapping):
                continue

            home = map_team(event["homeTeam"], team_mapping)
            away = map_team(event["awayTeam"], team_mapping)

            # Convert Unix timestamp to ISO 8601
            ts = event.get("startTimestamp", 0)
            utc_date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            date_only = utc_date[:10]

            status_code = event.get("status", {}).get("code", 0)

            match = {
                "id": generate_match_id(home["id"], away["id"], comp_info["code"], date_only),
                "homeTeam": home,
                "awayTeam": away,
                "utcDate": utc_date,
                "status": sofascore_status_to_fd(status_code),
                "competition": {
                    "id": comp_info["fd_id"],
                    "name": comp_info["name"],
                    "code": comp_info["code"],
                    "emblem": None,
                },
                "matchday": event.get("roundInfo", {}).get("round"),
                "venue": event.get("venue", {}).get("stadium", {}).get("name")
                if "venue" in event
                else None,
            }
            matches.append(match)

        # SofaScore pages with < 20 events means we've reached the end
        if len(events) < 20:
            break

    return matches


def main():
    print("Loading team mapping...")
    team_mapping = load_team_mapping()
    print(f"  {len(team_mapping)} teams mapped")

    all_matches = []

    for tournament_id, comp_info in TOURNAMENTS.items():
        print(f"\nFetching {comp_info['name']}...")
        matches = fetch_tournament_fixtures(tournament_id, comp_info, team_mapping)
        print(f"  Found {len(matches)} PL-team fixtures")
        all_matches.extend(matches)

    # Sort by date
    all_matches.sort(key=lambda m: m["utcDate"])

    # Deduplicate by match ID
    seen_ids = set()
    unique_matches = []
    for m in all_matches:
        if m["id"] not in seen_ids:
            seen_ids.add(m["id"])
            unique_matches.append(m)

    output = {
        "version": 1,
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "matches": unique_matches,
    }

    # Only write if we got data; preserve previous file on failure
    if not unique_matches:
        print("\nWARNING: No matches found — preserving previous output file", file=sys.stderr)
        if OUTPUT_FILE.exists():
            sys.exit(0)
        else:
            print("  No previous file exists either — writing empty", file=sys.stderr)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nWrote {len(unique_matches)} matches to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
