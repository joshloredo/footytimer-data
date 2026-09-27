# footytimer-data

Nightly US broadcast listings for the FootyTimer app, published at
https://footytimer.joshloredo.com/broadcasts.json (GitHub Pages from `docs/`).
Design: footytimer repo `docs/plans/2026-09-26-broadcast-feed-design.md`.

- The job is `scripts/build_feed.py` (standard library only). It runs on the Mac mini under launchd at 03:05 ET.
- Test: `/usr/bin/python3 -m unittest discover -s scripts -p 'test_*.py'`
- Dry run (no writes, no push): `/usr/bin/python3 scripts/build_feed.py --dry-run`
- Status for the morning Signal message: `state/status.line`. Log: `logs/feed.log`.
- Code and alias changes pushed to `main` reach the mini by themselves: each run starts by adopting `origin/main` (`git fetch`, then `git reset --hard`), so a change takes effect the following night.
- Set up the mini from scratch:
  1. Create the deploy key, `ssh-keygen -t ed25519 -N "" -f ~/.ssh/footytimer-data-deploy`, and add `~/.ssh/footytimer-data-deploy.pub` to this repo's deploy keys (Settings → Deploy keys) with write access.
  2. Clone with it: `GIT_SSH_COMMAND="ssh -i ~/.ssh/footytimer-data-deploy -o IdentitiesOnly=yes" git clone git@github.com:joshloredo/footytimer-data.git ~/footytimer-data`
  3. In `~/footytimer-data`, run `git config core.sshCommand "ssh -i ~/.ssh/footytimer-data-deploy -o IdentitiesOnly=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"`, then `git config user.name "FootyTimer Feed"` and `git config user.email feed@footytimer.joshloredo.com`.
  4. `mkdir -p logs state`
  5. Create `.env` with mode 600 (`touch .env && chmod 600 .env`), containing `FOOTBALL_DATA_API_KEY=…`.
  6. Install the LaunchAgent: `cp launchd/com.joshloredo.footytimer-feed.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.joshloredo.footytimer-feed.plist`
- To take a bad feed down: revert the feed commit on GitHub. The next run adopts origin/main and publishes a fresh feed.
- Switches the app reads, at most once an hour, from `docs/config.json` (served at https://footytimer.joshloredo.com/config.json). Edit it on GitHub:
  - `"listings": false` hides TV listings everywhere in the app: the Watch row, the widget's broadcaster line and its settings toggle.
  - `"crests": false` draws each club's colour badge (its colours and three-letter code) instead of its crest, in the app and the widgets.
  - A switch is on unless the file says otherwise. Devices keep the last values they fetched, so one that's off stays off offline. Turn it back on by setting it to `true`; deleting the file leaves each device as it was.
- Rollback: `launchctl bootout gui/$(id -u)/com.joshloredo.footytimer-feed && rm ~/Library/LaunchAgents/com.joshloredo.footytimer-feed.plist`
