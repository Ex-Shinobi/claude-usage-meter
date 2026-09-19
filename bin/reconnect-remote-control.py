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

How the command is typed depends on the channel, because only one of them
carries raw bytes. A Fredrin terminal pane is a straight PTY write, so it gets
the three keystrokes a human would type:

    /remote-control      typed without Enter
    ESC                  closes the slash-command picker (and ends a running
                         turn, so the command is not left queued behind it)
    Enter                submits the command

The broker and ticket channels carry flags, not bytes. A body is delivered to
the TUI as a bracketed paste, and a CR passed as payload text lands inside that
paste, where the TUI swallows it as content: the command is left sitting unsent
in the composer, and the next send is appended to it on a new line rather than
replacing it. Submitting is a flag there, so it has to be sent as one — the ESC
as `--interrupt`, then the body with no `--no-enter`, which makes the broker
(or, for a ticket, the Fredrin server) type the CR as its own write 120ms after
the body, outside the paste:

    fredrin <sessions|tickets> send <target> --interrupt
    fredrin <sessions|tickets> send <target> /remote-control

Every session those keystrokes went into cleanly then also gets, through the
same channel, once its Remote Control reports active (or, if it never does,
once the wait for that is over), `resume`, so the interrupted work carries on:
the text and then a bare Enter on a pane, and the one submitting send on the
other two.

Every session, because by the time anything can be typed the work is stopped
either way: the switch itself kills a turn in flight (its API call loses
authorization, or the account was switched *because* the old one ran out of
quota, which had already killed the turn), and the ESC kills whatever survived
that. The live status in ~/.claude/sessions/<pid>.json used to decide this and
could not: it is read ~35 seconds after the switch, long after a killed turn
has gone back to "idle", and some sessions leave it stale for hours. A session
that was genuinely idle loses nothing it minds — "resume" at an idle prompt
picks the last turn back up — while a session left unresumed sits dead until a
human notices.

Delivery channels, in order of preference:
  - a Fredrin terminal tab: the claude process carries FREDRIN_TERM_ID in its
    environment, which names its pane outright — no scrollback matching.
  - a ticket Worker (cwd under ~/.fredrin/worktrees): the broker session that
    owns the worktree, via `fredrin sessions send`.
  - a Worker the local broker does not own (run by a paired fredrin-agent):
    its ticket, via `fredrin tickets send`, which goes through the Fredrin API.
  - anything else (cmux, Terminal.app): no channel — reported, not touched.

Each one is checked before it is chosen, from a single `fredrin sessions list`:
the pane has to still exist, the broker session has to be live rather than a
hibernated or reapable one, and the ticket's Worker has to sit on a machine
that is still checking in. A channel that is not credible falls through to the
next, and a session left with none is reported rather than typed into — every
`fredrin ... send` exits 0 on a write the TUI may never act on, so an exit code
is not evidence that anything arrived. The transcript is (see confirmed()).

"On other machines" in that listing usually means this one: a Worker the local
fredrin-agent runs belongs to the agent's own broker, not the desktop app's, so
the CLI can only reach it by ticket, through the Fredrin API and back. The name
it is filed under is the hostname captured when the agent was paired, which
drifts, so the log says when that machine is in fact this machine.

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
  reconnect-remote-control.py --dry-run   show which channel each would use,
                                          and which would get "resume"
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
KEYCHAIN_CACHE_S = 30.0          # Claude Code's Keychain read cache
DEFAULT_DELAY_S = KEYCHAIN_CACHE_S + 5
KEY_GAP_S = 0.4                  # let the TUI draw the picker before ESC lands
# The bridge_status line is appended to the transcript as the bridge comes up,
# which can be before the TUI has finished redrawing around the status message.
# Text typed during that redraw can land before the prompt is ready for it, so
# "resume" waits this long past the confirmation.
RESUME_SETTLE_S = 1.0

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
        self._listing = None

    def run(self, *args, timeout=20):
        return subprocess.run([self.bin] + list(args), capture_output=True, text=True,
                              timeout=timeout, env=self.env)

    def panes(self):
        try:
            return set(re.findall(r"term-[0-9a-f-]+", self.run("terminals", "list").stdout))
        except Exception:
            return set()

    def listing(self):
        """`fredrin sessions list`, read once, as (by_cwd, by_ticket).

        The listing has two halves, because there are two brokers. The local
        half is one row per PTY the desktop app's broker spawned:

            <marker> <sessionId>  <ticketId>  <state>  <cwd>

        where the marker is "●" live, "💤" hibernated (idle-swept) or "✗" a
        reapable zombie — only a live one can be typed into. The remote half,
        under an "-- on other machines --" header, is one row per Worker some
        other broker owns, this machine's own fredrin-agent included:

            <marker> <jobId>  <ticketRef>  <machine>  <machineSource>  <status>

        where "●" means that machine is still checking in and "○" that it has
        gone quiet. Those rows are addressed by ticket, never by session id.

        Parsed here rather than at each call site so the whole plan is built
        from one snapshot, and one subprocess.
        """
        if self._listing is not None:
            return self._listing
        by_cwd, by_ticket, remote = {}, {}, False
        try:
            out = self.run("sessions", "list").stdout
        except Exception:
            out = ""
        for line in out.splitlines():
            if line.startswith("-- on other machines"):
                remote = True
                continue
            fields = line.split(None, 5 if remote else 4)
            if len(fields) < (6 if remote else 5) or fields[0] not in ("●", "○", "💤", "✗"):
                continue
            if remote:
                by_ticket[fields[2]] = {"marker": fields[0], "machine": fields[3],
                                        "where": fields[4], "status": fields[5],
                                        "online": fields[0] == "●"}
            else:
                # A cwd belongs to one ticket, so it identifies the row. The
                # marker, not the state word, says whether it can take input:
                # the state of a live session is its lifecycle (running,
                # needsInput, …), which is never a reason to skip it.
                by_cwd[fields[4].rstrip("/")] = {"marker": fields[0], "sessionId": fields[1],
                                                 "state": fields[3], "live": fields[0] == "●"}
        self._listing = (by_cwd, by_ticket)
        return self._listing

    def send(self, *args):
        """One `fredrin ... send`, raising on failure, then a pause for the TUI.

        A zero exit says the write was accepted, not that the TUI acted on it —
        on the ticket channel it says only that the Fredrin API published the
        event. Delivery is confirmed from the transcript instead.
        """
        r = self.run(*args)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip() or "send failed")
        time.sleep(KEY_GAP_S)

    def keys_to_pane(self, pane):
        """A pane is a raw PTY write, so the bytes a human would type land as
        typed: the command, the ESC that closes the picker, then the Enter."""
        for text in ("/remote-control", "\x1b", "\r"):
            self.send("terminals", "send", pane, text, "--no-enter")

    def keys_to_session(self, fsid, verb="sessions"):
        """The broker and the ticket channel take flags, not bytes.

        A body goes to the TUI as a bracketed paste. A CR handed over as
        payload text ("\\r" with --no-enter, which is what a bare Enter looks
        like here) lands inside that paste and is swallowed as content, so the
        command sits unsent in the composer and the next send is appended to it
        on a new line — observed on a Worker that collected "/remote-control",
        "resume" and a later probe in its composer across two runs, and
        submitted all three at once when something finally pressed Enter.

        So the submit is expressed as the flag it is: --interrupt for the ESC,
        on its own, then the body with Enter left on, which has the broker (or,
        for a ticket, the server) type the CR as its own write 120ms after the
        body. The picker never opens, because a paste does not open it, and the
        command is submitted straight from the composer.
        """
        self.send(verb, "send", fsid, "--interrupt")
        self.send(verb, "send", fsid, "/remote-control")

    def resume(self, kind, target):
        """Type "resume" and submit it through the channel the /remote-control
        keystrokes went through, in that channel's own shape: the text and then
        a bare Enter on a pane, one submitting send everywhere else."""
        if kind == "pane":
            for text in ("resume", "\r"):
                self.send("terminals", "send", target, text, "--no-enter")
        else:
            self.send("tickets" if kind == "ticket" else "sessions", "send", target, "resume")


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


def ticket_ident(cwd):
    """The ticket a worktree belongs to: it is named <project>.<TICKET-IDENT>.

    Matched against the listing's own ticket refs rather than guessed at, so an
    older worktree named for a bare suffix simply finds nothing — typing into
    the wrong session is the failure this whole check exists to avoid.
    """
    base = os.path.basename((cwd or "").rstrip("/"))
    return base.rsplit(".", 1)[-1] if "." in base else None


def this_machine():
    """The name this machine's own fredrin-agent is paired under, if it has one.

    It is the hostname captured when the agent was paired, so it drifts away
    from the current one — which is why a Worker this very machine is running
    turns up in the listing under "on other machines".
    """
    try:
        with open(os.path.expanduser("~/.fredrin/agent.json")) as f:
            return (json.load(f).get("name") or "").strip()
    except Exception:
        return ""


def session_file(pid):
    """Claude Code's per-pid session file, ~/.claude/sessions/<pid>.json, or {}."""
    try:
        with open(os.path.expanduser("~/.claude/sessions/%d.json" % int(pid))) as f:
            return json.load(f)
    except Exception:
        return {}


def already_connected(pid):
    """Claude Code keeps its per-pid session file's bridgeSessionId set while
    Remote Control is up and clears it on disconnect. Typing /remote-control
    into a session that is already connected opens a dialog instead, and
    leaves the session sitting in it."""
    return bool(session_file(pid).get("bridgeSessionId"))


def display_labels(sessions):
    """Map each session's pid to the name its lines are printed under.

    A session is recognisable by its folder, but the folder is not unique —
    several live sessions genuinely sit in the same one. Only the printed name
    has to tell them apart, since the bookkeeping keys off the pid, so a name
    shared by more than one session takes a suffix and a name that stands alone
    prints exactly as the folder does. The suffix is the tty where that
    separates the whole group, because it says which window to look in, and the
    pid otherwise, because that always separates them.
    """
    groups = {}
    for s in sessions:
        groups.setdefault(s.get("folder") or s.get("tty") or "?", []).append(s)
    labels = {}
    for name, group in groups.items():
        ttys = [s.get("tty") for s in group]
        for s in group:
            if len(group) == 1:
                labels[s["pid"]] = name
            elif s.get("tty") and ttys.count(s["tty"]) == 1:
                labels[s["pid"]] = "%s %s" % (name, s["tty"])
            else:
                labels[s["pid"]] = "%s pid %s" % (name, s["pid"])
    return labels


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

    # Resolve every channel up front, while nothing has been typed anywhere, and
    # from one snapshot of what Fredrin can still reach. A channel is only taken
    # if the listing says it is credible: an exit code cannot tell a live TUI
    # from a hibernated one, so the check has to happen before the send, not be
    # inferred from it afterwards. Where the preferred channel is not credible
    # the next one is tried, and a session left with none is reported.
    # The label is for the reader only; every list below tracks sessions by pid,
    # which the API guarantees is present and unique where the label is neither.
    labels = display_labels(sessions)
    by_cwd, by_ticket = fr.listing()
    here = this_machine()
    plan, notes = [], {}                         # notes: pid -> why, for the reader
    for s in sessions:
        cwd, label = (s.get("cwd") or "").rstrip("/"), labels[s["pid"]]
        pane = proc_env(s["pid"]).get("FREDRIN_TERM_ID")
        row = by_cwd.get(cwd)
        remote = by_ticket.get(ticket_ident(cwd) or "\0")
        if pane and pane in panes:
            plan.append((s, "pane", pane, label))
        elif row and row["live"]:
            plan.append((s, "worker", row["sessionId"], label))
        elif remote and remote["online"]:
            plan.append((s, "ticket", ticket_ident(cwd), label))
            if remote["machine"] and remote["machine"] == here:
                notes[s["pid"]] = ("on %s, which is this machine's own fredrin-agent"
                                   % remote["machine"])
            else:
                notes[s["pid"]] = "on %s" % (remote["machine"] or "another machine")
        else:
            plan.append((s, None, None, label))
            if row:
                notes[s["pid"]] = "its broker session is %s, so nothing typed would land" % row["state"]
            elif remote:
                notes[s["pid"]] = "its Worker's machine %s has gone quiet" % (remote["machine"] or "?")

    for s, kind, target, label in plan:
        what = {"pane": "terminal tab " + str(target),
                "worker": "Worker session " + str(target),
                "ticket": "Worker on ticket " + str(target)}.get(
                    kind, "no Fredrin channel — run /remote-control there yourself")
        note = notes.get(s["pid"])
        say("# %-28s %s%s" % (label, what, (" — " + note) if note else ""))
    if dry:
        for s, kind, target, label in plan:
            if already_connected(s["pid"]):
                say("# %-28s Remote Control is on right now — would be left alone if still on at typing time" % label)
            elif kind:
                say("# %-28s would get \"resume\" after its /remote-control" % label)
        return 0

    wait_for_keychain(delay)

    # Only now decide who is already connected. At the moment of the switch a
    # session's file still says connected — it takes it a second or two to
    # notice the account change and drop the bridge — so checking before the
    # wait skipped exactly the sessions the switch had just disconnected.
    connected = [row for row in plan if already_connected(row[0]["pid"])]
    for s, kind, target, label in connected:
        say("# %-28s Remote Control already on — left alone" % label)
    left_alone = {row[0]["pid"] for row in connected}
    plan = [row for row in plan if row[0]["pid"] not in left_alone]

    # Sessions are counted by what the transcript went on to say, not by what
    # the sends returned: `done` is only the ones that logged Remote Control
    # active, `unverified` the ones whose result could not be read at all, and
    # `failed` the ones that were typed into and never reported it. Counting a
    # zero exit as success is what let a Worker sit disconnected for two days
    # while every run reported it reconnected.
    done, failed, unreachable, unverified = [], [], [], []   # pids: labels repeat
    typed = []                                   # rows whose keystrokes all went in
    sent_at = time.time()
    for row in plan:
        s, kind, target, label = row
        try:
            if kind == "pane":
                fr.keys_to_pane(target)
            elif kind == "worker":
                fr.keys_to_session(target)
            elif kind == "ticket":
                fr.keys_to_session(target, verb="tickets")
            else:
                unreachable.append(s["pid"])
                continue
            typed.append(row)
        except Exception as e:
            failed.append(s["pid"])
            say("# %s: could not type into it — %s" % (label, e))
            # Some of the keys may have landed, so /remote-control could be
            # sitting half-typed in its prompt; "resume" typed now would be
            # appended to it rather than submitted on its own.
            say("# %s: resume not typed — a half-typed command may be sitting in its prompt" % label)

    # Every session that took the keystrokes is owed a "resume": its turn was
    # ended by the switch or by the ESC that followed it. The resume is typed
    # once the session's bridge reports active (and RESUME_SETTLE_S after that),
    # so it does not land while /remote-control is still running.
    owed = list(typed)
    due = {}                                     # pid -> when its resume may be typed
    resumed, not_resumed = [], []

    def resume(row):
        s, kind, target, label = row
        owed.remove(row)
        due.pop(s["pid"], None)
        try:
            fr.resume(kind, target)
            resumed.append(s["pid"])
            say("# %s: typed resume, so an interrupted turn carries on" % label)
        except Exception as e:
            not_resumed.append(s["pid"])
            say("# %s: resume could not be typed — %s" % (label, e))

    # Give each session a moment to bring the bridge up, then read the result
    # off its transcript. A session that never confirms is reported, since a
    # keystroke accepted by the PTY says nothing about what the TUI did with it.
    # A session with no sessionId has no transcript to read at all, so it can
    # only ever be unverified: its keystrokes went in and nothing can say more.
    end = time.time() + 20
    pending = {}
    for s, kind, target, label in typed:
        if s.get("sessionId"):
            pending[s["pid"]] = s["sessionId"]
        else:
            unverified.append(s["pid"])
            say("# %s: typed, but it has no transcript to confirm Remote Control from" % label)
    owed_pids = {row[0]["pid"] for row in owed}
    while (pending or due) and time.time() < end:
        for pid, sid in list(pending.items()):
            r = confirmed(sid, sent_at)
            if r is None or r:
                pending.pop(pid)
                if r:
                    done.append(pid)
                    if pid in owed_pids:
                        due[pid] = time.time() + RESUME_SETTLE_S
                else:
                    unverified.append(pid)
                    say("# %s: typed, but its transcript could not be read" % labels[pid])
        for row in [row for row in owed if due.get(row[0]["pid"], end + 1) <= time.time()]:
            resume(row)
        if pending or due:
            time.sleep(1.0)
    for pid in pending:
        failed.append(pid)
        say("# %s: typed, but the session never reported Remote Control active" % labels[pid])

    # Whatever is still owed confirmed too late to be typed inside the window,
    # never confirmed, or had no transcript to check. Its turn was ended either
    # way, so it gets "resume" now rather than being left dead.
    for row in list(owed):
        wait = due.get(row[0]["pid"], 0) - time.time()
        if wait > 0:
            time.sleep(wait)
        resume(row)

    parts = ["Remote Control back on in %d session%s" % (len(done), "" if len(done) == 1 else "s")]
    if resumed:
        parts.append("resumed %d" % len(resumed))
    if connected:
        parts.append("%d already on" % len(connected))
    if unverified:
        parts.append("%d unverified" % len(unverified))
    if failed:
        parts.append("%d failed" % len(failed))
    if not_resumed:
        parts.append("%d could not be resumed" % len(not_resumed))
    if unreachable:
        parts.append("%d not in Fredrin (%s)" % (len(unreachable),
                                                 ", ".join(labels[pid] for pid in unreachable[:3])))
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
