"""Reach the fredrin CLI from a process Fredrin did not spawn.

The CLI wants a family of FREDRIN_* variables — API address, tokens, the
terminals server, the broker socket — that exist only in the environment of
shells Fredrin spawned. launchd (the usage server) and SwiftBar (the menu) have
none of them, so `fredrin terminals ...` and `fredrin sessions send` fail there
with "missing env". This borrows the family from a running Fredrin-spawned
process: on macOS `ps -E` shows a process's environment to its own user.
"""
import os, re, subprocess

# The two keys that name the donor's own pane, which must not be borrowed.
PANE_ONLY_KEYS = ("FREDRIN_TERM_ID", "FREDRIN_CHAT_SESSION_SINK")


def parse_env(blob):
    """Pick the environment out of `ps -E` output.

    ps prints argv and then the environment as one space-joined line, with no
    quoting, and Fredrin's paths contain a space ("Application Support"). So a
    value runs from its `NAME=` up to the next `NAME=`, not to the next space.
    """
    out = {}
    hits = list(re.finditer(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=", blob))
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(blob)
        out[m.group(1)] = blob[m.end():end].strip()
    return out


def proc_env(pid):
    try:
        blob = subprocess.run(["ps", "-E", "-ww", "-o", "command=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return {}
    return parse_env(blob)


def fredrin_env(pids=()):
    """An environment the fredrin CLI can talk to Fredrin from.

    Prefer our own (a Fredrin shell already has it); otherwise borrow the keys
    from one of the given pids, or failing that from any process that has them.
    """
    env = dict(os.environ)
    env["PATH"] = ":".join([os.path.expanduser("~/.fredrin/bin"), "/opt/homebrew/bin",
                            "/usr/local/bin", env.get("PATH", "/usr/bin:/bin")])
    if env.get("FREDRIN_TERM_API") and env.get("FREDRIN_BROKER_SOCK"):
        return env
    candidates = [proc_env(p) for p in pids]
    if not any(c.get("FREDRIN_TERM_API") for c in candidates):
        try:
            blob = subprocess.run(["ps", "-E", "-ww", "-eo", "command="],
                                  capture_output=True, text=True, timeout=15).stdout
            candidates += [parse_env(l) for l in blob.splitlines() if "FREDRIN_TERM_API=" in l]
        except Exception:
            pass
    for c in candidates:
        if c.get("FREDRIN_TERM_API"):
            for k, v in c.items():
                if k.startswith("FREDRIN_") and k not in PANE_ONLY_KEYS and v:
                    env[k] = v
            break
    return env


def apply(pids=()):
    """Install the borrowed environment into this process, so every later
    `subprocess.run(["fredrin", ...])` just works."""
    env = fredrin_env(pids)
    os.environ.update(env)
    return bool(env.get("FREDRIN_TERM_API"))
