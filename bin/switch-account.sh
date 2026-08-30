#!/bin/sh
# Switch the active Claude Code account from the SwiftBar menu.
#
# The switch is a Keychain write. Claude Code resolves its bearer token once per
# process and memoizes it with no expiry, so this reaches sessions started
# afterwards — anything already running keeps its account until it restarts.
set -u
SLUG="${1:-}"
[ -n "$SLUG" ] || exit 1
STATE="${CLAUDE_USAGE_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/claude-usage-meter}"
TOKEN=$(cat "$STATE/token" 2>/dev/null) || exit 1

RESP=$(curl -s -m 20 -X POST -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:4177/api/switch?slug=$SLUG")

MSG=$(printf '%s' "$RESP" | /usr/bin/python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("Switch failed: no response from the usage server"); raise SystemExit
if not d.get("ok"):
    print("Switch failed: " + str(d.get("error") or "unknown error")); raise SystemExit
if d.get("already"):
    print(str(d.get("active")) + " is already the active account"); raise SystemExit
n = d.get("stale") or 0
tail = (" \u00b7 %d session%s still on the old account \u2014 restart from the menu"
        % (n, "" if n == 1 else "s")) if n else ""
print("Now on " + str(d.get("active")) + tail)
')

# Pass the text as an argument, not inside the script source, so quotes in an
# error message can't break (or inject into) the AppleScript.
/usr/bin/osascript -e 'on run argv
  display notification (item 1 of argv) with title "Claude Usage Meter"
end run' "$MSG" >/dev/null 2>&1
