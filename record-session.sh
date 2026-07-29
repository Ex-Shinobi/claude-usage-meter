#!/bin/bash
# SessionStart hook (READ-ONLY): record which account + terminal each Claude Code
# session launched as, so the Usage Meter's "Sessions" view can show them.
# Writes one small file per session to the sessions/ folder next to this script.
# Reads nothing sensitive it doesn't already have; never changes your login.
DIR="$(cd "$(dirname "$0")" && pwd)/sessions"
mkdir -p "$DIR" 2>/dev/null
SID="${CLAUDE_CODE_SESSION_ID:-}"
[ -z "$SID" ] && exit 0
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
open(os.path.join(os.environ["DIR"], rec["sessionId"] + ".json"), "w").write(json.dumps(rec))
PY
exit 0
