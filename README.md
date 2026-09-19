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

Below that, **▸ N live sessions · copy an id** lists every running session
(id prefix, folder, account, ⚠ when it is on another account); clicking a row
copies its full session id. **⟳ Restart a session by ID…** asks for an id in a
dialog (pre-filled from the clipboard when that holds one) and restarts just
that session the same way — stopped, then resumed in its own Fredrin pane — so
it comes back on the account the Mac is on now, with Remote Control on. Any
live session qualifies, stale or not, and the protected list is not consulted:
pasting an id is deliberate. It runs `restart-stale-sessions.py --relaunch
--session <id>`.

Both scripts run from the menu, and SwiftBar's environment has none of the
`FREDRIN_*` variables the `fredrin` CLI needs (API address, tokens, the
terminals server, the broker socket — they exist only in shells Fredrin
spawned), so each first borrows them from a running Fredrin-spawned process via
`bin/fredrin_env.py`. Without that, every `fredrin` call from the menu failed
with "missing env" and a restart could stop sessions but never reopen them.
A restart also no longer has to scan scrollback to find a session's pane: the
claude process carries `FREDRIN_TERM_ID` in its environment, which names the
pane outright, and the scrollback match is only the fallback.

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

### Remote Control after a switch

A switch also drops **Remote Control** in every running session: each one
prints "Remote Control disconnected — signed-in claude.ai account or
organization changed on this machine — run /remote-control", and until someone
types that, the session is unreachable from the phone or claude.ai/code.

So after every successful switch, whichever surface triggered it, the server
starts `bin/reconnect-remote-control.py` in the background. It runs
`/remote-control` in every live session, through one of three channels:

- a **terminal tab**: the claude process carries `FREDRIN_TERM_ID` in its
  environment, which names its pane outright — no scrollback matching;
- a **ticket Worker**: the broker session owning its worktree, via
  `fredrin sessions send`, or its ticket via `fredrin tickets send` when a
  fredrin-agent runs it;
- anything else (cmux, Terminal.app) has no channel and is only named in the
  notification.

**Only a pane takes the keystrokes a human would type.** There it writes
`/remote-control`, then ESC (closes the slash-command picker — and ends a
running turn, so the command is not left queued behind it), then Enter. The
other two channels carry flags rather than bytes: the body reaches the TUI as a
bracketed paste, and an Enter passed as payload text lands *inside* that paste,
where the TUI swallows it as content. The command then sits unsent in the
composer and the next send is appended to it on a new line — which is exactly
how one Worker spent two days disconnected while every run reported it fixed,
its composer quietly collecting `/remote-control` and `resume` until something
finally pressed Enter and submitted all of it at once. So on those two channels
the submit is sent as the flag it is: `--interrupt` for the ESC, on its own,
then the body with Enter left on, which makes the broker (or, for a ticket, the
Fredrin server) type the CR as its own write 120 ms later, outside the paste.

**Each channel is checked before it is chosen**, from a single
`fredrin sessions list`: the pane has to still exist, the broker session has to
be live rather than hibernated or a reapable zombie, and a ticket's Worker has
to sit on a machine that is still checking in. A channel that is not credible
falls through to the next, and a session left with none is reported rather than
typed into. "On other machines" in that listing usually means *this* one — a
Worker the local fredrin-agent runs belongs to the agent's own broker, not the
desktop app's, so it is only reachable by ticket, and it is filed under the
hostname captured when the agent was paired, which drifts. The log says so when
that machine is in fact this machine.

It waits until the switch is 35 seconds old first: Claude Code reads the
Keychain through a 30-second cache, and `/remote-control` typed inside that
window comes up on the *old* token and drops again. Then it reads each
session's transcript for the `/remote-control is active` line Claude Code logs.
**Only that line counts as success.** Every `fredrin ... send` exits 0 on a
write the TUI may never act on — on the ticket channel a zero exit means only
that the Fredrin API published the event — so a session is reported
`unverified` when its transcript cannot be read at all, and `failed` when it
was typed into and never reported Remote Control active. The full report is in
`reconnect-remote-control.log` in the state dir.

**Every session it typed into is then told to carry on.** Once that session's
Remote Control reports active (plus a second for the TUI to settle), it sends
`resume` through the same channel, in that channel's own shape; a session that
never confirms within the 20-second wait still gets `resume` at the end, so no
work is left stopped. Sessions left alone because Remote Control was already
on, and sessions with no credible channel, get nothing — they were never typed
into. Nor does a session whose keystrokes failed partway: `/remote-control` may
be sitting half-typed in its prompt, and `resume` would be appended to it
instead of submitted.

Every session, because by the time anything can be typed the turn is over
either way. The switch kills a turn in flight — its API call loses
authorization, or the account was switched *because* the old one ran out of
quota, which had already killed the turn — and the ESC kills whatever survived
that. The script used to resume only sessions whose
`~/.claude/sessions/<pid>.json` said `status: busy` just before its keystrokes,
and that read is worthless here: it happens ~35 seconds after the switch, by
which time a killed turn reads `idle` again, and some sessions leave the file
stale for hours. The cost of resuming a session that really was idle is small —
`resume` at an idle prompt picks the last turn back up — and the cost of
missing one is a session sitting dead until a human notices. The notification
adds "resumed N", and a `resume` that cannot be typed is logged without
changing the Remote Control counts.

Every session gets it, whatever account it started on: `/remote-control`
opens the bridge under the account in the Keychain *now*, and it holds — a
session started on one account, with the Mac switched to another, reconnected
under the new account and stayed up. Only the session's own API calls stay on
the account it started with; the notification counts those, and the menu's
**Restart them** is what moves them (a restarted session comes back with
Remote Control on by itself, via `remoteControlAtStartup` in
`~/.claude/settings.json`).

The `fredrin` CLI only works from a shell Fredrin spawned — its API address and
tokens exist nowhere but that shell's environment — and neither launchd nor
SwiftBar has them. The script borrows them from the environment of a running
Fredrin-spawned process (`ps -E` shows a process's environment to its own user).

To run it by hand:

```bash
bin/reconnect-remote-control.py --dry-run          # which channel each session would use, and which would be resumed
bin/reconnect-remote-control.py --delay 0          # type right now
bin/reconnect-remote-control.py --session <id>     # one session only
```

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
