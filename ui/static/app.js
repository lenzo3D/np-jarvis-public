"use strict";
const $ = (id) => document.getElementById(id);
const HEADERS = { "X-JARVIS-UI": "1", "Content-Type": "application/json" };

async function api(path, method = "GET", body) {
  const opt = { method, headers: HEADERS };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch (e) { /* non-JSON body */ }
  return { ok: r.ok, status: r.status, data };
}
function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) { if (k === "class") n.className = v; else n.setAttribute(k, v); }
  for (const k of kids) n.append(k instanceof Node ? k : document.createTextNode(k ?? ""));
  return n;
}
const fill = (node, ...kids) => node.replaceChildren(...kids);
const fmtNum = (n) => Number(n || 0).toLocaleString("en-GB");
let manualNotice = null, systemNotice = "";
function showNotice() {
  const n = $("notice");
  const manual = manualNotice && manualNotice.until > Date.now() ? manualNotice.text : "";
  const text = manual || systemNotice;
  n.hidden = !text;
  n.textContent = text;
}
/* notice(text) shows a message for `ms` (default 12 s) that the status poll will not overwrite; notice("") clears it */
function notice(msg, ms = 12000) { manualNotice = msg ? { text: msg, until: Date.now() + ms } : null; showNotice(); }
function systemMessage(msg) { systemNotice = msg || ""; showNotice(); }
function kv(node, rows) { fill(node, ...rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)])); }

/* ---------- clock (Singapore time) ---------- */
function tick() {
  const now = new Date();
  $("clock-date").textContent = now.toLocaleDateString("en-GB", { timeZone: "Asia/Singapore", weekday: "long", day: "numeric", month: "long", year: "numeric" });
  $("clock-time").textContent = now.toLocaleTimeString("en-GB", { timeZone: "Asia/Singapore", hourCycle: "h23" });
}
tick(); setInterval(tick, 1000);

/* ---------- views ---------- */
let currentView = "overview";
document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
document.querySelectorAll("[data-goto]").forEach((b) => b.addEventListener("click", () => showView(b.dataset.goto)));
function showView(name) {
  currentView = name;
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  document.querySelectorAll(".view").forEach((p) => p.classList.toggle("active", p.id === "view-" + name));
  if (name === "overview") { loadFeed(); loadCalls(); }
  if (name === "calendar") loadCalendar();
  if (name === "chats") loadChats();
  if (name === "inbox") loadInbox();
  if (name === "deadlines") loadDeadlines();
  if (name === "usage") loadUsage();
  if (name === "logs") loadLogs();
  if (name === "tools") loadTools();
  if (name === "chat") { const l = $("chat-log"); l.scrollTop = l.scrollHeight; }
}

/* ---------- status helpers ---------- */
const MODE_WORD = { running: "Online", starting: "Starting", unresponsive: "Not responding", restarting: "Restarting",
  external: "Online (external)", stopped: "Stopped" };
const MODE_COLOR = { running: "green", starting: "amber", unresponsive: "red", restarting: "amber", external: "amber", stopped: "grey" };
const STATE_WORD = { pick: "Choose groups", live: "Live", connected: "Connected", stale: "Check overdue", starting: "Starting", offline: "Not confirmed",
  unconfigured: "Not set up", login: "Login needed", failing: "Failing" };
function ago(iso) {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return "just now";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  if (s < 129600) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " days ago";
}
function uptime(sec) {
  if (sec == null) return "n/a";
  if (sec < 90) return sec + " s";
  if (sec < 5400) return Math.round(sec / 60) + " min";
  return (sec / 3600).toFixed(1) + " h";
}
function systemState(data) {
  const mode = data.ops.mode, conns = data.connections || [];
  if (mode === "stopped") return { key: "offline", word: "OFFLINE", dot: "grey", sub: "Not running. Press Start." };
  if (mode === "starting" || mode === "restarting") return { key: "starting", word: "STARTING", dot: "amber", sub: mode === "restarting" ? "Recovering from a problem" : "Starting up" };
  if (mode === "unresponsive") return { key: "attention", word: "ATTENTION", dot: "red", sub: "Not responding. Try Restart." };
  const red = conns.filter((c) => c.color === "red").length, other = conns.filter((c) => c.color !== "green").length;
  const sub = mode === "external" ? "Started outside this panel" : "Uptime " + uptime(data.ops.uptime_s);
  if (red) return { key: "attention", word: "ATTENTION", dot: "red", sub: red + " connection" + (red > 1 ? "s" : "") + " need a login" };
  if (other) return { key: "degraded", word: "DEGRADED", dot: "amber", sub: "Some connections are unconfirmed. " + sub };
  return { key: "online", word: "OPTIMAL", dot: "green", sub };
}
function gauge(id, numId, done, total) {
  const frac = total ? done / total : 0;
  $(id).style.strokeDashoffset = String(201 * (1 - frac));
  $(numId).textContent = done + "/" + total;
}

/* ---------- identity ---------- */
const who = { name: "You", address: "Sir" };
function setIdentity(id) {
  who.address = id.address || "Sir";
  who.name = id.name || "You";
  const label = id.name || "Operator";
  $("who-name").textContent = label;
  $("op-name").textContent = label;
  $("brand-foot").textContent = id.name ? id.name.toUpperCase() + " INDUSTRIES" : "PERSONAL INDUSTRIES";
}

/* ---------- command center ---------- */
async function refreshState() {
  const { ok, data } = await api("/api/state");
  if (!ok || !data) { systemMessage("The control panel cannot reach its server."); return; }
  const sys = systemState(data);
  $("version").textContent = data.version || "dev";
  if (data.identity) setIdentity(data.identity);
  $("sys-dot").className = "dot " + sys.dot;
  $("sys-text").textContent = sys.word;
  $("sys-text").style.color = { green: "var(--ok)", amber: "var(--warn)", red: "var(--bad)", grey: "var(--muted)" }[sys.dot];
  $("hero").dataset.state = sys.key;
  $("core-state").textContent = sys.key === "online" ? "ONLINE" : sys.word;
  $("core-sub").textContent = sys.sub;
  const mode = data.ops.mode;
  const live = ["running", "starting", "unresponsive", "restarting"].includes(mode);
  $("btn-start").hidden = live || mode === "external";
  $("btn-stop").hidden = !live;
  $("btn-restart").hidden = !live;
  systemMessage(data.claude ? "" : "Claude Code was not found on this computer, so chat and summaries will not work. Install it and sign in once by running: claude");

  renderConnections(data.connections || []);
  const c = data.counts || {};
  fill($("core-list"),
    coreRow(MODE_COLOR[mode], "JARVIS core", MODE_WORD[mode] || mode, ""),
    ...data.checks.map((k) => coreRow(k.ok ? "green" : "amber", k.name, k.ok ? "OK" : "Needs setup", k.ok ? "ok" : "todo", k.ok ? "" : k.hint)),
    coreRow("green", "Deadlines tracked", fmtNum(c.deadlines), ""),
    coreRow("green", "Library items", fmtNum(c.materials), ""));
  const cs = data.connections || [];
  gauge("g-links", "g-links-n", cs.filter((x) => x.color === "green").length, cs.length);
  gauge("g-setup", "g-setup-n", data.checks.filter((k) => k.ok).length, data.checks.length);

  const ctl = data.controls || {};
  kv($("controls-kv"), [["Alerts", ctl.paused ? "Paused" : "Active"], ["Quiet hours", ctl.quiet || "Off"],
    ["Muted courses", (ctl.muted || []).join(", ") || "None"], ["Morning briefing", ctl.briefing_time || "07:00"]]);
  $("autostart").checked = !!data.ops.autostart;
  fill($("pending"), data.pending.length ? el("ul", { class: "rows" }, ...data.pending.map((p) =>
    el("li", {}, el("span", { class: "dot amber" }), el("div", { class: "grow" }, el("div", { class: "nm" }, "#" + p.id + " " + p.kind.replace(/_/g, " ")), el("div", { class: "vl" }, p.summary || ""))))) :
    el("p", { class: "muted" }, "Nothing is waiting for approval."));
  $("chat-send").disabled = data.chat_busy;
  $("wave").classList.toggle("on", data.chat_busy);
}

function coreRow(color, name, value, cls, hint) {
  return el("li", {}, el("span", { class: "dot " + color }),
    el("div", { class: "grow" }, el("div", { class: "nm" }, name), hint ? el("div", { class: "vl todo" }, hint) : ""),
    hint ? "" : el("span", { class: "side-vl " + (cls || "") }, value));
}

function detailLine(c) {
  const word = STATE_WORD[c.state] || c.state;
  return c.detail.toLowerCase().startsWith(word.toLowerCase()) ? c.detail : word + ". " + c.detail;
}

function renderConnections(list) {
  fill($("chips"), ...list.map((c) => el("span", { class: "chip", title: c.detail }, el("span", { class: "dot " + c.color }), el("span", { class: "nm" }, c.name.replace("Google ", "").replace(" groups", "").replace(" email", "")))));
  fill($("connections"), ...list.map((c) => {
    const t = el("div", { class: "tile s-" + c.color },
      el("div", { class: "tile-head" }, el("span", { class: "dot " + c.color }), c.name, el("span", { class: "tile-state" }, STATE_WORD[c.state] || c.state)),
      el("div", { class: "tile-detail" }, c.detail));
    if (c.action) {
      const b = el("button", { class: "btn btn-primary" }, c.action_label || "Log in");
      b.addEventListener("click", () => runJob(c.action));
      t.append(b);
    } else if (c.goto) {
      const b = el("button", { class: "btn btn-primary" }, "Set up");
      b.addEventListener("click", () => showView(c.goto));
      t.append(b);
    }
    return t;
  }));
  fill($("tool-status"), ...list.map((c) => el("li", {}, el("span", { class: "dot " + c.color }),
    el("div", { class: "grow" }, el("div", { class: "nm" }, c.name), el("div", { class: "vl" }, detailLine(c))))));
  jobStates = Object.fromEntries(list.filter((c) => c.relogin).map((c) => [c.relogin, c.color]));
  styleToolButtons();
}
let jobStates = {};
function styleToolButtons() {
  document.querySelectorAll("#tool-buttons button[data-job]").forEach((b) => {
    const green = jobStates[b.dataset.job] === "green";
    b.classList.toggle("btn-quiet", green);
    b.textContent = (green ? "Log in again: " : "") + b.dataset.label;
  });
}
async function runJob(id) {
  const r = await api("/api/jobs/start", "POST", { id });
  if (!r.ok && r.data) notice(r.data.message);
  showView("tools");
}

async function loadFeed() {
  const { data } = await api("/api/deadlines?days=21");
  const rows = (data || []).slice(0, 6);
  fill($("feed"), ...(rows.length ? rows.map((d) => el("li", {},
    el("div", { class: "when" }, d.date, el("br"), d.time || "All day"),
    el("div", { class: "what" }, el("b", { title: d.title }, d.title),
      el("div", { class: "meta" }, d.course, el("span", { class: "tag " + (d.confirmed ? "tag-ok" : "tag-warn") }, d.confirmed ? "Confirmed" : "Tentative")))))
    : [el("li", {}, el("div", { class: "what muted" }, "Nothing due in the next 21 days."))]));
}
async function loadCalls() {
  const { data } = await api("/api/usage?days=1");
  $("g-calls").textContent = fmtNum(data ? data.total.calls : 0);
}

async function control(action) {
  for (const id of ["btn-start", "btn-stop", "btn-restart"]) $(id).disabled = true;
  const { data } = await api("/api/ops/" + action, "POST");
  if (data && data.message) notice(data.ok ? "" : data.message);
  for (const id of ["btn-start", "btn-stop", "btn-restart"]) $(id).disabled = false;
  refreshState();
}
$("btn-start").addEventListener("click", () => control("start"));
$("btn-stop").addEventListener("click", () => { if (confirm("Stop JARVIS? Alerts and the morning briefing will pause until you start it again.")) control("stop"); });
$("btn-restart").addEventListener("click", () => control("restart"));
$("autostart").addEventListener("change", (e) => api("/api/prefs", "POST", { autostart: e.target.checked }));

/* ---------- chat (the bar at the bottom works from every view) ---------- */
const chatLog = $("chat-log");
const history = [];
try { JSON.parse(sessionStorage.getItem("jarvis-chat") || "[]").forEach((m) => history.push(m)); } catch (e) { /* storage unavailable */ }
function persist() { try { sessionStorage.setItem("jarvis-chat", JSON.stringify(history.slice(-60))); } catch (e) { /* ignore */ } }
function addMsg(kind, text, who) {
  const m = el("div", { class: "msg " + kind }, who ? el("span", { class: "who" }, who) : "", text);
  chatLog.append(m);
  chatLog.scrollTop = chatLog.scrollHeight;
  return m;
}
history.forEach((m) => addMsg(m.kind, m.text, m.kind === "user" ? who.name : m.kind === "jarvis" ? "J.A.R.V.I.S." : ""));
if (!history.length) addMsg("progress", `Good day, ${who.address}. How may I help?`);

async function sendChat(text) {
  history.push({ kind: "user", text }); persist();
  addMsg("user", text, who.name);
  $("chat-send").disabled = true;
  $("wave").classList.add("on");
  const progress = addMsg("progress", `Working on it, ${who.address}.`);
  try {
    const r = await fetch("/api/chat", { method: "POST", headers: HEADERS, body: JSON.stringify({ text }) });
    if (!r.ok) {
      let msg = "Request failed.";
      try { msg = (await r.json()).error || msg; } catch (e) { /* ignore */ }
      progress.remove(); addMsg("error", msg); return;
    }
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.type === "progress") progress.textContent = ev.text.slice(-300);
        else if (ev.type === "final") { progress.remove(); addMsg("jarvis", ev.text, "J.A.R.V.I.S."); history.push({ kind: "jarvis", text: ev.text }); persist(); }
        else if (ev.type === "error") { progress.remove(); addMsg("error", `My apologies, ${who.address}. ` + ev.text); }
      }
    }
  } catch (e) {
    progress.remove(); addMsg("error", "The connection to the panel was lost.");
  } finally {
    progress.isConnected && progress.remove();
    $("chat-send").disabled = false;
    $("wave").classList.remove("on");
    $("chat-input").focus();
  }
}
$("chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = $("chat-input").value.trim();
  if (!t || $("chat-send").disabled) return;
  $("chat-input").value = "";
  showView("chat");
  sendChat(t);
});
$("chat-new").addEventListener("click", async () => {
  await api("/api/chat/reset", "POST");
  history.length = 0; persist(); chatLog.replaceChildren();
  addMsg("progress", `A fresh start, ${who.address}. What shall we tackle?`);
});

/* ---------- email (read-only; message text is untrusted, so it is only ever shown as plain text) ---------- */
let inboxFilter = "all", inboxSel = null, inboxTimer = null;
function inboxMessage(box, text, ...buttons) { fill(box, el("p", {}, text), el("div", { class: "btn-row" }, ...buttons)); }
function jobButton(label, id, primary = true) { const b = el("button", { class: "btn" + (primary ? " btn-primary" : "") }, label); b.addEventListener("click", () => runJob(id)); return b; }
async function loadInbox() {
  const q = $("inbox-search").value.trim();
  const { data } = await api("/api/mail?filter=" + inboxFilter + "&q=" + encodeURIComponent(q));
  if (!data) return;
  const st = data.state, setup = $("inbox-setup-body");
  const stage = st.stage;
  $("inbox-setup").hidden = stage === "ready"; $("inbox-main").hidden = stage !== "ready";
  if (stage === "not_set_up") return inboxMessage(setup, "Log in to your school Outlook once and JARVIS can read, search and summarise your email, and warn you about anything important. It never sends, moves or deletes mail.", jobButton("Log in to Outlook email", "login_outlook"));
  if (stage === "login") return inboxMessage(setup, "The Outlook session expired. Log in again to resume.", jobButton("Log in to Outlook email", "login_outlook"));
  if (stage === "no_access") return inboxMessage(setup, "You are logged in, but JARVIS could not read the mailbox with any known method. " + (st.message || "") + " Run the access check and send me its output so I can add the right method.", jobButton("Check Outlook email access", "probe_outlook"));
  if (stage === "waiting") return inboxMessage(setup, "Logged in. JARVIS will fetch your email within 10 minutes of being started. If nothing appears, run the access check.", jobButton("Check Outlook email access", "probe_outlook", false));
  $("inbox-stats").textContent = st.count + " stored, " + st.unread + " unread";
  fill($("inbox-list"), ...(data.messages.length ? data.messages.map((m) => {
    const li = el("li", {}, el("button", { class: "mail-item" + (m.is_read ? "" : " unread") + (m.id === inboxSel ? " sel" : ""), type: "button" },
      el("span", { class: "udot" }),
      el("div", { class: "grow", style: "min-width:0" },
        el("div", { class: "top" }, el("span", { class: "from" }, m.sender_name || m.sender_addr), el("span", { class: "when" }, m.when)),
        el("div", { class: "subj" }, el("span", { class: "st" }, m.subject), m.category === "important" ? el("span", { class: "tag tag-warn" }, "Important") : ""),
        el("div", { class: "pv" }, m.preview))));
    li.firstChild.addEventListener("click", () => openMail(m.id));
    return li;
  }) : [el("li", { class: "muted" }, q ? "No messages match your search." : "Nothing here yet.")]));
  clearTimeout(inboxTimer);
  if (currentView === "inbox") inboxTimer = setTimeout(loadInbox, 30000);
}
async function openMail(id) {
  inboxSel = id;
  document.querySelectorAll(".mail-item").forEach((b) => b.classList.remove("sel"));
  const { data } = await api("/api/mail/item?id=" + encodeURIComponent(id));
  if (!data || data.error) { fill($("inbox-reader"), el("p", { class: "muted" }, "Could not open that message.")); return; }
  const meta = [data.sender_name + (data.sender_addr ? " <" + data.sender_addr + ">" : ""), data.when, data.has_attachments ? "has attachments" : ""].filter(Boolean).join(" \u00b7 ");
  fill($("inbox-reader"), el("h3", {}, data.subject), el("div", { class: "meta" }, meta),
    el("pre", {}, data.body || data.preview || "(no text)"),
    data.web_link ? el("p", {}, el("a", { href: data.web_link, target: "_blank", rel: "noopener noreferrer" }, "Open in Outlook")) : "");
  loadInbox();
}
document.querySelectorAll("#inbox-filters .chat-tab").forEach((b) => b.addEventListener("click", () => {
  inboxFilter = b.dataset.filter;
  document.querySelectorAll("#inbox-filters .chat-tab").forEach((x) => x.classList.toggle("active", x === b));
  loadInbox();
}));
let inboxDebounce = null;
$("inbox-search").addEventListener("input", () => { clearTimeout(inboxDebounce); inboxDebounce = setTimeout(loadInbox, 250); });

/* ---------- chats (Telegram groups the reader mirrors; read-only) ---------- */
let chatsState = null, chatGroup = null, chatsEditing = false, chatsTimer = null;
function setupBox(title, ...kids) { $("cs-title").textContent = title; fill($("cs-body"), ...kids); }
async function tgCall(action, body) {
  const r = await api("/api/telegram/login/" + action, "POST", body || {});
  return r.data || { stage: "idle", error: "The panel did not answer." };
}
function loginForm(login) {
  const err = login && login.error ? el("div", { class: "form-err" }, login.error) : "";
  const stage = login ? login.stage : "idle";
  if (stage === "code_sent" || stage === "password_needed") {
    const pw = stage === "password_needed";
    const input = el("input", { type: pw ? "password" : "text", placeholder: pw ? "Two-step verification password" : "Code from Telegram", autocomplete: "off", inputmode: pw ? "text" : "numeric" });
    const go = el("button", { class: "btn btn-primary" }, pw ? "Confirm password" : "Confirm code");
    const cancel = el("button", { class: "btn btn-quiet" }, "Start over");
    const submit = async () => { go.disabled = true; const st = await tgCall(pw ? "password" : "code", pw ? { password: input.value } : { code: input.value }); chatsState.login = st; await loadChats(); };
    go.addEventListener("click", submit); input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    cancel.addEventListener("click", async () => { chatsState.login = await tgCall("cancel"); loadChats(); });
    return [el("p", {}, pw ? "Your Telegram account has two-step verification. Enter that password (it is used once and never saved)." : "Telegram has sent a login code to your Telegram app (or by SMS). Enter it here."),
      err, el("div", { class: "setup-form" }, input, go, cancel)];
  }
  const phone = el("input", { type: "tel", placeholder: "+65 9123 4567", autocomplete: "off" });
  const go = el("button", { class: "btn btn-primary" }, "Send me a code");
  const submit = async () => { go.disabled = true; chatsState.login = await tgCall("start", { phone: phone.value }); loadChats(); };
  go.addEventListener("click", submit); phone.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  return [el("p", {}, "JARVIS reads your groups as you, the same way the Telegram app does. Enter your phone number and Telegram will send a login code. Nothing is posted and the number is not stored."),
    err, el("div", { class: "setup-form" }, phone, go)];
}
function pickForm(state) {
  const chosen = Object.fromEntries(state.chosen.map((g) => [g.id, g]));
  if (!state.dialogs.length) return [el("p", {}, "JARVIS loads your Telegram groups once it is running. Start JARVIS from the top bar, wait a minute, then come back to this tab."),
    el("p", { class: "muted small" }, state.listener === "login_needed" ? "The Telegram reader says it still needs a login." : "")];
  const rows = state.dialogs.map((d) => {
    const c = chosen[d.id];
    const box = el("input", { type: "checkbox" }); box.checked = !!c;
    const label = el("input", { type: "text", placeholder: "Name, e.g. Project team", value: c ? c.label : d.name, maxlength: "40" });
    const kind = el("select", {}, ...["class", "project", "other"].map((k) => el("option", { value: k }, k[0].toUpperCase() + k.slice(1))));
    kind.value = c ? c.kind : "other";
    const row = el("div", { class: "pick-row" }, box, el("span", { class: "nm" }, d.name), label, kind);
    row.dataset.id = d.id; row._get = () => ({ on: box.checked, id: d.id, label: label.value, kind: kind.value });
    return row;
  });
  const save = el("button", { class: "btn btn-primary" }, "Save groups");
  const note = el("span", { class: "muted small" }, "");
  save.addEventListener("click", async () => {
    const picked = rows.map((r) => r._get()).filter((g) => g.on).map(({ id, label, kind }) => ({ id, label, kind }));
    const r = await api("/api/chats/groups", "POST", { groups: picked });
    note.textContent = r.ok ? "Saved. JARVIS starts reading them within a minute." : "Could not save.";
    if (r.ok) { chatsEditing = false; chatGroup = null; setTimeout(loadChats, 600); }
  });
  return [el("p", {}, "Tick the groups JARVIS should read, give each a name, and say what it is. Class and project groups are summarised and checked for dates and deadlines separately."),
    el("div", { class: "pick-list" }, ...rows), el("div", { class: "btn-row" }, save, note)];
}
/* A saved Telegram session file only proves a login was STARTED. What the group reader itself reports decides what to show. */
function ownAccountStatus(state) {
  if (state.ops_running === false) return [el("p", {}, "JARVIS is stopped. Press Start at the top: the group reader runs inside JARVIS.")];
  if (state.listener === "login_needed") {
    return [el("p", { class: "form-err" }, "Telegram has not confirmed your login yet (the login may have been started but never finished). Log in again below."),
      ...loginForm(state.login)];
  }
  if (state.listener === "error") {
    return [el("p", { class: "form-err" }, "The Telegram reader hit an error" + (state.listener_message ? ": " + state.listener_message : "") + ". It retries every 30 seconds. If this stays, press Restart, and tell me the message if it keeps coming back.")];
  }
  if (state.listener === "listening") return [el("p", {}, "Connected to Telegram. Your groups are loading and will appear here within a minute.")];
  return [el("p", {}, "Waiting for JARVIS's Telegram reader to connect (up to 30 seconds). If this stays for over a minute, press Restart at the top."),
    el("p", { class: "muted small" }, "Reader status: " + (state.listener || "not reported yet"))];
}
function renderChatsSetup(state) {
  $("chats-main").hidden = true; $("chats-setup").hidden = false;
  if (state.stage === "pick") return setupBox("Choose your groups", ...pickForm(state));
  const botSteps = el("ol", { class: "setup-steps" },
    el("li", {}, "In Telegram, open @BotFather, choose /mybots, pick your JARVIS bot, then Bot Settings, then Group Privacy, and turn it off. (This lets the bot see the group's messages. It still cannot post.)"),
    el("li", {}, "Add the JARVIS bot to your class group and to your project group as a normal member. You must be the one to add it."),
    el("li", {}, "Come back to this tab: the groups appear here for you to tick. If you added the bot before turning privacy off, remove it and add it again."));
  const bot = el("div", { class: "method" }, el("h3", {}, "Option 1: add the JARVIS bot to the group (recommended)"),
    el("p", {}, "The bot sits in the group and only listens. It never posts, replies, or answers anyone: commands, mentions and buttons are ignored in groups, even from you, and it leaves a group at once if anyone else adds it. It reads from the moment it joins; it cannot see older messages."), botSteps);
  const own = el("details", { class: "method" }, el("summary", {}, "Option 2: read as yourself (also gets older messages)"),
    ...(state.telethon === "no_api_keys" ? [el("p", {}, "One-time setup: open my.telegram.org, log in, choose API development tools, create an app, and copy the api_id and api_hash into secrets.env as TELETHON_API_ID and TELETHON_API_HASH. Restart JARVIS, then come back here.")]
      : state.telethon === "login" ? loginForm(state.login) : ownAccountStatus(state)));
  setupBox("Connect your group chats", bot, own);
}
function renderChatsMain(state) {
  $("chats-setup").hidden = true; $("chats-main").hidden = false;
  if (chatGroup === null || !state.chosen.some((g) => g.id === chatGroup)) chatGroup = state.chosen[0].id;
  fill($("chats-tabs"), ...state.chosen.map((g) => {
    const b = el("button", { class: "chat-tab" + (g.id === chatGroup ? " active" : ""), type: "button", role: "tab" }, g.label, el("small", {}, fmtNum(g.messages)));
    b.addEventListener("click", () => { chatGroup = g.id; $("chats-search").value = ""; renderChatsMain(state); loadChatMessages(); });
    return b;
  }));
  loadChatMessages();
}
async function loadChatMessages() {
  if (chatGroup === null) return;
  const q = $("chats-search").value.trim();
  const { data } = await api("/api/chats/messages?group=" + chatGroup + "&limit=150&q=" + encodeURIComponent(q));
  if (!data || !data.ok) { fill($("chats-log"), el("div", { class: "muted" }, (data && data.message) || "Could not load messages.")); return; }
  const log = $("chats-log"), stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 60;
  let day = "";
  const rows = [];
  data.messages.forEach((m) => {
    if (m.day !== day) { day = m.day; rows.push(el("div", { class: "msg-day" }, day)); }
    rows.push(el("div", { class: "gm" }, el("time", {}, m.time), el("div", {}, el("b", {}, m.sender), el("p", {}, m.text))));
  });
  fill(log, ...(rows.length ? rows : [el("div", { class: "muted" }, q ? "No messages match your search." : "No messages stored yet. They appear here as people write in the group.")]));
  $("chats-count").textContent = q ? data.messages.length + " match(es)" : "Latest " + data.messages.length + " messages";
  if (stick && !q) log.scrollTop = log.scrollHeight;
}
async function loadChats() {
  const { data } = await api("/api/chats");
  if (!data) return;
  chatsState = { ...data, login: data.login || (chatsState && chatsState.login) };
  if (chatsState.stage === "ready" && !chatsEditing) renderChatsMain(chatsState);
  else renderChatsSetup(chatsEditing && chatsState.stage === "ready" ? { ...chatsState, stage: "pick" } : chatsState);
  clearTimeout(chatsTimer);
  if (currentView === "chats" && chatsState.stage === "ready" && !chatsEditing) chatsTimer = setTimeout(loadChats, 10000);
}
$("chats-edit").addEventListener("click", () => { chatsEditing = true; loadChats(); });
let searchDebounce = null;
$("chats-search").addEventListener("input", () => { clearTimeout(searchDebounce); searchDebounce = setTimeout(loadChatMessages, 250); });

/* ---------- calendar (read-only month view of Google Calendar) ---------- */
const SGT_TODAY = () => new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Singapore" });
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
let calYear = null, calMonth = null, calEvents = [], calSelected = null, calLoaded = 0;
function isoAdd(iso, days) { const d = new Date(iso + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + days); return d.toISOString().slice(0, 10); }
const evClass = (e) => (e.jarvis ? (e.tentative ? "t" : "j") : "o");
function eventsOn(iso) {
  return calEvents.filter((e) => e.date <= iso && iso <= e.end_date)
    .sort((a, b) => (b.all_day - a.all_day) || String(a.start).localeCompare(String(b.start)));
}
function evLabel(e) { return e.all_day || !e.start ? e.title : e.start + " " + e.title; }
function gridDays() {
  const first = new Date(Date.UTC(calYear, calMonth - 1, 1));
  const offset = (first.getUTCDay() + 6) % 7; // Monday first
  const start = isoAdd(first.toISOString().slice(0, 10), -offset);
  return Array.from({ length: 42 }, (_, i) => isoAdd(start, i));
}
function renderCalendar() {
  $("cal-title").textContent = MONTHS[calMonth - 1] + " " + calYear;
  const today = SGT_TODAY();
  fill($("cal-dow"), ...["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => el("div", {}, d)));
  fill($("cal-grid"), ...gridDays().map((iso) => {
    const evs = eventsOn(iso), inMonth = Number(iso.slice(5, 7)) === calMonth;
    const cell = el("button", { class: "cal-cell" + (inMonth ? "" : " out") + (iso === today ? " today" : "") + (iso === calSelected ? " sel" : ""),
      type: "button", "aria-label": iso + ", " + evs.length + " event(s)" }, el("span", { class: "cal-num" }, String(Number(iso.slice(8)))));
    evs.slice(0, 3).forEach((e) => cell.append(el("span", { class: "ev " + evClass(e), title: e.title }, evLabel(e))));
    if (evs.length > 3) cell.append(el("span", { class: "more" }, "+" + (evs.length - 3) + " more"));
    cell.addEventListener("click", () => { calSelected = iso; renderCalendar(); });
    return cell;
  }));
  renderCalDay();
}
function renderCalDay() {
  if (!calSelected) return;
  const d = new Date(calSelected + "T00:00:00Z");
  $("cal-day-title").textContent = d.toLocaleDateString("en-GB", { timeZone: "UTC", weekday: "long", day: "numeric", month: "long" });
  const evs = eventsOn(calSelected);
  fill($("cal-day-list"), ...(evs.length ? evs.map((e) => {
    const when = e.all_day ? "All day" + (e.end_date !== e.date ? " (until " + e.end_date.slice(8) + "/" + e.end_date.slice(5, 7) + ")" : "") : e.start + (e.end ? " to " + e.end : "");
    const tag = e.jarvis ? (e.tentative ? "JARVIS deadline, tentative: please double-check" : "JARVIS deadline") : "";
    return el("li", { class: evClass(e) }, el("b", {}, e.title), el("div", { class: "sub" }, when),
      e.location ? el("div", { class: "sub" }, e.location) : "", tag ? el("div", { class: "sub" }, tag) : "",
      e.link ? el("a", { href: e.link, target: "_blank", rel: "noopener noreferrer" }, "Open in Google Calendar") : "");
  }) : [el("li", { class: "o" }, el("div", { class: "sub" }, "Nothing on this day."))]));
}
function calMessage(data) {
  const box = $("cal-msg");
  box.hidden = false; $("cal-body").hidden = true;
  const text = el("span", {}, data.message || "Could not load the calendar.");
  const kids = [text];
  if (data.state === "login") { const b = el("button", { class: "btn btn-primary" }, "Connect Google Calendar"); b.addEventListener("click", () => runJob("login_google")); kids.push(b); }
  if (data.state === "setup") kids.push(el("span", { class: "muted" }, "Save your Google OAuth client file as state/google_client.json first (see deploy/README.md), then use Connect in Logins & Tools."));
  if (data.state === "error") { const b = el("button", { class: "btn" }, "Try again"); b.addEventListener("click", () => loadCalendar(true)); kids.push(b); }
  fill(box, ...kids);
}
async function loadCalendar(refresh = false) {
  if (calYear === null) { const t = SGT_TODAY(); calYear = Number(t.slice(0, 4)); calMonth = Number(t.slice(5, 7)); calSelected = t; }
  $("cal-title").textContent = MONTHS[calMonth - 1] + " " + calYear;
  const { data } = await api("/api/calendar?year=" + calYear + "&month=" + calMonth + (refresh ? "&refresh=1" : ""));
  if (!data) { calMessage({ state: "error", message: "The panel could not load the calendar." }); return; }
  if (!data.ok) { calMessage(data); return; }
  $("cal-msg").hidden = true; $("cal-body").hidden = false;
  calEvents = data.events; calLoaded = Date.now();
  if (!calSelected || Number(calSelected.slice(5, 7)) !== calMonth || Number(calSelected.slice(0, 4)) !== calYear) calSelected = calYear + "-" + String(calMonth).padStart(2, "0") + "-01";
  renderCalendar();
}
function calStep(delta) {
  calMonth += delta;
  if (calMonth < 1) { calMonth = 12; calYear -= 1; } else if (calMonth > 12) { calMonth = 1; calYear += 1; }
  calSelected = null; loadCalendar();
}
$("cal-prev").addEventListener("click", () => calStep(-1));
$("cal-next").addEventListener("click", () => calStep(1));
$("cal-today").addEventListener("click", () => { calYear = null; loadCalendar(); });
$("cal-refresh").addEventListener("click", () => loadCalendar(true));

/* ---------- deadlines ---------- */
async function loadDeadlines() {
  const { data } = await api("/api/deadlines?days=" + $("dl-days").value);
  const rows = data || [];
  fill($("dl-table").querySelector("tbody"), ...(rows.length ? rows.map((d) => el("tr", {},
    el("td", {}, d.date), el("td", {}, d.time || "All day"), el("td", {}, d.course),
    el("td", {}, d.link ? el("a", { href: d.link, target: "_blank", rel: "noopener noreferrer" }, d.title) : d.title),
    el("td", {}, el("span", { class: "tag " + (d.confirmed ? "tag-ok" : "tag-warn") }, d.confirmed ? "Confirmed" : "Tentative"))))
    : [el("tr", {}, el("td", { colspan: "5", class: "muted" }, "No deadlines in this period."))]));
}
$("dl-days").addEventListener("change", loadDeadlines);

/* ---------- usage ---------- */
const JOB_NAMES = { chat_telegram: "Chat (Telegram)", chat_web: "Chat (browser)", do_it: "Do it (assignments)",
  briefing: "Morning briefing", group_summary: "Class chat summary", extract_dates: "Date extraction", other: "Other" };
async function loadUsage() {
  const { data } = await api("/api/usage?days=" + $("usage-days").value);
  if (!data) return;
  const t = data.total;
  const stat = (v, l) => el("div", { class: "stat" }, el("b", {}, v), el("span", {}, l));
  fill($("usage-total"), stat(fmtNum(t.calls), "Claude calls"), stat(fmtNum(t.failed), "Failed"),
    stat((t.seconds / 60).toFixed(1) + " min", "Time spent"), stat(fmtNum(t.in_tokens) + " / " + fmtNum(t.out_tokens), "Tokens in / out (partly estimated)"));
  fill($("usage-table").querySelector("tbody"), ...(data.by_job.length ? data.by_job.map((j) => {
    const mark = j.estimated ? "~" : "";
    return el("tr", {}, el("td", {}, JOB_NAMES[j.job] || j.job), el("td", { class: "num" }, fmtNum(j.calls)),
      el("td", { class: "num" }, fmtNum(j.failed)), el("td", { class: "num" }, (j.seconds / j.calls).toFixed(1) + " s"),
      el("td", { class: "num" }, mark + fmtNum(j.in_tokens)), el("td", { class: "num" }, mark + fmtNum(j.out_tokens)));
  }) : [el("tr", {}, el("td", { colspan: "6", class: "muted" }, "No Claude calls recorded in this period yet."))]));
  const max = Math.max(1, ...data.by_day.map((d) => d.calls));
  fill($("usage-days-chart"), ...data.by_day.map((d) => {
    const b = el("div", { class: "bar", title: d.day + ": " + d.calls + " calls" }, el("span", {}, String(d.calls)));
    b.style.height = Math.max(4, Math.round((d.calls / max) * 84)) + "px";
    return b;
  }));
}
$("usage-days").addEventListener("change", loadUsage);

/* ---------- logs ---------- */
async function loadLogs() {
  const { data } = await api("/api/logs?n=300");
  const v = $("log-view");
  const stick = v.scrollTop + v.clientHeight >= v.scrollHeight - 40;
  v.textContent = (data && data.lines.length) ? data.lines.join("\n") : "No log output yet.";
  if (stick && $("logs-follow").checked) v.scrollTop = v.scrollHeight;
}

/* ---------- tools ---------- */
let jobsLoaded = false;
async function loadTools() {
  const { data } = await api("/api/jobs");
  if (!data) return;
  if (!jobsLoaded) {
    const make = (j) => {
      const b = el("button", { class: j.id === "connect_all" ? "btn btn-primary btn-big" : "btn", "data-job": j.id, "data-label": j.label }, j.label);
      b.addEventListener("click", () => runJob(j.id));
      return b;
    };
    fill($("connect-main"), ...data.jobs.filter((j) => j.id === "connect_all").map(make));
    fill($("tool-buttons"), ...data.jobs.filter((j) => j.id !== "connect_all").map(make));
    jobsLoaded = true;
    styleToolButtons();
  }
  const cur = data.current;
  $("job-title").textContent = cur ? cur.label + " (" + cur.state + ")" : "Output";
  $("job-output").textContent = cur ? (cur.output || "Waiting for output...") : "No task has been run yet.";
  $("job-cancel").hidden = !(cur && cur.state === "running");
}
$("job-cancel").addEventListener("click", async () => { await api("/api/jobs/cancel", "POST"); loadTools(); });

/* ---------- updates ---------- */
async function checkUpdate() {
  $("update-msg").textContent = "Checking GitHub...";
  const r = await api("/api/update/check", "POST");
  const data = r.data;
  if (!data) { $("update-msg").textContent = "Could not check for updates (the panel answered with HTTP " + r.status + "). Quit the panel and start it again."; return; }
  $("update-msg").textContent = data.message;
  fill($("update-changes"), ...(data.changes || []).map((c) => el("li", {}, c)));
  let after = "";
  try { after = sessionStorage.getItem("jarvis-update-result") || ""; } catch (e) { /* ignore */ }
  $("update-notes").textContent = after || (data.notes || []).join(" ");
  $("update-apply").hidden = !(data.ok && data.behind);
}
$("update-check").addEventListener("click", checkUpdate);
$("update-apply").addEventListener("click", async () => {
  $("update-apply").disabled = true;
  $("update-msg").textContent = "Updating...";
  const r = await api("/api/update/apply", "POST");
  if (!r.ok) { $("update-msg").textContent = (r.data && r.data.message) || "Update failed."; $("update-apply").disabled = false; return; }
  $("update-msg").textContent = r.data.message;
  try { sessionStorage.setItem("jarvis-update-result", r.data.message); } catch (e) { /* ignore */ }
  if (r.data.restart) waitForRestart();
});
async function waitForRestart() {
  notice("Restarting with the new version. This page will reload by itself.", 180000);
  for (let i = 0; i < 90; i++) {
    await new Promise((res) => setTimeout(res, 2000));
    try { const r = await fetch("/api/state"); if (r.ok) { location.reload(); return; } } catch (e) { /* still restarting */ }
  }
  notice("The panel did not come back. Start it again with Open JARVIS.command.");
}
checkUpdate();
setInterval(checkUpdate, 30 * 60 * 1000);

/* ---------- first-run setup wizard ---------- */
const wiz = { step: 0, token: "", botName: "", ownerId: 0, ownerName: "", base: "" };
const WIZ_TITLES = ["Welcome", "Create your Telegram bot", "Connect your Telegram", "POLITEMall login", "About you"];
function wizPost(action, body) { return api("/api/setup/" + action, "POST", body).then((r) => r.data || { ok: false, error: "The panel did not answer." }); }
function wizErr(msg) { return msg ? el("div", { class: "form-err" }, msg) : ""; }
function wizField(label, attrs) { const input = el("input", attrs); return [el("label", {}, label), input, input]; }
function renderWizard(err) {
  $("wizard").hidden = false;
  $("wiz-title").textContent = WIZ_TITLES[wiz.step];
  fill($("wiz-steps"), ...WIZ_TITLES.map((_, i) => el("span", { class: i <= wiz.step ? "on" : "" })));
  const next = (label, fn) => { const b = el("button", { class: "btn btn-primary" }, label); b.addEventListener("click", async () => { b.disabled = true; await fn(); b.disabled = false; }); return b; };
  const back = () => { const b = el("button", { class: "btn btn-quiet" }, "Back"); b.addEventListener("click", () => { wiz.step--; renderWizard(); }); return b; };
  const body = $("wiz-body");
  if (wiz.step === 0) {
    fill(body, el("p", {}, "This takes about five minutes. JARVIS watches POLITEMall and Teams for you and messages you on Telegram. Everything stays on this computer."),
      el("p", {}, "You will need: the Telegram app, and your POLITEMall login."),
      el("div", { class: "wiz-actions" }, next("Start", () => { wiz.step = 1; renderWizard(); })));
  } else if (wiz.step === 1) {
    const [l, , input] = wizField("Bot token", { type: "password", autocomplete: "off", placeholder: "123456789:AAH...", value: wiz.token });
    fill(body, el("p", {}, "JARVIS talks to you through your own private Telegram bot."),
      el("ol", {}, el("li", {}, "In Telegram, search for @BotFather and open it."), el("li", {}, "Send /newbot and follow the questions."), el("li", {}, "Copy the token it gives you and paste it below.")),
      l, input, wizErr(err),
      el("div", { class: "wiz-actions" }, back(), next("Check token", async () => {
        const r = await wizPost("token", { token: input.value });
        if (!r.ok) return renderWizard(r.error);
        wiz.token = input.value.trim(); wiz.botName = r.username; wiz.step = 2; renderWizard();
      })));
  } else if (wiz.step === 2) {
    fill(body, el("p", {}, `Bot found: @${wiz.botName}.`),
      el("ol", {}, el("li", {}, `Open @${wiz.botName} in Telegram and press Start.`), el("li", {}, "Send it any message, for example: hi."), el("li", {}, "Come back here and press the button.")),
      el("p", { class: "muted small" }, "Only your account will ever be able to use the bot."), wizErr(err),
      el("div", { class: "wiz-actions" }, back(), next("I sent a message", async () => {
        const r = await wizPost("owner", { token: wiz.token });
        if (!r.ok) return renderWizard(r.error);
        wiz.ownerId = r.id; wiz.ownerName = r.name; wiz.step = 3; renderWizard();
      })));
  } else if (wiz.step === 3) {
    const [l1, , user] = wizField("POLITEMall username (student ID)", { type: "text", autocomplete: "off", value: wiz.user || "" });
    const [l2, , pass] = wizField("POLITEMall password", { type: "password", autocomplete: "off" });
    fill(body, el("p", {}, `Thanks${wiz.ownerName ? ", " + wiz.ownerName : ""}. Now your school login. It is saved only in secrets.env on this computer and used to sign in to POLITEMall.`),
      l1, user, l2, pass, wizErr(err),
      el("div", { class: "wiz-actions" }, back(), next("Continue", async () => {
        if (!user.value.trim() || !pass.value) return renderWizard("Enter both your username and password.");
        wiz.user = user.value.trim(); wiz.pass = pass.value; wiz.step = 4; renderWizard();
      })));
  } else {
    const [l1, , name] = wizField("What should JARVIS call you?", { type: "text", maxlength: "40", value: wiz.ownerName || "" });
    const [l2, , addr] = wizField("How should it address you? (Sir, Ma'am, boss, or just your name)", { type: "text", maxlength: "20", value: "Sir" });
    fill(body, l1, name, l2, addr, wizErr(err),
      el("div", { class: "wiz-actions" }, back(), next("Finish setup", async () => {
        const r = await wizPost("save", { token: wiz.token, owner_id: wiz.ownerId, school_user: wiz.user, school_pass: wiz.pass, school_base: wiz.base, name: name.value, address: addr.value });
        if (!r.ok) return renderWizard(r.error);
        fill(body, el("p", {}, "Saved. JARVIS is restarting; this page reloads by itself. Then open Logins & Tools and press Connect everything."));
        waitForRestart();
      })));
  }
}
async function checkSetup() {
  const r = await api("/api/setup");
  if (r.ok && r.data && r.data.needed) { wiz.base = r.data.default_base; renderWizard(); }
}
checkSetup();

/* ---------- polling ---------- */
refreshState(); loadFeed(); loadCalls();
setInterval(refreshState, 3000);
setInterval(() => { if (currentView === "overview") { loadFeed(); loadCalls(); } }, 60000);
setInterval(() => { if (currentView === "logs") loadLogs(); if (currentView === "tools") loadTools(); }, 2000);
setInterval(() => { if (currentView === "calendar" && Date.now() - calLoaded > 5 * 60 * 1000) loadCalendar(); }, 60000);
