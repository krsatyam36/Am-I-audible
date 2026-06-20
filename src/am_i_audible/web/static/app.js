"use strict";

// ---- helpers -------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const fmt = (sec) => {
  sec = Math.floor(sec || 0);
  const h = String(Math.floor(sec / 3600)).padStart(2, "0");
  const m = String(Math.floor((sec % 3600) / 60)).padStart(2, "0");
  const s = String(sec % 60).padStart(2, "0");
  return `${h}:${m}:${s}`;
};
async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return res.json();
}
function toast(msg, ms = 2600) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
}

// ---- theme ---------------------------------------------------------------
const themeSel = $("theme");
const savedTheme = localStorage.getItem("amia-theme") || "dark";
document.documentElement.dataset.theme = savedTheme;
themeSel.value = savedTheme;
themeSel.addEventListener("change", () => {
  document.documentElement.dataset.theme = themeSel.value;
  localStorage.setItem("amia-theme", themeSel.value);
});

// ---- waveform scopes -----------------------------------------------------
class Scope {
  constructor(name) {
    this.name = name;
    this.points = new Float32Array(900).fill(0);
    this.head = 0;
    const card = document.createElement("div");
    card.className = "scope-card";
    card.dataset.track = name;
    card.innerHTML = `<div class="scope-head"><span class="name">${name}</span>
      <span class="db" data-db>—</span></div>
      <div class="meter"><i data-meter></i></div><canvas></canvas>`;
    $("scopes").appendChild(card);
    this.canvas = card.querySelector("canvas");
    this.db = card.querySelector("[data-db]");
    this.meter = card.querySelector("[data-meter]");
    this.ctx = this.canvas.getContext("2d");
    this._resize();
    addEventListener("resize", () => this._resize());
  }
  _resize() {
    const dpr = devicePixelRatio || 1;
    this.canvas.width = this.canvas.clientWidth * dpr;
    this.canvas.height = this.canvas.clientHeight * dpr;
    this.ctx.scale(dpr, dpr);
  }
  push(env, level, peak) {
    for (const v of env) { this.points[this.head] = v; this.head = (this.head + 1) % this.points.length; }
    const dbfs = level > 1e-6 ? 20 * Math.log10(level) : -Infinity;
    this.db.textContent = dbfs === -Infinity ? "−inf dB" : `${dbfs.toFixed(1)} dB`;
    this.db.classList.toggle("clip", peak >= 0.99);
    this.meter.style.width = Math.min(100, Math.max(0, (dbfs + 60) / 60 * 100)) + "%";
  }
  draw() {
    const { ctx, canvas } = this;
    const w = canvas.clientWidth, h = canvas.clientHeight, mid = h / 2;
    ctx.clearRect(0, 0, w, h);
    const style = getComputedStyle(document.documentElement);
    const color = style.getPropertyValue(this.name === "system" ? "--wave2" : "--wave").trim();
    // soft center baseline
    ctx.globalAlpha = 0.25; ctx.strokeStyle = color; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(w, mid); ctx.stroke();
    ctx.globalAlpha = 1;
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, color); grad.addColorStop(0.5, color + "cc"); grad.addColorStop(1, color);
    ctx.fillStyle = grad; ctx.shadowColor = color; ctx.shadowBlur = 10;
    const n = this.points.length, bw = w / n, bar = Math.max(1, bw * 0.7);
    for (let i = 0; i < n; i++) {
      const amp = Math.min(1, this.points[(this.head + i) % n]) * (mid - 3);
      const x = i * bw, y = mid - amp, hh = Math.max(bar, amp * 2);
      const r = Math.min(bar / 2, hh / 2);
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(x, y, bar, hh, r); else ctx.rect(x, y, bar, hh);
      ctx.fill();
    }
    ctx.shadowBlur = 0;
  }
}

const scopes = {};
function ensureScopes(trackNames) {
  for (const n of trackNames) if (!scopes[n]) scopes[n] = new Scope(n);
}
function renderLoop() {
  for (const s of Object.values(scopes)) s.draw();
  requestAnimationFrame(renderLoop);
}
requestAnimationFrame(renderLoop);

// ---- state / UI ----------------------------------------------------------
let recording = false;
let maxSeconds = 0;

function setRecording(on) {
  recording = on;
  $("rec-led").classList.toggle("live", on);
  $("rec-label").textContent = on ? "RECORDING" : "IDLE";
  const b = $("start-stop");
  $("rec-btn-text").textContent = on ? "Stop" : "Start recording";
  b.classList.toggle("recording", on);
  $("t-mic").disabled = on; $("t-sys").disabled = on;
}

function fillMics(list, current) {
  const sel = $("mic-select");
  sel.innerHTML = "";
  for (const m of list || []) {
    const o = document.createElement("option");
    o.value = m; o.textContent = m; if (m === current) o.selected = true;
    sel.appendChild(o);
  }
}

function applyStatus(st) {
  $("backend-badge").textContent = st.backend || "—";
  setRecording(st.state === "recording");
  fillMics(st.microphones, st.currentMic);
  ensureScopes(st.tracks && st.tracks.length ? st.tracks : ["mic", "system"]);
}

function applyTelemetry(t) {
  $("timer").textContent = fmt(t.seconds);
  if (t.seconds > maxSeconds) maxSeconds = t.seconds;
  $("total").textContent = "total: " + fmt(maxSeconds);
  ensureScopes(Object.keys(t.tracks || {}));
  for (const [name, d] of Object.entries(t.tracks || {})) {
    if (scopes[name]) scopes[name].push(d.env || [], d.level || 0, d.peak || 0);
  }
}

// ---- websocket -----------------------------------------------------------
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => { $("conn-dot").classList.add("ok"); $("conn-label").textContent = "connected"; };
  ws.onclose = () => {
    $("conn-dot").classList.remove("ok"); $("conn-label").textContent = "reconnecting…";
    setTimeout(connect, 1000);
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "status") applyStatus(msg.data);
    else if (msg.type === "telemetry") applyTelemetry(msg.data);
  };
}
connect();

// ---- controls ------------------------------------------------------------
$("start-stop").addEventListener("click", async () => {
  if (recording) {
    await api("/api/stop", {});
    toast("Stopped");
  } else {
    maxSeconds = 0;
    const st = await api("/api/start", {
      recordMic: $("t-mic").checked, recordSystem: $("t-sys").checked,
    });
    applyStatus(st);
    toast("Recording…");
  }
});

$("mic-select").addEventListener("change", async (e) => {
  if (!recording) return;
  const st = await api("/api/swap-mic", { target: e.target.value });
  toast("Swapped mic → " + (st.currentMic || ""));
});

// ---- exit / save ---------------------------------------------------------
$("exit-btn").addEventListener("click", () => {
  $("save-name").value = "";
  $("modal").hidden = false;
  $("save-name").focus();
});
$("cancel-exit").addEventListener("click", () => ($("modal").hidden = true));
$("confirm-exit").addEventListener("click", async () => {
  const name = $("save-name").value.trim();
  const r = await api("/api/exit", { name });
  $("modal").hidden = true;
  document.body.innerHTML =
    `<div style="display:grid;place-items:center;height:100vh;text-align:center;font-family:system-ui">
       <div><h1>Saved ✓</h1><p style="opacity:.7">${r.saved || "(nothing was recording)"}</p>
       <p style="opacity:.5">You can close this tab.</p></div></div>`;
});

// keyboard: q = exit, s = focus mic swap
addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") return;
  if (e.key === "q") $("exit-btn").click();
});
