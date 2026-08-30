# Claude Usage Meter

Shows your Claude rate-limit usage — the same numbers as `/usage` in the
terminal (Session 5h, Weekly, per-model caps, extra usage) — in two places:

- **Localhost page** (`server.js`): one card per account with usage bars.
- **macOS menu bar** (SwiftBar plugin): live per-account percentages at a glance.

Read-only: it never changes your login or writes to your Claude Code credentials.

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
   ```

   On first run it creates its state directory and prints a URL with a one-time
   token. Open that URL — it sets a cookie for your browser, then plain
   `http://127.0.0.1:4177` works from then on. To authorize a browser later:

   ```bash
   open "http://127.0.0.1:4177/?t=$(cat ~/.config/claude-usage-meter/token)"
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
   existing SwiftBar plugin folder). It refreshes every 5 minutes and reads the
   API token itself, so there's nothing to configure.

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
red ≥95%), with segmented bars and reset times (with the date) in the dropdown.
When the server can't reach the usage endpoint it keeps showing its last good
numbers, marked `⚠ cached 2d ago` so old figures can't pass for current ones.

## Switching accounts

Each non-active account in the dropdown has a **⇄ Switch to this account** item.
Clicking it swaps the active Claude Code login, which takes effect for **sessions
started from then on**.

Sessions already running keep the account they started with. Claude Code reads
the Keychain through a 30-second cache, but the resolved bearer token sits above
that in a memo with no expiry, cleared only by that process logging in, logging
out, or saving refreshed tokens. Nothing watches the Keychain for outside
changes, so a switch cannot be pushed into a running session.

The dropdown therefore lists what got left behind — **⟳ N sessions on another
account**, grouped by account with each session's tty and folder. Two actions:

- **Restart them** stops those sessions and reopens each one with
  `claude --resume <id>`, so the conversations come back on the new account by
  themselves. It reuses the session's **own Fredrin pane** wherever it can, so
  the tab and its place in the layout survive: a pane prints the command line it
  launched with, so its scrollback carries the `--session-id` to match against
  the process's argv, and `fredrin terminals send` types the resume command back
  into that same pane. Text is only ever sent to a pane whose claude has been
  confirmed dead, so it can never land in a live prompt.

  The command it types is the session's **own argv**, read before the process is
  killed, with the session flags swapped for `--resume` — a bare `claude --resume`
  would drop the settings and plugin dir Fredrin launches its panes with. And
  before typing, the pane must name the session: on exit claude prints
  `Resume this session with: claude --resume <id>`, so a pane identified only by
  elimination still has to confirm itself, or a new tab is opened instead.

  Where a pane can't be identified — the launch line has scrolled out of
  scrollback, several panes share a cwd, or the session lives in another app
  entirely — it falls back to a new Fredrin tab, and nothing else. Sessions live
  in Fredrin (or cmux) panes, so scattering them into Terminal.app windows is
  worse than not reopening them. `fredrin terminals new` exits 0 without doing
  anything when the Terminals panel is closed, so a tab must actually appear;
  otherwise the session is left stopped and reported, resume command already on
  the clipboard.

  Before stopping anything it sends ESC and waits for the transcript to go quiet,
  so a session mid-turn is not cut off in the middle of a tool call. A session
  with no pane (cmux) has no channel for ESC and is reported as stopped anyway.

  **Ticket Workers go back through Fredrin.** A session in `~/.fredrin/worktrees/`
  belongs to Fredrin's Worker surface, which the terminals API can't reach, and
  reopening it as a loose terminal would keep the transcript but drop the ticket
  association. What works is that the pane's **shell outlives claude**: kill the
  agent and Fredrin still owns the session, so `fredrin sessions send` types
  straight into that same pane. The Worker's own argv is reused verbatim —
  Fredrin's settings, plugin dir, model and effort — with the session flags
  swapped for `--resume`, so it returns as the same Worker on the same ticket
  with its transcript.

  `fredrin tickets start` does **not** work here: the live shell means the
  session is no zombie, so it returns `reused: true` and spawns nothing.
  Without `--relaunch` these are only reported.
- **Stop them** stops the sessions without reopening.
- **Just copy the resume commands** stops nothing at all.

The helper scripts live in `bin/`, not in `menubar/`. SwiftBar's plugin directory
is `menubar/`, so anything executable placed there is loaded as a plugin in its
own right and its first line of output is painted into the menu bar — which is
exactly what happened when `restart-stale-sessions.py` sat next to the plugin.

A session listed in `protected-sessions` in the state dir (one id per line) is
never stopped — put the session you are working in there, since the script can
reopen a session but cannot undo stopping the one that was driving it.

All three put the resume lines on the clipboard and in `resume-stale-sessions.sh`
in the state dir. Don't run that file directly — it would launch every session
sequentially in one terminal, each blocking the next.

The switch replaces `claudeAiOauth` in the Keychain item and the `oauthAccount`
key in `~/.claude.json`. It deliberately leaves the rest of the Keychain blob
alone — `mcpOAuth` holds your MCP connector logins, which are machine-scoped,
not account-scoped, and swapping them out would sign you out of all of them.

An account can only be switched to if its snapshot still has a working refresh
token. If the refresh is rejected (`refresh HTTP 400`), the switch is refused
**before** anything is written, so a dead account can never corrupt your live
credentials — you just need to `/login` as that account once to revive it.

## How it reads usage

- Active account: token from the macOS Keychain (`Claude Code-credentials`).
- Other saved accounts: their snapshot in the state dir (token refreshed as
  needed). With a single account, only the Keychain is used and no snapshots
  are needed at all.

Reads are on-demand (open + refresh) and an account whose numbers are under four
minutes old is served from cache, which keeps this under the usage endpoint's
rate limit. On a 429 the backoff is capped at five minutes and cleared as soon as
a call succeeds — honoring the endpoint's own hour-long `retry-after` used to
freeze every account behind days-old numbers.

Credentials are written in exactly two cases:

- **Switching accounts**, on your click (see above).
- **Capturing the active account.** Anthropic rotates the refresh token on every
  redemption, so a snapshot taken once is dead the moment the running CLI
  refreshes. The server copies the live Keychain credentials back into the active
  account's snapshot as it goes, so the account you switch *away* from is still
  usable when you switch back. Without this, saved accounts quietly rot.

## Security

**This tool is single-machine, single-user. Nothing here is shareable — the
state directory holds credentials equivalent to a full Claude login.**

### Where state lives

Everything the meter stores is kept **outside this repo**, in
`~/.config/claude-usage-meter` (override with `$CLAUDE_USAGE_HOME`; respects
`$XDG_CONFIG_HOME`). Directories are `0700`, files `0600`:

| Path | Contents |
| --- | --- |
| `accounts/*.json` | Per-account snapshots, including **OAuth refresh tokens** |
| `sessions/*.json` | One record per live session: your email + working directory |
| `token` | The local API token |

Credential snapshots deliberately do **not** live in the checkout. A refresh
token is a long-lived, full-account credential; one stray `git add -f`, one
edited `.gitignore`, or one "zip up this folder and send it" would leak all of
them. If you're upgrading from a version that kept `accounts/` and `sessions/`
inside the repo, the server moves them on first start (copy, verify, then
remove — a failure leaves the originals untouched).

### How the server is protected

The server binds to `127.0.0.1` only — it is never reachable from the network.
On top of that:

- **Every request needs the API token**, presented as `Authorization: Bearer`,
  `?t=`, or the cookie the first tokenized visit sets. The token file is `0600`,
  so other accounts on this Mac can't read your usage or session list off the
  port. The token is only ever printed to a terminal, never to a log file.
- **Host allowlist.** A request whose `Host` isn't loopback is rejected, which
  is what defeats DNS rebinding: a page on `evil.com` that re-resolves to
  `127.0.0.1` still sends `Host: evil.com`.
- **Cross-site requests are rejected** via `Sec-Fetch-Site`/`Origin`, so a
  website you happen to be visiting can't fire `<img src="…/api/usage?force=1">`
  at the server. Following a link to the page still works — only sub-resource
  requests are blocked.
- **No token or credential is ever in a response body**, and error strings have
  filesystem paths stripped out.
- The page ships a nonce-based CSP with `default-src 'none'`; every value from
  the usage API or from disk is escaped before it reaches the DOM.

To rotate the API token, delete `~/.config/claude-usage-meter/token` and restart
the server. Browsers will need re-authorizing with the `?t=` URL.

### What this does not protect against

Any process running **as you** can read the token file — but it could equally
read `~/.claude/.credentials.json` directly, so that isn't a boundary this tool
can create. The protections above are against other accounts on the machine,
other machines on the network, and websites you visit.
