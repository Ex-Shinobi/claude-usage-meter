#!/usr/bin/env python3
# SwiftBar plugin — Claude usage in the menu bar.
#
# Menu bar : a rendered image  ● I 35 · ● B 100 · ● G 100
#            Each dot is independently colored by that account's Fable %
#            (SwiftBar's color= applies to a whole text line, so the title is
#             drawn as a retina PNG to get per-dot colors.)
# Dropdown : per-account ▰▱ segmented bars, %s, reset times.
#
# Refreshes every 5 min; each account fetched fresh, spaced out for the rate limit.
import base64, io, json, os, time, urllib.request
from datetime import datetime

BASE = "http://127.0.0.1:4177"

# The server's API is token-gated so that other accounts on this Mac can't read
# it off the port. The token file is 0600 in the state dir, outside the repo.
STATE = os.environ.get("CLAUDE_USAGE_HOME") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "claude-usage-meter")

def read_token():
    try:
        with open(os.path.join(STATE, "token")) as f:
            return f.read().strip()
    except Exception:
        return None

TOKEN = read_token()

def get(url, t=10):
    req = urllib.request.Request(url)
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=t) as r:
            return json.load(r)
    except Exception:
        return None

def level(p):
    return "crit" if p >= 95 else "warn" if p >= 80 else "ok"

# menu-bar dot colors (vivid enough to read at 10px on the dark bar)
RGB = {"ok": (94, 203, 138), "warn": (230, 189, 91), "crit": (236, 113, 137), "none": (150, 158, 175)}
# dropdown row colors
def col(p):
    return "#e0607a" if p >= 95 else "#e0b34f" if p >= 80 else None

def when(rs):
    if not rs:
        return ""
    try:
        d = datetime.fromisoformat(rs.replace("Z", "+00:00")).astimezone()
        return "resets " + d.strftime("%a %-m/%-d %-I:%M %p")
    except Exception:
        return ""

def ago(ms):
    """'2d ago' for a fetchedAt epoch-ms, so cached numbers can't pass as live."""
    if not ms:
        return "unknown age"
    sec = max(0, time.time() - ms / 1000.0)
    if sec < 90:
        return "just now"
    if sec < 5400:
        return str(int(round(sec / 60))) + "m ago"
    if sec < 129600:
        return str(int(round(sec / 3600))) + "h ago"
    return str(int(round(sec / 86400))) + "d ago"

def segbar(p):
    p = max(0, min(100, p))
    f = int(round(p / 10.0))
    return "▰" * f + "▱" * (10 - f)

def fable(u):
    for l in (u.get("limits") or []):
        if str(l.get("scope") or "").lower().startswith("fable") and l.get("percent") is not None:
            return l["percent"]
    return None

def title_image(parts):
    """parts = [(letter, text, level|'none'), ...] -> base64 PNG.

    Drawn at @2x for retina sharpness and saved at 144 dpi so macOS reads it as
    a 2x image and lays it out at half the pixel size — i.e. real menu-bar
    metrics (13pt text), not double-size.
    """
    from PIL import Image, ImageDraw, ImageFont
    S = 2                      # retina scale (pixels per point)
    PT = 13                    # macOS menu-bar text size, in points
    H = 22 * S                 # menu bar item height in pixels
    PAD, DOT_R, GAP = 3 * S, 2.5 * S, 4 * S
    SEP = "   "   # plain spacing between accounts (no divider dot)
    font = None
    for p in ("/System/Library/Fonts/SFNS.ttf",
              "/System/Library/Fonts/SFNSDisplay.ttf",
              "/System/Library/Fonts/Helvetica.ttc"):
        try:
            font = ImageFont.truetype(p, PT * S)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    # Bold face for the account initial. SFNS is variable — set the Bold named
    # instance when available; otherwise fall back to Helvetica Bold.
    bold = None
    try:
        b = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", PT * S)
        names = [n.decode() if isinstance(n, bytes) else n for n in b.get_variation_names()]
        pick = next((n for n in names if n.strip().lower() == "bold"), None)
        if pick:
            b.set_variation_by_name(pick)
            bold = b
    except Exception:
        bold = None
    if bold is None:
        for p in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/SFNSDisplay.ttf"):
            try:
                bold = ImageFont.truetype(p, PT * S, index=1)  # Helvetica Bold
                break
            except Exception:
                continue
    if bold is None:
        bold = font

    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    def w(s, f=None):
        return tmp.textbbox((0, 0), s, font=f or font)[2]

    total = PAD
    for i, (letter, text, lv) in enumerate(parts):
        if i:
            total += w(SEP)
        total += DOT_R * 2 + GAP + w(letter, bold) + GAP + w(text)
    total += PAD

    img = Image.new("RGBA", (int(total), H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x = PAD
    cy = H // 2
    for i, (letter, text, lv) in enumerate(parts):
        if i:
            d.text((x, cy), SEP, font=font, fill=(255, 255, 255, 130), anchor="lm")
            x += w(SEP)
        # colored dot, then the bold initial, then the number
        d.ellipse([x, cy - DOT_R, x + DOT_R * 2, cy + DOT_R], fill=RGB[lv])   # per-dot color
        x += DOT_R * 2 + GAP
        d.text((x, cy), letter, font=bold, fill=(255, 255, 255, 255), anchor="lm")
        x += w(letter, bold) + GAP
        d.text((x, cy), text, font=font, fill=(255, 255, 255, 235), anchor="lm")
        x += w(text)

    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(72 * S, 72 * S))  # 144dpi => macOS lays it out at 1/2 pixel size
    return base64.b64encode(buf.getvalue()).decode()

SWITCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "switch-account.sh")
RESTART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "restart-stale-sessions.py")

state = get(BASE + "/api/state", 4)
if not state:
    print("CC ⚠")
    print("---")
    if TOKEN is None:
        print("No API token at " + os.path.join(STATE, "token"))
        print("Start the server once to create it | font=Menlo size=12 color=#8b93a7")
    else:
        print("Usage server not running")
    print("Open folder | bash=/usr/bin/open param1=" + os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + " terminal=false")
    raise SystemExit

accounts = state.get("accounts", [])

usage = {}
for i, a in enumerate(accounts):
    if i:
        time.sleep(2)
    usage[a["slug"]] = get(BASE + "/api/usage?slug=" + a["slug"] + "&force=1")

# ----- menu bar title -----
parts, text_fallback = [], []
for a in accounts:
    u = usage.get(a["slug"])
    letter = ((a.get("email") or a.get("label") or "?")[:1]).upper()
    fv = fable(u) if (u and u.get("ok")) else None
    if fv is None:
        parts.append((letter, "—", "none"))
        text_fallback.append("● " + letter + " —")
    else:
        pct = str(round(fv)) + "%"
        parts.append((letter, pct, level(round(fv))))
        text_fallback.append("● " + letter + " " + pct)

printed = False
try:
    print("| image=" + title_image(parts))   # per-dot colors
    printed = True
except Exception:
    pass
if not printed:
    print("   ".join(text_fallback) if text_fallback else "CC —")
print("---")

# ----- dropdown: segmented bars per account -----
for a in accounts:
    u = usage.get(a["slug"])
    name = (a.get("label") or a.get("email") or a["slug"]) + (" active" if a.get("active") else "")
    print(name + " | size=14")
    if u and u.get("ok"):
        # The server serves its last good numbers when a live fetch fails, so say
        # so — otherwise days-old percentages and reset times read as current.
        if u.get("stale"):
            why = str(u.get("note") or "fetch failed")
            print("  \u26a0 cached " + ago(u.get("fetchedAt")) + " \u00b7 " + why +
                  " | font=Menlo size=11 color=#8b93a7")
        rows = [("Session 5h", u.get("fiveHour"), u.get("fiveHourResets")),
                ("Weekly    ", u.get("sevenDay"), u.get("sevenDayResets"))]
        for l in (u.get("limits") or []):
            if l.get("scope"):
                rows.append((l["scope"][:10].ljust(10), l.get("percent"), l.get("resets_at")))
        if u.get("extraUsage") and u["extraUsage"].get("enabled"):
            rows.append(("Extra     ", u["extraUsage"].get("percent"), None))
        any_row = False
        for label, val, rs in rows:
            if val is None:
                continue
            any_row = True
            line = "  " + label + "  " + segbar(val) + " " + str(round(val)).rjust(3) + "%"
            w2 = when(rs)
            if w2:
                line += "  " + w2
            c = col(val)
            print(line + " | font=Menlo size=12" + (" color=" + c if c else ""))
        if not any_row:
            print("  no active limits | font=Menlo size=12 color=#8b93a7")
    else:
        err = (u or {}).get("error", "")
        print("  usage unavailable" + ((" (" + err + ")") if err else "") + " | font=Menlo size=12 color=#8b93a7")
    # Switching is a Keychain write; every running session re-reads it within
    # about a minute, so one click moves them all — no restarts, no re-login.
    if not a.get("active"):
        print("  \u21c4 Switch to this account | bash=" + SWITCH + " param1=" + a["slug"] +
              " terminal=false refresh=true font=Menlo size=11")
    print("---")

# A switch only reaches sessions that start afterwards — Claude Code pins its
# token per process. Surface the ones still on the old account, since they are
# invisible otherwise and quietly keep spending the wrong account's limits.
sess = get(BASE + "/api/sessions", 4) or {}
stale = [x for x in (sess.get("sessions") or []) if x.get("stale")]
if stale:
    by_acct = {}
    for x in stale:
        by_acct.setdefault(x.get("email") or "unknown", []).append(x)
    print("\u27f3 " + str(len(stale)) + " session" + ("" if len(stale) == 1 else "s") +
          " on another account | font=Menlo size=12 color=#e0b34f")
    for acct, rows in sorted(by_acct.items()):
        print("--" + acct + " | font=Menlo size=12")
        for x in rows:
            print("--" + (x.get("tty") or "?").ljust(9) + " " + (x.get("folder") or "") +
                  " | font=Menlo size=11 color=#8b93a7")
    print("--Restart them \u00b7 stops and reopens each | bash=" + RESTART +
          " param1=--relaunch terminal=false refresh=true")
    print("--Stop them \u00b7 copy resume commands | bash=" + RESTART +
          " param1=--kill terminal=false refresh=true")
    print("--Just copy the resume commands | bash=" + RESTART + " terminal=false refresh=true")
print("Refresh now | refresh=true")
# carry the token so the first click authorizes the browser; the server then
# sets a cookie and redirects to the bare URL
link = BASE + ("/?t=" + TOKEN if TOKEN else "")
print(BASE.replace("http://", "") + " | href=" + link + " size=11 color=#8b93a7")
