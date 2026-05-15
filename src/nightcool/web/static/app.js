// NightCool PWA. Plain ES modules; no framework.
const $ = (id) => document.getElementById(id);

const PROFILE_HINTS = {
  commuter: "Skips weekday morning CLOSE — you handle that on the way out.",
  wfh: "Pings for midday cooling windows too.",
  night_shift: "Cools the house in the morning before you get home.",
  light_sleeper: "Holds overnight OPENs for a morning summary.",
  aggressive: "Opens for any 1°F edge.",
  conservative: "Only alerts on sustained 3+ hour cool spells.",
  custom: "Behavior follows the explicit flags in config.yaml.",
};

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
  $("profile-select").value = s.profile;
  $("profile-hint").textContent = PROFILE_HINTS[s.profile] || "";

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

  const tbody = $("forecast-table").querySelector("tbody");
  tbody.innerHTML = "";
  for (const h of s.forecast_head) {
    const tr = document.createElement("tr");
    const t = new Date(h.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    tr.innerHTML = `<td>${t}</td><td>${h.temp_f.toFixed(0)}</td><td>${h.wind_mph.toFixed(0)} mph</td><td>${h.rain_pct.toFixed(0)}%</td>`;
    tbody.appendChild(tr);
  }
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

$("profile-select").addEventListener("change", async (e) => {
  const profile = e.target.value;
  const r = await fetch("/api/profile", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ profile }),
  });
  if (!r.ok) {
    setStatus("Profile change refused (server has no config path).");
    return;
  }
  $("profile-hint").textContent = PROFILE_HINTS[profile] || "";
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

// Reflect existing subscription, if any.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.getRegistration().then(async (reg) => {
    if (!reg) return;
    const sub = await reg.pushManager.getSubscription();
    if (sub) $("push-status").textContent = "Subscribed.";
  });
}

refreshState();
refreshSavings();
setInterval(refreshState, 5 * 60 * 1000);
setInterval(refreshSavings, 30 * 60 * 1000);
