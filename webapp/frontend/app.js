/* Drone Watchtower — live detection frontend
 * Aircraft-centric view: one entry per drone, merging RemoteID (beacon/BLE)
 * and DroneID (OcuSync) sessions by concurrent activity.
 */
const $ = (id) => document.getElementById(id);
let map, stationMarker, activityRing, dotLayer;
let station = null;
let statusMode = null;
const sessions = new Map(); // "rid:<serial>" | "djiid:<hash>" -> session

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
  const wasReplay = statusMode === "replay";
  statusMode = st.mode || null;
  if (wasReplay && statusMode !== "replay") {
    // replay finished: clear replayed data, return to live view
    for (const [k, s] of [...sessions]) if (s.mode === "replay") sessions.delete(k);
    renderAll();
  }
  station = st.station;
  const pill = $("status-pill");
  pill.textContent = st.running
    ? (st.mode === "replay" ? "REPLAY · " : "live · ") + st.events + " pkts"
    : "idle";
  pill.className = "pill " + (st.running ? (st.mode === "replay" ? "ok" : "live") : "");
  $("btn-scan").textContent = st.running && st.mode === "live" ? "Stop Scan" : "Start Scan";
  $("btn-replay").disabled = st.running;
  $("btn-scan").disabled = st.running && st.mode === "replay";
  if (map && station && !stationMarker) placeStation();
}

function sess(key, mode) {
  if (!sessions.has(key)) {
    sessions.set(key, {
      key, mode, source: key.startsWith("rid:") ? "remoteid" : "droneid",
      count: 0, infp: 0, cryp: 0, lastTime: null, bestRssi: -999,
      channels: new Map(), packets: [], trail: [],
    });
  }
  const s = sessions.get(key);
  if (s.mode !== mode && mode) s.mode = mode;
  return s;
}

function onRemoteId(evt) {
  const mode = evt.mode || (statusMode === "replay" ? "replay" : "live");
  const s = sess("rid:" + evt.session, mode);
  s.count++;
  s.lastTime = evt.time;
  s.selfid = evt.selfid;
  s.opLat = evt.op_lat; s.opLon = evt.op_lon;
  s.pos = { lat: evt.lat, lon: evt.lon, alt: evt.alt, speed: evt.speed, status: evt.status };
  s.trail.push([evt.lat, evt.lon]);
  if (s.trail.length > 300) s.trail.shift();
  s.packets.unshift({ time: evt.time, freq: evt.freq || 2437, marker: "RemoteID", rssi: evt.rssi || 0 });
  s.packets = s.packets.slice(0, 50);
  renderAll();
  renderMap();
  if (selectedKey === s.key) renderInfo();
}

function onDetection(evt) {
  if (!evt.crc) return;
  const mode = evt.mode || (statusMode === "replay" ? "replay" : "live");
  const s = sess("djiid:" + evt.session, mode);
  s.count++;
  if (evt.marker === "CRYP") s.cryp++; else s.infp++;
  s.lastTime = evt.time;
  if (evt.rssi > s.bestRssi) s.bestRssi = evt.rssi;
  s.channels.set(evt.freq, (s.channels.get(evt.freq) || 0) + 1);
  s.packets.unshift({ time: evt.time, freq: evt.freq, marker: evt.marker, rssi: evt.rssi });
  s.packets = s.packets.slice(0, 50);
  renderAll();
  renderMap();
  if (selectedKey === s.key) renderInfo();
}

/* ---- aircraft grouping: merge rid + djiid sessions active together ---- */
function aircraftList() {
  const rids = [], djiids = [];
  for (const s of sessions.values()) (s.source === "remoteid" ? rids : djiids).push(s);
  const list = [];
  const used = new Set();
  for (const rid of rids) {
    const entry = { rid, djiids: [], lastTime: rid.lastTime };
    for (const dj of djiids) {
      if (used.has(dj.key) || dj.mode !== rid.mode) continue;
      if (Math.abs(tsec(dj.lastTime) - tsec(rid.lastTime)) < 180000) {
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
function renderAll() {
  renderSidebar();
  renderBanner();
}

function renderBanner() {
  const banner = $("banner");
  if (statusMode === "replay") {
    banner.textContent = "Replay — archived flight session";
    banner.classList.remove("hidden");
    banner.classList.add("replay");
    return;
  }
  banner.classList.remove("replay");
  const list = aircraftList();
  const fresh = list.some((e) => e.last && Date.now() - e.last < 15000 && e.last > 0);
  banner.textContent = "Drone detected — identification active";
  banner.classList.toggle("hidden", !fresh);
}

function renderSidebar() {
  const list = aircraftList();
  $("session-count").textContent = list.length;
  const el = $("alert-list");
  el.innerHTML = "";
  for (const e of list) {
    const id = entryId(e);
    const rid = e.rid, dj = e.djiids[0];
    const div = document.createElement("div");
    div.className = "alert" + (id === selectedKey ? " selected" : "");
    let badges = "";
    if (rid) badges += '<span class="tag tag-rid">RemoteID</span> ';
    if (dj) badges += '<span class="tag">DroneID</span>';
    if (e.djiids[0] && e.djiids[0].mode === "replay") badges += ' <span class="tag tag-replay">replay</span>';
    const ident = rid ? rid.key.slice(4) : dj.key.slice(6);
    const metaParts = [];
    if (rid) metaParts.push(`RID ${rid.count} fixes`);
    if (dj) metaParts.push(`DroneID ${dj.count} pkts (${dj.infp} INFP / ${dj.cryp} CRYP)`);
    const lastIso = rid && (!dj || tsec(rid.lastTime) > tsec(dj.lastTime)) ? rid.lastTime : dj.lastTime;
    metaParts.push(`last ${fmtTime(lastIso)}`);
    div.innerHTML = `
      <div class="row1"><span class="model">DJI ${rid ? "Avata 2" : "Drone (OcuSync)"}</span><span>${badges}</span></div>
      <code>${ident}</code>
      <div class="meta">${metaParts.join(" · ")}</div>`;
    div.onclick = () => { selectedKey = id; renderSidebar(); renderInfo(); };
    el.appendChild(div);
  }
}

function placeStation() {
  stationMarker = L.marker([station.lat, station.lon], {
    icon: L.divIcon({
      className: "station-icon",
      html: `<svg width="26" height="26" viewBox="0 0 26 26">
        <circle cx="13" cy="13" r="12" fill="rgba(63,185,80,.15)"/>
        <path d="M13 5v4M13 17v4M5 13h4M17 13h4M8 8l2.5 2.5M15.5 15.5L18 18M18 8l-2.5 2.5M10.5 15.5L8 18"
          stroke="#3fb950" stroke-width="1.6"/>
        <circle cx="13" cy="13" r="2.2" fill="#3fb950"/></svg>`,
      iconSize: [26, 26], iconAnchor: [13, 13],
    }),
  }).addTo(map).bindPopup(`<b>${station.name}</b><br>DroneID sensor station`);
  map.setView([station.lat, station.lon], 15);
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
    if (s.source !== "remoteid" || !s.packets.length) continue;
    if (!s.trailLine) {
      s.trailLine = L.polyline([], { color: "#d29922", weight: 2, opacity: 0.8 }).addTo(map);
      s.droneDot = L.circleMarker([s.pos.lat, s.pos.lon], {
        radius: 7, color: "#f85149", fillColor: "#f85149", fillOpacity: 0.9, weight: 2,
      }).addTo(map).bindTooltip("Drone — Remote ID");
      if (s.opLat && s.opLon) {
        s.opDot = L.circleMarker([s.opLat, s.opLon], {
          radius: 6, color: "#3fb950", fillColor: "#3fb950", fillOpacity: 0.9, weight: 2,
        }).addTo(map).bindTooltip("Operator position");
        map.fitBounds(L.latLngBounds([[s.pos.lat, s.pos.lon], [s.opLat, s.opLon]]).pad(0.3));
      }
    }
    s.trailLine.setLatLngs(s.trail);
    s.droneDot.setLatLng([s.pos.lat, s.pos.lon]);
    s.droneDot.bindPopup(`<b>${s.key.slice(4)}</b><br>${Number(s.pos.lat).toFixed(5)}, ${Number(s.pos.lon).toFixed(5)}` +
      `<br>${Number(s.pos.alt).toFixed(1)} m${s.selfid ? `<br>${s.selfid}` : ""}`);
    if (s.opDot && s.opLat) s.opDot.setLatLng([s.opLat, s.opLon]);
  }
}

function renderInfo() {
  const e = aircraftList().find((x) => entryId(x) === selectedKey);
  if (!e) return;
  $("info-empty").classList.add("hidden");
  $("info-body").classList.remove("hidden");
  const rid = e.rid, dj = e.djiids[0];
  const ident = rid ? rid.key.slice(4) : dj.key.slice(6);
  $("i-model").textContent = rid ? "DJI Avata 2" : "DJI Drone (OcuSync)";
  $("i-marker").textContent = rid ? "RemoteID + DroneID" : "DroneID";
  const kv = [];
  kv.push(["Identifier", `<code>${ident}</code>`]);
  if (dj) {
    kv.push(["DroneID session", `<code>${dj.key.slice(6)}</code>`]);
    kv.push(["DroneID packets", `${dj.count} (${dj.infp} INFP / ${dj.cryp} CRYP)`]);
    kv.push(["Channels", [...dj.channels.entries()].sort((a, b) => b[1] - a[1]).map(([c, n]) => `${c.toFixed(1)}×${n}`).join("  ")]);
    kv.push(["Best signal", dj.bestRssi > -900 ? dj.bestRssi.toFixed(1) + " dBFS" : "—"]);
    kv.push(["O4 telemetry", "encrypted (AES-CTR)"]);
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
    const running = $("btn-scan").textContent.startsWith("Stop");
    await fetch(running ? "/api/scan/stop" : "/api/scan/start", { method: "POST" });
  };
  $("btn-replay").onclick = async () => { await fetch("/api/replay/start", { method: "POST" }); };
  connect();
}

document.querySelectorAll("[data-coming]").forEach((el) =>
  el.addEventListener("click", () => alert("Coming soon")));

init();
