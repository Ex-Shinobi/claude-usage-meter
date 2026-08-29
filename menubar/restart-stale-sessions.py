#!/usr/bin/env python3
"""Restart the Claude sessions a switch left behind.

Claude Code resolves its bearer token once per process and memoizes it with no
expiry, so switching accounts reaches new sessions only — anything already
running keeps the account it started with. This kills those stale sessions and
hands back a `claude --resume <id>` line for each, so the conversations come
back on the new account.

  restart-stale-sessions.py              report only
  restart-stale-sessions.py --kill       terminate them, then emit the commands
  restart-stale-sessions.py --relaunch   terminate them and reopen each one
"""
import json, os, shutil, subprocess, sys, time, urllib.request

BASE = "http://127.0.0.1:4177"
STATE = os.environ.get("CLAUDE_USAGE_HOME") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "claude-usage-meter")
RESUME_FILE = os.path.join(STATE, "resume-stale-sessions.sh")
# Session ids listed here are never stopped, however this script is invoked.
# A session you are working in should be in this file: stopping it is not
# recoverable by the script, only by resuming it by hand afterwards.
PROTECT_FILE = os.path.join(STATE, "protected-sessions")


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


def term_count():
    """How many tabs the Fredrin terminals panel currently holds."""
    try:
        out = subprocess.run(["fredrin", "terminals", "list"], capture_output=True, text=True, timeout=15).stdout
        return sum(1 for l in out.splitlines() if "term-" in l)
    except Exception:
        return -1


def reopen(cmd, name):
    """Reopen a session. Fredrin's terminals panel first — it is where these
    sessions live — but `terminals new` exits 0 and does nothing when that panel
    is closed, so confirm a tab actually appeared before believing it."""
    if shutil.which("fredrin"):
        before = term_count()
        try:
            subprocess.run(["fredrin", "terminals", "new", "1", "--cmd", cmd, "--name", name],
                           capture_output=True, timeout=20)
            if before >= 0 and term_count() > before:
                return "fredrin"
        except Exception:
            pass
    try:
        subprocess.run(["/usr/bin/osascript", "-e",
                        'on run argv\ntell application "Terminal"\nactivate\ndo script (item 1 of argv)\n'
                        'end tell\nend run', cmd], capture_output=True, timeout=20)
        return "terminal"
    except Exception:
        return None


def main():
    relaunch = "--relaunch" in sys.argv
    kill = "--kill" in sys.argv or relaunch
    d = api("/api/sessions")
    if d is None:
        notify("Can't reach the usage server")
        return 1
    protected = set()
    try:
        with open(PROTECT_FILE) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    protected.add(line)
    except Exception:
        pass

    stale = [s for s in (d.get("sessions") or []) if s.get("stale")]
    skipped = [s for s in stale if s.get("sessionId") in protected]
    stale = [s for s in stale if s.get("sessionId") not in protected]
    if skipped:
        print("# protected, left running: %s" %
              ", ".join((s.get("tty") or "?") + " " + (s.get("folder") or "") for s in skipped))
    if not stale:
        notify("No sessions to restart%s" % (" (%d protected)" % len(skipped) if skipped else ""))
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
    if not relaunch:
        notify("Stopped %d session(s) · resume commands copied to the clipboard" % killed)
        return 0

    time.sleep(2)                        # let them release their panes first
    opened, how = 0, set()
    for s in stale:
        sid, cwd = s.get("sessionId"), s.get("cwd") or os.path.expanduser("~")
        if not sid:
            continue
        where = reopen("cd %s && claude --resume %s" % (json.dumps(cwd), sid), s.get("folder") or "claude")
        if where:
            opened += 1
            how.add(where)
    notify("Stopped %d · reopened %d via %s · commands still on the clipboard"
           % (killed, opened, "/".join(sorted(how)) or "nothing"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
