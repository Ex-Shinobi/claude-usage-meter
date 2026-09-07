#!/bin/sh
# "Restart a session by ID…" from the SwiftBar menu.
#
# SwiftBar menus have no text field, so the ID is asked for in a macOS dialog.
# The session is then stopped and resumed in its own Fredrin pane by the same
# code as "Restart them", which puts it on the account the Mac is on now — with
# Remote Control on, via remoteControlAtStartup. Any live session qualifies,
# stale or not; pasting an ID is deliberate, so the protected list is not
# consulted here.
set -u
BIN="$(cd "$(dirname "$0")" && pwd)"
CLIP="$(/usr/bin/pbpaste 2>/dev/null | tr -d '[:space:]')"
case "$CLIP" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-*) DEFAULT="$CLIP" ;;
  *) DEFAULT="" ;;
esac
# System Events puts the dialog in front; SwiftBar's own process is a menu
# extra and cannot. The answer is read back as text.
SID="$(/usr/bin/osascript -e 'on run argv
  tell application "System Events"
    activate
    set r to display dialog "Claude session ID to restart on the current account:" default answer (item 1 of argv) with title "Claude Usage Meter" buttons {"Cancel", "Restart"} default button "Restart"
    return text returned of r
  end tell
end run' "$DEFAULT" 2>/dev/null | tr -d '[:space:]')"
[ -n "$SID" ] || exit 0                          # cancelled
case "$SID" in
  *[!0-9a-fA-F-]*|"")
    /usr/bin/osascript -e 'display notification "That is not a session ID (expected a uuid)" with title "Claude Usage Meter"' >/dev/null 2>&1
    exit 1 ;;
esac
exec /usr/bin/python3 "$BIN/restart-stale-sessions.py" --relaunch --session "$SID"
