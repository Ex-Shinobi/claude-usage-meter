#!/usr/bin/env node
"use strict";

/*
 * Claude Usage Meter — a localhost page that shows your Claude rate-limit usage
 * (the same numbers as /usage in the terminal). Read-only: no switching, no login.
 *
 * It reads your login token(s) locally to query the usage endpoint:
 *   - active account: macOS Keychain "Claude Code-credentials" (authoritative)
 *   - any other saved account: its snapshot in the state dir (refreshed as needed)
 * Nothing is ever written to your Claude Code credentials.
 *
 * Security model — see README "Security" for the long version:
 *   - State (credential snapshots, session records, API token) lives OUTSIDE this
 *     repo, in ~/.config/claude-usage-meter, 0700 dirs / 0600 files. Snapshots hold
 *     OAuth refresh tokens; they must never sit in a git working tree.
 *   - Every request needs the API token, so other accounts on this Mac can't read
 *     your data off the port.
 *   - Host + Origin + Sec-Fetch-Site checks stop websites you visit from reaching
 *     the server (DNS rebinding, and cross-site GETs with side effects).
 *   - No token or credential is ever included in a response body.
 */

const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");
const { execFileSync } = require("child_process");

const HOME = os.homedir();
const CRED_FILE = path.join(HOME, ".claude", ".credentials.json");
const CLAUDE_JSON = path.join(HOME, ".claude.json");
const KC_SERVICE = "Claude Code-credentials";
const KC_ACCOUNT = os.userInfo().username;
const HOST = "127.0.0.1";
const PORT = Number(process.env.PORT) || 4177;

// ---------- state dir (outside the repo, on purpose) ----------
const STATE_DIR = process.env.CLAUDE_USAGE_HOME ||
  path.join(process.env.XDG_CONFIG_HOME || path.join(HOME, ".config"), "claude-usage-meter");
const ACCT_DIR = path.join(STATE_DIR, "accounts");
const SESS_DIR = path.join(STATE_DIR, "sessions");           // hook records (by sessionId)
const TOKEN_FILE = path.join(STATE_DIR, "token");
const CLAUDE_SESS_DIR = path.join(HOME, ".claude", "sessions"); // Claude's own per-pid files

const USAGE_URL = "https://api.anthropic.com/api/oauth/usage";
const TOKEN_URL = "https://platform.claude.com/v1/oauth/token";
const CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"; // public OAuth client id, not a secret

function ensureDirs() {
  for (const d of [STATE_DIR, ACCT_DIR, SESS_DIR]) {
    fs.mkdirSync(d, { recursive: true, mode: 0o700 });
    // mkdir's mode is umask-filtered and ignored for dirs that already exist
    try { fs.chmodSync(d, 0o700); } catch (_) {}
  }
}

// One-time move of state out of the repo checkout. Copy + verify first; the
// source is only unlinked once the destination is known-good, so a failure
// here loses nothing.
function migrateOut(from, to, what) {
  if (path.resolve(from) === path.resolve(to)) return;
  let files = [];
  try { files = fs.readdirSync(from).filter((f) => f.endsWith(".json")); } catch (_) { return; }
  let moved = 0;
  for (const f of files) {
    const src = path.join(from, f);
    const dst = path.join(to, f);
    try {
      if (fs.existsSync(dst)) continue;
      const buf = fs.readFileSync(src);
      JSON.parse(buf.toString("utf8"));
      const tmp = dst + ".tmp-" + process.pid;
      fs.writeFileSync(tmp, buf, { mode: 0o600 });
      fs.renameSync(tmp, dst);
      fs.unlinkSync(src);
      moved++;
    } catch (e) {
      console.error("migrate: left " + what + "/" + f + " where it was (" + e.message + ")");
    }
  }
  if (moved) console.log("moved " + moved + " " + what + " file(s) out of the repo → " + to);
  try { if (!fs.readdirSync(from).length) fs.rmdirSync(from); } catch (_) {}
}

function ensureToken() {
  try {
    const t = fs.readFileSync(TOKEN_FILE, "utf8").trim();
    if (t.length >= 32) { try { fs.chmodSync(TOKEN_FILE, 0o600); } catch (_) {} return t; }
  } catch (_) {}
  const t = crypto.randomBytes(32).toString("hex");
  const tmp = TOKEN_FILE + ".tmp-" + process.pid;
  fs.writeFileSync(tmp, t + "\n", { mode: 0o600 });
  fs.renameSync(tmp, TOKEN_FILE);
  return t;
}

try {
  ensureDirs();
  migrateOut(path.join(__dirname, "accounts"), ACCT_DIR, "accounts");
  migrateOut(path.join(__dirname, "sessions"), SESS_DIR, "sessions");
} catch (e) {
  console.error("cannot prepare " + STATE_DIR + ": " + e.message);
  process.exit(1);
}
const API_TOKEN = ensureToken();

// ---------- read current login (never written) ----------
function readJson(f) { return JSON.parse(fs.readFileSync(f, "utf8")); }
function keychainRead() {
  try {
    const out = execFileSync("security", ["find-generic-password", "-s", KC_SERVICE, "-a", KC_ACCOUNT, "-w"],
      { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
    return JSON.parse(out.trim());
  } catch (_) { return null; }
}
function currentCreds() {
  const kc = keychainRead();
  if (kc && kc.claudeAiOauth) return kc;
  try { return readJson(CRED_FILE); } catch (_) { return null; }
}
function currentIdentity() {
  try { const j = readJson(CLAUDE_JSON); return { oauthAccount: j.oauthAccount || null }; }
  catch (_) { return { oauthAccount: null }; }
}
function currentEmail() { const id = currentIdentity(); return (id.oauthAccount && id.oauthAccount.emailAddress) || null; }
function currentAccountUuid() { const id = currentIdentity(); return (id.oauthAccount && id.oauthAccount.accountUuid) || null; }

// ---------- saved accounts (labels for the meter) ----------
function loadSnapshot(slug) { return readJson(path.join(ACCT_DIR, slug + ".json")); }
function saveSnapshot(slug, snap) {
  const tmp = path.join(ACCT_DIR, slug + ".json.tmp-" + process.pid);
  fs.writeFileSync(tmp, JSON.stringify(snap), { mode: 0o600 });
  fs.renameSync(tmp, path.join(ACCT_DIR, slug + ".json"));
}
function listAccounts() {
  let files = [];
  try { files = fs.readdirSync(ACCT_DIR).filter((f) => f.endsWith(".json")); } catch (_) {}
  const activeUuid = currentAccountUuid();
  const activeEmail = currentEmail();
  const rows = files.map((f) => {
    let a = {}; try { a = readJson(path.join(ACCT_DIR, f)); } catch (_) {}
    const slug = f.replace(/\.json$/, "");
    const email = (a.oauthAccount && a.oauthAccount.emailAddress) || a.email || null;
    const uuid = (a.oauthAccount && a.oauthAccount.accountUuid) || null;
    const active = (activeUuid && uuid && activeUuid === uuid) ||
                   (!activeUuid && activeEmail && email && activeEmail === email);
    return { slug, label: a.label || email || slug, email,
             subscriptionType: (a.claudeAiOauth && a.claudeAiOauth.subscriptionType) || null, active: !!active };
  });
  return rows.sort((x, y) => (x.active === y.active ? (x.label || "").localeCompare(y.label || "") : (x.active ? -1 : 1)));
}

// ---------- live sessions ----------
// Shows EVERY interactive Claude Code session that's running (from the process
// list), and overlays the account recorded by the SessionStart hook. Sessions
// that started before the hook was installed show up with an unknown account
// until they're restarted.
function runningClaudeSessions() {
  const map = new Map(); // pid -> tty, interactive claude processes only (those with a terminal)
  try {
    const out = execFileSync("ps", ["-eo", "pid=,tty=,comm="], { encoding: "utf8" });
    for (const line of out.split("\n")) {
      const m = line.trim().match(/^(\d+)\s+(\S+)\s+(.*)$/);
      if (!m) continue;
      const pid = Number(m[1]), tty = m[2], comm = m[3];
      if (!/claude/i.test(comm)) continue;
      if (tty === "??" || tty === "?" || tty === "-" || !tty) continue; // skip daemon/bg helpers (no tty)
      map.set(pid, tty);
    }
  } catch (_) {}
  return map;
}
function readAllJson(dir) {
  const rows = [];
  let files = []; try { files = fs.readdirSync(dir).filter((f) => f.endsWith(".json")); } catch (_) {}
  for (const f of files) { try { rows.push({ f, r: readJson(path.join(dir, f)) }); } catch (_) {} }
  return rows;
}
function listSessions() {
  const live = runningClaudeSessions();                  // pid -> tty
  const metaByPid = new Map(readAllJson(CLAUDE_SESS_DIR).map(({ r }) => [Number(r.pid), r]));
  const acctBySid = new Map(readAllJson(SESS_DIR).map(({ r }) => [r.sessionId, r]));
  const out = [];
  const liveSids = new Set();
  for (const [pid, tty] of live) {
    const md = metaByPid.get(pid) || {};
    const sid = md.sessionId || null;
    if (sid) liveSids.add(sid);
    const rec = sid ? acctBySid.get(sid) : null;
    out.push({
      pid, tty, sessionId: sid,
      folder: (md.cwd || "").split("/").filter(Boolean).pop() || "",
      email: rec ? (rec.email || null) : null,
    });
  }
  // prune hook records for sessions that are no longer live
  for (const { f, r } of readAllJson(SESS_DIR)) {
    if (!liveSids.has(r.sessionId)) { try { fs.unlinkSync(path.join(SESS_DIR, f)); } catch (_) {} }
  }
  return out.sort((a, b) => ((a.email || "~~") .localeCompare(b.email || "~~")) || (a.tty || "").localeCompare(b.tty || ""));
}

// ---------- usage (cached; fetched on open + on manual refresh) ----------
const usageCache = new Map();
let cooldownUntil = 0;

// Anthropic rotates the refresh token every time one is redeemed, so two
// concurrent refreshes of the same account leave one snapshot holding a dead
// token — that account then locks out until it's re-added. The page and the
// SwiftBar plugin both fan out across every account, so collapse concurrent
// work per slug onto a single in-flight promise.
const inflight = new Map();
function once(key, fn) {
  const running = inflight.get(key);
  if (running) return running;
  const p = Promise.resolve().then(fn).finally(() => inflight.delete(key));
  inflight.set(key, p);
  return p;
}

function shapeUsage(raw) {
  const u = (raw && raw.utilization) ? raw.utilization : raw || {};
  const limits = (u.limits || []).map((l) => ({
    kind: l.kind, group: l.group, percent: l.percent, severity: l.severity,
    resets_at: l.resets_at, scope: (l.scope && l.scope.model && l.scope.model.display_name) || (l.scope && l.scope.surface) || null,
  }));
  return {
    fiveHour: u.five_hour ? u.five_hour.utilization : null, fiveHourResets: u.five_hour ? u.five_hour.resets_at : null,
    sevenDay: u.seven_day ? u.seven_day.utilization : null, sevenDayResets: u.seven_day ? u.seven_day.resets_at : null,
    limits, extraUsage: u.extra_usage ? { enabled: !!u.extra_usage.is_enabled, percent: u.extra_usage.utilization } : null,
  };
}
async function rawFetchUsage(accessToken) {
  const r = await fetch(USAGE_URL, {
    headers: { "Authorization": "Bearer " + accessToken, "anthropic-beta": "oauth-2025-04-20",
      "anthropic-version": "2023-06-01", "User-Agent": "claude-usage-meter/1.0", "Accept": "application/json" },
    signal: AbortSignal.timeout(12000),
  });
  if (r.status === 429) { cooldownUntil = Date.now() + (Number(r.headers.get("retry-after")) || 90) * 1000;
    const e = new Error("rate limited — try again shortly"); e.code = 429; throw e; }
  if (r.status === 401) return { unauth: true };
  if (!r.ok) throw new Error("usage HTTP " + r.status);
  return { usage: shapeUsage(await r.json()) };
}
function refreshSnapshot(slug) {
  return once("refresh:" + slug, async () => {
    const snap = loadSnapshot(slug);
    const rt = snap.claudeAiOauth && snap.claudeAiOauth.refreshToken;
    if (!rt) throw new Error("no refresh token");
    const r = await fetch(TOKEN_URL, { method: "POST",
      headers: { "content-type": "application/json", "Accept": "application/json", "User-Agent": "claude-usage-meter/1.0" },
      body: JSON.stringify({ grant_type: "refresh_token", refresh_token: rt, client_id: CLIENT_ID }),
      signal: AbortSignal.timeout(12000) });
    if (!r.ok) throw new Error("refresh HTTP " + r.status);
    const d = await r.json();
    snap.claudeAiOauth.accessToken = d.access_token;
    snap.claudeAiOauth.refreshToken = d.refresh_token || rt;
    if (Number(d.expires_in)) snap.claudeAiOauth.expiresAt = Date.now() + Number(d.expires_in) * 1000;
    saveSnapshot(slug, snap);
    return d.access_token;
  });
}
async function tokenForAccount(slug, meta) {
  if (meta.active) {
    const creds = currentCreds();
    const tok = creds && creds.claudeAiOauth && creds.claudeAiOauth.accessToken;
    if (!tok) throw new Error("no live token");
    return { tok, canRefresh: false };
  }
  const snap = loadSnapshot(slug);
  let tok = snap.claudeAiOauth && snap.claudeAiOauth.accessToken;
  const exp = snap.claudeAiOauth && snap.claudeAiOauth.expiresAt;
  if (!tok) throw new Error("no token");
  if (exp && exp < Date.now() + 120000) { try { tok = await refreshSnapshot(slug); } catch (_) {} }
  return { tok, canRefresh: true };
}
function fetchAndCache(slug) {
  return once("usage:" + slug, async () => {
    const meta = listAccounts().find((a) => a.slug === slug);
    if (!meta) throw new Error("unknown account");
    if (Date.now() < cooldownUntil) { const e = new Error("cooling down"); e.code = 429; throw e; }
    let { tok, canRefresh } = await tokenForAccount(slug, meta);
    let res = await rawFetchUsage(tok);
    if (res.unauth && canRefresh) { tok = await refreshSnapshot(slug); res = await rawFetchUsage(tok); }
    if (res.unauth) throw new Error("login expired");
    const data = { active: meta.active, ...res.usage };
    usageCache.set(slug, { at: Date.now(), data });
    return data;
  });
}
async function getUsage(slug, force) {
  const c = usageCache.get(slug);
  if (!force && c) return { ...c.data, stale: false, fetchedAt: c.at };
  try { const data = await fetchAndCache(slug); return { ...data, stale: false, fetchedAt: Date.now() }; }
  catch (e) { if (c) return { ...c.data, stale: true, fetchedAt: c.at, note: safeErr(e) }; throw e; }
}

// ---------- request guards ----------
// Filesystem paths in an error body would hand a caller a map of this machine,
// so keep the message and drop anything path-shaped. Full detail goes to stderr.
function safeErr(e) {
  const m = String((e && e.message) || e || "error").replace(/\/[^\s'"]*/g, "…");
  return m.length > 140 ? m.slice(0, 140) : m;
}
function isLoopback(h) {
  const n = String(h || "").replace(/^\[|\]$/g, "");
  return n === "127.0.0.1" || n === "localhost" || n === "::1";
}
// Host allowlist is the DNS-rebinding defense: a page on evil.com that
// re-resolves to 127.0.0.1 still sends Host: evil.com, and lands here.
function hostOk(req) { return isLoopback((req.headers.host || "").replace(/:\d+$/, "")); }
// Sec-Fetch-Site/Origin are the cross-site defense: they reject the
// <img src="…/api/usage?force=1"> class of request, which the browser will
// happily send even though CORS stops it reading the reply. "none" is a direct
// navigation (bookmark, typed URL); a header-less client is a CLI, which still
// has to present the token below.
function originOk(req, pathname) {
  const site = req.headers["sec-fetch-site"];
  if (site && site !== "same-origin" && site !== "none") {
    // Following a link from some other page is a cross-site *top-level
    // navigation* — legitimate, and harmless: it still needs the token, the
    // reply can't be read cross-origin, and frame-ancestors blocks embedding.
    // Every other cross-site shape (image, fetch, iframe) is the CSRF vector.
    const navigating = pathname === "/" &&
      req.headers["sec-fetch-mode"] === "navigate" &&
      req.headers["sec-fetch-dest"] === "document";
    if (!navigating) return false;
  }
  const o = req.headers.origin;
  if (!o) return true; // GET navigations don't carry Origin
  try { const u = new URL(o); return isLoopback(u.hostname) && u.port === String(PORT); }
  catch (_) { return false; }
}
const COOKIE = "cu_token";
function tokenOk(given) {
  if (typeof given !== "string" || given.length !== API_TOKEN.length) return false;
  return crypto.timingSafeEqual(Buffer.from(given), Buffer.from(API_TOKEN));
}
function cookieToken(req) {
  for (const part of String(req.headers.cookie || "").split(";")) {
    const i = part.indexOf("=");
    if (i > 0 && part.slice(0, i).trim() === COOKIE) {
      try { return decodeURIComponent(part.slice(i + 1).trim()); } catch (_) { return null; }
    }
  }
  return null;
}
function bearerToken(req) {
  const m = /^Bearer\s+(\S+)$/i.exec(req.headers.authorization || "");
  return m ? m[1] : null;
}
function authOk(req, url) {
  return tokenOk(bearerToken(req)) || tokenOk(url.searchParams.get("t")) || tokenOk(cookieToken(req));
}

// ---------- HTTP ----------
function headers(extra) {
  return Object.assign({
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
  }, extra || {});
}
function send(res, code, obj) {
  res.writeHead(code, headers({ "content-type": "application/json" }));
  res.end(JSON.stringify(obj));
}
function deny(res, code, msg) {
  res.writeHead(code, headers({ "content-type": "text/plain; charset=utf-8" }));
  res.end(msg + "\n");
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://" + HOST + ":" + PORT);
  if (!hostOk(req)) return deny(res, 403, "localhost only");
  if (!originOk(req, url.pathname)) return deny(res, 403, "cross-site request blocked");

  const authed = authOk(req, url);
  try {
    if (req.method === "GET" && url.pathname === "/") {
      if (!authed) {
        res.writeHead(401, headers({ "content-type": "text/html; charset=utf-8", "content-security-policy": "default-src 'none'" }));
        return res.end(UNAUTH_HTML);
      }
      // Arriving with ?t=… — set the cookie and bounce to a clean URL so the
      // token stays out of the address bar, history, and any future referer.
      if (url.searchParams.has("t")) {
        res.writeHead(302, headers({
          "location": "/",
          "set-cookie": COOKIE + "=" + encodeURIComponent(API_TOKEN) + "; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000",
        }));
        return res.end();
      }
      const nonce = crypto.randomBytes(16).toString("base64");
      res.writeHead(200, headers({
        "content-type": "text/html; charset=utf-8",
        "content-security-policy": "default-src 'none'; script-src 'nonce-" + nonce + "'; style-src 'nonce-" + nonce +
          "'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
      }));
      return res.end(renderHtml(nonce));
    }

    if (!authed) return send(res, 401, { ok: false, error: "unauthorized" });

    if (req.method === "GET" && url.pathname === "/api/state") { return send(res, 200, { currentEmail: currentEmail(), accounts: listAccounts() }); }
    if (req.method === "GET" && url.pathname === "/api/sessions") { return send(res, 200, { sessions: listSessions() }); }
    if (req.method === "GET" && url.pathname === "/api/usage") {
      const slug = url.searchParams.get("slug"); const force = url.searchParams.get("force") === "1";
      if (!slug) return send(res, 400, { ok: false, error: "missing slug" });
      try { return send(res, 200, { ok: true, ...(await getUsage(slug, force)) }); }
      catch (e) { console.error("usage " + slug + ":", e); return send(res, 200, { ok: false, error: safeErr(e) }); }
    }
    return deny(res, 404, "not found");
  } catch (e) { console.error(e); send(res, 500, { ok: false, error: safeErr(e) }); }
});
server.listen(PORT, HOST, () => {
  const base = "http://" + HOST + ":" + PORT;
  // Only ever put the token on a terminal. Under launchd, stdout is a log file
  // — often 0644 in /tmp — and printing it there would hand the token to every
  // account on the machine, undoing the point of having one.
  if (process.stdout.isTTY) {
    console.log("Claude Usage Meter → " + base + "/?t=" + API_TOKEN);
    console.log("  (that first visit sets a cookie; plain " + base + " works after)");
  } else {
    console.log("Claude Usage Meter → " + base);
    console.log("  authorize once: open \"" + base + "/?t=$(cat " + TOKEN_FILE.replace(HOME, "~") + ")\"");
  }
  console.log("  state: " + STATE_DIR);
});

// ---------- UI ----------
const UNAUTH_HTML = `<!doctype html><meta charset="utf-8"><title>Claude Usage</title>
<body style="font:15px/1.6 -apple-system,BlinkMacSystemFont,sans-serif;background:#0b0f19;color:#e6e8ee;margin:0;padding:60px 24px">
<div style="max-width:520px;margin:0 auto">
<h1 style="font-size:19px;margin:0 0 10px">Claude Usage — not authorized</h1>
<p style="color:#8b93a7">This page needs the local API token. Open the URL printed when the server started, or run:</p>
<pre style="background:#111629;border:1px solid #232c47;border-radius:9px;padding:12px;overflow:auto;color:#9fb0d6"
>open "http://127.0.0.1:4177/?t=$(cat ~/.config/claude-usage-meter/token)"</pre>
<p style="color:#7b8398;font-size:13px">That sets a cookie for this browser, so the plain URL works from then on.</p>
</div></body>`;

function renderHtml(nonce) { return `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Claude Usage</title>
<style nonce="${nonce}">
 :root{color-scheme:dark light} *{box-sizing:border-box}
 body{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;background:#0b0f19;color:#e6e8ee}
 .wrap{max-width:680px;margin:0 auto;padding:26px 20px 60px}
 .head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin:0 0 18px}
 h1{font-size:20px;margin:0} .sub{color:#8b93a7;font-size:13px;margin:2px 0 0}
 button{cursor:pointer;border:0;border-radius:8px;padding:8px 12px;font-size:13px;font-weight:600;background:#1b2238;color:#c7cfe0;border:1px solid #2a3350}
 button:hover{background:#222c47}
 .card{background:#111629;border:1px solid #232c47;border-radius:12px;padding:14px 16px;margin:0 0 12px}
 .card.active{border-color:#3f8f5f;box-shadow:0 0 0 1px #2f6f47 inset}
 .top{display:flex;align-items:center;gap:12px;margin-bottom:4px}
 .dot{width:9px;height:9px;border-radius:50%;background:#37415c;flex:0 0 auto}
 .card.active .dot{background:#49c07a;box-shadow:0 0 8px #49c07a}
 .label{font-weight:600;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}
 .tag{display:inline-block;font-size:11px;color:#9fb0d6;background:#1a2138;border:1px solid #2a3350;border-radius:20px;padding:1px 8px;margin-left:6px}
 .tag.badge{color:#8ff0b6;background:#12271a;border-color:#2f6f47}
 .email{color:#8b93a7;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:8px}
 .u{margin:9px 0}
 .u .lab{display:flex;justify-content:space-between;font-size:12px;color:#aab3c8;margin-bottom:3px}
 .bar{height:8px;border-radius:5px;background:#1a2138;overflow:hidden}
 .fill{height:100%;border-radius:5px;background:#4f7cff;transition:width .4s}
 .fill.warning{background:#e0b34f} .fill.critical{background:#e0607a}
 .umsg{font-size:12px;color:#8b93a7;margin-top:8px}
 .reset{font-size:11px;color:#7b8398;margin-top:3px}
 .sgrp{margin-top:10px} .sgrp:first-of-type{margin-top:6px}
 .semail{font-size:13px;font-weight:600;color:#cfd6e6;margin-bottom:4px}
 .srow{display:flex;gap:10px;font-size:12px;color:#8b93a7;padding:1px 0}
 .stty{color:#9fb0d6;min-width:66px;flex:0 0 auto} .sfolder{color:#7b8398;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .shead{color:#8b93a7;font-size:12px;margin:2px 0 2px}
 .shint{font-weight:400;font-size:11px;color:#7b8398}
 .empty{color:#8b93a7;text-align:center;padding:26px 0}
 a{color:#7fa0ff;text-decoration:none;font-weight:600;cursor:pointer} a:hover{text-decoration:underline}
 .toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:#1b2238;border:1px solid #2a3350;color:#e6e8ee;padding:9px 14px;border-radius:10px;font-size:13px;opacity:0;transition:opacity .2s;max-width:92vw}
 .toast.show{opacity:1} .toast.err{border-color:#5a2b3a;color:#ffb3c4}
</style></head><body><div class="wrap">
 <div class="head"><div><h1>Claude Usage</h1><p class="sub">Your rate-limit levels — same as <code>/usage</code>.</p></div>
  <button id="refreshAll">↻ Refresh</button></div>
 <div id="sessions"></div>
 <div id="list"></div>
</div>
<div id="toast" class="toast"></div>
<script nonce="${nonce}">
let T, cache={};
function toast(m,err){const t=document.getElementById('toast');t.textContent=m;t.className='toast show'+(err?' err':'');clearTimeout(T);T=setTimeout(()=>t.className='toast',3200);}
async function api(p){const r=await fetch(p);return r.json();}
function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function when(s){if(!s)return'';const n=typeof s==='number'?s:Date.parse(s);if(!n)return'';const dm=(n-Date.now())/60000;if(dm<0)return'now';if(dm<60)return Math.round(dm)+'m';if(dm<1440)return Math.round(dm/60)+'h';return Math.round(dm/1440)+'d';}
function sev(p){return p>=95?'critical':p>=80?'warning':'';}
function resetLabel(s){if(!s)return'';const d=new Date(typeof s==='number'?s:Date.parse(s));if(isNaN(d))return'';return d.toLocaleString(undefined,{weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});}
// every value below is escaped: limit scope/group/kind come straight off the
// usage API, and slugs come off disk — neither is ours to trust in innerHTML.
function bar(name,pct,resets){
 if(pct==null)return'';
 const p=Math.min(100,Math.round(pct));const rl=resetLabel(resets);const rel=resets?when(resets):'';
 return '<div class="u"><div class="lab"><span>'+esc(name)+'</span><span>'+p+'%</span></div>'+
  '<div class="bar"><div class="fill '+sev(p)+'" style="width:'+p+'%"></div></div>'+
  (rl?'<div class="reset">resets '+esc(rl)+(rel?' ('+esc(rel)+')':'')+'</div>':'')+'</div>';
}
function retryLink(slug,text){return '<a data-slug="'+esc(slug)+'">'+text+'</a>';}
function renderUsage(slug,u){
 const box=document.getElementById('u-'+slug);if(!box)return;
 if(!u||!u.ok){box.innerHTML='<div class="umsg">Usage unavailable'+((u&&u.error)?' ('+esc(u.error)+')':'')+'. '+retryLink(slug,'↻ retry')+'</div>';return;}
 let h=bar('Session (5h)',u.fiveHour,u.fiveHourResets)+bar('Weekly',u.sevenDay,u.sevenDayResets);
 (u.limits||[]).forEach(l=>{if(l.scope)h+=bar(l.scope+' ('+(l.group||l.kind)+')',l.percent,l.resets_at);});
 if(u.extraUsage&&u.extraUsage.enabled)h+=bar('Extra usage',u.extraUsage.percent,null);
 if(!h)h='<div class="umsg">No active limits.</div>';
 h+='<div class="umsg">'+(u.stale?'updating… · ':'')+retryLink(slug,'↻ Refresh')+'</div>';
 box.innerHTML=h;
}
async function one(slug){
 const box=document.getElementById('u-'+slug);if(box&&!cache[slug])box.innerHTML='<div class="umsg">Loading…</div>';
 const u=await api('/api/usage?slug='+encodeURIComponent(slug)+'&force=1');
 if(u&&u.ok){cache[slug]=u;renderUsage(slug,u);}
 else if(cache[slug]){renderUsage(slug,{...cache[slug],stale:true});toast(u&&u.error?('Usage: '+u.error):'Refresh failed',true);}
 else renderUsage(slug,u);
}
async function fillAll(slugs){for(const s of slugs){await one(s);await new Promise(r=>setTimeout(r,4000));}}
async function refreshAll(){const s=await api('/api/state');fillAll((s.accounts||[]).map(a=>a.slug));}
async function load(){
 const s=await api('/api/state');
 const list=document.getElementById('list');
 if(!s.accounts||!s.accounts.length){list.innerHTML='<div class="empty">No accounts on file.</div>';return;}
 list.innerHTML=s.accounts.map(a=>{
  const badge=a.active?'<span class="tag badge">active</span>':'';
  const sub=a.subscriptionType?'<span class="tag">'+esc(a.subscriptionType)+'</span>':'';
  return '<div class="card'+(a.active?' active':'')+'"><div class="top"><div class="dot"></div><div class="label">'+esc(a.label)+'</div>'+badge+sub+'</div>'+
   (a.email&&a.email!==a.label?'<div class="email">'+esc(a.email)+'</div>':'')+
   '<div id="u-'+esc(a.slug)+'">'+(cache[a.slug]?'':'<div class="umsg">Loading…</div>')+'</div></div>';
 }).join('');
 s.accounts.forEach(a=>{if(cache[a.slug])renderUsage(a.slug,cache[a.slug]);});
 if(!load.done){load.done=true;fillAll(s.accounts.map(a=>a.slug));}
}
async function loadSessions(){
 const box=document.getElementById('sessions');
 let d; try{d=await api('/api/sessions');}catch(_){return;}
 const s=(d&&d.sessions)||[];
 if(!s.length){box.innerHTML='<div class="card"><div class="shead">No live Claude sessions detected.</div></div>';return;}
 const groups={};
 s.forEach(x=>{const k=x.email||'__unknown__';(groups[k]=groups[k]||[]).push(x);});
 let h='<div class="card"><div class="top"><div class="label">Live sessions</div><span class="tag badge">'+s.length+' running</span></div>';
 Object.keys(groups).sort((a,b)=>(a==='__unknown__'?1:b==='__unknown__'?-1:a.localeCompare(b))).forEach(email=>{
  const rows=groups[email];const unknown=email==='__unknown__';
  h+='<div class="sgrp"><div class="semail">'+(unknown?'Account unknown':esc(email))+' <span class="tag">'+rows.length+'</span>'+(unknown?' <span class="shint">restart these to identify</span>':'')+'</div>';
  rows.forEach(x=>{h+='<div class="srow"><span class="stty">'+esc(x.tty||'—')+'</span><span class="sfolder">'+esc(x.folder||'')+'</span></div>';});
  h+='</div>';
 });
 h+='</div>';
 box.innerHTML=h;
}
// delegated — a nonce CSP blocks inline onclick=, and this keeps slugs out of
// generated JS source entirely
document.getElementById('refreshAll').addEventListener('click',refreshAll);
document.addEventListener('click',e=>{const a=e.target.closest('a[data-slug]');if(!a)return;e.preventDefault();one(a.getAttribute('data-slug'));});
function tick(){load();loadSessions();}
tick();setInterval(tick,8000);
</script></body></html>`; }
