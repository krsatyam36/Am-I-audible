"use strict";

// ---- helpers -------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const fmt = (sec) => {
  sec = Math.floor(sec || 0);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(Math.floor(sec / 3600))}:${p(Math.floor((sec % 3600) / 60))}:${p(sec % 60)}`;
};
const mb = (b) => (b / 1048576).toFixed(1) + " MB";
async function api(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}) });
  return r.json();
}
async function apiGet(path) { return (await fetch(path)).json(); }
function toast(msg, ms = 2600) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
}

// ---- theme ---------------------------------------------------------------
const savedTheme = localStorage.getItem("amia-theme") || "dark";
document.documentElement.dataset.theme = savedTheme;
$("theme").value = savedTheme;
$("theme").addEventListener("change", (e) => {
  document.documentElement.dataset.theme = e.target.value;
  localStorage.setItem("amia-theme", e.target.value);
});

// ---- waveform scope ------------------------------------------------------
class Scope {
  constructor(name) {
    this.name = name;
    this.points = new Float32Array(900).fill(0);
    this.head = 0;
    const card = document.createElement("div");
    card.className = "scope-card"; card.dataset.track = name;
    card.innerHTML = `<div class="scope-head"><span class="name">${name}</span>
      <span class="db" data-db>—</span></div>
      <div class="meter"><i data-meter></i></div><canvas></canvas>`;
    $("scopes").appendChild(card);
    this.canvas = card.querySelector("canvas");
    this.db = card.querySelector("[data-db]");
    this.meter = card.querySelector("[data-meter]");
    this._resize(); addEventListener("resize", () => this._resize());
  }
  _resize() {
    const dpr = devicePixelRatio || 1;
    this.canvas.width = this.canvas.clientWidth * dpr;
    this.canvas.height = this.canvas.clientHeight * dpr;
    this.ctx = this.canvas.getContext("2d"); this.ctx.scale(dpr, dpr);
  }
  push(env, level, peak) {
    for (const v of env) { this.points[this.head] = v; this.head = (this.head + 1) % this.points.length; }
    const db = level > 1e-6 ? 20 * Math.log10(level) : -Infinity;
    this.db.textContent = db === -Infinity ? "−inf dB" : `${db.toFixed(1)} dB`;
    this.db.classList.toggle("clip", peak >= 0.99);
    this.meter.style.width = Math.min(100, Math.max(0, (db + 60) / 60 * 100)) + "%";
  }
  draw() {
    const { ctx, canvas } = this; if (!ctx) return;
    const w = canvas.clientWidth, h = canvas.clientHeight, mid = h / 2;
    ctx.clearRect(0, 0, w, h);
    const cs = getComputedStyle(document.documentElement);
    const color = cs.getPropertyValue(this.name === "system" ? "--wave2" : "--wave").trim();
    ctx.globalAlpha = .22; ctx.strokeStyle = color; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(w, mid); ctx.stroke();
    ctx.globalAlpha = 1; ctx.fillStyle = color; ctx.shadowColor = color; ctx.shadowBlur = 10;
    const n = this.points.length, bw = w / n, bar = Math.max(1, bw * .7);
    for (let i = 0; i < n; i++) {
      const amp = Math.min(1, this.points[(this.head + i) % n]) * (mid - 3);
      const y = mid - amp, hh = Math.max(bar, amp * 2), r = Math.min(bar / 2, hh / 2);
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(i * bw, y, bar, hh, r); else ctx.rect(i * bw, y, bar, hh);
      ctx.fill();
    }
    ctx.shadowBlur = 0;
  }
}
const scopes = {};
function ensureScopes(names) { for (const n of names) if (!scopes[n]) scopes[n] = new Scope(n); }
(function loop() { for (const s of Object.values(scopes)) s.draw(); requestAnimationFrame(loop); })();

// ---- state ---------------------------------------------------------------
let state = "idle", maxSeconds = 0, transcriptEmpty = true;
const isRec = () => state === "recording" || state === "paused";

function setState(s) {
  state = s; const rec = isRec();
  $("rec-led").classList.toggle("live", s === "recording");
  $("rec-label").textContent = s.toUpperCase();
  $("rec-btn-text").textContent = rec ? "Stop" : "Start recording";
  $("start-stop").classList.toggle("recording", rec);
  $("live-controls").hidden = !rec;
  $("pause-btn").textContent = s === "paused" ? "▶ Resume" : "⏸ Pause";
  for (const id of ["t-mic", "t-sys", "t-stt"]) $(id).disabled = rec;
}

function renderGains(tracks, gains) {
  const box = $("gains"); box.innerHTML = "";
  for (const name of tracks || []) {
    const v = (gains && gains[name] != null) ? gains[name] : 1;
    const row = document.createElement("div"); row.className = "gain-row";
    row.innerHTML = `<span class="glabel">${name}</span>
      <input type="range" min="0" max="3" step="0.1" value="${v}" data-gain="${name}"/>
      <span class="gval">${(+v).toFixed(1)}×</span>`;
    const slider = row.querySelector("input"), val = row.querySelector(".gval");
    slider.addEventListener("input", () => {
      val.textContent = (+slider.value).toFixed(1) + "×";
      api("/api/gain", { name, value: +slider.value });
    });
    box.appendChild(row);
  }
}

function fillMics(list, current) {
  const sel = $("mic-select"); sel.innerHTML = "";
  for (const m of list || []) {
    const o = document.createElement("option");
    o.value = m; o.textContent = m; if (m === current) o.selected = true; sel.appendChild(o);
  }
}

let lastSettings = null;
function applyStatus(st) {
  $("backend-badge").textContent = st.backend || "—";
  setState(st.state || "idle");
  fillMics(st.microphones, st.currentMic);
  ensureScopes(st.tracks && st.tracks.length ? st.tracks : ["mic", "system"]);
  renderGains(st.tracks, st.gains);
  $("marker-count").textContent = (st.markers || []).length;
  if (st.settings) { lastSettings = st.settings; $("t-stt").checked = !!st.settings.transcribe; }
  const sttEl = $("stt-status");
  if (st.sttError) sttEl.textContent = "unavailable";
  else if (st.sttDevice) sttEl.textContent = "on · " + st.sttDevice;
  else sttEl.textContent = "off";
}

function addSegments(segs) {
  if (!segs || !segs.length) return;
  const box = $("transcript");
  if (transcriptEmpty) { box.innerHTML = ""; transcriptEmpty = false; }
  for (const s of segs) {
    const el = document.createElement("div"); el.className = "seg";
    const spk = s.speaker
      ? `<b class="spk ${s.speaker === "You" ? "me" : "them"}">${s.speaker}:</b> ` : "";
    el.innerHTML = `<span class="t">${fmt(s.start)}</span><span>${spk}${s.text}</span>`;
    box.appendChild(el);
  }
  box.scrollTop = box.scrollHeight;
}

function applyTelemetry(t) {
  if (t.state) setState(t.state);
  $("timer").textContent = fmt(t.seconds);
  if (t.seconds > maxSeconds) maxSeconds = t.seconds;
  $("total").textContent = "total recorded · " + fmt(maxSeconds);
  ensureScopes(Object.keys(t.tracks || {}));
  for (const [n, d] of Object.entries(t.tracks || {}))
    if (scopes[n]) scopes[n].push(d.env || [], d.level || 0, d.peak || 0);
  addSegments(t.segments);
}

// ---- websocket -----------------------------------------------------------
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => { $("conn-dot").classList.add("ok"); $("conn-label").textContent = "connected"; };
  ws.onclose = () => { $("conn-dot").classList.remove("ok"); $("conn-label").textContent = "reconnecting…"; setTimeout(connect, 1000); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "status") applyStatus(m.data);
    else if (m.type === "telemetry") applyTelemetry(m.data);
  };
}
connect();

// ---- record controls -----------------------------------------------------
$("start-stop").addEventListener("click", async () => {
  if (isRec()) {
    setState("idle");                       // update UI instantly, don't wait
    const r = await api("/api/stop", {});
    applyStatus(r);
    toast(r.saved ? "Saved to History" : "Stopped");
  } else {
    maxSeconds = 0; transcriptEmpty = true;
    $("transcript").innerHTML = `<p class="empty">Listening…</p>`;
    const st = await api("/api/start", {
      recordMic: $("t-mic").checked, recordSystem: $("t-sys").checked, transcribe: $("t-stt").checked });
    applyStatus(st); toast(st.sttError && $("t-stt").checked ? "Recording (STT unavailable)" : "Recording…");
  }
});
$("pause-btn").addEventListener("click", async () => {
  applyStatus(await api(state === "paused" ? "/api/resume" : "/api/pause", {}));
});
$("marker-btn").addEventListener("click", async () => {
  const st = await api("/api/marker", {}); $("marker-count").textContent = (st.markers || []).length;
  toast("Marker dropped @ " + fmt(st.seconds));
});
$("mic-select").addEventListener("change", async (e) => {
  if (!isRec()) return;
  const st = await api("/api/swap-mic", { target: e.target.value }); toast("Swapped mic → " + (st.currentMic || ""));
});

// ---- exit / save ---------------------------------------------------------
$("exit-btn").addEventListener("click", () => { $("save-name").value = ""; $("modal").hidden = false; $("save-name").focus(); });
$("cancel-exit").addEventListener("click", () => ($("modal").hidden = true));
$("confirm-exit").addEventListener("click", async () => {
  const r = await api("/api/exit", { name: $("save-name").value.trim() });
  $("modal").hidden = true;
  document.body.innerHTML = `<div style="display:grid;place-items:center;height:100vh;text-align:center;font-family:system-ui">
    <div><h1>Saved ✓</h1><p style="opacity:.7">${r.saved || "(nothing was recording)"}</p>
    ${r.transcript ? `<p style="opacity:.6">📝 ${r.transcript}</p>` : ""}
    <p style="opacity:.5">You can close this tab.</p></div></div>`;
});

// ---- settings ------------------------------------------------------------
let engineList = [];
function populateModels(engineKey, selected) {
  const eng = engineList.find((e) => e.key === engineKey);
  const sel = $("s-model"); sel.innerHTML = "";
  for (const m of (eng ? eng.models : [])) {
    const id = typeof m === "string" ? m : m.id;
    const label = typeof m === "string" ? m : m.label;
    const o = document.createElement("option"); o.value = id; o.textContent = label;
    if (id === selected) o.selected = true; sel.appendChild(o);
  }
  if (eng && !sel.value && eng.defaultModel) sel.value = eng.defaultModel;
}
function showEngineAvailability() {
  const eng = engineList.find((e) => e.key === $("s-engine").value);
  $("stt-availability").textContent = !eng ? ""
    : eng.available ? `${eng.title} ✓ ready`
    : `${eng.title} not installed — run: ${eng.installHint}`;
}
$("open-settings").addEventListener("click", async () => {
  const s = lastSettings || (await apiGet("/api/settings"));
  engineList = s.engines || [];
  const esel = $("s-engine"); esel.innerHTML = "";
  for (const e of engineList) {
    const o = document.createElement("option"); o.value = e.key;
    o.textContent = e.title + (e.available ? "" : " (not installed)");
    if (e.key === (s.engine || "whisper")) o.selected = true; esel.appendChild(o);
  }
  populateModels(esel.value, s.model);
  showEngineAvailability();
  $("s-transcribe").checked = !!s.transcribe;
  $("s-language").value = s.language || ""; $("s-window").value = s.window || 5;
  $("s-diarize").checked = !!s.diarize; $("s-diarize").disabled = !s.diarizeAvailable;
  $("s-finalize").checked = s.finalizeRepass !== false;
  $("settings-modal").hidden = false;
});
$("s-engine").addEventListener("change", () => {
  populateModels($("s-engine").value); showEngineAvailability();
});
$("cancel-settings").addEventListener("click", () => ($("settings-modal").hidden = true));
$("save-settings").addEventListener("click", async () => {
  await api("/api/settings", {
    transcribe: $("s-transcribe").checked, engine: $("s-engine").value,
    model: $("s-model").value,
    language: $("s-language").value.trim(), window: parseFloat($("s-window").value) || 5,
    diarize: $("s-diarize").checked, finalizeRepass: $("s-finalize").checked });
  $("t-stt").checked = $("s-transcribe").checked;
  $("settings-modal").hidden = true; toast("Settings saved");
});

// ---- navigation + history ------------------------------------------------
function showView(which) {
  $("view-record").hidden = which !== "record";
  $("view-history").hidden = which !== "history";
  $("nav-record").classList.toggle("active", which === "record");
  $("nav-history").classList.toggle("active", which === "history");
  if (which === "history") loadSessions();
}
$("nav-record").addEventListener("click", () => showView("record"));
$("nav-history").addEventListener("click", () => showView("history"));
$("refresh-sessions").addEventListener("click", loadSessions);

async function loadSessions() {
  const box = $("sessions"); box.innerHTML = `<p class="empty">Loading…</p>`;
  const list = await apiGet("/api/sessions");
  if (!list.length) { box.innerHTML = `<p class="empty">No recordings yet.</p>`; return; }
  box.innerHTML = "";
  for (const s of list) {
    const card = document.createElement("div"); card.className = "session";
    const players = s.tracks.map((f) =>
      `<audio controls preload="none" src="/api/recording/${encodeURIComponent(s.name)}/${encodeURIComponent(f)}"></audio>`).join("");
    card.innerHTML = `<div class="meta">
        <div class="sname">${s.name}</div>
        <div class="sinfo">${fmt(s.seconds)} · ${s.tracks.length} track(s) · ${mb(s.sizeBytes)}</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:6px">${players}</div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        ${s.hasTranscript ? `<span class="badge">📝 transcript</span>
           <button class="btn" data-view="${s.name}">View</button>` : ""}
        <button class="btn" data-tx="${s.name}">${s.hasTranscript ? "Re-transcribe" : "Transcribe"}</button>
      </div>`;
    box.appendChild(card);
  }
  box.querySelectorAll("[data-view]").forEach((b) =>
    b.addEventListener("click", () => window.open(`/api/transcript/${encodeURIComponent(b.dataset.view)}`, "_blank")));
  box.querySelectorAll("[data-tx]").forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true; b.textContent = "Transcribing…";
      const r = await api("/api/transcribe", { name: b.dataset.tx });
      toast(r.ok ? `Transcribed (${r.segments} segments)` : `Failed: ${r.error}`);
      loadSessions();
    }));
}

// ---- keyboard ------------------------------------------------------------
addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") return;
  if (e.key === "q") $("exit-btn").click();
  else if (e.key === "m" && isRec()) $("marker-btn").click();
  else if (e.key === " " && isRec()) { e.preventDefault(); $("pause-btn").click(); }
});
