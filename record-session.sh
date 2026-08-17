#!/bin/bash
# SessionStart hook (READ-ONLY): record which account + terminal each Claude Code
# session launched as, so the Usage Meter's "Sessions" view can show them.
# Writes one small file per session into the meter's state dir — deliberately
# outside this repo, since these records carry your email and project paths.
# Reads nothing sensitive it doesn't already have; never changes your login.
STATE="${CLAUDE_USAGE_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/claude-usage-meter}"
DIR="$STATE/sessions"
mkdir -p "$DIR" 2>/dev/null
chmod 700 "$STATE" "$DIR" 2>/dev/null
SID="${CLAUDE_CODE_SESSION_ID:-}"
[ -z "$SID" ] && exit 0
# the id becomes a filename, so refuse anything that isn't a bare uuid
case "$SID" in *[!a-zA-Z0-9-]*) exit 0 ;; esac
CPID="${CLAUDE_PID:-$PPID}"
TTY="$(ps -o tty= -p "$CPID" 2>/dev/null | tr -d ' ')"
SID="$SID" CPID="$CPID" TTY="$TTY" CWD="$(pwd)" DIR="$DIR" python3 - <<'PY' 2>/dev/null
import json, os, time
d = {}
try: d = json.load(open(os.path.expanduser("~/.claude.json")))
except Exception: pass
a = d.get("oauthAccount") or {}
rec = {
    "sessionId": os.environ.get("SID", ""),
    "pid": int(os.environ.get("CPID") or 0),
    "tty": os.environ.get("TTY", ""),
    "email": a.get("emailAddress") or "",
    "accountUuid": a.get("accountUuid") or "",
    "cwd": os.environ.get("CWD", ""),
    "at": int(time.time() * 1000),
}
# 0600 from the moment it exists — never a window where it is world-readable
p = os.path.join(os.environ["DIR"], rec["sessionId"] + ".json")
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(json.dumps(rec))
PY
exit 0
