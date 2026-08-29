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
import glob, json, os, re, shlex, shutil, subprocess, sys, time, urllib.request

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


UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def fredrin_panes():
    """Map a Fredrin pane id to the session id its shell launched claude with.

    A pane prints its own command line, so its scrollback carries the
    `--session-id`/`--resume` it started from. That is the launch id, not
    necessarily the live one (resuming mints a new id), so it is matched
    against the same flag in the process's argv rather than against the
    session id the meter reports.
    """
    out, cwds = {}, {}
    try:
        listing = subprocess.run(["fredrin", "terminals", "list"], capture_output=True,
                                 text=True, timeout=15).stdout
    except Exception:
        return out, cwds
    for line in listing.splitlines():
        m = re.search(r"(term-[0-9a-f-]+)\s+(.*?)\s\s+(/.*)$", line)
        if m:
            cwds[m.group(1)] = m.group(3).strip()
    for pane in re.findall(r"term-[0-9a-f-]+", listing):
        try:
            text = subprocess.run(["fredrin", "terminals", "read", pane, "--tail", "400"],
                                  capture_output=True, text=True, timeout=25).stdout
        except Exception:
            continue
        found = re.findall(r"--(?:session-id|resume)\s+(" + UUID + ")", text)
        if found:
            out[found[-1]] = pane          # last launch wins: the pane may have restarted
    return out, cwds


def fredrin_session_for(cwd):
    """The Fredrin session id owning a worktree. `sessions list` covers ticket
    Workers, and a worktree path belongs to exactly one ticket, so this is
    unambiguous where a bare cwd match would not be."""
    try:
        out = subprocess.run(["fredrin", "sessions", "list"], capture_output=True,
                             text=True, timeout=20).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if line.rstrip().endswith(cwd.rstrip("/")):
            m = re.search(r"(" + UUID + ")", line)
            if m:
                return m.group(1)
    return None


def relaunch_cmd(pid, session_id):
    """Rebuild a session's own launch command, pointed at its transcript.

    A pane carries Fredrin's settings, plugin dir, model and effort — relaunching
    it as a bare `claude` would keep the pane but drop all of that — so its argv
    is reused, minus any session flags, plus --resume.

    `ps` prints argv flattened with spaces and no quoting, so it cannot be split
    on whitespace: Fredrin's settings path contains one ("Application Support"),
    and splitting there produces `--settings /…/Application`, which claude
    rejects with "Settings file not found". Arguments are therefore cut at
    `--flag` boundaries, and everything up to the next flag is one value.
    """
    try:
        cmd = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return None
    if not cmd:
        return None
    pairs, flag, value = [], None, []
    for tok in cmd.split(" "):
        if not tok:
            continue
        if tok.startswith("--"):
            if flag is not None:
                pairs.append((flag, " ".join(value)))
            flag, value = tok, []
        elif flag is None:
            continue                      # the program path itself
        else:
            value.append(tok)
    if flag is not None:
        pairs.append((flag, " ".join(value)))

    out = ["claude"]
    for f, v in pairs:
        if f in ("--resume", "--session-id", "--fork-session"):
            continue
        out.append(f)
        if v:
            out.append(shlex.quote(v))
    out += ["--resume", session_id]
    return " ".join(out)


def ticket_for(cwd):
    """A worktree is named <project>.<TICKET-IDENT>, so the ticket id is the
    suffix — confirmed against the API before it is used to dispatch anything."""
    base = os.path.basename(cwd.rstrip("/"))
    ident = base.rsplit(".", 1)[-1] if "." in base else None
    if not ident:
        return None
    try:
        out = subprocess.run(["fredrin", "tickets", "get", ident], capture_output=True,
                             text=True, timeout=20).stdout
        return ident if json.loads(out).get("ok") else None
    except Exception:
        return None


def launch_id(pid):
    """The session id in a running claude's argv."""
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    m = re.search(r"--(?:session-id|resume)\s+(" + UUID + ")", cmd)
    return m.group(1) if m else None


def gone(pid, timeout=8.0):
    """Wait for a pid to actually exit — never type into a pane still running claude."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.25)
    return False


def transcript(sid):
    hits = glob.glob(os.path.expanduser("~/.claude/projects/*/" + str(sid) + ".jsonl"))
    return hits[0] if hits else None


def settle(sid, quiet=4.0, timeout=45.0):
    """Wait for a session to stop writing its transcript.

    SIGTERM mid-turn can cut a tool call half-done, so an interrupted session is
    given a chance to come to rest first. False means it never went quiet — the
    caller stops it anyway, but says so.
    """
    path = transcript(sid)
    if not path:
        return True
    end = time.time() + timeout
    while time.time() < end:
        try:
            if time.time() - os.path.getmtime(path) >= quiet:
                return True
        except Exception:
            return True
        time.sleep(1.0)
    return False


def interrupt(sid, pane=None, fsid=None):
    """Send ESC so the session ends its turn cleanly before being stopped.

    Workers take `sessions send --interrupt`; a terminal pane has no such flag,
    so ESC is typed straight in with no newline, which is what the key does.
    """
    try:
        if fsid:
            subprocess.run(["fredrin", "sessions", "send", fsid, "--interrupt"],
                           capture_output=True, timeout=20)
        elif pane:
            subprocess.run(["fredrin", "terminals", "send", pane, "\x1b", "--no-enter"],
                           capture_output=True, timeout=20)
        else:
            return False
    except Exception:
        return False
    return settle(sid)


def term_count():
    """How many tabs the Fredrin terminals panel currently holds."""
    try:
        out = subprocess.run(["fredrin", "terminals", "list"], capture_output=True, text=True, timeout=15).stdout
        return sum(1 for l in out.splitlines() if "term-" in l)
    except Exception:
        return -1


def reopen(cmd, name):
    """Open a session in a new Fredrin tab.

    Only Fredrin: these sessions live in Fredrin (or cmux) panes, and putting one
    in a Terminal.app window instead scatters them across apps the user is not
    working in. `terminals new` exits 0 and does nothing when the Terminals panel
    is closed, so a tab must actually appear; otherwise the session is left
    stopped and reported, with its resume command already on the clipboard.
    """
    if not shutil.which("fredrin"):
        return None
    before = term_count()
    try:
        subprocess.run(["fredrin", "terminals", "new", "1", "--cmd", cmd, "--name", name],
                       capture_output=True, timeout=20)
    except Exception:
        return None
    return "fredrin" if before >= 0 and term_count() > before else None


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
    # A ticket Worker lives in Fredrin's Worker surface, which the terminals API
    # cannot reach. Relaunching it as a loose terminal would keep the transcript
    # but drop the ticket association, so leave it to Fredrin.
    workers = [s for s in stale if "/.fredrin/worktrees/" in (s.get("cwd") or "")]
    stale = [s for s in stale if s not in workers]
    if workers:
        print("# ticket Workers — redispatched through Fredrin so the ticket keeps its "
              "Worker: %s" % ", ".join((w.get("folder") or "?") for w in workers))
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

    # Read the pane map before killing anything: a dead session's pane still
    # shows the command line it launched with, but the argv match needs the
    # process alive.
    panes, pane_cwds = fredrin_panes() if relaunch else ({}, {})
    launch_ids = {s.get("pid"): launch_id(s.get("pid")) for s in stale} if relaunch else {}
    # argv has to be read while the process is alive, and a bare `claude --resume`
    # would drop the flags Fredrin launches its panes with — its hooks settings and
    # plugin dir — leaving a session that no longer talks to Fredrin.
    cmds = ({s.get("pid"): relaunch_cmd(s.get("pid"), s.get("sessionId"))
             for s in stale + workers if s.get("sessionId")} if relaunch else {})

    # Resolve each pane while its session is still running, so the session can be
    # interrupted through that pane before it is stopped.
    pane_for, used = {}, set()
    for s in stale:
        pane = panes.get(launch_ids.get(s.get("pid")))
        if not pane:
            hits = [p for p, c in pane_cwds.items()
                    if c == (s.get("cwd") or "") and p not in panes.values() and p not in used]
            pane = hits[0] if len(hits) == 1 else None
        if pane:
            used.add(pane)
            pane_for[s.get("pid")] = pane

    killed = 0
    for s in stale:
        if relaunch and not interrupt(s.get("sessionId"), pane=pane_for.get(s.get("pid"))):
            print("# %s: did not go quiet after ESC — stopping it anyway"
                  % (s.get("folder") or s.get("tty") or "?"))
        try:
            os.kill(int(s["pid"]), 15)   # SIGTERM: let it shut down cleanly
            killed += 1
        except Exception:
            pass
    print(body, end="")
    if not relaunch:
        notify("Stopped %d session(s) · resume commands copied to the clipboard" % killed)
        return 0

    # Ticket Workers can't be reached through the terminals API, but Fredrin can
    # redispatch them itself: `tickets start` resumes the ticket's session and
    # keeps the association the ticket is tracked by. Only with --relaunch —
    # otherwise a Worker is reported and left alone.
    if relaunch:
        for w in workers:
            cwd, sid, pid = w.get("cwd") or "", w.get("sessionId"), w.get("pid")
            fsid = fredrin_session_for(cwd)
            cmd = cmds.get(pid)
            if not (fsid and cmd):
                print("# %s: could not resolve its Fredrin session — left running"
                      % (w.get("folder") or "?"))
                continue
            if not interrupt(sid, fsid=fsid):
                print("# %s: did not go quiet after ESC — stopping it anyway"
                      % (w.get("folder") or "?"))
            try:
                os.kill(int(pid), 15)
                killed += 1
            except Exception:
                pass
            if not gone(int(pid)):
                print("# %s: did not exit, not restarted" % (w.get("folder") or "?"))
                continue
            # The pane's shell outlives claude, so Fredrin still owns the session
            # and `sessions send` types straight into that same pane. `tickets
            # start` cannot do this: it sees the session as live and no-ops.
            try:
                subprocess.run(["fredrin", "sessions", "send", fsid, cmd],
                               capture_output=True, timeout=30)
                print("# %s: restarted in its own Worker pane" % (w.get("folder") or "?"))
            except Exception:
                print("# %s: restart failed — start it from Fredrin" % (w.get("folder") or "?"))

    opened, how = 0, set()
    for s in stale:
        sid, cwd = s.get("sessionId"), s.get("cwd") or os.path.expanduser("~")
        if not sid:
            continue
        # The pane resolved before the kill — it keeps the tab and its place in
        # the layout, instead of the session coming back somewhere else.
        pane = pane_for.get(s.get("pid"))
        cmd = cmds.get(s.get("pid"))
        if pane and cmd and gone(int(s["pid"])):
            # On exit claude prints "Resume this session with: claude --resume <id>",
            # so the pane names the session it just lost. Requiring that before
            # typing turns a by-elimination guess into a verified match — and stops
            # a command being typed into some other session as a chat message.
            try:
                tail = subprocess.run(["fredrin", "terminals", "read", pane, "--tail", "8"],
                                      capture_output=True, text=True, timeout=20).stdout
            except Exception:
                tail = ""
            if sid in tail:
                try:
                    subprocess.run(["fredrin", "terminals", "send", pane, cmd],
                                   capture_output=True, timeout=20)
                    opened += 1
                    how.add("in place")
                    continue
                except Exception:
                    pass
            else:
                print("# %s: pane did not confirm the session, opening a new tab instead"
                      % (s.get("folder") or "?"))
        where = reopen("cd %s && %s" % (shlex.quote(cwd), cmd or ("claude --resume " + sid)),
                       s.get("folder") or "claude")
        if where:
            opened += 1
            how.add(where)
        else:
            print("# %s: stopped, but no Fredrin tab could be opened (is the Terminals "
                  "panel open?) — its resume command is on the clipboard"
                  % (s.get("folder") or s.get("tty") or "?"))
    notify("Stopped %d · reopened %d via %s · commands still on the clipboard"
           % (killed, opened, "/".join(sorted(how)) or "nothing"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
