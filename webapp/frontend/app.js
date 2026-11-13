/* Drone Watchtower — live detection frontend
 * Aircraft-centric view: one entry per drone, merging RemoteID (beacon/BLE)
 * and DroneID (OcuSync) sessions by concurrent activity.
 */
const $ = (id) => document.getElementById(id);
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

function fmtTime(iso) {
  return iso ? iso.slice(11, 23) : "--";
}
function tsec(iso) {
  return Date.parse(iso.endsWith("+00:00") ? iso.replace("+00:00", "Z") : iso) || 0;
}

function connect() {
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
  ws.onclose = () => setTimeout(connect, 1500);
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
  if (map && station) placeStation();
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

  const hopLabel = $("dongle-one") && $("dongle-one").querySelector(".dongle-hop-label");
  if (hopLabel) hopLabel.textContent = hop || "One";
  const dongleOne = $("dongle-one");
  if (dongleOne) {
    dongleOne.title = hop
      ? `HackRF One · scanning ${hop}`
      : "HackRF One · ELRS / TBS / MAVLink / VTX hop";
  }
}

async function pullStatus() {
  try {
    const st = await fetch("/api/status", { cache: "no-store" }).then((r) => r.json());
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
  s.opLat = evt.op_lat; s.opLon = evt.op_lon;
  if (hasFix(evt.lat, evt.lon)) {
    s.pos = { lat: evt.lat, lon: evt.lon, alt: evt.alt, speed: evt.speed, status: evt.status };
    s.trail.push([evt.lat, evt.lon]);
    if (s.trail.length > 300) s.trail.shift();
  }
  s.packets.unshift({ time: evt.time, freq: evt.freq || 2437, marker: "RemoteID", rssi: evt.rssi || 0 });
  s.packets = s.packets.slice(0, 50);
  renderAll();
  renderMap();
  if (selectedKey === s.key) renderInfo();
}

function onDetection(evt) {
  if (!evt.crc) return;
  const mode = evt.mode || "live";
  const s = sess("djiid:" + evt.session, mode);
  s.count++;
  if (evt.marker === "CRYP") s.cryp++; else s.infp++;
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
  renderSidebar();
  renderBanner();
}

function renderBanner() {
  const banner = $("banner");
  const list = aircraftList();
  const fresh = list.some((e) => e.last && Date.now() - e.last < 15000 && e.last > 0);
  banner.textContent = "Drone detected — identification active";
  banner.classList.toggle("hidden", !fresh);
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
    <div class="row1"><span class="model">FPV camera</span><span class="tag tag-vtx">${l.channel || "5.8G"}</span></div>
    <code>analog video</code>
    <div class="meta">${l.freq} MHz · peak ${l.peak_db} dB · ${l.width_mhz} MHz wide</div>`;
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
  $("info-kv").innerHTML = kv.map(([k, v]) => `<div class="kv"><span>${k}</span><span>${v}</span></div>`).join("");
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
    <div class="row1"><span class="model">${title}</span><span class="tag tag-elrs">${link.band}</span></div>
    <code>${code}</code>
    <div class="meta">${meta}</div>`;
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
    <div class="row1"><span class="model">${v.type_name || "MAVLink"}</span><span class="tag tag-mav">MAVLink</span></div>
    <code>sys ${v.sysid} · ${v.autopilot_name || "autopilot"}</code>
    <div class="meta">${meta}</div>`;
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
    ["System ID", `<code>${v.sysid}</code>`],
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
  $("info-kv").innerHTML = kv.map(([k, val]) => `<div class="kv"><span>${k}</span><span>${val}</span></div>`).join("");
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
  $("info-kv").innerHTML = kv.map(([k, v]) => `<div class="kv"><span>${k}</span><span>${v}</span></div>`).join("");
  $("pkt-body").innerHTML = "";
}

function renderElrsInfo() {
  renderHopInfo(elrs, "ELRS");
}

function renderSidebar() {
  const list = aircraftList();
  const hopCount = (elrs && elrs.present ? 1 : 0) + (tbs && tbs.present ? 1 : 0) + vtxLinks.length + mavlink.length;
  $("session-count").textContent = list.length + (view === "live" ? hopCount : 0);
  const el = $("alert-list");
  el.innerHTML = "";
  renderHopRow(el, elrs, "elrs:24", "ELRS");
  renderHopRow(el, tbs, "tbs", "TBS");
  for (const v of mavlink) renderMavRow(el, v);
  for (const l of vtxLinks) renderVtxRow(el, l);
  if (!list.length && view === "live" && !hopCount) {
    el.innerHTML = '<div class="empty-note">No drones</div>';
    renderHopRow(el, elrs, "elrs:24", "ELRS");
    renderHopRow(el, tbs, "tbs", "TBS");
    for (const v of mavlink) renderMavRow(el, v);
    for (const l of vtxLinks) renderVtxRow(el, l);
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
    if (dj) metaParts.push(`DroneID ${dj.count} pkts (${dj.infp} INFP / ${dj.cryp} CRYP)`);
    if (dj && dj.pos) metaParts.push("O2 plaintext position");
    const lastIso = rid && (!dj || tsec(rid.lastTime) > tsec(dj.lastTime)) ? rid.lastTime : dj.lastTime;
    metaParts.push(`last ${fmtTime(lastIso)}`);
    div.innerHTML = `
      <div class="row1"><span class="model">${model}</span><span>${badges}</span></div>
      <code>${ident}</code>
      <div class="meta">${metaParts.join(" · ")}</div>`;
    div.onclick = () => { selectedKey = id; renderSidebar(); renderInfo(); };
    el.appendChild(div);
  }
  renderAdsbList();
}

function renderAdsbList() {
  const el = $("adsb-list");
  if (!el) return;
  const tracks = [...adsb.values()].sort((a, b) => (a.distance_nm || 999) - (b.distance_nm || 999));
  $("adsb-count").textContent = tracks.length;
  el.innerHTML = "";
  if (!tracks.length) {
    el.innerHTML = '<div class="empty-note">No aircraft</div>';
    return;
  }
  for (const a of tracks) {
    const id = "adsb:" + a.hex;
    const div = document.createElement("div");
    div.className = "alert" + (id === selectedKey ? " selected" : "");
    const typeName = a.type && a.type !== "Unknown" ? a.type : "";
    const name = a.flight || typeName || a.hex;
    const meta = [
      a.alt_ft != null ? `${Math.round(a.alt_ft)} ft` : null,
      a.gs_kt != null ? `${Math.round(a.gs_kt)} kt` : null,
      a.distance_nm != null ? `${Number(a.distance_nm).toFixed(1)} nm` : null,
    ].filter(Boolean).join(" · ");
    div.innerHTML = `
      <div class="row1"><span class="model">${name}</span><span class="tag tag-adsb">ADS-B</span></div>
      <code>${a.hex}</code>
      <div class="meta">${meta || "1090 MHz"}</div>`;
    div.onclick = () => { selectedKey = id; renderSidebar(); renderAdsbInfo(); renderAdsb(); };
    el.appendChild(div);
  }
}

function planeIcon(track, selected) {
  const rot = Number(track) || 0;
  const fill = selected ? "#58a6ff" : "#79c0ff";
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
    const label = a.flight || a.hex;
    L.marker([a.lat, a.lon], { icon: planeIcon(a.track, selected), zIndexOffset: selected ? 500 : 0 })
      .addTo(adsbLayer)
      .bindTooltip(label, { direction: "top", offset: [0, -8] })
      .on("click", () => { selectedKey = "adsb:" + a.hex; renderSidebar(); renderAdsbInfo(); renderAdsb(); });
  }
  if (adsb.size && station && !adsbFitted) {
    const pts = [[station.lat, station.lon], ...[...adsb.values()].map((a) => [a.lat, a.lon])];
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
  $("i-marker").textContent = "ADS-B";
  const kv = [
    ["ICAO", `<code>${a.hex}</code>`],
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
  ];
  $("info-kv").innerHTML = kv.map(([k, v]) => `<div class="kv"><span>${k}</span><span>${v}</span></div>`).join("");
  $("pkt-body").innerHTML = `<tr><td>${a.last_seen ? String(a.last_seen).slice(11, 19) : "—"}</td><td>1090.0</td><td>ADS-B</td><td>—</td></tr>`;
}

function placeStation() {
  const ll = [station.lat, station.lon];
  const where = station.source === "gps" ? "GPS fix" : "approximate position";
  const text = `<b>${station.name}</b><br>${where}<br>${Number(station.lat).toFixed(5)}, ${Number(station.lon).toFixed(5)}`;
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
  } else if (!mapSeeded) {
    // first status carries the configured station — drop the placeholder view
    map.setView(ll, Math.max(map.getZoom(), 13));
    mapSeeded = true;
  }
}

function jitter() { return (Math.random() - 0.5) * 0.0016; }

function renderMap() {
  if (!map || !station) return;
  if (!dotLayer) dotLayer = L.layerGroup().addTo(map);
  dotLayer.clearLayers();
  if (activityRing) activityRing.remove();
  const djs = [...sessions.values()].filter((s) => s.source === "droneid" && s.packets.length);
  const latestDj = djs.sort((a, b) => tsec(b.lastTime) - tsec(a.lastTime))[0];
  if (latestDj) {
    const last = latestDj.packets[0];
    const rM = Math.max(60, Math.min(900, 60000 * Math.pow(2, (last.rssi + 50) / 10)));
    activityRing = L.circle([station.lat, station.lon], {
      radius: rM, color: "#f85149", weight: 1, fillOpacity: 0.08,
    }).addTo(dotLayer);
  }
  for (const p of latestDj ? latestDj.packets.slice(0, 60) : []) {
    L.circleMarker([station.lat + jitter(), station.lon + jitter()], {
      radius: p.marker === "CRYP" ? 6 : 4, color: p.marker === "CRYP" ? "#d29922" : "#f85149",
      fillColor: p.marker === "CRYP" ? "#d29922" : "#f85149", fillOpacity: 0.85, weight: 1,
    }).addTo(dotLayer).bindPopup(`<b>${p.marker}</b> · session ${latestDj.key.slice(6)}<br>${p.freq} MHz · ${p.rssi} dBFS`);
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
      map.fitBounds(L.latLngBounds([[s.pos.lat, s.pos.lon], [station.lat, station.lon]]).pad(0.3));
    }
    s.trailLine.setLatLngs(s.trail);
    s.droneDot.setLatLng([s.pos.lat, s.pos.lon]);
    s.droneDot.bindPopup(`<b>${ident}</b><br>${Number(s.pos.lat).toFixed(5)}, ${Number(s.pos.lon).toFixed(5)}` +
      `<br>${s.pos.alt != null ? Number(s.pos.alt).toFixed(1) + " m" : ""}` +
      (s.selfid ? `<br>${s.selfid}` : ""));
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
      .bindTooltip(`${v.type_name || "MAVLink"} · sys ${v.sysid}`)
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
  kv.push(["Identifier", `<code>${ident}</code>`]);
  if (dj) {
    kv.push(["DroneID session", `<code>${dj.key.slice(6)}</code>`]);
    kv.push(["DroneID packets", `${dj.count} (${dj.infp} INFP / ${dj.cryp} CRYP)`]);
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
    } else {
      kv.push(["O4 telemetry", "encrypted (AES-CTR)"]);
    }
  }
  if (rid) {
    kv.push(["Remote ID", "decoded (F3411 beacon)"]);
    kv.push(["Position", rid.pos ? `${Number(rid.pos.lat).toFixed(6)}, ${Number(rid.pos.lon).toFixed(6)}` : "—"]);
    if (rid.pos) kv.push(["Altitude / status", `${Number(rid.pos.alt).toFixed(1)} m · ${rid.pos.status || ""}`]);
    kv.push(["Operator position", rid.opLat ? `${Number(rid.opLat).toFixed(6)}, ${Number(rid.opLon).toFixed(6)}` : "—"]);
  }
  const lastIso = rid && (!dj || tsec(rid.lastTime) > tsec(dj.lastTime)) ? rid.lastTime : dj.lastTime;
  kv.push(["Last seen", fmtTime(lastIso) + " UTC"]);
  const rows = kv.map(([k, v]) => `<div class="kv"><span>${k}</span><span>${v}</span></div>`).join("");
  $("info-kv").innerHTML = rows;
  // merged packet table
  const all = [];
  if (rid) for (const p of rid.packets) all.push({ ...p, proto: "RID" });
  if (dj) for (const p of dj.packets) all.push({ ...p, proto: "DJI" });
  all.sort((a, b) => tsec(b.time) - tsec(a.time));
  const body = $("pkt-body");
  body.innerHTML = "";
  for (const p of all.slice(0, 20)) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${fmtTime(p.time)}</td><td>${p.freq.toFixed(1)}</td>` +
      `<td class="${p.marker === "CRYP" ? "cryp" : ""}">${p.marker}</td><td>${p.proto === "RID" ? "—" : p.rssi.toFixed(1)}</td>`;
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

async function loadHistory() {
  historySessions = await (await fetch("/api/history/sessions?limit=100")).json();
  renderHistoryList();
  histOffset = 0;
  const evs = await (await fetch("/api/history/events?" + histQuery(true))).json();
  histEvents = evs;
  renderHistoryTable(evs, "All events");
  $("hist-more").disabled = evs.length < HIST_PAGE;
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
      <div class="row1"><span class="model">${model}</span><span>${badges}</span></div>
      <code>${h.session}</code>
      <div class="meta"><b>${h.n}</b> events · ${h.first ? h.first.slice(11, 19) : "?"} → ${h.last ? h.last.slice(11, 19) : "?"} UTC</div>`;
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
    const level = e.rssi != null && e.source !== "remoteid" ? e.rssi.toFixed(1) + " dB" : "—";
    const pos = (e.lat || e.lon) ? `${e.lat.toFixed(5)}, ${e.lon.toFixed(5)}` : "—";
    const crc = e.crc ? "" : " · bad CRC";
    return `<tr><td>${(e.time || "").replace("T", " ").slice(0, 23)}</td>` +
      `<td><code>${e.session}</code></td><td>${src}</td>` +
      `<td class="${e.marker === "CRYP" ? "cryp" : ""}">${e.marker || "—"}${crc}</td>` +
      `<td>${e.freq != null ? e.freq.toFixed(1) : "—"}</td><td>${level}</td><td>${pos}</td></tr>`;
  }).join("");
  el.innerHTML = `<div class="ht-title">${title} · ${evs.length} rows</div>` +
    `<table><thead><tr><th>Time (UTC)</th><th>Identifier</th><th>Protocol</th>` +
    `<th>Marker</th><th>MHz</th><th>Level</th><th>Position</th></tr></thead><tbody>${rows}</tbody></table>`;
}

async function showHistoryDetail(h) {
  const evs = await (await fetch(`/api/history/events?session=${encodeURIComponent(h.session)}&limit=500`)).json();
  histEvents = evs;
  renderHistoryTable(evs, `Session ${h.session}`);
  if (histMapOn && map) drawHistoryMap(evs);
  $("info-empty").classList.add("hidden");
  $("info-body").classList.remove("hidden");
  const rid = (h.sources || "").includes("remoteid");
  const isElrs = (h.sources || "").includes("elrs");
  const isMav = (h.sources || "").includes("mavlink");
  $("i-model").textContent = (h.model || (isMav ? "MAVLink" : (isElrs ? "ELRS link" : (rid ? "Drone" : "DroneID")))) + " (history)";
  $("i-marker").textContent = "History";
  const kv = [
    ["Identifier", `<code>${h.session}</code>`],
    ["Events", h.n],
    ["Protocols", h.sources],
    ["First seen", (h.first || "").slice(11, 19) + " UTC"],
    ["Last seen", (h.last || "").slice(11, 19) + " UTC"],
    ["Markers", h.markers],
  ];
  $("info-kv").innerHTML = kv.map(([k, v]) => `<div class="kv"><span>${k}</span><span>${v}</span></div>`).join("");
  const body = $("pkt-body");
  body.innerHTML = "";
  for (const e of evs.slice(0, 20)) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${(e.time || "").slice(11, 23)}</td><td>${e.freq ? e.freq.toFixed(1) : "—"}</td>` +
      `<td class="${e.marker === "CRYP" ? "cryp" : ""}">${e.marker || "?"}</td>` +
      `<td>${e.rssi != null && e.source !== "remoteid" ? e.rssi.toFixed(1) : "—"}</td>`;
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
    L.circleMarker([slat + jitter(), slon + jitter()], {
      radius: e.marker === "CRYP" ? 5 : 3.2,
      color: e.marker === "CRYP" ? "#d29922" : "#f85149",
      fillColor: e.marker === "CRYP" ? "#d29922" : "#f85149",
      fillOpacity: 0.85, weight: 1,
    }).addTo(historyLayer).bindPopup(`<b>${e.marker}</b><br>${e.freq} MHz · ${(e.time || "").slice(11, 19)}`);
  }
}

function init() {
  map = L.map("map", { zoomControl: true, attributionControl: false });
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}", {
    maxZoom: 16,
  }).addTo(map);
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}", {
    maxZoom: 16,
  }).addTo(map);
  map.setView([22.2783, 114.1747], 12);

  $("btn-scan").onclick = async () => {
    $("btn-scan").disabled = true;
    await fetch(scanArmed ? "/api/scan/stop" : "/api/scan/start", { method: "POST" });
    $("btn-scan").disabled = false;
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
    histOffset += HIST_PAGE;
    const more = await (await fetch("/api/history/events?" + histQuery(true))).json();
    histEvents = histEvents.concat(more);
    renderHistoryTable(histEvents, "All events");
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
  connect();
}

document.querySelectorAll("[data-coming]").forEach((el) =>
  el.addEventListener("click", () => alert("Coming soon")));

init();
