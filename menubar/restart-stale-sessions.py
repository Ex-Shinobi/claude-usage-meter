#!/usr/bin/env python3
"""Restart the Claude sessions a switch left behind.

Claude Code resolves its bearer token once per process and memoizes it with no
expiry, so switching accounts reaches new sessions only — anything already
running keeps the account it started with. This kills those stale sessions and
hands back a `claude --resume <id>` line for each, so the conversations come
back on the new account.

  restart-stale-sessions.py            report only
  restart-stale-sessions.py --kill     terminate them, then emit the commands
"""
import json, os, subprocess, sys, urllib.request

BASE = "http://127.0.0.1:4177"
STATE = os.environ.get("CLAUDE_USAGE_HOME") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "claude-usage-meter")
RESUME_FILE = os.path.join(STATE, "resume-stale-sessions.sh")


def api(path):
    try:
        with open(os.path.join(STATE, "token")) as f:
            token = f.read().strip()
    except Exception:
        return None
    req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except Exception:
        return None


def notify(msg):
    subprocess.run(["/usr/bin/osascript", "-e",
                    'on run argv\ndisplay notification (item 1 of argv) with title "Claude Usage Meter"\nend run',
                    msg], capture_output=True)


def main():
    kill = "--kill" in sys.argv
    d = api("/api/sessions")
    if d is None:
        notify("Can't reach the usage server")
        return 1
    stale = [s for s in (d.get("sessions") or []) if s.get("stale")]
    if not stale:
        notify("No sessions left on another account")
        return 0

    # A session with no recorded id can't be resumed — only reported.
    lines = ["#!/bin/sh", "# Resume the sessions restarted by the usage meter."]
    for s in stale:
        sid, cwd = s.get("sessionId"), s.get("cwd") or os.path.expanduser("~")
        if sid:
            lines.append("(cd %s && claude --resume %s)   # was %s on %s" %
                         (json.dumps(cwd), sid, s.get("tty"), s.get("email")))
        else:
            lines.append("# %s (%s): no session id recorded — start it manually in %s" %
                         (s.get("tty"), s.get("email"), cwd))
    body = "\n".join(lines) + "\n"
    try:
        with open(RESUME_FILE, "w") as f:
            f.write(body)
        os.chmod(RESUME_FILE, 0o700)
    except Exception:
        pass
    subprocess.run(["/usr/bin/pbcopy"], input=body.encode(), capture_output=True)

    if not kill:
        print(body, end="")
        notify("%d session(s) on another account · commands copied" % len(stale))
        return 0

    killed = 0
    for s in stale:
        try:
            os.kill(int(s["pid"]), 15)   # SIGTERM: let it shut down cleanly
            killed += 1
        except Exception:
            pass
    print(body, end="")
    notify("Stopped %d session(s) · resume commands copied to the clipboard" % killed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
