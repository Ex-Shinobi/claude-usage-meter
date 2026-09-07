#!/usr/bin/env python3
"""Turn Remote Control back on in every running Claude session after a switch.

When the signed-in account changes, each running session drops its Remote
Control link and prints "Remote Control disconnected — signed-in claude.ai
account or organization changed on this machine — run /remote-control". That
command has to be typed into every session by hand, and a session missed once
is unreachable from the phone until someone notices.

This types it for you, into every live session. /remote-control opens the
bridge under the account in the Keychain now, whatever account the session
itself started on (verified to hold). The session's own API calls still use
the account it started with; the notification counts those, since only a
restart from the menu moves them.

For each session it sends three keystrokes, in order, through the pane Fredrin
owns for it:

    /remote-control      typed without Enter
    ESC                  closes the slash-command picker (and ends a running
                         turn, so the command is not left queued behind it)
    Enter                submits the command

Delivery channels, in order of preference:
  - a Fredrin terminal tab: the claude process carries FREDRIN_TERM_ID in its
    environment, which names its pane outright — no scrollback matching.
  - a ticket Worker (cwd under ~/.fredrin/worktrees): the broker session that
    owns the worktree, via `fredrin sessions send`.
  - a Worker the local broker does not own (run by a paired fredrin-agent):
    its ticket, via `fredrin tickets send`, which goes through the Fredrin API.
  - anything else (cmux, Terminal.app): no channel — reported, not touched.

The Fredrin terminals API and its token exist only in the environment of shells
Fredrin spawned. This script is started by the usage server (launchd) or by
SwiftBar, which have neither, so it borrows them from the environment of any
running Fredrin-spawned process (`ps -E`, which shows a process's environment
to its own user) — see fredrin_env.py, shared with the restart script.

Timing: Claude Code reads the Keychain through a 30-second cache. Typing
/remote-control inside that window reconnects on the *old* token and drops
again moments later, so the script waits until the switch is 35 seconds old
(SWITCHED_AT, ms since the epoch, set by the server; --delay N overrides).

  reconnect-remote-control.py             reconnect every live session
  reconnect-remote-control.py --dry-run   show which channel each would use
  reconnect-remote-control.py --session <id>   one session only
  reconnect-remote-control.py --delay 0   type right away
"""
import calendar, glob, json, os, re, shutil, subprocess, sys, time, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fredrin_env import fredrin_env, proc_env

BASE = "http://127.0.0.1:4177"
STATE = os.environ.get("CLAUDE_USAGE_HOME") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "claude-usage-meter")
LOG_FILE = os.path.join(STATE, "reconnect-remote-control.log")
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
KEYCHAIN_CACHE_S = 30.0          # Claude Code's Keychain read cache
DEFAULT_DELAY_S = KEYCHAIN_CACHE_S + 5
KEY_GAP_S = 0.4                  # let the TUI draw the picker before ESC lands

_log = []


def say(msg):
    _log.append(msg)
    print(msg)


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


class Fredrin:
    def __init__(self, env):
        self.env = env
        self.bin = shutil.which("fredrin", path=env["PATH"]) or os.path.expanduser("~/.fredrin/bin/fredrin")

    def run(self, *args, timeout=20):
        return subprocess.run([self.bin] + list(args), capture_output=True, text=True,
                              timeout=timeout, env=self.env)

    def panes(self):
        try:
            return set(re.findall(r"term-[0-9a-f-]+", self.run("terminals", "list").stdout))
        except Exception:
            return set()

    def session_for(self, cwd):
        """The broker session owning a worktree — a path belongs to one ticket."""
        try:
            out = self.run("sessions", "list").stdout
        except Exception:
            return None
        for line in out.splitlines():
            if line.rstrip().endswith(cwd.rstrip("/")):
                m = re.search(r"(" + UUID + ")", line)
                if m:
                    return m.group(1)
        return None

    def ticket_for(self, cwd):
        """A worktree is named <project>.<TICKET-IDENT>; confirmed against the API."""
        base = os.path.basename(cwd.rstrip("/"))
        ident = base.rsplit(".", 1)[-1] if "." in base else None
        if not ident:
            return None
        try:
            return ident if json.loads(self.run("tickets", "get", ident).stdout).get("ok") else None
        except Exception:
            return None

    def keys_to_pane(self, pane):
        for text in ("/remote-control", "\x1b", "\r"):
            r = self.run("terminals", "send", pane, text, "--no-enter")
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout).strip() or "send failed")
            time.sleep(KEY_GAP_S)

    def keys_to_session(self, fsid, verb="sessions"):
        # --interrupt is the broker's own ESC; "\r" raw is a bare Enter. The
        # same flags travel through `tickets send` for a Worker on a paired
        # machine, where the server composes the bytes instead of the broker.
        for args in (("/remote-control", "--no-enter"), ("--interrupt",), ("\r", "--no-enter")):
            r = self.run(verb, "send", fsid, *args)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout).strip() or "send failed")
            time.sleep(KEY_GAP_S)


def transcript(sid):
    hits = glob.glob(os.path.expanduser("~/.claude/projects/*/" + str(sid) + ".jsonl"))
    return hits[0] if hits else None


def confirmed(sid, since):
    """Whether the session logged "/remote-control is active" after `since`.

    Claude Code appends a bridge_status line to the transcript when Remote
    Control comes up, so success is read off the session itself rather than
    inferred from keystrokes having been accepted.
    """
    path = transcript(sid)
    if not path:
        return None                              # nothing to check against
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 65536))
            tail = f.read().decode("utf-8", "replace")
    except Exception:
        return None
    for line in reversed(tail.splitlines()):
        if '"subtype":"bridge_status"' not in line or "is active" not in line:
            continue
        m = re.search(r'"timestamp":"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', line)
        if not m:
            continue
        try:
            at = calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            continue
        return at >= since - 2
    return False


def already_connected(pid):
    """Claude Code keeps its per-pid session file's bridgeSessionId set while
    Remote Control is up and clears it on disconnect. Typing /remote-control
    into a session that is already connected opens a dialog instead, and
    leaves the session sitting in it."""
    try:
        return bool(json.load(open(os.path.expanduser("~/.claude/sessions/%d.json" % int(pid)))).get("bridgeSessionId"))
    except Exception:
        return False


def wait_for_keychain(delay):
    """Hold until the switch is older than the Keychain cache."""
    try:
        switched = int(os.environ.get("SWITCHED_AT") or 0) / 1000.0
    except ValueError:
        switched = 0
    start = switched or time.time()
    remaining = start + delay - time.time()
    if remaining > 0:
        say("# waiting %.0fs for the sessions to see the new account" % remaining)
        time.sleep(remaining)


def main():
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    only = argv[argv.index("--session") + 1] if "--session" in argv and argv.index("--session") + 1 < len(argv) else None
    delay = DEFAULT_DELAY_S
    if "--delay" in argv and argv.index("--delay") + 1 < len(argv):
        try:
            delay = float(argv[argv.index("--delay") + 1])
        except ValueError:
            pass
    elif os.environ.get("RC_RECONNECT_DELAY"):
        try:
            delay = float(os.environ["RC_RECONNECT_DELAY"])
        except ValueError:
            pass

    d = api("/api/sessions")
    if d is None:
        notify("Can't reach the usage server, so Remote Control was not reconnected")
        return 1
    sessions = [s for s in (d.get("sessions") or []) if s.get("pid")]
    if only:
        sessions = [s for s in sessions if s.get("sessionId") == only]
    # Every session, whatever account it started on: /remote-control opens the
    # bridge under the account in the Keychain *now*, and it holds (verified:
    # a session started on account A, machine switched to B, reconnected as B
    # and stayed up). The drops seen earlier were the switches themselves.
    stale = [s for s in sessions if s.get("stale")]
    if not sessions:
        say("# no live sessions")
        return 0

    fr = Fredrin(fredrin_env([s["pid"] for s in sessions]))
    panes = fr.panes()
    if not fr.env.get("FREDRIN_TERM_API"):
        say("# no Fredrin terminal environment found on this machine")

    # Resolve every channel up front, while nothing has been typed anywhere.
    plan = []
    for s in sessions:
        cwd, label = s.get("cwd") or "", s.get("folder") or s.get("tty") or "?"
        pane = proc_env(s["pid"]).get("FREDRIN_TERM_ID")
        if pane and pane in panes:
            plan.append((s, "pane", pane, label))
        elif "/.fredrin/worktrees/" in cwd and fr.session_for(cwd):
            plan.append((s, "worker", fr.session_for(cwd), label))
        elif "/.fredrin/" in cwd and fr.ticket_for(cwd):
            plan.append((s, "ticket", fr.ticket_for(cwd), label))
        else:
            plan.append((s, None, None, label))

    for s, kind, target, label in plan:
        say("# %-28s %s" % (label, {"pane": "terminal tab " + str(target),
                                     "worker": "Worker session " + str(target),
                                     "ticket": "Worker on ticket " + str(target)}.get(
                                         kind, "no Fredrin channel — run /remote-control there yourself")))
    if dry:
        for s, kind, target, label in plan:
            if already_connected(s["pid"]):
                say("# %-28s Remote Control is on right now — would be left alone if still on at typing time" % label)
        return 0

    wait_for_keychain(delay)

    # Only now decide who is already connected. At the moment of the switch a
    # session's file still says connected — it takes it a second or two to
    # notice the account change and drop the bridge — so checking before the
    # wait skipped exactly the sessions the switch had just disconnected.
    connected = [s for s, kind, target, label in plan if already_connected(s["pid"])]
    for s in connected:
        say("# %-28s Remote Control already on — left alone" % (s.get("folder") or s.get("tty") or "?"))
    plan = [row for row in plan if row[0] not in connected]

    done, failed, unreachable = [], [], []
    sent_at = time.time()
    for s, kind, target, label in plan:
        try:
            if kind == "pane":
                fr.keys_to_pane(target)
            elif kind == "worker":
                fr.keys_to_session(target)
            elif kind == "ticket":
                fr.keys_to_session(target, verb="tickets")
            else:
                unreachable.append(label)
                continue
            done.append(label)
        except Exception as e:
            failed.append(label)
            say("# %s: could not type into it — %s" % (label, e))

    # Give each session a moment to bring the bridge up, then read the result
    # off its transcript. A session that never confirms is reported, since a
    # keystroke accepted by the PTY says nothing about what the TUI did with it.
    typed = [(s, label) for s, kind, target, label in plan if label in done]
    end = time.time() + 20
    pending = {s.get("sessionId"): label for s, label in typed if s.get("sessionId")}
    while pending and time.time() < end:
        for sid in list(pending):
            r = confirmed(sid, sent_at)
            if r is None or r:
                pending.pop(sid)
        if pending:
            time.sleep(1.0)
    for sid, label in pending.items():
        done.remove(label)
        failed.append(label)
        say("# %s: typed, but the session never reported Remote Control active" % label)

    parts = ["Remote Control back on in %d session%s" % (len(done), "" if len(done) == 1 else "s")]
    if connected:
        parts.append("%d already on" % len(connected))
    if failed:
        parts.append("%d failed" % len(failed))
    if unreachable:
        parts.append("%d not in Fredrin (%s)" % (len(unreachable), ", ".join(unreachable[:3])))
    if stale:
        parts.append("%d still run on the old account — Restart them from the menu to move them" % len(stale))
    say("# " + " · ".join(parts))
    notify(" · ".join(parts))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        try:
            os.makedirs(STATE, mode=0o700, exist_ok=True)
            # Append, one block per run, so a later run cannot erase the record
            # of what the switch itself did.
            fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write("=== " + time.strftime("%Y-%m-%d %H:%M:%S") + " " + " ".join(sys.argv[1:])
                        + ("" if os.environ.get("SWITCHED_AT") else " (by hand)") + "\n" + "\n".join(_log) + "\n")
        except Exception:
            pass
    sys.exit(code)
