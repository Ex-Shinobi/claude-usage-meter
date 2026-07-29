# Claude Usage Meter

Shows your Claude rate-limit usage — the same numbers as `/usage` in the
terminal (Session 5h, Weekly, per-model caps, extra usage) — in two places:

- **Localhost page** (`server.js`): one card per account with usage bars.
- **macOS menu bar** (SwiftBar plugin): live per-account percentages at a glance.

Read-only: it never changes your login or writes to your credentials.

## Requirements

- macOS with [Claude Code](https://claude.com/claude-code) installed and logged in
- Node.js (any recent version)
- Python 3 with [Pillow](https://pypi.org/project/pillow/) for the menu-bar image
  (`pip3 install pillow`) — the plugin falls back to plain text without it
- [SwiftBar](https://swiftbar.app) (`brew install swiftbar`) for the menu bar part

## Setup

1. **Clone this repo** anywhere you like:

   ```bash
   git clone <repo-url> claude-usage-meter
   cd claude-usage-meter
   ```

2. **Start the usage server:**

   ```bash
   node server.js
   # open http://127.0.0.1:4177
   ```

   To keep it running permanently, create a LaunchAgent at
   `~/Library/LaunchAgents/com.claudeusage.meter.plist`:

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
     <key>Label</key><string>com.claudeusage.meter</string>
     <key>ProgramArguments</key>
     <array>
       <string>/usr/local/bin/node</string> <!-- adjust: `which node` -->
       <string>/PATH/TO/claude-usage-meter/server.js</string>
     </array>
     <key>RunAtLoad</key><true/>
     <key>KeepAlive</key><true/>
   </dict>
   </plist>
   ```

   Then: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.claudeusage.meter.plist`

3. **Menu bar (optional):** open SwiftBar and set its plugin folder to this
   repo's `menubar/` directory (or copy `menubar/claude-usage.5m.py` into your
   existing SwiftBar plugin folder). It refreshes every 5 minutes.

4. **Session tracking (optional):** to have the meter show which terminal each
   Claude Code session runs in, add `record-session.sh` as a `SessionStart` hook
   in `~/.claude/settings.json`:

   ```json
   "hooks": {
     "SessionStart": [
       { "hooks": [ { "type": "command", "command": "/PATH/TO/claude-usage-meter/record-session.sh" } ] }
     ]
   }
   ```

## What it shows

One card per account on file, each with usage bars. The account you're currently
logged in as is marked **active**. Usage loads when you open the page; click
**↻ Refresh** (top-right, or per-card) to pull fresh numbers.

The menu bar shows one colored dot + percentage per account (green / yellow ≥80% /
red ≥95%), with segmented bars and reset times in the dropdown.

## How it reads usage

- Active account: token from the macOS Keychain (`Claude Code-credentials`).
- Other saved accounts: their snapshot in `accounts/` (token refreshed as needed).
  With a single account, only the Keychain is used — `accounts/` stays empty.

Nothing is ever written to your credentials. Calls are on-demand only (open +
refresh), so it stays well under the usage endpoint's rate limit.

> **Note:** `accounts/` and `sessions/` hold your local credentials snapshots and
> session history. They are gitignored — never commit or share them.
