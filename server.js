#!/usr/bin/env node
"use strict";

/*
 * Claude Usage Meter — a localhost page that shows your Claude rate-limit usage
 * (the same numbers as /usage in the terminal). Read-only: no switching, no login.
 *
 * It reads your login token(s) locally to query the usage endpoint:
 *   - active account: macOS Keychain "Claude Code-credentials" (authoritative)
 *   - any other saved account: its snapshot in ./accounts (refreshed as needed)
 * Nothing is ever written to your credentials.
 */

const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync } = require("child_process");

const HOME = os.homedir();
const CRED_FILE = path.join(HOME, ".claude", ".credentials.json");
const CLAUDE_JSON = path.join(HOME, ".claude.json");
const APP_DIR = __dirname;
const ACCT_DIR = path.join(APP_DIR, "accounts");
const KC_SERVICE = "Claude Code-credentials";
const KC_ACCOUNT = os.userInfo().username;
const HOST = "127.0.0.1";
const PORT = Number(process.env.PORT) || 4177;

const USAGE_URL = "https://api.anthropic.com/api/oauth/usage";
const TOKEN_URL = "https://platform.claude.com/v1/oauth/token";
const CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e";

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
const SESS_DIR = path.join(APP_DIR, "sessions");                 // hook records (by sessionId)
const CLAUDE_SESS_DIR = path.join(HOME, ".claude", "sessions");  // Claude's own per-pid session files

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
async function refreshSnapshot(slug) {
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
async function fetchAndCache(slug) {
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
}
async function getUsage(slug, force) {
  const c = usageCache.get(slug);
  if (!force && c) return { ...c.data, stale: false, fetchedAt: c.at };
  try { const data = await fetchAndCache(slug); return { ...data, stale: false, fetchedAt: Date.now() }; }
  catch (e) { if (c) return { ...c.data, stale: true, fetchedAt: c.at, note: String(e.message || e) }; throw e; }
}

// ---------- HTTP ----------
function send(res, code, obj) { res.writeHead(code, { "content-type": "application/json", "cache-control": "no-store" }); res.end(JSON.stringify(obj)); }
function localHostOk(req) { const h = (req.headers.host || "").split(":")[0]; return h === "127.0.0.1" || h === "localhost" || h === "[::1]" || h === "::1"; }

const server = http.createServer(async (req, res) => {
  if (!localHostOk(req)) { res.writeHead(403); return res.end("localhost only"); }
  const url = new URL(req.url, "http://" + HOST);
  try {
    if (req.method === "GET" && url.pathname === "/") { res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" }); return res.end(HTML); }
    if (req.method === "GET" && url.pathname === "/api/state") { return send(res, 200, { currentEmail: currentEmail(), accounts: listAccounts() }); }
    if (req.method === "GET" && url.pathname === "/api/sessions") { return send(res, 200, { sessions: listSessions() }); }
    if (req.method === "GET" && url.pathname === "/api/usage") {
      const slug = url.searchParams.get("slug"); const force = url.searchParams.get("force") === "1";
      if (!slug) return send(res, 400, { ok: false, error: "missing slug" });
      try { return send(res, 200, { ok: true, ...(await getUsage(slug, force)) }); }
      catch (e) { return send(res, 200, { ok: false, error: String(e.message || e) }); }
    }
    res.writeHead(404); res.end("not found");
  } catch (e) { send(res, 500, { ok: false, error: String(e.message || e) }); }
});
server.listen(PORT, HOST, () => { console.log("Claude Usage Meter → http://" + HOST + ":" + PORT); });

// ---------- UI ----------
const HTML = `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Claude Usage</title>
<style>
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
 a{color:#7fa0ff;text-decoration:none;font-weight:600} a:hover{text-decoration:underline}
 .toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:#1b2238;border:1px solid #2a3350;color:#e6e8ee;padding:9px 14px;border-radius:10px;font-size:13px;opacity:0;transition:opacity .2s;max-width:92vw}
 .toast.show{opacity:1} .toast.err{border-color:#5a2b3a;color:#ffb3c4}
</style></head><body><div class="wrap">
 <div class="head"><div><h1>Claude Usage</h1><p class="sub">Your rate-limit levels — same as <code>/usage</code>.</p></div>
  <button onclick="refreshAll()">↻ Refresh</button></div>
 <div id="sessions"></div>
 <div id="list"></div>
</div>
<div id="toast" class="toast"></div>
<script>
let T, cache={};
function toast(m,err){const t=document.getElementById('toast');t.textContent=m;t.className='toast show'+(err?' err':'');clearTimeout(T);T=setTimeout(()=>t.className='toast',3200);}
async function api(p){const r=await fetch(p);return r.json();}
function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function when(s){if(!s)return'';const n=typeof s==='number'?s:Date.parse(s);if(!n)return'';const dm=(n-Date.now())/60000;if(dm<0)return'now';if(dm<60)return Math.round(dm)+'m';if(dm<1440)return Math.round(dm/60)+'h';return Math.round(dm/1440)+'d';}
function sev(p){return p>=95?'critical':p>=80?'warning':'';}
function resetLabel(s){if(!s)return'';const d=new Date(typeof s==='number'?s:Date.parse(s));if(isNaN(d))return'';return d.toLocaleString(undefined,{weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});}
function bar(name,pct,resets){
 if(pct==null)return'';
 const p=Math.min(100,Math.round(pct));const rl=resetLabel(resets);const rel=resets?when(resets):'';
 return '<div class="u"><div class="lab"><span>'+name+'</span><span>'+p+'%</span></div>'+
  '<div class="bar"><div class="fill '+sev(p)+'" style="width:'+p+'%"></div></div>'+
  (rl?'<div class="reset">resets '+rl+(rel?' ('+rel+')':'')+'</div>':'')+'</div>';
}
function renderUsage(slug,u){
 const box=document.getElementById('u-'+slug);if(!box)return;
 if(!u||!u.ok){box.innerHTML='<div class="umsg">Usage unavailable'+((u&&u.error)?' ('+esc(u.error)+')':'')+'. <a href="#" onclick="one(\\''+slug+'\\');return false;">↻ retry</a></div>';return;}
 let h=bar('Session (5h)',u.fiveHour,u.fiveHourResets)+bar('Weekly',u.sevenDay,u.sevenDayResets);
 (u.limits||[]).forEach(l=>{if(l.scope)h+=bar(l.scope+' ('+(l.group||l.kind)+')',l.percent,l.resets_at);});
 if(u.extraUsage&&u.extraUsage.enabled)h+=bar('Extra usage',u.extraUsage.percent,null);
 if(!h)h='<div class="umsg">No active limits.</div>';
 h+='<div class="umsg">'+(u.stale?'updating… · ':'')+'<a href="#" onclick="one(\\''+slug+'\\');return false;">↻ Refresh</a></div>';
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
async function refreshAll(){const s=await api('/api/state');fillAll(s.accounts.map(a=>a.slug));}
async function load(){
 const s=await api('/api/state');
 const list=document.getElementById('list');
 if(!s.accounts.length){list.innerHTML='<div class="empty">No accounts on file.</div>';return;}
 list.innerHTML=s.accounts.map(a=>{
  const badge=a.active?'<span class="tag badge">active</span>':'';
  const sub=a.subscriptionType?'<span class="tag">'+esc(a.subscriptionType)+'</span>':'';
  return '<div class="card'+(a.active?' active':'')+'"><div class="top"><div class="dot"></div><div class="label">'+esc(a.label)+'</div>'+badge+sub+'</div>'+
   (a.email&&a.email!==a.label?'<div class="email">'+esc(a.email)+'</div>':'')+
   '<div id="u-'+a.slug+'">'+(cache[a.slug]?'':'<div class="umsg">Loading…</div>')+'</div></div>';
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
function tick(){load();loadSessions();}
tick();setInterval(tick,8000);
</script></body></html>`;
