/* Drone Watchtower — live detection frontend */
const $ = (id) => document.getElementById(id);
let map, stationMarker, activityRing;
let station = null;
let selectedSession = null;
let latestDetections = [];

const sessions = new Map(); // hash -> {count, infp, cryp, lastTime, bestRssi, channels: Map, packets: []}

function fmtTime(iso) {
  return iso ? iso.slice(11, 23) : "--";
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
  station = st.station;
  const pill = $("status-pill");
  pill.textContent = st.running ? `${st.mode} · ${st.events} pkts` : "idle";
  pill.className = "pill " + (st.running ? (st.mode === "live" ? "live" : "ok") : "");
  $("btn-scan").textContent = st.running && st.mode === "live" ? "Stop Scan" : "Start Scan";
  $("btn-replay").disabled = st.running;
  $("btn-scan").disabled = st.running && st.mode === "replay";
  if (map && station && !stationMarker) placeStation();
}

function onRemoteId(evt) {
  const s = sessions.get(evt.session) || {
    source: "remoteid", count: 0, infp: 0, cryp: 0, lastTime: null,
    bestRssi: 0, channels: new Map(), packets: [], trail: [],
  };
  s.count++;
  s.source = "remoteid";
  s.lastTime = evt.time;
  s.selfid = evt.selfid;
  s.opLat = evt.op_lat;
  s.opLon = evt.op_lon;
  s.pos = { lat: evt.lat, lon: evt.lon, alt: evt.alt, speed: evt.speed, status: evt.status };
  s.trail.push([evt.lat, evt.lon]);
  if (s.trail.length > 300) s.trail.shift();
  s.packets.unshift({ time: evt.time, freq: 2437, marker: "RemoteID", rssi: 0, lat: evt.lat, lon: evt.lon });
  s.packets = s.packets.slice(0, 50);
  sessions.set(evt.session, s);
  renderSidebar();
  renderRemoteIdMap(s, evt);
  renderBannerRemoteId(s);
  if (selectedSession === evt.session) renderInfo();
}

function renderBannerRemoteId(s) {
  const fresh = s.lastTime && Date.now() - Date.parse(s.lastTime) < 15000;
  $("banner").classList.toggle("hidden", !fresh);
}

function renderRemoteIdMap(s, evt) {
  if (!map) return;
  if (!s.trailLine) {
    s.trailLine = L.polyline([], { color: "#d29922", weight: 2, opacity: 0.8 }).addTo(map);
    s.droneDot = L.circleMarker([evt.lat, evt.lon], {
      radius: 7, color: "#f85149", fillColor: "#f85149", fillOpacity: 0.9, weight: 2,
    }).addTo(map).bindTooltip("Drone — Remote ID");
    if (s.opLat && s.opLon) {
      s.opDot = L.circleMarker([s.opLat, s.opLon], {
        radius: 6, color: "#3fb950", fillColor: "#3fb950", fillOpacity: 0.9, weight: 2,
      }).addTo(map).bindTooltip("Operator position");
      map.fitBounds(L.latLngBounds([[evt.lat, evt.lon], [s.opLat, s.opLon]]).pad(0.3));
    }
  }
  s.trailLine.setLatLngs(s.trail);
  s.droneDot.setLatLng([evt.lat, evt.lon]);
  s.droneDot.bindPopup(
    `<b>${evt.session}</b><br>${Number(evt.lat).toFixed(5)}, ${Number(evt.lon).toFixed(5)}` +
    `<br>${Number(evt.alt).toFixed(1)} m · ${evt.status || ""}` +
    (evt.selfid ? `<br>${evt.selfid}` : ""));
  if (s.opDot && s.opLat) s.opDot.setLatLng([s.opLat, s.opLon]);
}

function onDetection(evt) {
  if (!evt.crc) return;
  const s = sessions.get(evt.session) || {
    count: 0, infp: 0, cryp: 0, lastTime: null, bestRssi: 0, channels: new Map(), packets: [],
  };
  s.count++;
  if (evt.marker === "CRYP") s.cryp++; else s.infp++;
  s.lastTime = evt.time;
  if (evt.rssi > s.bestRssi) s.bestRssi = evt.rssi;
  s.channels.set(evt.freq, (s.channels.get(evt.freq) || 0) + 1);
  s.packets.unshift(evt);
  s.packets = s.packets.slice(0, 50);
  sessions.set(evt.session, s);
  latestDetections.unshift(evt);
  latestDetections = latestDetections.slice(0, 300);
  renderSidebar();
  renderMap(evt);
  renderBanner();
  if (selectedSession === evt.session) renderInfo();
}

function renderBanner() {
  const recent = latestDetections[0];
  const fresh = recent && Date.now() - Date.parse(recent.time + "Z") < 15000;
  $("banner").classList.toggle("hidden", !fresh);
}

function renderSidebar() {
  const list = $("alert-list");
  $("session-count").textContent = sessions.size;
  const sorted = [...sessions.entries()].sort((a, b) =>
    Date.parse(b[1].lastTime) - Date.parse(a[1].lastTime));
  list.innerHTML = "";
  for (const [hash, s] of sorted) {
    const rid = s.source === "remoteid";
    const div = document.createElement("div");
    div.className = "alert" + (hash === selectedSession ? " selected" : "");
    const model = rid ? "DJI Drone" : "DJI Avata 2";
    const tag = rid ? "RemoteID" : "DroneID";
    const meta = rid
      ? `<b>${s.count}</b> fixes · ${s.pos ? Number(s.pos.alt).toFixed(1) + " m · " : ""}last ${fmtTime(s.lastTime)}`
      : `<b>${s.count}</b> pkts · ${s.infp} INFP / ${s.cryp} CRYP · last ${fmtTime(s.lastTime)}`;
    div.innerHTML = `
      <div class="row1"><span class="model">${model}</span><span class="tag">${tag}</span></div>
      <code>${hash}</code>
      <div class="meta">${meta}</div>`;
    div.onclick = () => { selectedSession = hash; renderSidebar(); renderInfo(); };
    list.appendChild(div);
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

function renderMap(evt) {
  if (!map || !station) return;
  // proximity ring from RSSI (stronger = closer); rough dBFS mapping
  if (activityRing) activityRing.remove();
  const rM = Math.max(60, Math.min(900, 60000 * Math.pow(2, (evt.rssi + 50) / 10)));
  activityRing = L.circle([station.lat, station.lon], {
    radius: rM, color: "#f85149", weight: 1, fillOpacity: 0.08,
  }).addTo(map);

  // event dot: jittered around station so clusters are visible
  const jitter = () => (Math.random() - 0.5) * 0.0016;
  const color = evt.marker === "CRYP" ? "#d29922" : "#f85149";
  L.circleMarker([station.lat + jitter(), station.lon + jitter()], {
    radius: evt.marker === "CRYP" ? 6 : 4, color, fillColor: color,
    fillOpacity: 0.85, weight: 1,
  }).addTo(map)
    .bindPopup(`<b>${evt.marker}</b> · session ${evt.session}<br>${evt.freq} MHz · ${evt.rssi} dBFS<br>${evt.time.slice(11, 19)} UTC`)
    .on("click", () => { selectedSession = evt.session; renderSidebar(); renderInfo(); });
}

function renderInfo() {
  const s = sessions.get(selectedSession);
  if (!s) return;
  $("info-empty").classList.add("hidden");
  $("info-body").classList.remove("hidden");
  $("i-session").textContent = selectedSession;
  $("i-count").textContent = s.count;
  if (s.source === "remoteid") {
    $("i-mix").textContent = "Remote ID (plaintext)";
    $("i-rssi").textContent = "—";
    $("i-channels").textContent = "WiFi NAN · ch 6";
    $("i-state").textContent = "position decoded (F3411)";
    $("i-state").className = "ok";
    const pos = s.pos ? `${Number(s.pos.lat).toFixed(6)}, ${Number(s.pos.lon).toFixed(6)}` : "—";
    const op = s.opLat ? `${Number(s.opLat).toFixed(6)}, ${Number(s.opLon).toFixed(6)}` : "—";
    const alt = s.pos ? `${Number(s.pos.alt).toFixed(1)} m · ${s.pos.status || ""}` : "—";
    const rows = [["i-pos", pos], ["i-op", op], ["i-alt", alt]];
    for (const [id, v] of rows) {
      let el = $(id);
      if (!el) {
        el = document.createElement("div");
        el.className = "kv";
        el.id = id;
        el.innerHTML = `<span>${id === "i-pos" ? "Drone position" : id === "i-op" ? "Operator position" : "Altitude / status"}</span><span></span>`;
        $("i-state").insertAdjacentElement("afterend", el);
      }
      el.lastElementChild.textContent = v;
      el.classList.remove("hidden");
    }
  $("i-marker").textContent = "RemoteID";
  } else {
    $("i-mix").textContent = `${s.infp} / ${s.cryp}`;
    $("i-rssi").textContent = s.bestRssi.toFixed(1) + " dBFS";
    $("i-state").textContent = "telemetry encrypted (O4)";
    $("i-state").className = "ok";
    for (const id of ["i-pos", "i-op", "i-alt"]) {
      const el = $(id);
      if (el) el.classList.add("hidden");
    }
    $("i-marker").textContent = "DroneID";
    const chans = [...s.channels.entries()].sort((a, b) => b[1] - a[1])
      .map(([c, n]) => `${c.toFixed(1)}×${n}`).join("  ");
    $("i-channels").textContent = chans;
  }
  $("i-last").textContent = fmtTime(s.lastTime) + " UTC";
  const body = $("pkt-body");
  body.innerHTML = "";
  for (const p of s.packets.slice(0, 20)) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${fmtTime(p.time)}</td><td>${p.freq.toFixed(1)}</td>` +
      `<td class="${p.marker === "CRYP" ? "cryp" : ""}">${p.marker}</td><td>${p.rssi.toFixed(1)}</td>`;
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
  map.setView([22.3193, 114.1694], 12);

  $("btn-scan").onclick = async () => {
    const running = $("btn-scan").textContent.startsWith("Stop");
    await fetch(running ? "/api/scan/stop" : "/api/scan/start", { method: "POST" });
  };
  $("btn-replay").onclick = async () => {
    await fetch("/api/replay/start", { method: "POST" });
  };
  connect();
}

document.querySelectorAll("[data-coming]").forEach(el =>
  el.addEventListener("click", () => alert("Coming soon")));

init();
