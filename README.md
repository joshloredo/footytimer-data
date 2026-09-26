# footytimer-data

Nightly US broadcast listings for the FootyTimer app, published at
https://footytimer.joshloredo.com/broadcasts.json (GitHub Pages from `docs/`).
Design: footytimer repo `docs/plans/2026-09-26-broadcast-feed-design.md`.

- The job is `scripts/build_feed.py` (standard library only). It runs on the Mac mini under launchd at 03:05 ET.
- Test: `/usr/bin/python3 -m unittest discover -s scripts -p 'test_*.py'`
- Dry run (no writes, no push): `/usr/bin/python3 scripts/build_feed.py --dry-run`
- Status for the morning Signal message: `state/status.line`. Log: `logs/feed.log`.
- Install: `cp launchd/com.joshloredo.footytimer-feed.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.joshloredo.footytimer-feed.plist`
- Rollback: `launchctl bootout gui/$(id -u)/com.joshloredo.footytimer-feed && rm ~/Library/LaunchAgents/com.joshloredo.footytimer-feed.plist`
