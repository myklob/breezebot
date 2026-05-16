// NightCool PWA. Plain ES modules; no framework.
const $ = (id) => document.getElementById(id);
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_LABELS = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };

async function refreshState() {
  try {
    const r = await fetch("/api/state");
    if (!r.ok) throw new Error(`/api/state ${r.status}`);
    const s = await r.json();
    renderState(s);
    setStatus(`Updated ${new Date().toLocaleTimeString()}`);
  } catch (e) {
    setStatus(`Error: ${e.message}`);
  }
}

function renderState(s) {
  $("indoor-readout").textContent = `${s.indoor_f.toFixed(1)}°F`;
  $("indoor-input").placeholder = s.indoor_f.toFixed(1);
  $("source-note").textContent = `Source: ${s.indoor_source}`;
  $("address-input").value = s.location.address || "";
  if (s.location.address) {
    $("address-note").textContent = s.location.latitude
      ? `Resolved to (${s.location.latitude.toFixed(4)}, ${s.location.longitude.toFixed(4)}).`
      : "Used to look up your local NWS forecast.";
  }

  const rec = s.recommendation;
  $("action-title").textContent = rec.title || "No action";
  $("action-body").textContent = rec.body || "";
  $("action-windows").textContent = rec.windows && rec.windows.length
    ? `Windows: ${rec.windows.join(", ")}`
    : "";
  const times = [];
  if (rec.open_at) times.push(`Open ${new Date(rec.open_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`);
  if (rec.close_at) times.push(`Close ${new Date(rec.close_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`);
  $("action-times").textContent = times.join(" → ");

  const wl = $("action-warnings");
  wl.innerHTML = "";
  for (const w of rec.warnings || []) {
    const li = document.createElement("li");
    li.textContent = w;
    wl.appendChild(li);
  }

  const tbody = $("forecast-table").querySelector("tbody");
  tbody.innerHTML = "";
  for (const h of s.forecast_head) {
    const tr = document.createElement("tr");
    const t = new Date(h.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    tr.innerHTML = `<td>${t}</td><td>${h.temp_f.toFixed(0)}</td><td>${h.wind_mph.toFixed(0)} mph</td><td>${h.rain_pct.toFixed(0)}%</td>`;
    tbody.appendChild(tr);
  }
}

async function refreshSchedule() {
  const r = await fetch("/api/schedule");
  if (!r.ok) return;
  const sched = await r.json();
  const tbody = $("schedule-table").querySelector("tbody");
  tbody.innerHTML = "";
  for (const day of DAYS) {
    const d = sched[day];
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${DAY_LABELS[day]}</td>
      <td><input type="number" data-day="${day}" data-field="target_f" value="${d.target_f}" min="50" max="85" step="1" /></td>
      <td><input type="time" data-day="${day}" data-field="leave_at" value="${d.leave_at || ''}" ${d.home_all_day ? 'disabled' : ''} /></td>
      <td><input type="checkbox" data-day="${day}" data-field="home_all_day" ${d.home_all_day ? 'checked' : ''} /></td>
    `;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll("input").forEach((el) => {
    el.addEventListener("change", onScheduleEdit);
  });
}

async function onScheduleEdit(e) {
  const el = e.target;
  const day = el.dataset.day;
  const field = el.dataset.field;
  const row = el.closest("tr");
  const payload = {};
  if (field === "target_f") payload.target_f = parseFloat(el.value);
  if (field === "leave_at") payload.leave_at = el.value || "";
  if (field === "home_all_day") {
    payload.home_all_day = el.checked;
    if (el.checked) row.querySelector('[data-field="leave_at"]').disabled = true;
    else row.querySelector('[data-field="leave_at"]').disabled = false;
  }
  await fetch(`/api/schedule/${day}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await refreshState();
}

async function refreshSavings() {
  try {
    const r = await fetch("/api/savings");
    const s = await r.json();
    if (!s.model) {
      $("savings-readout").textContent = s.reason || "Collecting data…";
      return;
    }
    $("savings-readout").textContent =
      `$${s.dollars_saved.toFixed(2)} saved, ${s.kwh_saved.toFixed(1)} kWh ` +
      `(R²=${s.model.r_squared.toFixed(2)}, ${s.model.sample_count} samples)`;
  } catch (e) {
    $("savings-readout").textContent = "Savings unavailable.";
  }
}

function setStatus(text) { $("status-line").textContent = text; }

$("indoor-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const temp = parseFloat($("indoor-input").value);
  if (Number.isNaN(temp)) return;
  await fetch("/api/indoor-temp", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ temperature_f: temp }),
  });
  $("indoor-input").value = "";
  await refreshState();
});

$("address-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const address = $("address-input").value.trim();
  if (!address) return;
  $("address-note").textContent = "Resolving…";
  const r = await fetch("/api/geocode", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ address }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: "geocode failed" }));
    $("address-note").textContent = err.detail || "Geocode failed.";
    return;
  }
  const data = await r.json();
  $("address-note").textContent = `Resolved to (${data.latitude.toFixed(4)}, ${data.longitude.toFixed(4)}).`;
  await refreshState();
});

// ---- Push subscription ----

function urlBase64ToUint8Array(base64) {
  const padding = "=".repeat((4 - base64.length % 4) % 4);
  const b64 = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

async function enablePush() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    $("push-status").textContent = "This browser doesn't support web push.";
    return;
  }
  const reg = await navigator.serviceWorker.register("/sw.js");
  await navigator.serviceWorker.ready;

  const perm = await Notification.requestPermission();
  if (perm !== "granted") {
    $("push-status").textContent = "Permission denied.";
    return;
  }

  const pkResp = await fetch("/api/vapid-public");
  if (!pkResp.ok) {
    $("push-status").textContent = "Server has no VAPID key configured.";
    return;
  }
  const { public_key } = await pkResp.json();

  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(public_key),
  });

  await fetch("/api/subscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sub.toJSON()),
  });
  $("push-status").textContent = "Subscribed.";
}

$("enable-push").addEventListener("click", enablePush);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.getRegistration().then(async (reg) => {
    if (!reg) return;
    const sub = await reg.pushManager.getSubscription();
    if (sub) $("push-status").textContent = "Subscribed.";
  });
}

refreshState();
refreshSchedule();
refreshSavings();
setInterval(refreshState, 5 * 60 * 1000);
setInterval(refreshSavings, 30 * 60 * 1000);
