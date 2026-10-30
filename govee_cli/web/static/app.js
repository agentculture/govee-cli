/* govee-cli web console. Served by `govee serve`; talks only to its own /api.
   Every /api call carries `Authorization: Bearer <token>` from localStorage when there is one;
   without one it goes out bare (answered on a network serve trusts). A 401 shows the pairing screen.
   This file is ASCII only: non-ASCII characters are written as \u escapes. */
(() => {
  "use strict";

  // ---------- helpers ----------
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, attrs = {}, html = "") => {
    const e = document.createElement(tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (html) e.innerHTML = html;
    return e;
  };
  const NS = "http://www.w3.org/2000/svg";
  const sv = (tag, attrs = {}) => {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  };
  const esc = v => String(v == null ? "" : v).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const hex = (r, g, b) => "#" + [r, g, b].map(v => Math.round(v).toString(16).padStart(2, "0")).join("").toUpperCase();
  const HEX_RE = /^#[0-9A-Fa-f]{6}$/;
  const safeColor = c => (typeof c === "string" && HEX_RE.test(c) ? c.toUpperCase() : null);
  const DOT = "\u00B7", DASH = "\u2013", ELL = "\u2026";
  // Kelvin to RGB (Tanner Helland approximation)
  function kelvin(k) {
    const t = k / 100; let r, g, b;
    r = t <= 66 ? 255 : 329.698727446 * Math.pow(t - 60, -0.1332047592);
    g = t <= 66 ? 99.4708025861 * Math.log(t) - 161.1195681661 : 288.1221695283 * Math.pow(t - 60, -0.0755148492);
    if (t >= 66) b = 255;
    else if (t <= 19) b = 0;
    else b = 138.5177312231 * Math.log(t - 10) - 305.0447927307;
    const c = v => Math.max(0, Math.min(255, v));
    return hex(c(r), c(g), c(b));
  }
  const toastEl = $("#toast"); let toastT;
  const toast = msg => { toastEl.textContent = msg; toastEl.classList.add("show"); clearTimeout(toastT); toastT = setTimeout(() => toastEl.classList.remove("show"), 2800); };
  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // ---------- token + api ----------
  const TOKEN_KEY = "govee.console.token";
  // Pairing tokens are url-safe base64 (secrets.token_urlsafe): store nothing else.
  const TOKEN_RE = /^[A-Za-z0-9_-]{16,128}$/;
  const validToken = v => typeof v === "string" && TOKEN_RE.test(v);
  const store = {
    get() { try { const v = localStorage.getItem(TOKEN_KEY) || ""; return validToken(v) ? v : ""; } catch (e) { return ""; /* storage blocked (private mode): treat as unpaired */ } },
    set(v) {
      if (!validToken(v)) {
        return false;
      }
      try {
        localStorage.setItem(TOKEN_KEY, v);
      } catch (e) {
        // storage blocked (private mode): the token lives for this page only
      }
      return true;
    },
    clear() { try { localStorage.removeItem(TOKEN_KEY); } catch (e) { /* storage blocked */ } },
  };
  // A pairing link looks like http://host:8742/#token=<secret>: keep the secret, drop it from the URL.
  // The pairing secret in the URL fragment, or null when the fragment carries none.
  const hashToken = () => new URLSearchParams(location.hash.slice(1)).get("token");
  function takeTokenFromHash() {
    const tok = hashToken();
    if (tok === null) return;
    if (tok.trim()) store.set(tok.trim());
    history.replaceState(null, "", location.pathname + location.search);
  }

  class Unauthorized extends Error {}
  class ApiError extends Error {
    constructor(status, body) {
      const e = body?.error || {};
      super(e.message || `Request failed (HTTP ${status})`);
      this.status = status; this.code = e.code || "http_" + status; this.remediation = e.remediation || "";
    }
  }
  // Without a token the request goes out bare: serve answers it when this device is on a network
  // it was told to trust (`govee serve --trust`), else 401 shows the pairing screen.
  async function send(path, body, token) {
    const headers = { Accept: "application/json" };
    if (token) headers.Authorization = "Bearer " + token;
    const init = { method: body ? "POST" : "GET", headers, cache: "no-store", credentials: "same-origin" };
    if (body) { headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
    return fetch(path, init);
  }
  async function api(path, body) {
    const token = store.get();
    let res = await send(path, body, token);
    if (res.status === 401 && token) { store.clear(); res = await send(path, body, ""); }
    if (res.status === 401) {
      showGate(token ? "That token was not accepted. Pair again with a fresh one." : undefined);
      throw new Unauthorized();
    }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) throw new ApiError(res.status, data);
    return data || {};
  }
  function errText(e) {
    if (!(e instanceof ApiError)) return "Cannot reach govee serve.";
    return e.remediation ? e.message + " " + e.remediation : e.message;
  }

  // ---------- screens ----------
  let started = false;
  function showGate(msg) {
    stopPolling();
    $("#app").hidden = true; $("#boot").hidden = true; $("#gate").hidden = false;
    const ge = $("#gateError"); ge.hidden = !msg; ge.textContent = msg || "";
  }
  function showApp() { $("#gate").hidden = true; $("#boot").hidden = true; $("#app").hidden = false; }
  $("#gateForm").addEventListener("submit", e => {
    e.preventDefault();
    const v = $("#gateToken").value.trim();
    if (!v) return;
    if (!store.set(v)) { showGate("That does not look like a govee token. Copy the whole token from 'govee token new'."); return; }
    $("#gateToken").value = "";
    $("#gate").hidden = true; $("#boot").hidden = false;
    void boot();
  });

  // ---------- routing ----------
  const views = new Set(["lamp", "stats", "agents"]);
  let view = "lamp";
  function route() {
    view = views.has(location.hash.slice(1)) ? location.hash.slice(1) : "lamp";
    document.querySelectorAll("[data-view-panel]").forEach(p => { p.hidden = p.dataset.viewPanel !== view; });
    document.querySelectorAll(".tabs a").forEach(a => (a.dataset.view === view ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
    if (view === "stats") void loadStats();
  }
  window.addEventListener("hashchange", () => {
    // A pairing link opened in a tab that already shows the pairing screen.
    if (hashToken() !== null) { takeTokenFromHash(); if (!$("#gate").hidden) { $("#gate").hidden = true; $("#boot").hidden = false; void boot(); return; } }
    if (started) route();
  });

  // ---------- lamp state ----------
  const COLORS = [["Ember", "#FF6A2B"], ["Amber", "#FFB23F"], ["Rose", "#FF5C8A"], ["Violet", "#9B6BFF"], ["Ocean", "#2F8CFF"], ["Aurora", "#2FD6A3"], ["Lime", "#B6F23A"], ["Daylight", "#FFF3DE"]];
  let scenes = []; // [{id, name}] from /api/scenes
  let device = null; let reachable = false;
  const actual = { on: false, bright: 50, mode: "color", color: "#FFFFFF", temp: 2700, scene: null };
  let target = { ...actual };
  let mode = "dry"; // "dry" -> apply:false, "live" -> apply:true
  let plan = null; // last dry-run response for the current target: {key, commands, cli} | {key, error}
  let planSeq = 0; let busy = false;

  const sceneName = id => { const s = scenes.find(x => x.id === id || x.name === id); return s ? s.name : String(id); };
  const lampColor = s => (s.mode === "white" ? kelvin(s.temp) : s.color);
  // The colour part of a state in words: its scene, its white temperature, or its hex color.
  const shade = s => {
    if (s.scene) return sceneName(s.scene) + " scene";
    return s.mode === "white" ? s.temp + " K" : s.color;
  };
  const describe = s => (s.on ? `${s.bright}% ${DOT} ${shade(s)}` : "off");

  function fromState(st) {
    device = st.device || device; reachable = !!st.reachable;
    const next = {
      on: !!st.on,
      bright: Number.isFinite(st.brightness) ? Math.max(1, Math.min(100, Math.round(st.brightness))) : actual.bright,
      mode: st.mode === "white" ? "white" : "color",
      color: safeColor(st.color) || actual.color,
      temp: Number.isFinite(st.kelvin) ? st.kelvin : actual.temp,
      scene: st.scene || null,
    };
    const wasClean = requests(false).length === 0;
    Object.assign(actual, next);
    if (wasClean) target = { ...actual };
  }

  // The writes needed to move the lamp from `actual` to `target`, as API requests.
  function requests(apply) {
    const out = [], light = lightChanges();
    // Fade (approved deviation d11): "" leaves it to the server (on/off fade by its default).
    const fade = $("#fade").value;
    if (Object.keys(light).length && fade !== "") light.fade_seconds = Number(fade);
    if (Object.keys(light).length) out.push({ path: "/api/light", body: { ...light, apply } });
    if (target.on && target.scene && target.scene !== actual.scene) out.push({ path: "/api/scene", body: { scene: target.scene, apply } });
    return out;
  }
  // The /api/light fields that differ between `actual` and `target` (fade not included).
  function lightChanges() {
    const light = {};
    if (target.on !== actual.on) light.on = target.on;
    if (!target.on) return light;
    if (target.bright !== actual.bright) light.brightness = target.bright;
    if (target.scene) return light;
    if (target.mode === "color" && (target.color !== actual.color || actual.mode !== "color" || actual.scene)) light.color = target.color;
    if (target.mode === "white" && (target.temp !== actual.temp || actual.mode !== "white" || actual.scene)) light.kelvin = target.temp;
    return light;
  }

  async function post(reqs) {
    const commands = [], cli = []; const statuses = []; const warnings = []; let fade = null;
    for (const r of reqs) {
      const res = await api(r.path, r.body);
      (res.commands || []).forEach(c => commands.push(c));
      (res.cli || []).forEach(c => cli.push(c));
      (Array.isArray(res.warnings) ? res.warnings : []).forEach(w => warnings.push(String(w)));
      if (res.fade && typeof res.fade === "object") fade = res.fade;
      statuses.push(res.status || (res.applied ? "sent" : "dry_run"));
    }
    return { commands, cli, statuses, warnings, fade };
  }
  function fmtSec(v) {
    const s = Math.max(0, num(v));
    if (s < 60) return `${+s.toFixed(1)} s`;
    if (s >= 3600) return `${+(s / 3600).toFixed(1)} h`;
    const rest = s % 60 >= 1 ? " " + Math.round(s % 60) + " s" : "";
    return `${Math.floor(s / 60)} min${rest}`;
  }
  const STATUS_TEXT = { dry_run: "dry run, not sent", sent: "sent", confirmed: "confirmed by the lamp", unconfirmed: "sent, not confirmed" };
  function statusToast(statuses) {
    const rank = ["unconfirmed", "sent", "confirmed", "dry_run"];
    const worst = rank.find(s => statuses.includes(s)) || statuses[0];
    toast({ dry_run: "Dry run: nothing was sent", sent: "Sent to lamp", confirmed: "Sent and confirmed by the lamp", unconfirmed: "Sent, but the lamp has not confirmed it yet" }[worst] || "Done");
  }

  // Ask the server what it would send (apply:false) for the current target.
  async function refreshPlan() {
    const reqs = requests(false); const key = JSON.stringify(reqs); const seq = ++planSeq;
    if (!reqs.length) { plan = null; render(); return; }
    plan = { key, pending: true }; render();
    try {
      const r = await post(reqs);
      if (seq !== planSeq) return;
      plan = { key, commands: r.commands, cli: r.cli, fade: r.fade, warnings: r.warnings };
      void loadEvents();
    } catch (e) {
      if (e instanceof Unauthorized || seq !== planSeq) return;
      plan = { key, error: errText(e) };
    }
    render();
  }

  async function applyNow() {
    const reqs = requests(true); if (!reqs.length || busy) return;
    const sent = { ...target };
    busy = true; render();
    try {
      const r = await post(reqs);
      Object.assign(actual, sent); plan = null;
      const running = r.fade && r.statuses.includes("sent");
      if (r.warnings.length) toast(r.warnings.join(" "));
      else if (running) toast(`Fading over ${fmtSec(r.fade.seconds)}`);
      else statusToast(r.statuses);
      if (running) void loadFade();
    } catch (e) {
      busy = false;
      if (e instanceof Unauthorized) return;
      toast(errText(e));
      if (mode === "live") target = { ...actual };
    }
    busy = false;
    render(); void loadEvents();
    // Live: a change made while this one was in flight goes out next.
    if (mode === "live" && requests(true).length) void applyNow();
  }

  // A committed change (click, or 'change' at the end of a drag / pick).
  function change(patch) {
    Object.assign(target, patch);
    if (mode === "live") { render(); void applyNow(); } else void refreshPlan();
  }
  // A preview while dragging or while the picker is open: visual only, no request.
  function preview(patch) { Object.assign(target, patch); render(); }

  function render() {
    const c = lampColor(target);
    document.documentElement.style.setProperty("--lamp-css", target.on ? c : "");
    const stage = $("#stage");
    stage.style.setProperty("--lamp-css", c);
    stage.style.setProperty("--glow", target.on ? (0.25 + target.bright / 100 * 0.75).toFixed(2) : 0);
    $("#lamp").dataset.on = target.on;
    $("#lampName").textContent = device?.name || "Lamp";
    $("#readout").innerHTML = target.on
      ? `<span>On</span><span>${target.bright}%</span><span>${esc(shade(target))}</span>`
      : "<span>Off</span>";
    const reqs = requests(false); const dirty = reqs.length > 0;
    const diff = $("#lampDiff");
    diff.dataset.shown = dirty ? "true" : "false";
    diff.textContent = `Preview. The lamp is still ${describe(actual)}.`;
    $("#power").setAttribute("aria-checked", target.on);
    $("#bright").value = target.bright; $("#brightOut").textContent = target.bright + "%";
    $("#temp").value = target.temp; $("#tempOut").textContent = target.temp + " K";
    const isWhite = target.mode === "white";
    $("#tab-color").setAttribute("aria-selected", !isWhite); $("#tab-white").setAttribute("aria-selected", isWhite);
    $("#pane-color").hidden = isWhite; $("#pane-white").hidden = !isWhite;
    document.querySelectorAll(".swatch").forEach(s => s.setAttribute("aria-pressed", !target.scene && !isWhite && s.dataset.c === target.color));
    document.querySelectorAll("#scenes .chip").forEach(s => s.setAttribute("aria-pressed", s.dataset.s === target.scene));
    const cc = $("#customColor"); if (cc && document.activeElement !== cc && HEX_RE.test(target.color)) cc.value = target.color.toLowerCase();

    renderStatus();

    // what will be sent
    const current = plan?.key === JSON.stringify(reqs) ? plan : null;
    renderWire(dirty, current);
    renderSend(mode === "dry" && current?.commands, current, dirty);
  }

  // status pill + device line
  function renderStatus() {
    $("#statusPill").dataset.state = reachable ? "ok" : "bad";
    const note = $("#lampNotice");
    note.hidden = reachable || !device;
    if (!device) {
      $("#statusText").textContent = "No lamp";
      $("#deviceSub").textContent = DASH;
      note.textContent = "";
      return;
    }
    const lan = device.transport === "lan";
    $("#statusText").textContent = `${device.name || device.id} ${DOT} ${lan ? "LAN" : "cloud"} ${reachable ? "reachable" : "unreachable"}`;
    $("#deviceSub").textContent = `${device.model || "Govee lamp"} ${DOT} ${lan ? "LAN on UDP 4003, cloud as fallback" : "Govee cloud API"}`;
    note.textContent = reachable ? "" : `${device.name || "The lamp"} is not answering right now. The state shown is the last one govee serve saw.`;
  }

  // The placeholder line of the "what will be sent" panel, or null when there is a plan to show.
  function wireHint(dirty, current) {
    if (!dirty) return mode === "dry" ? "No changes yet. Adjust the lamp above to see the exact commands." : "Live: changes are sent as you make them.";
    if (mode === "live") return busy ? "Sending" + ELL : "Release to send.";
    if (!current) return "Release to see the exact commands.";
    if (current.pending) return "Asking govee serve what it would send" + ELL;
    return null;
  }
  function renderWire(dirty, current) {
    const wire = $("#wire"); wire.innerHTML = "";
    const hint = wireHint(dirty, current);
    if (hint) wire.append(el("div", { class: "wire-empty" }, hint));
    else if (current.error) wire.append(el("div", { class: "wire-error", role: "alert" }, esc(current.error)));
    else if (!current.commands.length) wire.append(el("div", { class: "wire-empty" }, "govee serve would send nothing for this change."));
    else renderCommands(wire, current);
  }
  function wireItem(wire, cm) {
    const it = el("div", { class: "wire-item" });
    const where = cm.transport === "lan" ? `LAN ${DOT} UDP 4003` : `Cloud ${DOT} Govee Developer API`;
    it.append(el("div", { class: "wire-meta" }, `<span>${esc(cm.verb)}</span><span>${where}</span>`));
    const pre = el("pre"); pre.textContent = JSON.stringify(cm.payload); it.append(pre); wire.append(it);
  }
  function renderCommands(wire, current) {
    const item = cm => wireItem(wire, cm);
    (current.warnings || []).forEach(w => wire.append(el("div", { class: "wire-warn", role: "note" }, esc(w))));
    const f = current.fade; const cmds = current.commands;
    if (f) wire.append(el("div", { class: "wire-fade" }, `<b>Fade over ${esc(fmtSec(f.seconds))}</b><span>${esc(num(f.steps))} steps, at most one every ${esc(num(f.interval_ms))} ms</span>`));
    if (f && cmds.length > 6) {
      cmds.slice(0, 3).forEach(item);
      wire.append(el("div", { class: "wire-more" }, `${ELL} ${cmds.length - 5} more commands ${ELL}`));
      cmds.slice(-2).forEach(item);
    } else cmds.forEach(item);
  }

  // CLI equivalent, Send / Discard buttons and the mode badge.
  function renderSend(ready, current, dirty) {
    $("#cliEquiv").innerHTML = ready && current.cli.length ? "Same as: " + current.cli.map(x => `<code>${esc(x)}</code>`).join(" ") : "";
    const n = ready ? current.commands.length : 0;
    $("#sendBtn").disabled = !ready || busy; $("#discardBtn").disabled = !dirty || busy;
    let label = "Send to lamp";
    if (busy) label = "Sending" + ELL;
    else if (n > 1) label = `Send ${n} commands`;
    $("#sendBtn").textContent = label;
    $("#modeBadge").className = "badge " + (mode === "dry" ? "dry" : "live");
    $("#modeBadge").textContent = mode === "dry" ? "Dry run" : "Live";
    $("#sendSub").textContent = mode === "dry" ? "Dry run: your changes stay here until you send them." : "Live: every change goes straight to the lamp.";
  }

  // ---------- active fade (/api/fade) ----------
  let fadeT = null;
  const FADE_LABEL = { fade_on: "Fading in", fade_off: "Fading out" };
  function renderFade(a) {
    $("#fadeBar").hidden = !a;
    if (!a) return;
    const p = Math.max(0, Math.min(1, num(a.progress)));
    $("#fadeProgress").value = p;
    const left = num(a.seconds) * (1 - p);
    $("#fadeText").textContent = `${FADE_LABEL[a.kind] || "Fading"} ${DOT} ${Math.round(p * 100)}% ${DOT} ${fmtSec(left)} left`;
  }
  async function loadFade() {
    let active = null;
    try {
      const r = await api("/api/fade");
      active = r.active && typeof r.active === "object" ? r.active : null;
    } catch (e) {
      if (e instanceof Unauthorized) return;
      active = null;
    }
    renderFade(active);
    if (active && !fadeT) fadeT = setInterval(() => { if (!document.hidden) void loadFade(); }, 750);
    else if (!active && fadeT) { clearInterval(fadeT); fadeT = null; void loadState(); void loadEvents(); }
  }
  function stopFadeWatch() {
    if (fadeT) clearInterval(fadeT);
    fadeT = null;
  }

  // ---------- activity feed (/api/events) ----------
  const SRC_LABEL = { cli: "CLI", web: "Web", mcp: "MCP", rules: "Rules" };
  function ago(iso) {
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return "";
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 45) return "now";
    if (s < 3600) return Math.round(s / 60) + " min";
    if (s < 86400) return Math.round(s / 3600) + " h";
    return Math.round(s / 86400) + " d";
  }
  function renderFeed(events) {
    const ul = $("#feed"); ul.innerHTML = "";
    if (!events.length) { ul.append(el("li", { class: "feed-empty" }, "No commands logged yet.")); return; }
    events.slice(0, 8).forEach(f => {
      const src = SRC_LABEL[f.source] ? f.source : "cli";
      const li = el("li");
      li.append(el("span", { class: "src", style: `--s: var(--s-${src})` }, SRC_LABEL[src]));
      const status = STATUS_TEXT[f.status] || esc(f.status || "");
      li.append(el("div", {}, `<b>${esc(f.summary || f.verb)}</b><span class="who">${esc(f.actor || "unknown")} ${DOT} ${status}</span>`));
      li.append(el("time", { datetime: esc(f.ts), title: esc(f.ts) }, esc(ago(f.ts))));
      ul.append(li);
    });
  }
  async function loadEvents() {
    try { const r = await api("/api/events?limit=20"); renderFeed(Array.isArray(r.events) ? r.events : []); } catch (e) { if (!(e instanceof Unauthorized)) renderFeed([]); }
  }
  async function loadState() {
    try {
      fromState(await api("/api/state"));
    } catch (e) {
      if (e instanceof Unauthorized) return false;
      reachable = false;
    }
    render();
    return true;
  }
  async function loadScenes() {
    let list = [];
    try { const r = await api("/api/scenes"); list = Array.isArray(r.scenes) ? r.scenes : []; } catch (e) { list = []; /* no scenes: the chip row says so */ }
    scenes = list.filter(s => s?.id != null).map(s => ({ id: String(s.id), name: String(s.name || s.id) }));
    const sc = $("#scenes"); sc.innerHTML = "";
    if (!scenes.length) { sc.append(el("span", { class: "hint" }, "No scenes reported for this lamp.")); return; }
    scenes.forEach(s => {
      const b = el("button", { type: "button", class: "chip", "data-s": s.id }); b.textContent = s.name;
      b.addEventListener("click", () => change({ scene: target.scene === s.id ? null : s.id, on: true }));
      sc.append(b);
    });
    render();
  }

  // ---------- controls ----------
  const sw = $("#swatches");
  COLORS.forEach(([n, c]) => {
    const b = el("button", { type: "button", class: "swatch", style: `--c:${c}`, "data-c": c, "aria-label": n, title: n });
    b.addEventListener("click", () => change({ mode: "color", color: c, scene: null }));
    sw.append(b);
  });
  sw.append(el("label", { class: "custom-color" }, `<input type="color" id="customColor" value="#ffffff" aria-label="Custom color">Custom`));
  // 'input' previews on the lamp visual only (no request); 'change' commits.
  $("#customColor").addEventListener("input", e => preview({ mode: "color", color: e.target.value.toUpperCase(), scene: null }));
  $("#customColor").addEventListener("change", e => change({ mode: "color", color: e.target.value.toUpperCase(), scene: null }));
  $("#power").addEventListener("click", () => change({ on: !target.on }));
  $("#bright").addEventListener("input", e => preview({ bright: +e.target.value }));
  $("#bright").addEventListener("change", e => change({ bright: +e.target.value }));
  $("#temp").addEventListener("input", e => preview({ temp: +e.target.value, mode: "white", scene: null }));
  $("#temp").addEventListener("change", e => change({ temp: +e.target.value, mode: "white", scene: null }));
  $("#tab-color").addEventListener("click", () => change({ mode: "color", scene: null }));
  $("#tab-white").addEventListener("click", () => change({ mode: "white", scene: null }));
  $("#sendBtn").addEventListener("click", () => void applyNow());
  $("#fade").addEventListener("change", () => { if (mode === "dry" && requests(false).length) void refreshPlan(); else render(); });
  $("#fadeStop").addEventListener("click", async () => {
    try {
      const r = await api("/api/fade/cancel", {});
      toast(r.cancelled ? "Fade stopped" : "No fade was running");
    } catch (e) {
      if (e instanceof Unauthorized) return;
      toast(errText(e));
    }
    stopFadeWatch(); renderFade(null); void loadState(); void loadEvents();
  });
  $("#discardBtn").addEventListener("click", () => { target = { ...actual }; plan = null; planSeq++; render(); toast("Changes discarded"); });
  document.querySelectorAll(".seg button").forEach(b => b.addEventListener("click", () => {
    mode = b.dataset.mode;
    document.querySelectorAll(".seg button").forEach(x => x.setAttribute("aria-pressed", x === b));
    if (mode === "live") { target = { ...actual }; plan = null; planSeq++; toast("Live: changes now go straight to the lamp"); }
    render();
  }));
  $("#copyMcp").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("#mcpJson").textContent);
      toast("Copied .mcp.json");
    } catch (e) {
      // Clipboard blocked (plain-http origin or permission denied): select the text for a manual copy.
      const r = document.createRange(); r.selectNodeContents($("#mcpJson"));
      const s = getSelection(); s.removeAllRanges(); s.addRange(r);
      toast("Selected. Copy it with your keyboard");
    }
  });
  $("#unpairBtn").addEventListener("click", () => { store.clear(); showGate(); });

  // ---------- tooltip ----------
  const tip = $("#tip");
  function showTip(e, html) {
    tip.innerHTML = html; tip.hidden = false;
    const r = tip.getBoundingClientRect();
    let x = e.clientX + 14, y = e.clientY + 14;
    if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = e.clientY - r.height - 14;
    tip.style.left = Math.max(8, x) + "px"; tip.style.top = Math.max(8, y) + "px";
  }
  const hideTip = () => { tip.hidden = true; };

  // ---------- chart frame (Table toggle + Source line, embodiment pattern) ----------
  function frame(fig, { title, sub, source, legend, draw, table, empty }) {
    fig.innerHTML = "";
    const head = el("div", { class: "chart-head" }, `<div><h3>${title}</h3><p>${sub}</p></div>`);
    const btn = el("button", { type: "button", class: "pill-btn", "aria-pressed": "false" }, "Table");
    if (!empty) head.append(btn);
    fig.append(head);
    if (legend && !empty) fig.append(el("div", { class: "legend" }, legend.map(([n, v]) => `<span style="--s:var(${v})">${n}</span>`).join("")));
    const body = el("div", { class: "plot" }); fig.append(body);
    fig.append(el("figcaption", { class: "chart-src" }, "Source: " + source));
    if (empty) { body.append(el("div", { class: "chart-empty", role: "note" }, `<div><b>${empty[0]}</b><br>${empty[1]}</div>`)); return; }
    let showTable = false;
    const paint = () => {
      body.innerHTML = "";
      if (showTable) { const w = el("div", { class: "table-wrap" }); w.append(table()); body.append(w); } else draw(body);
      btn.textContent = showTable ? "Chart" : "Table"; btn.setAttribute("aria-pressed", showTable);
    };
    btn.addEventListener("click", () => { showTable = !showTable; paint(); });
    paint();
  }
  function mkTable(cols, rows) {
    const t = el("table", { class: "chart-table" });
    const th = c => `<th scope="col">${c}</th>`;
    const td = c => `<td>${esc(c)}</td>`;
    const tr = r => `<tr>${r.map(td).join("")}</tr>`;
    t.innerHTML = `<thead><tr>${cols.map(th).join("")}</tr></thead><tbody>${rows.map(tr).join("")}</tbody>`;
    return t;
  }
  const fmtH = h => { const hh = Math.floor(h), mm = Math.round((h - hh) * 60); return mm === 60 ? `${hh + 1}h 00m` : `${hh}h ${String(mm).padStart(2, "0")}m`; };
  const clock = h => { const m = Math.round(h * 60); return `${String(Math.floor(m / 60) % 24).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`; };
  const tclock = t => { const d = new Date(t); return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; };
  const NICE = [1, 2, 2.5, 5]; // axis steps per power of ten; 10 above the last
  function niceMax(v, steps = 4) {
    if (Number.isNaN(v) || v <= 0) return { max: steps, step: 1 };
    const raw = v / steps, p = Math.pow(10, Math.floor(Math.log10(raw))), n = raw / p;
    const s = (NICE.find(m => n <= m) ?? 10) * p; return { max: s * steps, step: s };
  }
  const parseDay = s => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s)); return m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(Number.NaN); };
  const dayLabel = d => d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  const wk = d => d.toLocaleDateString("en-GB", { weekday: "short" });
  const num = v => (Number.isFinite(+v) ? +v : 0);
  const EDGE_ANCHOR = { 0: "start", 24: "end" }; // 24h axis: first and last labels hug the edges
  // Commands-by-source series: [label, color token, stats field].
  const SRC = [["CLI", "--s-cli", "cli"], ["Web", "--s-web", "web"], ["MCP agents", "--s-mcp", "mcp"], ["Rules", "--s-rules", "rules"]];
  const total = d => d.cli + d.web + d.mcp + d.rules;
  // Index of the last source with commands that day: its segment tops the stack.
  const topSrc = d => {
    let top = -1;
    SRC.forEach(([, , k], i) => { if (d[k]) top = i; });
    return top;
  };
  function srcTip(d) {
    const row = ([n, v, k]) => `<div class="row"><span><i class="sw" style="background:var(${v})"></i>${n}</span><span>${d[k]}</span></div>`;
    return `<strong>${wk(d.d)} ${dayLabel(d.d)}</strong>` + SRC.map(row).join("") + `<div class="row"><span>Total</span><span>${total(d)}</span></div>`;
  }

  // Turn day_samples into contiguous segments. A {gap:true} entry, or a hole
  // longer than MAX_SPAN, is a break: no strip, no line, "no data" in the tooltip.
  const MAX_SPAN = 5 * 60 * 1000;
  function daySegments(samples, t0, t1) {
    const pts = samples.map(s => ({ ...s, t: Date.parse(s.ts) })).filter(s => Number.isFinite(s.t)).sort((a, b) => a.t - b.t);
    const segs = [];
    pts.forEach((s, i) => {
      const next = i + 1 < pts.length ? pts[i + 1].t : Math.min(t1, s.t + 60 * 1000);
      const start = Math.max(t0, s.t), end = Math.min(t1, next, s.gap ? next : s.t + MAX_SPAN);
      if (end <= start) return;
      if (s.gap) { segs.push({ start, end, gap: true }); return; }
      const color = safeColor(s.rgb) || (Number.isFinite(s.kelvin) ? kelvin(s.kelvin) : null);
      segs.push({ start, end, on: !!s.on, b: num(s.brightness), color, kelvin: s.kelvin, rgb: safeColor(s.rgb) });
      if (end < next && end < t1) segs.push({ start: end, end: Math.min(next, t1), gap: true });
    });
    return segs;
  }

  // ---------- stats (/api/stats) ----------
  let stats = null; let statsAt = 0;
  async function loadStats(force) {
    if (!force && stats && Date.now() - statsAt < 30000) { drawStats(); return; }
    try {
      stats = await api("/api/stats"); statsAt = Date.now();
      $("#statsNotice").hidden = true;
    } catch (e) {
      if (e instanceof Unauthorized) return;
      $("#statsNotice").textContent = "Stats are unavailable right now. " + errText(e); $("#statsNotice").hidden = false;
      stats = stats || {};
    }
    drawStats();
  }

  function drawStats() {
    if (view !== "stats" || !stats) return;
    const tiles = stats.tiles || {};
    const hoursPerDay = (Array.isArray(stats.hours_per_day) ? stats.hours_per_day : []).map(r => ({ d: parseDay(r.date), hrs: num(r.hours) })).filter(r => !Number.isNaN(r.d.getTime()));
    const bySrc = (Array.isArray(stats.commands_by_source) ? stats.commands_by_source : []).map(r => ({ d: parseDay(r.date), cli: num(r.cli), web: num(r.web), mcp: num(r.mcp), rules: num(r.rules) })).filter(r => !Number.isNaN(r.d.getTime()));
    const samples = Array.isArray(stats.day_samples) ? stats.day_samples : [];
    const heatRows = Array.isArray(stats.heatmap) ? stats.heatmap : [];
    const fmtNum = (v, f) => (Number.isFinite(v) ? f(v) : DASH);
    $("#tiles").innerHTML = [
      [fmtNum(tiles.on_today_h, fmtH), "On today", "since midnight"],
      [fmtNum(tiles.on_week_h, v => v.toFixed(1) + " h"), "On this week", "last 7 days"],
      [fmtNum(tiles.commands_7d, String), "Commands", `last 7 days, ${Number.isFinite(tiles.agent_commands_7d) ? tiles.agent_commands_7d : 0} from agents`],
      [fmtNum(tiles.avg_brightness, v => v + "%"), "Typical brightness", "average while on"],
    ].map(([b, s, m]) => `<div class="tile"><b>${esc(b)}</b><span>${s}</span><small>${esc(m)}</small></div>`).join("");

    const W = 720;
    // ---- 24h: color strip + brightness line
    const t1 = Date.now(), t0 = t1 - 24 * 3600 * 1000;
    const segs = samples.length ? daySegments(samples, t0, t1) : [];
    const segRows = () => {
      const rows = []; let prev = null;
      segs.forEach(s => {
        let label = "Off";
        if (s.gap) label = "No data";
        else if (s.on) label = "On";
        const key = label + "|" + (s.on ? s.b + "|" + s.color : "");
        if (prev?.key === key && prev.end === s.start) { prev.end = s.end; return; }
        prev = { key, label, start: s.start, end: s.end, b: s.on ? s.b + "%" : DASH, c: s.on && s.color ? s.color : DASH }; rows.push(prev);
      });
      return rows.map(r => [`${tclock(r.start)}${DASH}${tclock(r.end)}`, r.label, r.b, r.c]);
    };
    frame($("#c-day"), {
      title: "Last 24 hours", sub: "The strip shows the lamp's actual color; the line is brightness while on. Breaks are times with no samples.",
      source: "state samples from <code>govee serve</code>, local stats database",
      empty: segs.length ? null : ["No samples yet", "Samples appear here while govee serve is running."],
      table: () => mkTable(["Time", "State", "Brightness", "Color"], segRows()),
      draw: body => {
        const H = 230, L = 36, R = 12, T = 8, stripH = 26, gap = 14, chartT = T + stripH + gap, B = 26, ph = H - chartT - B;
        const x = t => L + (t - t0) / (t1 - t0) * (W - L - R), y = v => chartT + ph - v / 100 * ph;
        const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Lamp state and brightness over the last 24 hours" });
        const fallback = css("--cobalt");
        segs.forEach(s => {
          if (s.gap) return; // a break: nothing drawn
          const x0 = x(s.start), w = Math.max(0.5, x(s.end) - x0 + 0.3);
          svg.append(sv("rect", s.on ? { x: x0, y: T, width: w, height: stripH, fill: s.color || fallback } : { x: x0, y: T + stripH / 2 - 1, width: w, height: 2, fill: css("--rule") }));
        });
        const g = sv("g", { class: "grid" }), ax = sv("g", { class: "axis" });
        [0, 50, 100].forEach(v => { g.append(sv("line", { x1: L, x2: W - R, y1: y(v), y2: y(v) })); const t = sv("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }); t.textContent = v + "%"; ax.append(t); });
        [0, 6, 12, 18, 24].forEach(h => { const tt = t0 + h * 3600 * 1000; const t = sv("text", { x: x(tt), y: H - 6, "text-anchor": EDGE_ANCHOR[h] || "middle" }); t.textContent = h === 24 ? "now" : tclock(tt); ax.append(t); });
        svg.append(g, ax);
        // line + area only across contiguous "on" segments; off and gaps break it
        let d = "", a = "", run = [];
        const flush = () => {
          if (!run.length) return;
          d += run.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join("");
          a += `M${run[0][0].toFixed(1)},${y(0)}` + run.map(p => `L${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("") + `L${run.at(-1)[0].toFixed(1)},${y(0)}Z`;
          run = [];
        };
        segs.forEach((s, i) => {
          const prev = segs[i - 1];
          if (!s.on || s.gap) { flush(); return; }
          if (prev && (prev.end !== s.start || !prev.on || prev.gap)) flush();
          run.push([x(s.start), y(s.b)], [x(s.end), y(s.b)]);
        });
        flush();
        svg.append(sv("path", { d: a, fill: css("--cobalt"), opacity: 0.12 }));
        svg.append(sv("path", { d, fill: "none", stroke: css("--cobalt"), "stroke-width": 2, "stroke-linejoin": "round" }));
        svg.append(sv("line", { class: "base", x1: L, x2: W - R, y1: y(0), y2: y(0) }));
        const cross = sv("line", { y1: T, y2: y(0), stroke: css("--quiet"), "stroke-width": 1, "stroke-dasharray": "3 3", visibility: "hidden" });
        const dot = sv("circle", { r: 5, fill: css("--cobalt"), stroke: css("--sheet"), "stroke-width": 2, visibility: "hidden" });
        svg.append(cross, dot);
        const hit = sv("rect", { x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent" });
        hit.addEventListener("pointermove", e => {
          const r = svg.getBoundingClientRect(); const px = (e.clientX - r.left) / r.width * W;
          const t = t0 + (px - L) / (W - L - R) * (t1 - t0);
          const s = segs.find(z => t >= z.start && t < z.end);
          cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
          if (s?.on && !s.gap) { dot.setAttribute("cx", px); dot.setAttribute("cy", y(s.b)); dot.setAttribute("visibility", "visible"); } else dot.setAttribute("visibility", "hidden");
          let html = `<strong>${tclock(t)}</strong>`;
          if (!s || s.gap) html += `<div class="row"><span>No data</span><span></span></div>`;
          else if (!s.on) html += `<div class="row"><span>Off</span><span></span></div>`;
          else html += `<div class="row"><span><i class="sw" style="background:${s.color || fallback}"></i>Color</span><span>${esc(s.rgb || (s.kelvin ? s.kelvin + " K" : DASH))}</span></div><div class="row"><span>Brightness</span><span>${s.b}%</span></div>`;
          showTip(e, html);
        });
        hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); hideTip(); });
        svg.append(hit); body.append(svg);
      },
    });

    // ---- hours on per day (columns)
    const lastIdx = hoursPerDay.length - 1;
    frame($("#c-hours"), {
      title: "Hours on per day", sub: "Last 14 days", source: "state samples, summed per day",
      empty: hoursPerDay.length ? null : ["No days recorded yet", "Each day appears once govee serve has sampled the lamp."],
      table: () => mkTable(["Day", "Hours on"], hoursPerDay.map(d => [`${wk(d.d)} ${dayLabel(d.d)}`, d.hrs.toFixed(1)])),
      draw: body => {
        const Wc = 360, H = 210, L = 28, R = 6, T = 14, B = 24, ph = H - T - B;
        const { max, step } = niceMax(Math.max(...hoursPerDay.map(d => d.hrs)));
        const y = v => T + ph - v / max * ph, bw = (Wc - L - R) / hoursPerDay.length;
        const svg = sv("svg", { viewBox: `0 0 ${Wc} ${H}`, role: "img", "aria-label": "Hours on per day" });
        const g = sv("g", { class: "grid" }), ax = sv("g", { class: "axis" });
        for (let v = 0; v <= max + 1e-9; v += step) {
          if (v) g.append(sv("line", { x1: L, x2: Wc - R, y1: y(v), y2: y(v) }));
          const t = sv("text", { x: L - 5, y: y(v) + 4, "text-anchor": "end" }); t.textContent = +v.toFixed(2); ax.append(t);
        }
        hoursPerDay.forEach((d, i) => { if (hoursPerDay.length <= 8 || i % 2 === lastIdx % 2) { const t = sv("text", { x: L + i * bw + bw / 2, y: H - 6, "text-anchor": "middle" }); t.textContent = d.d.getDate(); ax.append(t); } });
        svg.append(g, ax);
        hoursPerDay.forEach((d, i) => {
          const x0 = L + i * bw + 1, w = Math.max(1, bw - 2), top = y(d.hrs), r = Math.min(4, w / 2, y(0) - top);
          const op = i === lastIdx ? 1 : 0.78;
          const p = sv("path", { d: `M${x0},${y(0)}V${top + r}Q${x0},${top} ${x0 + r},${top}H${x0 + w - r}Q${x0 + w},${top} ${x0 + w},${top + r}V${y(0)}Z`, fill: css("--cobalt"), opacity: op });
          const hit = sv("rect", { x: L + i * bw, y: T, width: bw, height: ph, fill: "transparent" });
          hit.addEventListener("pointermove", e => { p.setAttribute("opacity", 1); showTip(e, `<strong>${wk(d.d)} ${dayLabel(d.d)}</strong><div class="row"><span>On for</span><span>${fmtH(d.hrs)}</span></div>`); });
          hit.addEventListener("pointerleave", () => { p.setAttribute("opacity", op); hideTip(); });
          svg.append(p, hit);
        });
        svg.append(sv("line", { class: "base", x1: L, x2: Wc - R, y1: y(0), y2: y(0) }));
        const last = hoursPerDay[lastIdx];
        const lt = sv("text", { x: L + lastIdx * bw + bw / 2, y: y(last.hrs) - 5, "text-anchor": "middle", fill: css("--ink"), "font-size": 11, "font-weight": 700, "font-family": "inherit" }); lt.textContent = last.hrs.toFixed(1);
        svg.append(lt);
        body.append(svg);
      },
    });

    // ---- commands by source (stacked columns, last 7 days)
    frame($("#c-src"), {
      title: "Who changes the lamp", sub: "Applied commands per day, last 7 days",
      source: "command log, <code>events</code> table", legend: SRC.map(s => [s[0], s[1]]),
      empty: bySrc.length ? null : ["No commands logged yet", "Commands from the CLI, this page, MCP agents and culture-rules appear here."],
      table: () => mkTable(["Day", "CLI", "Web", "MCP", "Rules", "Total"], bySrc.map(d => [`${wk(d.d)} ${dayLabel(d.d)}`, d.cli, d.web, d.mcp, d.rules, total(d)])),
      draw: body => {
        const Wc = 360, H = 210, L = 28, R = 6, T = 10, B = 24, ph = H - T - B;
        const { max, step } = niceMax(Math.max(...bySrc.map(total)));
        const y = v => T + ph - v / max * ph, bw = (Wc - L - R) / bySrc.length;
        const svg = sv("svg", { viewBox: `0 0 ${Wc} ${H}`, role: "img", "aria-label": "Applied commands per day by source" });
        const g = sv("g", { class: "grid" }), ax = sv("g", { class: "axis" });
        for (let v = 0; v <= max + 1e-9; v += step) {
          if (v) g.append(sv("line", { x1: L, x2: Wc - R, y1: y(v), y2: y(v) }));
          const t = sv("text", { x: L - 5, y: y(v) + 4, "text-anchor": "end" }); t.textContent = +v.toFixed(2); ax.append(t);
        }
        bySrc.forEach((d, i) => { const t = sv("text", { x: L + i * bw + bw / 2, y: H - 6, "text-anchor": "middle" }); t.textContent = bySrc.length > 7 ? d.d.getDate() : wk(d.d); ax.append(t); });
        svg.append(g, ax);
        bySrc.forEach((d, i) => {
          const w = Math.max(2, Math.min(30, bw - 10)), x0 = L + i * bw + (bw - w) / 2; let acc = 0; const grp = sv("g");
          const topIdx = topSrc(d);
          SRC.forEach(([, v, k], si) => {
            const val = d[k]; if (!val) return;
            const y1 = y(acc + val), y0 = y(acc); acc += val;
            const hgt = Math.max(0, y0 - y1 - 2), r = si === topIdx ? Math.min(4, hgt, w / 2) : 0;
            // rounded top corners only on the segment that tops the stack
            const tl = r ? `Q${x0},${y1} ${x0 + r},${y1}` : "", tr = r ? `Q${x0 + w},${y1} ${x0 + w},${y1 + r}` : "";
            grp.append(sv("path", { d: `M${x0},${y1 + hgt}V${y1 + r}${tl}H${x0 + w - r}${tr}V${y1 + hgt}Z`, fill: css(v) }));
          });
          const hit = sv("rect", { x: L + i * bw, y: T, width: bw, height: ph, fill: "transparent" });
          hit.addEventListener("pointermove", e => { grp.setAttribute("opacity", 0.85); showTip(e, srcTip(d)); });
          hit.addEventListener("pointerleave", () => { grp.setAttribute("opacity", 1); hideTip(); });
          svg.append(grp, hit);
        });
        svg.append(sv("line", { class: "base", x1: L, x2: Wc - R, y1: y(0), y2: y(0) }));
        body.append(svg);
      },
    });

    // ---- heatmap: when the lamp is on
    const WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    const heat = [];
    for (let d = 0; d < 7; d++) for (let h = 0; h < 24; h++) heat.push({ d, h, v: 0 });
    heatRows.forEach(r => { const d = +r.weekday, h = +r.hour; if (d >= 0 && d < 7 && h >= 0 && h < 24) heat[d * 24 + h].v = Math.max(0, Math.min(60, Math.round(num(r.minutes)))); });
    frame($("#c-heat"), {
      title: "When the lamp is on", sub: "Average minutes on, per hour of the week, last 4 weeks",
      source: "state samples, averaged per weekday and hour",
      empty: heatRows.length ? null : ["No pattern yet", "The heatmap fills in as govee serve samples the lamp over the weeks."],
      table: () => mkTable(["Day", "Busiest hour", "Minutes on that hour", "Hours on per day"], WD.map((n, d) => { const row = heat.filter(c => c.d === d); const top = row.reduce((a, c) => (c.v > a.v ? c : a), { h: 0, v: 0 }); return [n, top.v ? clock(top.h) : DASH, top.v, (row.reduce((a, c) => a + c.v, 0) / 60).toFixed(1)]; })),
      draw: body => {
        const H = 7 * 22 + 30, L = 36, R = 4, T = 2, cw = (W - L - R) / 24, ch = 20;
        const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Heatmap of minutes on by weekday and hour" });
        const ax = sv("g", { class: "axis" });
        WD.forEach((n, d) => { const t = sv("text", { x: L - 6, y: T + d * 22 + ch / 2 + 4, "text-anchor": "end" }); t.textContent = n; ax.append(t); });
        [0, 3, 6, 9, 12, 15, 18, 21].forEach(h => { const t = sv("text", { x: L + h * cw + cw / 2, y: H - 6, "text-anchor": "middle" }); t.textContent = String(h).padStart(2, "0"); ax.append(t); });
        svg.append(ax);
        const lo = css("--rule-soft"), hi = css("--cobalt");
        heat.forEach(c => {
          const pct = Math.round(c.v / 60 * 100);
          const r = sv("rect", { x: L + c.h * cw + 1, y: T + c.d * 22, width: cw - 2, height: ch, rx: 3, fill: lo });
          r.style.fill = `color-mix(in srgb, ${hi} ${pct}%, ${lo})`;
          r.addEventListener("pointermove", e => { r.setAttribute("stroke", css("--ink")); r.setAttribute("stroke-width", 1.5); showTip(e, `<strong>${WD[c.d]} ${clock(c.h)}${DASH}${clock(c.h + 1)}</strong><div class="row"><span>Average on</span><span>${c.v} min</span></div>`); });
          r.addEventListener("pointerleave", () => { r.removeAttribute("stroke"); hideTip(); });
          svg.append(r);
        });
        body.append(svg);
        body.append(el("div", { class: "heat-scale" }, `0 min <i aria-hidden="true"></i> 60 min`));
      },
    });
  }

  // redraw charts on theme change so SVG colors follow tokens
  const mq = matchMedia("(prefers-color-scheme: dark)");
  const redraw = () => { if (view === "stats") drawStats(); };
  if (mq.addEventListener) mq.addEventListener("change", redraw);
  new MutationObserver(redraw).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  // ---------- polling ----------
  let pollT = null;
  function startPolling() {
    stopPolling();
    pollT = setInterval(() => {
      if (document.hidden) return;
      void loadState(); void loadEvents();
      if (!fadeT) void loadFade();
      if (view === "stats") void loadStats(true);
    }, 30000);
  }
  function stopPolling() {
    if (pollT) clearInterval(pollT);
    pollT = null;
    stopFadeWatch();
  }

  // ---------- boot ----------
  async function boot() {
    takeTokenFromHash();
    let ok;
    try { fromState(await api("/api/state")); ok = true; } catch (e) {
      if (e instanceof Unauthorized) return;
      // Paired, but the lamp (or the server) is not answering: show the console with that status.
      ok = e instanceof ApiError;
      if (!ok) { $("#boot").textContent = "Cannot reach govee serve. Is it running? Reload to try again."; return; }
      reachable = false;
    }
    showApp(); started = true;
    render(); route();
    void loadScenes(); void loadEvents(); void loadFade(); startPolling();
  }
  void boot();
})();
