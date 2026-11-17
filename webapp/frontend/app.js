/* Drone Watchtower — live detection frontend
 * Aircraft-centric view: one entry per drone, merging RemoteID (beacon/BLE)
 * and DroneID (OcuSync) sessions by concurrent activity.
 */
const $ = (id) => document.getElementById(id);

/* esc() and kvHtml() live in escape.js. Broadcast strings are escaped
 * there before they enter HTML or an attribute. */

let map, stationMarker, activityRing, dotLayer;
let station = null;
let statusMode = null;
let scanArmed = true;
let gpsCentered = false;
let mapSeeded = false;
let selectedKey = null;
const sessions = new Map(); // "rid:<serial>" | "djiid:<hash>" -> session
const adsb = new Map(); // ICAO hex -> latest 1090 MHz track
let adsbLayer = null;
let adsbFitted = false;
let elrs = null;
let tbs = null;
let vtxLinks = [];
let mavlink = [];
let adsbError = null;

/* Event times are stored and served in UTC. The dashboard shows them in
 * the viewer's own timezone, so a browser set to UTC+8 sees UTC+8 — the
 * same rule as any other application on that machine. */
const LOCAL_TIME = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit", minute: "2-digit", second: "2-digit",
  hour12: false,
});
const LOCAL_DATE = new Intl.DateTimeFormat(undefined, {
  year: "numeric", month: "2-digit", day: "2-digit",
});
const LOCAL_FULL = new Intl.DateTimeFormat(undefined, {
  year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit",
  hour12: false,
});

function toDate(iso) {
  if (!iso) return null;
  const text = String(iso);
  const d = Date.parse(text.endsWith("+00:00") ? text.replace("+00:00", "Z") : text);
  return Number.isFinite(d) ? new Date(d) : null;
}

// "14:22:07" — time of day, local.
function fmtTime(iso) {
  const d = toDate(iso);
  return d ? LOCAL_TIME.format(d) : "--";
}

// "2026-11-14 14:22:07" — date and time, local.
function fmtDateTime(iso) {
  const d = toDate(iso);
  return d ? LOCAL_FULL.format(d).replace(",", "") : "--";
}

// "2026-11-14" — date only, local.
function fmtDate(iso) {
  const d = toDate(iso);
  return d ? LOCAL_DATE.format(d) : "?";
}

// The zone the viewer's browser is in, e.g. "GMT+8". Shown next to times
// so nobody has to guess which zone a reading is in.
function fmtFixed(value, digits) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(digits) : "—";
}

function localZoneLabel() {
  try {
    const off = -new Date().getTimezoneOffset() / 60;
    const sign = off >= 0 ? "+" : "-";
    const abs = Math.abs(off);
    const hh = Math.floor(abs);
    const mm = Math.round((abs - hh) * 60);
    return "UTC" + sign + hh + (mm ? ":" + String(mm).padStart(2, "0") : "");
  } catch (e) {
    return "local";
  }
}
function tsec(iso) {
  return Date.parse(iso.endsWith("+00:00") ? iso.replace("+00:00", "Z") : iso) || 0;
}

/* API token.
 *
 * The dashboard is on the LAN, so the station can require a token. The
 * token lives in localStorage and goes out as X-Watchtower-Token on every
 * call; the cookie the server sets is what the websocket uses.
 */
const TOKEN_KEY = "watchtower_token";

function apiToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch (e) {
    return "";
  }
}

function setApiToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch (e) {
    /* private mode: the cookie still works for this session */
  }
}

// Ask once per page load: the status poll runs every second, so a
// prompt that reappears on every failed request turns a dismissed
// dialog into a modal loop.
let tokenPrompted = false;
// Set when the server rejects us for a missing token: the poll and the
// websocket then stay quiet until the operator enters it.
let authBlocked = false;

/* fetch wrapper that attaches the token and handles a 401 by asking for
 * the token once. */
async function apiFetch(url, options = {}) {
  const opts = { credentials: "same-origin", ...options };
  const token = apiToken();
  if (token) opts.headers = { ...(opts.headers || {}), "X-Watchtower-Token": token };
  const res = await fetch(url, opts);
  if (res.status === 401) {
    authBlocked = true;
    const ok = await promptForToken();
    if (ok) {
      authBlocked = false;
      tokenPrompted = false;
      opts.headers = { ...(opts.headers || {}), "X-Watchtower-Token": apiToken() };
      const retry = await fetch(url, opts);
      if (retry.ok) {
        // The websocket was closed for want of a token: open it now.
        connect();
      }
      return retry;
    }
  }
  return res;
}



/* Ask for the token with an inline form.
 *
 * window.prompt() would block the whole page and reappear on every
 * failed request; a form that stays on the page does not.
 */
function promptForToken() {
  if (tokenPrompted) return Promise.resolve(false);
  tokenPrompted = true;
  return new Promise((resolve) => {
    const box = document.getElementById("token-box");
    const input = document.getElementById("token-input");
    const err = document.getElementById("token-error");
    if (!box || !input) {
      resolve(false);
      return;
    }
    box.classList.remove("hidden");
    input.value = apiToken();
    input.focus();
    err.textContent = "";

    const submit = async () => {
      const token = input.value.trim();
      if (!token) {
        err.textContent = "Enter the token.";
        return;
      }
      err.textContent = "Checking…";
      try {
        const r = await fetch("/api/auth/login", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token }),
        });
        if (!r.ok) {
          err.textContent = "That token was not accepted.";
          return;
        }
        setApiToken(token);
        box.classList.add("hidden");
        tokenPrompted = false;
        authBlocked = false;
        resolve(true);
        // The websocket was closed for want of a token: open it now, and
        // pick up any code that arrived since this page was loaded.
        connect();
        pullStatus();
      } catch (e) {
        err.textContent = "Could not reach the station.";
      }
    };

    const form = document.getElementById("token-form");
    form.onsubmit = (ev) => {
      ev.preventDefault();
      submit();
    };
    const cancel = document.getElementById("token-cancel");
    cancel.onclick = () => {
      box.classList.add("hidden");
      tokenPrompted = false;
      resolve(false);
    };
  });
}

function connect() {
  // The token rides in the cookie the login set, not in the URL: a URL
  // is written to the server's access log, and the access log is not
  // the place for a credential.
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "status") setStatus(msg.status);
    if (msg.type === "detection") onDetection(msg.event);
    if (msg.type === "remoteid") onRemoteId(msg.event);
    if (msg.type === "adsb") onAdsb(msg.aircraft || []);
    if (msg.type === "elrs") onElrs(msg.link || null);
    if (msg.type === "tbs") onTbs(msg.link || null);
    if (msg.type === "vtx") onVtx(msg.links || []);
    if (msg.type === "mavlink") onMavlink(msg.vehicles || []);
    if (msg.type === "vtx_frame") onVtxFrame(msg);
    if (msg.type === "vtx_status") onVtxStatus(msg.text);
    if (msg.type === "vtx_frame_stop") hideVtxPanel();
  };
  ws.onclose = (ev) => {
    // 4401 is our "no token" close code. Reconnecting every 1.5s just
    // produces a storm of rejected handshakes, so wait for the operator
    // to enter the token instead.
    if (ev && ev.code === 4401) {
      authBlocked = true;
      setStatus({});
      return;
    }
    setTimeout(connect, 1500);
  };
}

function setStatus(st) {
  // self-update: reload this tab when a newer build is deployed
  if (st.build) {
    const prev = localStorage.getItem("wt-build");
    if (prev && prev !== st.build) {
      localStorage.setItem("wt-build", st.build);
      location.reload();
      return;
    }
    localStorage.setItem("wt-build", st.build);
  }
  statusMode = st.mode || null;
  scanArmed = st.armed !== false;
  adsbError = st.adsb_error || null;
  station = st.station;
  const btn = $("btn-scan");
  btn.textContent = scanArmed ? "Stop scan" : "Start scan";
  btn.className = scanArmed ? "scanning" : "ready";
  setDongles(st.dongles);
  setGps(st.gps);
  setInternet(st.internet);
  setReadyBanner(st);
  setScanBar(st);
  setFr24Status(st.adsb_provider);
  if (map && station) placeStation();
}

function setFr24Status(provider) {
  const el = $("fr24-status");
  if (!el) return;
  if (provider === "flightradar24") {
    el.textContent = "using FlightRadar24";
    el.title = "Network ADS-B from your FR24 API subscription";
  } else {
    el.textContent = "using adsb.lol";
    el.title = "No FR24 key — public adsb.lol / adsb.fi feeds";
  }
}

async function loadFr24Config() {
  try {
    const cfg = await apiFetch("/api/config/adsb", { cache: "no-store" }).then((r) => r.json());
    const input = $("fr24-key");
    if (input && cfg.fr24_configured && !input.dataset.dirty) {
      input.placeholder = cfg.fr24_token_hint || "FR24 key saved";
      input.value = "";
    }
    setFr24Status(cfg.provider);
  } catch (_) { /* offline */ }
}

async function saveFr24Key() {
  const input = $("fr24-key");
  const btn = $("fr24-save");
  const status = $("fr24-status");
  if (!input) return;
  const token = input.value.trim();
  if (btn) btn.disabled = true;
  if (status) status.textContent = "saving…";
  try {
    const cfg = await apiFetch("/api/config/adsb", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fr24_api_token: token }),
    }).then((r) => r.json());
    input.value = "";
    input.dataset.dirty = "";
    input.placeholder = cfg.fr24_configured
      ? (cfg.fr24_token_hint || "FR24 key saved")
      : "optional key — else adsb.lol";
    setFr24Status(cfg.provider);
  } catch (_) {
    if (status) status.textContent = "save failed";
  } finally {
    if (btn) btn.disabled = false;
  }
}

function setScanBar(st) {
  const armed = st.armed !== false;
  const dongles = {};
  for (const d of st.dongles || []) dongles[d.id] = d;
  const oneAtt = !!(dongles.one && dongles.one.attached);
  const proAtt = !!(dongles.pro && dongles.pro.attached);
  const c5Att = !!(dongles.c5 && dongles.c5.attached);
  const c5Live = !!(dongles.c5 && dongles.c5.live);
  const hop = armed && oneAtt ? (st.phase_hop || null) : null;
  const droneidOn = !!(armed && proAtt && st.phase_droneid);

  const rfLive = $("rf-live");
  const rfActive = $("rf-active");
  if (rfLive && rfActive) {
    const scanning = !!(armed && oneAtt && hop);
    rfLive.classList.toggle("on", scanning);
    rfLive.classList.toggle("off", !scanning);
    if (!oneAtt) rfActive.textContent = "HackRF One offline";
    else if (!armed) rfActive.textContent = "scan stopped";
    else {
      const age = st.phase_hop_s != null ? ` · ${st.phase_hop_s}s` : "";
      rfActive.textContent = hop
        ? `HackRF One · ${hop}${age}`
        : "HackRF One · hopping…";
    }
  }
  const steps = $("rf-steps");
  if (steps) {
    steps.querySelectorAll("[data-hop]").forEach((el) => {
      el.classList.toggle("on", el.getAttribute("data-hop") === hop);
    });
  }

  const pro = $("scan-pro");
  const proProto = $("scan-pro-proto");
  if (pro && proProto) {
    pro.classList.toggle("active", droneidOn);
    pro.classList.toggle("idle", !proAtt || !armed);
    proProto.textContent = !proAtt ? "offline" : !armed ? "idle" : (droneidOn ? "DroneID" : "DroneID");
  }

  const c5 = $("scan-c5");
  const c5Proto = $("scan-c5-proto");
  if (c5 && c5Proto) {
    c5.classList.toggle("active", c5Att && c5Live);
    c5.classList.toggle("idle", !c5Att);
    c5Proto.textContent = !c5Att ? "offline" : c5Live ? "RemoteID" : "waiting";
  }
  setC5Bands(st.c5_bands);

  const hopLabel = $("dongle-one") && $("dongle-one").querySelector(".dongle-hop-label");
  if (hopLabel) hopLabel.textContent = hop || "One";
  const dongleOne = $("dongle-one");
  if (dongleOne) {
    dongleOne.title = hop
      ? `HackRF One · scanning ${hop}`
      : "HackRF One · ELRS / TBS / MAVLink / VTX hop";
  }
}

function setC5Bands(bands) {
  const map = [
    ["c5-dot-24", "wifi24"],
    ["c5-dot-5", "wifi5"],
    ["c5-dot-ble", "ble"],
  ];
  for (const [id, key] of map) {
    const el = $(id);
    if (!el) continue;
    const state = bands && bands[key];
    const shown = state === "ok" || state === "down" || state === "away" || state === "off"
      ? state : "offline";
    el.classList.remove("ok", "down", "offline");
    el.classList.add(shown);
    const detail = bands && bands.detail && bands.detail[key];
    if (detail) el.title = detail;
  }
}

async function pullStatus() {
  if (authBlocked) return;
  try {
    const st = await apiFetch("/api/status", { cache: "no-store" }).then((r) => r.json());
    setStatus(st);
  } catch (_) { /* offline */ }
}

function setReadyBanner(st) {
  const el = $("ready-banner");
  if (!el) return;
  const problems = st.problems || [];
  if (st.ready === false && problems.length) {
    el.textContent = "Not ready — " + problems.join("; ");
    el.classList.remove("hidden");
  } else {
    el.textContent = "";
    el.classList.add("hidden");
  }
}

function setInternet(up) {
  const el = $("dongle-net");
  if (!el || up == null) return;
  el.classList.toggle("on", !!up);
  el.classList.toggle("off", !up);
  el.title = up ? "Internet · connected" : "Internet · no connection";
}

function setGps(gps) {
  const el = $("dongle-gps");
  if (!el || !gps) return;
  const count = el.querySelector(".dongle-count");
  const locked = gps.sats || 0;
  const seen = gps.seen || 0;
  if (el.classList.contains("off")) {
    if (count) count.textContent = "GPS";
    return;
  }
  if (count) count.textContent = locked ? "GPS " + locked : "GPS";
  el.title = gps.fix
    ? `u-blox 7 · ${locked} locked · ${Number(gps.lat).toFixed(5)}, ${Number(gps.lon).toFixed(5)}`
    : `u-blox 7 · attached · ${locked} locked · ${seen} in view`;
}

function setDongles(list) {
  for (const d of list || []) {
    const el = $("dongle-" + d.id);
    if (!el) continue;
    const live = d.live !== false;
    el.classList.toggle("on", !!d.attached && live);
    el.classList.toggle("wait", !!d.attached && !live);
    el.classList.toggle("off", !d.attached);
    if (d.id === "gps") {
      if (!d.attached) el.title = "u-blox 7 · not attached";
    } else if (d.id === "rtl" && d.attached && !live) {
      el.title = adsbError
        ? "RTL-SDR · attached · decoder error: " + adsbError
        : "RTL-SDR · attached · no 1090 decoder running (see README)";
    } else if (!d.attached) {
      el.title = d.role + " · not attached";
    } else {
      el.title = d.role + (live ? " · signal" : " · attached · no signal");
    }
  }
}

function sess(key, mode) {
  if (!sessions.has(key)) {
    sessions.set(key, {
      key, mode, source: key.startsWith("rid:") ? "remoteid" : "droneid",
      count: 0, infp: 0, cryp: 0, lastTime: null, bestRssi: -999,
      channels: new Map(), packets: [], trail: [],
      pos: null, opLat: null, opLon: null, home: null, pclass: null,
    });
  }
  const s = sessions.get(key);
  if (s.mode !== mode && mode) s.mode = mode;
  return s;
}

function hasFix(lat, lon) {
  return Number.isFinite(Number(lat)) && Number.isFinite(Number(lon)) && !(Number(lat) === 0 && Number(lon) === 0);
}

function onRemoteId(evt) {
  const mode = evt.mode || "live";
  const s = sess("rid:" + evt.session, mode);
  s.count++;
  s.lastTime = evt.time;
  s.model = evt.model || s.model;
  s.selfid = evt.selfid;
  s.operator = evt.operator || s.operator;
  s.opLat = evt.op_lat; s.opLon = evt.op_lon;
  if (hasFix(evt.lat, evt.lon)) {
    s.pos = { lat: evt.lat, lon: evt.lon, alt: evt.alt, speed: evt.speed, status: evt.status };
    s.trail.push([evt.lat, evt.lon]);
    if (s.trail.length > 300) s.trail.shift();
  }
  s.packets.unshift({ time: evt.time, freq: evt.freq || 2437, marker: "RemoteID", rssi: evt.rssi || 0 });
  s.packets = s.packets.slice(0, 50);
  // Keep history sidebar/table alone — live RID flood used to wipe protocol filters.
  if (view !== "live") { renderBanner(); return; }
  renderAll();
  renderMap();
  if (selectedKey === s.key) renderInfo();
}

function onDetection(evt) {
  if (!evt.crc) return;
  const mode = evt.mode || "live";
  const s = sess("djiid:" + evt.session, mode);
  s.count++;
  if (evt.marker === "CRYP") s.cryp++;
  else if (evt.marker === "ENCR") s.encr = (s.encr || 0) + 1;
  else s.infp++;
  s.lastTime = evt.time;
  if (evt.rssi > s.bestRssi) s.bestRssi = evt.rssi;
  s.channels.set(evt.freq, (s.channels.get(evt.freq) || 0) + 1);
  if (evt.pclass) s.pclass = evt.pclass;
  // classic O2/O3 carries plaintext telemetry in the clear
  if (hasFix(evt.lat, evt.lon)) {
    s.pos = { lat: evt.lat, lon: evt.lon, alt: evt.alt, yaw: evt.yaw };
    s.trail.push([evt.lat, evt.lon]);
    if (s.trail.length > 300) s.trail.shift();
  }
  if (hasFix(evt.op_lat, evt.op_lon)) { s.opLat = evt.op_lat; s.opLon = evt.op_lon; }
  if (evt.home_lat != null && evt.home_lon != null) s.home = { lat: evt.home_lat, lon: evt.home_lon };
  s.packets.unshift({ time: evt.time, freq: evt.freq, marker: evt.marker, rssi: evt.rssi });
  s.packets = s.packets.slice(0, 50);
  if (view !== "live") { renderBanner(); return; }
  renderAll();
  renderMap();
  if (selectedKey === s.key) renderInfo();
}

/* ---- aircraft grouping: merge rid + djiid by serial (Task 2.1 fusion) ---- */
function aircraftSerial(s) {
  if (!s) return "";
  // rid:SERIAL or djiid:SERIAL (O2 plaintext uses the real serial as session)
  if (s.source === "remoteid") return s.key.slice(4);
  return s.key.startsWith("djiid:") ? s.key.slice(6) : s.key;
}

function aircraftList() {
  const rids = [], djiids = [];
  for (const s of sessions.values()) (s.source === "remoteid" ? rids : djiids).push(s);
  const list = [];
  const used = new Set();
  for (const rid of rids) {
    const entry = { rid, djiids: [], lastTime: rid.lastTime };
    const ridSerial = aircraftSerial(rid);
    for (const dj of djiids) {
      if (used.has(dj.key)) continue;
      const sameSerial = ridSerial && aircraftSerial(dj) === ridSerial;
      const closeInTime = Math.abs(tsec(dj.lastTime) - tsec(rid.lastTime)) < 180000;
      // Prefer exact serial match; fall back to time+model for legacy O4 hash sessions
      if (sameSerial || (rid.model && closeInTime && dj.mode === rid.mode)) {
        entry.djiids.push(dj);
        used.add(dj.key);
      }
    }
    entry.last = Math.max(tsec(rid.lastTime), ...entry.djiids.map((d) => tsec(d.lastTime)));
    list.push(entry);
  }
  for (const dj of djiids) {
    if (!used.has(dj.key)) list.push({ rid: null, djiids: [dj], last: tsec(dj.lastTime) });
  }
  list.sort((a, b) => b.last - a.last);
  return list;
}

function entryId(e) {
  return e.rid ? "rid:" + e.rid.key.slice(4) : e.djiids[0].key;
}

/* ---- rendering ---- */
const RID_STALE_MS = 12000;
const DJI_STALE_MS = 20000;

function dropSession(s) {
  if (s.trailLine) s.trailLine.remove();
  if (s.droneDot) s.droneDot.remove();
  if (s.opDot) s.opDot.remove();
}

function hideInfo() {
  $("info-body").classList.add("hidden");
  $("info-empty").classList.remove("hidden");
}

function expireLive() {
  const now = Date.now();
  let changed = false;
  for (const [k, s] of [...sessions]) {
    const age = s.lastTime ? now - tsec(s.lastTime) : Infinity;
    const limit = s.source === "remoteid" ? RID_STALE_MS : DJI_STALE_MS;
    if (age > limit) {
      dropSession(s);
      sessions.delete(k);
      if (selectedKey === k) selectedKey = null;
      changed = true;
    }
  }
  if (!changed || view !== "live") return;
  renderAll();
  renderMap();
  if (!selectedKey) hideInfo();
}

function renderAll() {
  // History owns #alert-list via renderHistoryList; never paint live rows over it.
  if (view === "live") renderSidebar();
  renderBanner();
}

function renderSidebar() {
  if (view !== "live") return;
  _renderLiveSidebar();
}

function renderBanner() {
  const banner = $("banner");
  const list = aircraftList();
  const fresh = list.some((e) => e.last && Date.now() - e.last < 15000 && e.last > 0);
  banner.textContent = "Drone detected — identification active";
  banner.classList.toggle("hidden", !fresh);

  const heliBanner = $("heli-banner");
  if (heliBanner) {
    const helis = [...adsb.values()].filter((a) => a.helicopter);
    if (helis.length) {
      const names = helis.map((a) => a.flight || a.type || a.hex).slice(0, 3).join(", ");
      heliBanner.textContent = helis.length === 1
        ? `Helicopter alert — ${names}`
        : `Helicopter alert — ${helis.length} tracks (${names})`;
      heliBanner.classList.remove("hidden");
    } else {
      heliBanner.classList.add("hidden");
    }
  }
}

function onAdsb(list) {
  adsb.clear();
  for (const a of list) {
    if (!a.hex || a.lat == null || a.lon == null) continue;
    adsb.set(String(a.hex).toUpperCase(), a);
  }
  if (selectedKey && selectedKey.startsWith("adsb:") && !adsb.has(selectedKey.slice(5))) {
    selectedKey = null;
  }
  if (view === "live") {
    renderAll();
    renderAdsb();
    if (selectedKey && selectedKey.startsWith("adsb:")) renderAdsbInfo();
  }
}

function onElrs(link) {
  elrs = link;
  if (view === "live") {
    renderAll();
    if (selectedKey === "elrs:24") renderElrsInfo();
  }
}

function onTbs(link) {
  tbs = link;
  if (view === "live") {
    renderAll();
    if (selectedKey === "tbs") renderHopInfo(tbs, "TBS");
  }
}

function onVtx(links) {
  vtxLinks = links;
  if (view === "live") {
    renderAll();
    if (selectedKey && selectedKey.startsWith("vtx:")) {
      const l = vtxLinks.find((v) => "vtx:" + (v.channel || v.freq) === selectedKey);
      if (l) renderVtxInfo(l);
    }
  }
}

function onMavlink(list) {
  mavlink = list || [];
  if (view === "live") {
    renderAll();
    renderMap();
    if (selectedKey && selectedKey.startsWith("mav:")) renderMavInfo();
  }
}

function vtxKey(l) { return "vtx:" + (l.channel || l.freq); }

let vtxFeedFreq = null;
let videoOpen = true;

function onVtxFrame(msg) {
  if (!videoOpen) return;
  const panel = $("vtx-panel");
  panel.classList.remove("hidden");
  $("vtx-video").src = "data:image/jpeg;base64," + msg.jpeg;
  $("vtx-video").classList.remove("hidden");
  $("vtx-status").classList.add("hidden");
  $("vtx-panel-label").textContent = msg.no_signal
    ? "FPV receiver · no signal"
    : "FPV receiver · live picture";
}

function onVtxStatus(text) {
  const s = $("vtx-status");
  s.textContent = text;
  s.classList.remove("hidden");
  $("vtx-video").classList.add("hidden");
}

function hideVtxPanel() {
  $("vtx-panel").classList.add("hidden");
  vtxFeedFreq = null;
}

function renderVtxRow(el, l) {
  if (view !== "live") return;
  const key = vtxKey(l);
  const div = document.createElement("div");
  div.className = "alert" + (selectedKey === key ? " selected" : "");
  div.innerHTML = `
    <div class="row1"><span class="model">FPV camera</span><span class="tag tag-vtx">${esc(l.channel || "5.8G")}</span></div>
    <code>analog video</code>
    <div class="meta">${esc(l.freq)} MHz · peak ${esc(l.peak_db)} dB · ${esc(l.width_mhz)} MHz wide</div>`;
  div.onclick = () => { selectedKey = key; renderSidebar(); renderVtxInfo(l); };
  el.appendChild(div);
}

function renderVtxInfo(l) {
  $("info-empty").classList.add("hidden");
  $("info-body").classList.remove("hidden");
  $("i-model").textContent = "FPV camera (analog VTX)";
  $("i-marker").textContent = l.channel || "5.8 GHz";
  const kv = [
    ["Link", "analog 5.8 GHz video"],
    ["Channel", l.channel || "custom frequency"],
    ["Frequency", `${l.freq} MHz`],
    ["Peak", `${l.peak_db} dB above meter`],
    ["Carrier width", `${l.width_mhz} MHz`],
    ["Noise floor", `${l.floor_db} dB`],
    ["Position", "not in this signal"],
  ];
  $("info-kv").innerHTML = kvHtml(kv);
  $("pkt-body").innerHTML = "";
}

function renderHopRow(el, link, key, title) {
  if (!link || !link.present || view !== "live") return;
  const div = document.createElement("div");
  div.className = "alert" + (selectedKey === key ? " selected" : "");
  let code, meta;
  if (link.modulation) {
    code = "control link";
    meta = `${link.modulation} · ${link.rate} · ${link.packets} pkts · SNR ${link.snr_db} dB`;
  } else {
    code = "hop";
    meta = `${link.bursts} channels · peak ${link.peak_db} dB · ${link.span_mhz} MHz span`;
  }
  div.innerHTML = `
    <div class="row1"><span class="model">${esc(title)}</span><span class="tag tag-elrs">${esc(link.band)}</span></div>
    <code>${esc(code)}</code>
    <div class="meta">${esc(meta)}</div>`;
  div.onclick = () => { selectedKey = key; renderSidebar(); renderHopInfo(link, title); };
  el.appendChild(div);
}

function mavKey(v) { return "mav:" + v.sysid; }

function renderMavRow(el, v) {
  if (!v || !v.present || view !== "live") return;
  const key = mavKey(v);
  const div = document.createElement("div");
  div.className = "alert" + (selectedKey === key ? " selected" : "");
  const where = (v.lat != null && v.lon != null)
    ? `${Number(v.lat).toFixed(5)}, ${Number(v.lon).toFixed(5)}`
    : "no GPS yet";
  const meta = [
    v.mode,
    v.armed ? "armed" : "disarmed",
    v.alt_m != null ? `${v.alt_m} m` : null,
    v.battery_pct != null ? `${v.battery_pct}%` : (v.voltage != null ? `${v.voltage} V` : null),
    where,
  ].filter(Boolean).join(" · ");
  div.innerHTML = `
    <div class="row1"><span class="model">${esc(v.type_name || "MAVLink")}</span><span class="tag tag-mav">MAVLink</span></div>
    <code>sys ${esc(v.sysid)} · ${esc(v.autopilot_name || "autopilot")}</code>
    <div class="meta">${esc(meta)}</div>`;
  div.onclick = () => { selectedKey = key; renderSidebar(); renderMavInfo(); renderMap(); };
  el.appendChild(div);
}

function renderMavInfo() {
  const v = mavlink.find((x) => mavKey(x) === selectedKey);
  if (!v) return;
  $("info-empty").classList.add("hidden");
  $("info-body").classList.remove("hidden");
  $("i-model").textContent = v.type_name || "MAVLink vehicle";
  $("i-marker").textContent = "MAVLink";
  const kv = [
    ["Link", "MAVLink telemetry (passive)"],
    ["Autopilot", v.autopilot_name || "—"],
    ["System ID", v.sysid, "mono"],
    ["Mode", v.mode || "—"],
    ["Armed", v.armed ? "yes" : "no"],
    ["Position", (v.lat != null) ? `${Number(v.lat).toFixed(6)}, ${Number(v.lon).toFixed(6)}` : "—"],
    ["Altitude", v.alt_m != null ? `${v.alt_m} m` : "—"],
    ["Heading / speed", [v.heading != null ? `${v.heading}°` : null, v.gs_ms != null ? `${v.gs_ms} m/s` : null].filter(Boolean).join(" · ") || "—"],
    ["Attitude", (v.roll != null) ? `R ${v.roll}°  P ${v.pitch}°  Y ${v.yaw}°` : "—"],
    ["Battery", [v.voltage != null ? `${v.voltage} V` : null, v.battery_pct != null ? `${v.battery_pct}%` : null].filter(Boolean).join(" · ") || "—"],
    ["GPS", [v.fix != null ? `fix ${v.fix}` : null, v.sats != null ? `${v.sats} sats` : null].filter(Boolean).join(" · ") || "—"],
    ["Via", v.via || "—"],
    ["Frequency", v.freq != null ? `${v.freq} MHz` : "—"],
    ["SiK netid", v.sik_netid != null ? `0x${Number(v.sik_netid).toString(16).toUpperCase().padStart(4, "0")}` : "—"],
    ["Air rate", v.baud != null ? `${v.baud} baud` : "—"],
    ["Packets", v.packets || 0],
    ["Status text", v.statustext || "—"],
  ];
  $("info-kv").innerHTML = kvHtml(kv);
  $("pkt-body").innerHTML = "";
}

function renderHopInfo(link, title) {
  if (!link || !link.present) return;
  $("info-empty").classList.add("hidden");
  $("info-body").classList.remove("hidden");
  $("i-model").textContent = title;
  $("i-marker").textContent = link.band;
  const kv = link.modulation ? [
    ["Link", "ExpressLRS"],
    ["Modulation", link.modulation],
    ["Rate", link.rate],
    ["Packets", link.packets],
    ["Uplink SNR", `${link.snr_db} dB`],
    ["Downlink pkts", link.downlink != null ? link.downlink : 0],
    ["Position", "not in this link"],
  ] : [
    ["Link", "control hop"],
    ["Band", link.band],
    ["Channels lit", link.bursts],
    ["Span", `${link.span_mhz} MHz`],
    ["Peak", `${link.peak_db} dB`],
    ["Floor", `${link.floor_db} dB`],
    ["Position", "not in this link"],
  ];
  $("info-kv").innerHTML = kvHtml(kv);
  $("pkt-body").innerHTML = "";
}

function renderElrsInfo() {
  renderHopInfo(elrs, "ELRS");
}

function helicopterTracks() {
  return [...adsb.values()]
    .filter((a) => a.helicopter)
    .sort((a, b) => (a.distance_nm || 999) - (b.distance_nm || 999));
}

function renderHeliList() {
  const el = $("heli-list");
  const count = $("heli-count");
  if (!el) return;
  const helis = view === "live" ? helicopterTracks() : [];
  if (count) count.textContent = helis.length;
  el.innerHTML = "";
  if (!helis.length) {
    el.innerHTML = '<div class="empty-note">No helicopters</div>';
    return;
  }
  for (const a of helis) {
    const id = "adsb:" + a.hex;
    const div = document.createElement("div");
    div.className = "alert heli-alert" + (id === selectedKey ? " selected" : "");
    const name = a.flight || a.type || a.hex;
    const meta = [
      a.alt_ft != null ? `${Math.round(a.alt_ft)} ft` : null,
      a.gs_kt != null ? `${Math.round(a.gs_kt)} kt` : null,
      a.distance_nm != null ? `${Number(a.distance_nm).toFixed(1)} nm` : null,
      (a.helicopter_flags || []).join(" · ") || null,
    ].filter(Boolean).join(" · ");
    div.innerHTML = `
      <div class="row1"><span class="model">${esc(name)}</span><span class="tag tag-heli">HELI</span></div>
      <code>${esc(a.hex)}</code>
      <div class="meta">${esc(meta || "Helicopter · ADS-B")}</div>`;
    div.onclick = () => {
      selectedKey = id;
      renderSidebar();
      renderAdsbInfo();
      renderAdsb();
    };
    el.appendChild(div);
  }
}

function _renderLiveSidebar() {
  const list = aircraftList();
  const hopCount = (elrs && elrs.present ? 1 : 0) + (tbs && tbs.present ? 1 : 0) + vtxLinks.length + mavlink.length;
  $("session-count").textContent = list.length + hopCount;
  const el = $("alert-list");
  el.innerHTML = "";
  renderHopRow(el, elrs, "elrs:24", "ELRS");
  renderHopRow(el, tbs, "tbs", "TBS");
  for (const v of mavlink) renderMavRow(el, v);
  for (const l of vtxLinks) renderVtxRow(el, l);
  if (!list.length && !hopCount) {
    const note = document.createElement("div");
    note.className = "empty-note";
    note.textContent = "No drones";
    el.appendChild(note);
  }
  for (const e of list) {
    const id = entryId(e);
    const rid = e.rid, dj = e.djiids[0];
    const div = document.createElement("div");
    div.className = "alert" + (id === selectedKey ? " selected" : "");
    let badges = "";
    if (rid) badges += '<span class="tag tag-rid">RemoteID</span> ';
    if (dj) badges += '<span class="tag">DroneID</span>';
    const ident = rid ? rid.key.slice(4) : dj.key.slice(6);
    const model = rid ? (rid.model || "Drone") : "DroneID";
    const metaParts = [];
    if (rid) metaParts.push(`RID ${rid.count} fixes`);
    if (dj) {
      const encr = dj.encr || 0;
      metaParts.push(encr
        ? `DroneID ${dj.count} pkts (${encr} ENCR presence)`
        : `DroneID ${dj.count} pkts (${dj.infp} INFP / ${dj.cryp} CRYP)`);
    }
    if (dj && dj.pos) metaParts.push("O2 plaintext position");
    const lastIso = rid && (!dj || tsec(rid.lastTime) > tsec(dj.lastTime)) ? rid.lastTime : dj.lastTime;
    metaParts.push(`last ${fmtTime(lastIso)}`);
    div.innerHTML = `
      <div class="row1"><span class="model">${esc(model)}</span><span>${badges}</span></div>
      <code>${esc(ident)}</code>
      <div class="meta">${esc(metaParts.join(" · "))}</div>`;
    div.onclick = () => { selectedKey = id; renderSidebar(); renderInfo(); };
    el.appendChild(div);
  }
  renderHeliList();
}

function adsbHoverHtml(a) {
  const name = a.flight || (a.type && a.type !== "Unknown" ? a.type : a.hex);
  const src = a.source === "net" ? `NET · ${a.feed || "adsb"}` : "1090 MHz";
  const kind = a.helicopter ? "HELICOPTER" : (a.uas_candidate ? "UAS?" : "ADS-B");
  const lines = [
    `<b>${esc(name)}</b> · ${esc(kind)} · ${esc(src)}`,
    `ICAO ${esc(a.hex)}`,
    [
      a.alt_ft != null ? `${Math.round(a.alt_ft)} ft` : null,
      a.gs_kt != null ? `${Math.round(a.gs_kt)} kt` : null,
      a.track != null ? `${Math.round(a.track)}°` : null,
      a.distance_nm != null ? `${Number(a.distance_nm).toFixed(1)} nm` : null,
    ].filter(Boolean).join(" · "),
    a.type && a.type !== "Unknown" ? esc(a.type) : null,
    a.squawk ? `Squawk ${esc(a.squawk)}` : null,
    a.helicopter && a.helicopter_flags ? esc(a.helicopter_flags.join(" · ")) : null,
    a.uas_candidate && a.uas_flags ? `UAS? ${esc(a.uas_flags.join(" · "))}` : null,
  ].filter(Boolean);
  return `<div class="adsb-tip">${lines.join("<br>")}</div>`;
}

/* A track is either what this box received over the air ("1090") or what
 * a public aggregator heard ("net"). The badge keeps the two apart. */
function adsbBadges(a) {
  let html = "";
  if (a.helicopter) {
    html += '<span class="tag tag-heli" title="Helicopter / rotorcraft">HELI</span> ';
  }
  if (a.source === "net") {
    html += `<span class="tag tag-net" title="Heard by the ${esc(a.feed || "network")} aggregator, not by this station">NET</span> `;
  } else {
    html += '<span class="tag tag-adsb">1090</span> ';
  }
  if (a.uas_candidate) {
    html += '<span class="tag tag-uas" title="Small-UAS characteristics">UAS?</span>';
  }
  return html;
}

function planeIcon(track, selected, isNet, isHeli) {
  const rot = Number(track) || 0;
  const fill = selected ? "#58a6ff" : (isHeli ? "#ffa657" : (isNet ? "#d29922" : "#79c0ff"));
  if (isHeli) {
    // Top-down helicopter: rotor disc + fuselage + tail boom (not the airplane glyph).
    return L.divIcon({
      className: "adsb-icon adsb-heli",
      html: `<svg width="28" height="28" viewBox="0 0 28 28" style="transform:rotate(${rot}deg)">
        <circle cx="14" cy="12" r="9" fill="none" stroke="${fill}" stroke-width="1.4" opacity="0.55"/>
        <path d="M5 12 H23" stroke="${fill}" stroke-width="1.2" stroke-linecap="round" opacity="0.85"/>
        <path d="M14 3 V21" stroke="${fill}" stroke-width="1.2" stroke-linecap="round" opacity="0.85"/>
        <path d="M11.2 9.5 L14 7.2 L16.8 9.5 L16.2 16.5 L14 18.2 L11.8 16.5 Z"
          fill="${fill}" stroke="#0d1117" stroke-width="0.7"/>
        <path d="M14 18.2 V24.5" stroke="${fill}" stroke-width="1.8" stroke-linecap="round"/>
        <path d="M11.5 24.5 H16.5" stroke="${fill}" stroke-width="1.6" stroke-linecap="round"/>
        <path d="M16.5 24.5 L18.2 23.2" stroke="${fill}" stroke-width="1.4" stroke-linecap="round"/>
        <path d="M10.2 16.8 H17.8" stroke="${fill}" stroke-width="1.5" stroke-linecap="round"/>
      </svg>`,
      iconSize: [28, 28], iconAnchor: [14, 14],
    });
  }
  return L.divIcon({
    className: "adsb-icon",
    html: `<svg width="22" height="22" viewBox="0 0 22 22" style="transform:rotate(${rot}deg)">
      <path d="M11 1.5 L13.2 9.2 L20 11.2 L13.2 12.4 L12.2 20.5 L11 14.2 L9.8 20.5 L8.8 12.4 L2 11.2 L8.8 9.2 Z"
        fill="${fill}" stroke="#0d1117" stroke-width="0.7"/></svg>`,
    iconSize: [22, 22], iconAnchor: [11, 11],
  });
}

function renderAdsb() {
  if (!map) return;
  if (!adsbLayer) adsbLayer = L.layerGroup().addTo(map);
  adsbLayer.clearLayers();
  if (view !== "live") return;
  for (const a of adsb.values()) {
    const selected = selectedKey === "adsb:" + a.hex;
    L.marker([a.lat, a.lon], {
      icon: planeIcon(a.track, selected, a.source === "net", !!a.helicopter),
      zIndexOffset: selected ? 500 : (a.helicopter ? 400 : 0),
      title: a.helicopter ? `HELI ${a.flight || a.hex || ""}` : `${a.flight || a.hex || ""}`,
    })
      .addTo(adsbLayer)
      .bindTooltip(adsbHoverHtml(a), {
        direction: "top",
        offset: [0, -10],
        opacity: 0.95,
        className: "adsb-tooltip",
        sticky: true,
      })
      .on("click", () => {
        selectedKey = "adsb:" + a.hex;
        renderSidebar();
        renderAdsbInfo();
        renderAdsb();
      });
  }
  if (adsb.size && !adsbFitted) {
    const pts = [...adsb.values()].map((a) => [a.lat, a.lon]);
    const here = stationLl();
    if (here) pts.push(here);
    map.fitBounds(L.latLngBounds(pts).pad(0.35));
    adsbFitted = true;
  }
}

function renderAdsbInfo() {
  const a = adsb.get((selectedKey || "").slice(5));
  if (!a) return;
  $("info-empty").classList.add("hidden");
  $("info-body").classList.remove("hidden");
  $("i-model").textContent = a.flight || (a.type && a.type !== "Unknown" ? a.type : a.hex);
  $("i-marker").textContent = a.helicopter
    ? (a.source === "net" ? "HELI · ADS-B NET" : "HELI · ADS-B 1090")
    : (a.source === "net" ? "ADS-B · NET" : "ADS-B · 1090");
  const kv = [
    ["ICAO", a.hex, "mono"],
    ["Callsign", a.flight || "—"],
    ["Type", a.type || a.category || "—"],
    ["Altitude", a.alt_ft != null ? `${Math.round(a.alt_ft)} ft` : "—"],
    ["Ground speed", a.gs_kt != null ? `${Math.round(a.gs_kt)} kt` : "—"],
    ["Track", a.track != null ? `${Math.round(a.track)}°` : "—"],
    ["Position", `${Number(a.lat).toFixed(5)}, ${Number(a.lon).toFixed(5)}`],
    ["Distance", a.distance_nm != null ? `${Number(a.distance_nm).toFixed(1)} nm` : "—"],
    ["Squawk", a.squawk || "—"],
    ["Country", a.country || "—"],
    ["Messages", a.messages != null ? a.messages : "—"],
    ["Last seen", a.seen != null ? `${Math.round(a.seen)} s ago` : "—"],
    ["Source", a.source === "net"
      ? `network aggregator (${a.feed || "adsb"}) — not received by this station`
      : "local 1090 MHz receiver"],
  ];
  if (a.helicopter) {
    kv.splice(3, 0, ["Helicopter", (a.helicopter_flags || []).join(" · ") || "yes"]);
  }
  if (a.uas_candidate) {
    kv.push(["UAS candidate", a.uas_flags.join(" · ")]);
  }
  $("info-kv").innerHTML = kvHtml(kv);
  $("pkt-body").innerHTML = `<tr><td>${esc(a.last_seen ? fmtTime(a.last_seen) : "—")}</td><td>${a.source === "net" ? "net" : "1090.0"}</td><td>ADS-B</td><td>—</td></tr>`;
}

function stationLl() {
  if (!station || !hasFix(station.lat, station.lon)) return null;
  return [Number(station.lat), Number(station.lon)];
}

function placeStation() {
  const ll = stationLl();
  if (!ll) return;
  const where = station.source === "gps" ? "GPS fix"
    : station.source === "network" ? "network location" : "approximate position";
  const text = `<b>${esc(station.name)}</b><br>${esc(where)}<br>${ll[0].toFixed(5)}, ${ll[1].toFixed(5)}`;
  if (!stationMarker) {
    stationMarker = L.marker(ll, {
      icon: L.divIcon({
        className: "station-icon",
        html: `<svg width="26" height="26" viewBox="0 0 26 26">
          <circle cx="13" cy="13" r="12" fill="rgba(63,185,80,.15)"/>
          <path d="M13 5v4M13 17v4M5 13h4M17 13h4M8 8l2.5 2.5M15.5 15.5L18 18M18 8l-2.5 2.5M10.5 15.5L8 18"
            stroke="#3fb950" stroke-width="1.6"/>
          <circle cx="13" cy="13" r="2.2" fill="#3fb950"/></svg>`,
        iconSize: [26, 26], iconAnchor: [13, 13],
      }),
    }).addTo(map).bindPopup(text);
  } else {
    stationMarker.setLatLng(ll);
    stationMarker.setPopupContent(text);
  }
  if (station.source === "gps" && !gpsCentered) {
    map.setView(ll, Math.max(map.getZoom(), 15));
    gpsCentered = true;
  } else if (station.source === "network" && !mapSeeded) {
    map.setView(ll, 13);
    mapSeeded = true;
  }
}

function jitter() { return (Math.random() - 0.5) * 0.0016; }

function renderMap() {
  if (!map || !station) return;
  if (!dotLayer) dotLayer = L.layerGroup().addTo(map);
  dotLayer.clearLayers();
  if (activityRing) activityRing.remove();
  const here = stationLl();
  const djs = [...sessions.values()].filter((s) => s.source === "droneid" && s.packets.length);
  const latestDj = djs.sort((a, b) => tsec(b.lastTime) - tsec(a.lastTime))[0];
  if (latestDj && here) {
    const last = latestDj.packets[0];
    const rM = Math.max(60, Math.min(900, 60000 * Math.pow(2, (last.rssi + 50) / 10)));
    activityRing = L.circle(here, {
      radius: rM, color: "#f85149", weight: 1, fillOpacity: 0.08,
    }).addTo(dotLayer);
  }
  for (const p of latestDj && here ? latestDj.packets.slice(0, 60) : []) {
    L.circleMarker([here[0] + jitter(), here[1] + jitter()], {
      radius: p.marker === "CRYP" ? 6 : 4, color: p.marker === "CRYP" ? "#d29922" : "#f85149",
      fillColor: p.marker === "CRYP" ? "#d29922" : "#f85149", fillOpacity: 0.85, weight: 1,
    }).addTo(dotLayer).bindPopup(`<b>${esc(p.marker)}</b> · session ${esc(latestDj.key.slice(6))}<br>${esc(p.freq)} MHz · ${esc(p.rssi)} dBFS`);
  }
  for (const s of sessions.values()) {
    if (!s.pos || !hasFix(s.pos.lat, s.pos.lon)) continue;
    const ident = s.source === "remoteid" ? s.key.slice(4) : s.key.slice(6);
    const via = s.source === "remoteid" ? "Remote ID" : "DroneID plaintext";
    if (!s.trailLine) {
      s.trailLine = L.polyline([], { color: "#d29922", weight: 2, opacity: 0.8 }).addTo(map);
      s.droneDot = L.circleMarker([s.pos.lat, s.pos.lon], {
        radius: 7, color: "#f85149", fillColor: "#f85149", fillOpacity: 0.9, weight: 2,
      }).addTo(map).bindTooltip("Drone — " + via);
      const bounds = [[s.pos.lat, s.pos.lon]];
      if (here) bounds.push(here);
      map.fitBounds(L.latLngBounds(bounds).pad(0.3));
    }
    s.trailLine.setLatLngs(s.trail);
    s.droneDot.setLatLng([s.pos.lat, s.pos.lon]);
    s.droneDot.bindPopup(`<b>${esc(ident)}</b><br>${Number(s.pos.lat).toFixed(5)}, ${Number(s.pos.lon).toFixed(5)}` +
      `<br>${s.pos.alt != null ? Number(s.pos.alt).toFixed(1) + " m" : ""}` +
      (s.operator ? `<br>operator ${esc(s.operator)}` : "") +
      (s.selfid ? `<br>${esc(s.selfid)}` : ""));
    if (hasFix(s.opLat, s.opLon)) {
      if (!s.opDot) {
        s.opDot = L.circleMarker([s.opLat, s.opLon], {
          radius: 6, color: "#3fb950", fillColor: "#3fb950", fillOpacity: 0.9, weight: 2,
        }).addTo(map).bindTooltip("Operator position");
        map.fitBounds(L.latLngBounds([[s.pos.lat, s.pos.lon], [s.opLat, s.opLon]]).pad(0.3));
      }
      s.opDot.setLatLng([s.opLat, s.opLon]);
    }
  }
  for (const v of mavlink) {
    if (v.lat == null || v.lon == null) continue;
    L.circleMarker([v.lat, v.lon], {
      radius: 8, color: "#3fb950", fillColor: "#3fb950", fillOpacity: 0.9, weight: 2,
    }).addTo(dotLayer)
      .bindTooltip(`${esc(v.type_name || "MAVLink")} · sys ${esc(v.sysid)}`)
      .on("click", () => { selectedKey = mavKey(v); renderSidebar(); renderMavInfo(); renderMap(); });
  }
}

function renderInfo() {
  if (selectedKey && selectedKey.startsWith("vtx:")) {
    const l = vtxLinks.find((v) => vtxKey(v) === selectedKey);
    if (l) { renderVtxInfo(l); return; }
  }
  if (selectedKey && selectedKey.startsWith("mav:")) { renderMavInfo(); return; }
  if (selectedKey && selectedKey.startsWith("adsb:")) { renderAdsbInfo(); return; }
  const e = aircraftList().find((x) => entryId(x) === selectedKey);
  if (!e) return;
  $("info-empty").classList.add("hidden");
  $("info-body").classList.remove("hidden");
  const rid = e.rid, dj = e.djiids[0];
  const ident = rid ? rid.key.slice(4) : dj.key.slice(6);
  $("i-model").textContent = rid ? (rid.model || "Drone") : "DroneID";
  $("i-marker").textContent = rid && dj ? "RemoteID + DroneID" : (rid ? "RemoteID" : "DroneID");
  const kv = [];
  kv.push(["Identifier", ident, "mono"]);
  if (dj) {
    kv.push(["DroneID session", dj.key.slice(6), "mono"]);
    const encr = dj.encr || 0;
    kv.push(["DroneID packets", `${dj.count} (${dj.infp} INFP / ${dj.cryp} CRYP / ${encr} ENCR)`]);
    kv.push(["Channels", [...dj.channels.entries()].sort((a, b) => b[1] - a[1]).map(([c, n]) => `${c.toFixed(1)}×${n}`).join("  ")]);
    kv.push(["Best signal", dj.bestRssi > -900 ? dj.bestRssi.toFixed(1) + " dBFS" : "—"]);
    if (dj.pos) {
      kv.push(["Position", `${Number(dj.pos.lat).toFixed(6)}, ${Number(dj.pos.lon).toFixed(6)}`]);
      kv.push(["Telemetry", "O2/O3 plaintext (unencrypted)"]);
      if (dj.pos.alt != null) kv.push(["Altitude", `${Number(dj.pos.alt).toFixed(1)} m`]);
      if (dj.pos.yaw != null) kv.push(["Yaw", `${Number(dj.pos.yaw).toFixed(1)}°`]);
      if (dj.home) kv.push(["Home point", `${Number(dj.home.lat).toFixed(6)}, ${Number(dj.home.lon).toFixed(6)}`]);
      if (!rid || !rid.opLat) {
        kv.push(["Operator position", dj.opLat
          ? `${Number(dj.opLat).toFixed(6)}, ${Number(dj.opLon).toFixed(6)}` : "—"]);
      }
    } else if (encr || (dj.pclass || "").includes("truncat") || (dj.pclass || "").includes("encrypt")) {
      kv.push(["Telemetry", "presence only (O3+/encrypted stub — no lat/lon)"]);
      if (dj.pclass) kv.push(["Decode class", dj.pclass]);
    } else {
      kv.push(["O4 telemetry", "encrypted (AES-CTR)"]);
    }
  }
  if (rid) {
    kv.push(["Remote ID", "decoded (F3411 beacon)"]);
    kv.push(["Position", rid.pos ? `${Number(rid.pos.lat).toFixed(6)}, ${Number(rid.pos.lon).toFixed(6)}` : "—"]);
    if (rid.pos) kv.push(["Altitude / status", `${Number(rid.pos.alt).toFixed(1)} m · ${rid.pos.status || ""}`]);
    kv.push(["Operator position", rid.opLat ? `${Number(rid.opLat).toFixed(6)}, ${Number(rid.opLon).toFixed(6)}` : "—"]);
    if (rid.operator) kv.push(["Operator", rid.operator]);
    if (rid.selfid) kv.push(["Self ID", rid.selfid]);
  }
  const lastIso = rid && (!dj || tsec(rid.lastTime) > tsec(dj.lastTime)) ? rid.lastTime : dj.lastTime;
  kv.push(["Last seen", fmtTime(lastIso) + " " + localZoneLabel()]);
  $("info-kv").innerHTML = kvHtml(kv);
  // merged packet table
  const all = [];
  if (rid) for (const p of rid.packets) all.push({ ...p, proto: "RID" });
  if (dj) for (const p of dj.packets) all.push({ ...p, proto: "DJI" });
  all.sort((a, b) => tsec(b.time) - tsec(a.time));
  const body = $("pkt-body");
  body.innerHTML = "";
  for (const p of all.slice(0, 20)) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(fmtTime(p.time))}</td><td>${esc(fmtFixed(p.freq, 1))}</td>` +
      `<td class="${p.marker === "CRYP" ? "cryp" : ""}">${esc(p.marker)}</td><td>${p.proto === "RID" ? "—" : esc(fmtFixed(p.rssi, 1))}</td>`;
    body.appendChild(tr);
  }
}

/* ---- history (Events tab) ---- */
let view = "live";
let historySessions = [];
let historyLayer = null;
let selectedHistory = null;
let histOffset = 0;
let histEvents = [];
let histMapOn = false;
const HIST_PAGE = 500;

function histQuery(paged) {
  const p = new URLSearchParams();
  const src = $("hist-source").value;
  const from = $("hist-from").value;
  const to = $("hist-to").value;
  if (src) p.set("source", src);
  if (from) p.set("frm", from);
  if (to) p.set("to", to);
  if (paged) {
    p.set("limit", HIST_PAGE);
    p.set("offset", histOffset);
  }
  return p.toString();
}

function switchView(v) {
  view = v;
  $("tab-monitor").classList.toggle("active", v === "live");
  $("tab-history").classList.toggle("active", v === "history");
  const showMap = v === "live";
  $("map").classList.toggle("hidden", !showMap);
  $("history-table").classList.toggle("hidden", showMap);
  $("hist-controls").classList.toggle("hidden", v !== "history");
  document.querySelector("main").classList.toggle("hist", v === "history");
  $("infopanel").classList.toggle("hidden", v === "history");
  if (v === "history") {
    $("side-head-label").textContent = "Drone history";
    selectedHistory = null;
    histMapOn = false;
    $("hist-map").textContent = "Show map";
    loadHistory();
  } else {
    histMapOn = false;
    $("hist-map").textContent = "Show map";
    $("side-head-label").textContent = "Drone detection";
    if (historyLayer) { historyLayer.remove(); historyLayer = null; }
    if (map) map.invalidateSize();
    renderAll();
    renderAdsb();
    if (selectedKey) renderInfo();
    else hideInfo();
  }
}

function histSourceMatch(sources, src) {
  if (!src) return true;
  return String(sources || "")
    .split(",")
    .map((s) => s.trim())
    .includes(src);
}

async function loadHistory() {
  selectedHistory = null;
  histOffset = 0;
  const all = await (await apiFetch("/api/history/sessions?limit=100")).json();
  const src = $("hist-source").value;
  historySessions = src
    ? all.filter((h) => histSourceMatch(h.sources, src))
    : all;
  renderHistoryList();
  if (src) {
    const evs = await (await apiFetch("/api/history/events?" + histQuery(true))).json();
    histEvents = evs;
    renderHistoryTable(evs, `${SRC_LABEL[src] || src} events`);
    $("hist-more").disabled = evs.length < HIST_PAGE;
    return;
  }
  // "All protocols" used to dump the latest 500 rows — RemoteID flood buried
  // ELRS/DroneID. Show one latest row per session instead; open a session for detail.
  const overview = historySessions.map((h) => {
    const sources = String(h.sources || "").split(",").map((s) => s.trim()).filter(Boolean);
    const markers = String(h.markers || "").split(",").map((s) => s.trim()).filter(Boolean);
    return {
      time: h.last,
      session: h.session,
      source: sources[0] || "",
      marker: markers[0] || "",
      freq: null,
      rssi: h.best,
      lat: null,
      lon: null,
      crc: true,
    };
  });
  histEvents = overview;
  renderHistoryTable(overview, "Sessions (latest per identifier)");
  $("hist-more").disabled = true;
}

function renderHistoryList() {
  $("session-count").textContent = historySessions.length;
  const el = $("alert-list");
  el.innerHTML = "";
  for (const h of historySessions) {
    const div = document.createElement("div");
    div.className = "alert" + (selectedHistory === h.session ? " selected" : "");
    const rid = (h.sources || "").includes("remoteid");
    const dj = (h.sources || "").includes("droneid");
    const isElrs = (h.sources || "").includes("elrs");
    const isMav = (h.sources || "").includes("mavlink");
    let badges = "";
    if (rid) badges += '<span class="tag tag-rid">RemoteID</span> ';
    if (dj) badges += '<span class="tag">DroneID</span>';
    if (isElrs) badges += '<span class="tag tag-elrs">ELRS</span>';
    if (isMav) badges += '<span class="tag tag-mav">MAVLink</span>';
    const model = h.model || (isMav ? "MAVLink" : (isElrs ? "ELRS link" : (rid ? "Drone" : "DroneID")));
    div.innerHTML = `
      <div class="row1"><span class="model">${esc(model)}</span><span>${badges}</span></div>
      <code>${esc(h.session)}</code>
      <div class="meta"><b>${esc(h.n)}</b> events · ${esc(fmtTime(h.first))} → ${esc(fmtTime(h.last))} ${esc(localZoneLabel())}</div>`;
    div.onclick = () => { selectedHistory = h.session; renderHistoryList(); showHistoryDetail(h); };
    el.appendChild(div);
  }
}

const SRC_LABEL = { remoteid: "RemoteID", droneid: "DroneID", elrs: "ELRS", mavlink: "MAVLink", vtx: "VTX" };

function renderHistoryTable(evs, title) {
  const el = $("history-table");
  if (!evs.length) {
    el.innerHTML = '<div class="empty-note">No events recorded yet</div>';
    return;
  }
  const rows = evs.map((e) => {
    const src = SRC_LABEL[e.source] || e.source || "—";
    const level = e.rssi != null && e.source !== "remoteid" ? fmtFixed(e.rssi, 1) + " dB" : "—";
    const lat = Number(e.lat);
    const lon = Number(e.lon);
    const pos = Number.isFinite(lat) && Number.isFinite(lon) && !(lat === 0 && lon === 0)
      ? `${lat.toFixed(5)}, ${lon.toFixed(5)}` : "—";
    const crc = e.crc ? "" : " · bad CRC";
    return `<tr><td>${esc(fmtDateTime(e.time))}</td>` +
      `<td><code>${esc(e.session)}</code></td><td>${esc(src)}</td>` +
      `<td class="${e.marker === "CRYP" ? "cryp" : ""}">${esc(e.marker || "—")}${esc(crc)}</td>` +
      `<td>${esc(e.freq != null ? fmtFixed(e.freq, 1) : "—")}</td><td>${esc(level)}</td><td>${esc(pos)}</td></tr>`;
  }).join("");
  el.innerHTML = `<div class="ht-title">${esc(title)} · ${evs.length} rows</div>` +
    `<table><thead><tr><th>Time (${esc(localZoneLabel())})</th><th>Identifier</th><th>Protocol</th>` +
    `<th>Marker</th><th>MHz</th><th>Level</th><th>Position</th></tr></thead><tbody>${rows}</tbody></table>`;
}

async function showHistoryDetail(h) {
  const p = new URLSearchParams({ session: h.session, limit: "500" });
  const src = $("hist-source").value;
  if (src) p.set("source", src);
  const from = $("hist-from").value;
  const to = $("hist-to").value;
  if (from) p.set("frm", from);
  if (to) p.set("to", to);
  const evs = await (await apiFetch(`/api/history/events?${p}`)).json();
  histEvents = evs;
  const title = src
    ? `Session ${h.session} · ${SRC_LABEL[src] || src}`
    : `Session ${h.session}`;
  renderHistoryTable(evs, title);
  $("hist-more").disabled = true;
  if (histMapOn && map) drawHistoryMap(evs);
  $("info-empty").classList.add("hidden");
  $("info-body").classList.remove("hidden");
  const rid = (h.sources || "").includes("remoteid");
  const isElrs = (h.sources || "").includes("elrs");
  const isMav = (h.sources || "").includes("mavlink");
  $("i-model").textContent = (h.model || (isMav ? "MAVLink" : (isElrs ? "ELRS link" : (rid ? "Drone" : "DroneID")))) + " (history)";
  $("i-marker").textContent = "History";
  const kv = [
    ["Identifier", h.session, "mono"],
    ["Events", h.n],
    ["Protocols", h.sources],
    ["First seen", fmtDateTime(h.first)],
    ["Last seen", fmtDateTime(h.last)],
    ["Markers", h.markers],
  ];
  $("info-kv").innerHTML = kvHtml(kv);
  const body = $("pkt-body");
  body.innerHTML = "";
  for (const e of evs.slice(0, 20)) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(fmtDateTime(e.time))}</td><td>${esc(e.freq ? fmtFixed(e.freq, 1) : "—")}</td>` +
      `<td class="${e.marker === "CRYP" ? "cryp" : ""}">${esc(e.marker || "?")}</td>` +
      `<td>${esc(e.rssi != null && e.source !== "remoteid" ? fmtFixed(e.rssi, 1) : "—")}</td>`;
    body.appendChild(tr);
  }
}

function drawHistoryMap(evs) {
  if (!map) return;
  if (historyLayer) historyLayer.remove();
  historyLayer = L.layerGroup().addTo(map);
  const ridPts = evs.filter((e) => e.lat || e.lon).sort((a, b) => (a.time < b.time ? -1 : 1));
  if (ridPts.length) {
    const latlngs = ridPts.map((e) => [e.lat, e.lon]);
    L.polyline(latlngs, { color: "#d29922", weight: 2, opacity: 0.8 }).addTo(historyLayer);
    const last = latlngs[latlngs.length - 1];
    L.circleMarker(last, { radius: 7, color: "#f85149", fillColor: "#f85149", fillOpacity: 0.9, weight: 2 })
      .addTo(historyLayer).bindTooltip("Drone — last position");
    map.fitBounds(L.latLngBounds(latlngs).pad(0.3));
  }
  const stationPts = evs.filter((e) => !e.lat && !e.lon);
  for (const e of stationPts.slice(0, 60)) {
    if (!station) break;
    const slat = e.station_lat != null ? e.station_lat : station.lat;
    const slon = e.station_lon != null ? e.station_lon : station.lon;
    if (!hasFix(slat, slon)) continue;
    L.circleMarker([slat + jitter(), slon + jitter()], {
      radius: e.marker === "CRYP" ? 5 : 3.2,
      color: e.marker === "CRYP" ? "#d29922" : "#f85149",
      fillColor: e.marker === "CRYP" ? "#d29922" : "#f85149",
      fillOpacity: 0.85, weight: 1,
    }).addTo(historyLayer).bindPopup(`<b>${esc(e.marker)}</b><br>${esc(e.freq)} MHz · ${esc(fmtDateTime(e.time))}`);
  }
}

/* Ask for the token up front when the station wants one.

 * Without this the page renders empty and then starts 401ing once a
 * second, which looks like a broken station rather than a locked one.
 */
async function checkAuthRequired() {
  try {
    const r = await fetch("/api/auth/status", { cache: "no-store" });
    if (!r.ok) return;
    const st = await r.json();
    if (st.required && !st.ok) {
      authBlocked = true;
      promptForToken();
    }
  } catch (_) { /* offline: the status poll will report it */ }
}

function init() {
  map = L.map("map", { zoomControl: true, attributionControl: false });
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}", {
    maxZoom: 16,
  }).addTo(map);
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}", {
    maxZoom: 16,
  }).addTo(map);
  map.setView([20, 0], 2);

  $("btn-scan").onclick = async () => {
    const btn = $("btn-scan");
    if (btn.disabled) return;
    btn.disabled = true;
    const wantStart = !scanArmed;
    try {
      await apiFetch(wantStart ? "/api/scan/start" : "/api/scan/stop", { method: "POST" });
      scanArmed = wantStart;
      btn.textContent = scanArmed ? "Stop scan" : "Start scan";
      btn.className = scanArmed ? "scanning" : "ready";
    } catch (_) { /* pullStatus will resync */ }
    btn.disabled = false;
    pullStatus();
  };
  const dragPanel = $("vtx-panel");
  $("btn-video").onclick = () => {
    videoOpen = !videoOpen;
    $("vtx-panel").classList.toggle("hidden", !videoOpen);
    $("btn-video").textContent = videoOpen ? "Hide video" : "FPV video";
  };
  const dragHead = dragPanel.querySelector(".vtx-head");
  const resizeHandle = dragPanel.querySelector(".vtx-resize");
  let dragState = null;
  let resizeState = null;
  dragHead.addEventListener("mousedown", (e) => {
    const r = dragPanel.getBoundingClientRect();
    dragState = { dx: e.clientX - r.left, dy: e.clientY - r.top };
    e.preventDefault();
  });
  resizeHandle.addEventListener("mousedown", (e) => {
    const r = dragPanel.getBoundingClientRect();
    resizeState = { x: e.clientX, y: e.clientY, w: r.width, h: r.height };
    e.preventDefault();
    e.stopPropagation();
  });
  window.addEventListener("mousemove", (e) => {
    if (dragState) {
      dragPanel.style.left = Math.max(0, Math.min(window.innerWidth - 120, e.clientX - dragState.dx)) + "px";
      dragPanel.style.top = Math.max(0, Math.min(window.innerHeight - 40, e.clientY - dragState.dy)) + "px";
      dragPanel.style.right = "auto";
    } else if (resizeState) {
      const w = Math.max(200, Math.min(window.innerWidth - 24, resizeState.w + (e.clientX - resizeState.x)));
      const h = Math.max(140, Math.min(window.innerHeight - 24, resizeState.h + (e.clientY - resizeState.y)));
      dragPanel.style.width = w + "px";
      dragPanel.style.height = h + "px";
    }
  });
  window.addEventListener("mouseup", () => { dragState = null; resizeState = null; });
  $("tab-monitor").onclick = () => switchView("live");
  $("tab-history").onclick = () => switchView("history");
  $("hist-more").onclick = async () => {
    const src = $("hist-source").value;
    if (!src || selectedHistory) return;
    histOffset += HIST_PAGE;
    const more = await (await apiFetch("/api/history/events?" + histQuery(true))).json();
    histEvents = histEvents.concat(more);
    renderHistoryTable(histEvents, `${SRC_LABEL[src] || src} events`);
    $("hist-more").disabled = more.length < HIST_PAGE;
  };
  $("hist-export").onclick = () => {
    window.open("/api/history/export?" + histQuery(false), "_blank");
  };
  $("hist-map").onclick = () => {
    histMapOn = !histMapOn;
    $("hist-map").textContent = histMapOn ? "Show table" : "Show map";
    $("map").classList.toggle("hidden", !histMapOn);
    $("history-table").classList.toggle("hidden", histMapOn);
    if (histMapOn) {
      if (map) map.invalidateSize();
      drawHistoryMap(histEvents);
    }
  };
  for (const id of ["hist-source", "hist-from", "hist-to"]) {
    $(id).addEventListener("change", loadHistory);
  }
  setInterval(expireLive, 1000);
  pullStatus();
  setInterval(pullStatus, 1000);  // keep RF hop phase visible even between WS pushes
  const fr24Input = $("fr24-key");
  if (fr24Input) {
    fr24Input.addEventListener("input", () => { fr24Input.dataset.dirty = "1"; });
  }
  const fr24Save = $("fr24-save");
  if (fr24Save) fr24Save.onclick = saveFr24Key;
  loadFr24Config();
  checkAuthRequired();
  connect();
}

document.querySelectorAll("[data-coming]").forEach((el) =>
  el.addEventListener("click", () => alert("Coming soon")));

init();