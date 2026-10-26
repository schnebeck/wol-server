"use strict";

// ---------- Hilfsfunktionen ----------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const DAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
const ACTIONS = { wake: "Aufwecken", sleep: "Schlafen legen", net: "Änderung" };
const SLEEP_METHODS = { none: "–", ssh: "SSH", http: "HTTP", sol: "Sleep-on-LAN" };
const STATUS_INTERVAL = 10000;

const state = {
  view: "devices",
  devices: [],
  schedules: [],
  status: {},
  serverNow: null,
  authRequired: false,
  hotspot: false,
  info: null,
  wifi: null,
};

const SVG_NS = "http://www.w3.org/2000/svg";
const ICONS = {
  power: ["M12 3v8", "M6.6 6.6a7.5 7.5 0 1 0 10.8 0"],
  moon: ["M20 14.2A8 8 0 0 1 9.8 4a8 8 0 1 0 10.2 10.2z"],
  edit: ["M4 20h4L19 9l-4-4L4 16v4z", "M13.5 6.5l4 4"],
  clock: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M12 7.5V12l3 2"],
  check: ["M5 12.5l4.5 4.5L19 7.5"],
  x: ["M6 6l12 12", "M18 6L6 18"],
  plus: ["M12 5v14", "M5 12h14"],
  lock: ["M7.5 11V8a4.5 4.5 0 0 1 9 0v3", "M5.5 11h13v9.5h-13z"],
  alert: ["M12 4l9 16H3z", "M12 10v4.5", "M12 17.5v.01"],
  info: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M12 11v5.5", "M12 7.5v.01"],
  device: ["M4 5h16v11H4z", "M9 20h6", "M12 16v4"],
  undo: ["M9 14L4 9l5-5", "M4 9h10a6 6 0 0 1 0 12h-3"],
};

function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  for (const d of ICONS[name]) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === false || value == null) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(method, path, body) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET") opts.headers["X-Requested-With"] = "wol-server";
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new HttpError(0, "Server nicht erreichbar");
  }
  let data = null;
  try {
    data = await res.json();
  } catch (e) {
    data = null;
  }
  if (res.status === 401 && path !== "/api/login") showLogin();
  if (!res.ok && !(data && "ok" in data)) {
    throw new HttpError(res.status, (data && data.error) || `Fehler ${res.status}`);
  }
  return data;
}

function toast(message, kind = "info") {
  const node = el("div", { class: `toast ${kind}`, role: "status" },
    kind === "error" ? icon("x") : icon("check"), el("span", {}, message));
  $("#toasts").append(node);
  setTimeout(() => node.classList.add("leaving"), 4200);
  setTimeout(() => node.remove(), 4600);
}

function confirmDialog(title, text, okLabel = "OK", danger = false) {
  const dlg = $("#confirm-dialog");
  $("#confirm-title").textContent = title;
  // Text oder Liste (z. B. die Änderungen vor dem Übernehmen)
  $("#confirm-text").replaceChildren(Array.isArray(text) ? el("ul", { class: "change-list" }, text.map((t) => el("li", {}, t))) : text);
  const ok = $("#confirm-ok");
  ok.textContent = okLabel;
  ok.className = danger ? "btn danger" : "btn primary";
  dlg.returnValue = "";
  dlg.showModal();
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
  });
}

function showError(form, message) {
  const box = $(".form-error", form);
  box.textContent = message || "";
  box.hidden = !message;
}

async function busy(button, fn) {
  button.disabled = true;
  button.classList.add("busy");
  try {
    return await fn();
  } finally {
    button.disabled = false;
    button.classList.remove("busy");
  }
}

// Server liefert lokale Zeit ohne Zone ("2026-10-24T07:30"); Vergleich daher über Strings.
function fmtNext(iso) {
  if (!iso) return "";
  const [date, time] = iso.split("T");
  const today = (state.serverNow || "").slice(0, 10);
  if (date === today) return `heute ${time}`;
  const d = new Date(`${date}T00:00:00`);
  const t = new Date(`${today}T00:00:00`);
  const diff = Math.round((d - t) / 86400000);
  if (diff === 1) return `morgen ${time}`;
  return `${DAYS[(d.getDay() + 6) % 7]}, ${d.getDate()}.${d.getMonth() + 1}. ${time}`;
}

function fmtDateTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleString("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function fmtDuration(seconds) {
  if (seconds == null) return "–";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
}

function fmtDays(days) {
  const key = days.join(",");
  if (key === "0,1,2,3,4,5,6") return "täglich";
  if (key === "0,1,2,3,4") return "werktags";
  if (key === "5,6") return "am Wochenende";
  return days.map((d) => DAYS[d]).join(", ");
}

const deviceById = (id) => state.devices.find((d) => d.id === id);

// ---------- Anmeldung ----------

function showLogin() {
  $("#boot").hidden = true;
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#login-form input[name=password]").focus();
}

async function onLogin(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const password = form.password.value;
  if (!password) return;
  await busy($("button[type=submit]", form), async () => {
    try {
      await api("POST", "/api/login", { password });
      form.reset();
      $("#login-error").hidden = true;
      await startApp();
    } catch (e) {
      $("#login-error").textContent = e.message;
      $("#login-error").hidden = false;
    }
  });
}

async function onLogout() {
  await api("POST", "/api/logout");
  showLogin();
}

// ---------- Navigation ----------

function setView(view) {
  state.view = view;
  for (const tab of $$(".tab")) tab.classList.toggle("active", tab.dataset.view === view);
  for (const section of $$(".view")) section.hidden = section.id !== `view-${view}`;
  history.replaceState(null, "", view === "network" ? `#network/${net.sub}` : `#${view}`);
  refreshView().catch((e) => toast(e.message, "error"));
}

async function refreshView() {
  if (state.view === "devices") {
    await Promise.all([loadDevices(), loadSchedules()]);
    renderDevices();
    refreshStatus();
  } else if (state.view === "schedules") {
    await Promise.all([loadDevices(), loadSchedules()]);
    renderSchedules();
  } else if (state.view === "events") {
    await renderEvents();
  } else if (state.view === "network") {
    await renderNetwork();
  } else if (state.view === "system") {
    await renderSystem();
  }
}

async function loadDevices() {
  state.devices = (await api("GET", "/api/devices")).devices;
}

async function loadSchedules() {
  const data = await api("GET", "/api/schedules");
  state.schedules = data.schedules;
  state.serverNow = data.now;
}

// ---------- Geräte ----------

let statusBusy = false;

async function refreshStatus() {
  if (statusBusy || !state.devices.length) return;
  statusBusy = true;
  try {
    const data = await api("GET", "/api/status");
    state.status = data.status;
    const online = Object.values(data.status).filter((s) => s === true).length;
    $("#status-line").textContent =
      `${online} von ${state.devices.length} online · geprüft ${new Date(data.checked * 1000).toLocaleTimeString("de-DE")}`;
    renderDevices();
  } catch (e) {
    if (e.status !== 401) $("#status-line").textContent = e.message;
  } finally {
    statusBusy = false;
  }
}

function nextActionFor(deviceId) {
  return state.schedules
    .filter((s) => s.device_id === deviceId && s.next_run)
    .sort((a, b) => a.next_run.localeCompare(b.next_run))[0];
}

function renderDevices() {
  const list = $("#device-list");
  if (!state.devices.length) {
    $("#status-line").textContent = " ";
    list.replaceChildren(el("div", { class: "empty" },
      el("h3", {}, "Keine Geräte"),
      el("p", { class: "muted" }, "Geräte werden über ihre MAC-Adresse per Wake-on-LAN aufgeweckt."),
      el("button", { class: "btn primary", type: "button", onclick: () => openDeviceDialog() }, "Erstes Gerät anlegen")));
    return;
  }
  list.replaceChildren(...state.devices.map(deviceCard));
}

function deviceCard(d) {
  const st = state.status[d.id];
  const cls = st === true ? "online" : st === false ? "offline" : "unknown";
  const label = st === true ? "Online" : st === false ? "Offline" : d.host ? "Prüfe …" : "Kein Host";
  const next = nextActionFor(d.id);
  const canSleep = d.sleep_method !== "none";
  return el("article", { class: `card device ${cls}` },
    el("header", { class: "card-head" },
      el("span", { class: `dot ${cls}`, "aria-hidden": "true" }),
      el("div", { class: "card-title" },
        el("h3", {}, d.name),
        el("span", { class: `status-label ${cls}` }, label)),
      el("button", { class: "icon-btn", type: "button", title: "Bearbeiten", "aria-label": `${d.name} bearbeiten`, onclick: () => openDeviceDialog(d) }, icon("edit"))),
    el("dl", { class: "meta" },
      el("div", {}, el("dt", {}, "MAC"), el("dd", { class: "mono" }, d.mac)),
      el("div", {}, el("dt", {}, "Host"), el("dd", { class: "mono" }, d.host || "–")),
      el("div", {}, el("dt", {}, "Schlafen"), el("dd", {}, SLEEP_METHODS[d.sleep_method]))),
    el("p", { class: next ? "next" : "next muted" }, icon("clock"),
      next ? el("span", {}, el("strong", {}, ACTIONS[next.action]), " ", fmtNext(next.next_run)) : "Kein aktiver Zeitplan"),
    el("div", { class: "card-actions" },
      el("button", { class: "btn wake", type: "button", onclick: (e) => deviceAction(d, "wake", e.currentTarget) }, icon("power"), "Aufwecken"),
      el("button", {
        class: "btn sleep", type: "button", disabled: !canSleep,
        title: canSleep ? "" : "Keine Schlaf-Methode eingerichtet",
        onclick: (e) => deviceAction(d, "sleep", e.currentTarget),
      }, icon("moon"), "Schlafen")));
}

async function deviceAction(device, action, button) {
  if (action === "sleep" &&
      !(await confirmDialog(`${device.name} schlafen legen?`, "Nicht gespeicherte Arbeit auf dem Gerät kann verloren gehen.", "Schlafen legen"))) {
    return;
  }
  await busy(button, async () => {
    try {
      const res = await api("POST", `/api/devices/${device.id}/${action}`);
      toast(`${device.name}: ${res.message}`, res.ok ? "success" : "error");
    } catch (e) {
      toast(`${device.name}: ${e.message}`, "error");
    }
  });
  // Nach dem Wecken braucht ein PC meist 10–60 s – Status in kurzen Abständen nachziehen.
  for (const delay of [3000, 15000, 40000]) setTimeout(() => state.view === "devices" && refreshStatus(), delay);
}

let editingDevice = null;

function openDeviceDialog(device = null, prefill = {}) {
  editingDevice = device;
  const form = $("#device-form");
  form.reset();
  showError(form, "");
  const d = device || { broadcast: "255.255.255.255", wol_port: 9, sleep_method: "none", ssh_port: 22, ssh_command: "sudo systemctl suspend", http_method: "POST", ...prefill };
  for (const [key, value] of Object.entries(d)) {
    const field = form.elements[key];
    if (!field || field instanceof RadioNodeList) continue;
    field.value = value ?? "";
  }
  form.elements.ssh_port.value = d.ssh_port || 22;
  form.elements.ssh_command.value = d.ssh_command || "sudo systemctl suspend";
  setSleepMethod(d.sleep_method || "none");
  $("#device-dialog-title").textContent = device ? "Gerät bearbeiten" : "Neues Gerät";
  $("#delete-device").hidden = !device;
  $("#device-dialog").showModal();
  updateDeviceNetHint();
}

// Feste IP im Netzwerk (DHCP-Reservierung) zum Gerät anzeigen – nur wenn die Netzwerk-Verwaltung läuft.
async function updateDeviceNetHint() {
  const box = $("#device-net-hint");
  box.hidden = true;
  try {
    if (!net.config) await loadNetConfig();
  } catch (e) {
    return;
  }
  const mac = normMac($("#device-form").elements.mac.value);
  const res = mac && net.config.config.reservations.find((r) => r.mac === mac);
  const domain = net.config.config.dns.domain;
  box.replaceChildren(res
    ? el("span", {}, icon("check"), "Feste IP im Netzwerk: ", el("strong", { class: "mono" }, res.ip),
      res.name ? ` (${res.name}.${domain})` : "")
    : el("span", {}, "Keine feste IP im Netzwerk. ",
      el("button", { type: "button", class: "linkish", disabled: !mac, onclick: () => openResDialog(null, {
        mac, name: $("#device-form").elements.name.value }) }, "Reservieren …")));
  box.hidden = false;
}

function setSleepMethod(method) {
  const form = $("#device-form");
  for (const radio of form.querySelectorAll("input[name=sleep_method]")) radio.checked = radio.value === method;
  for (const panel of $$(".method-panel", form)) panel.hidden = panel.dataset.method !== method;
}

async function onDeviceSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  data.wol_port = Number(data.wol_port) || 9;
  data.ssh_port = Number(data.ssh_port) || 22;
  await busy($("button[type=submit]", form), async () => {
    try {
      if (editingDevice) await api("PUT", `/api/devices/${editingDevice.id}`, data);
      else await api("POST", "/api/devices", data);
      $("#device-dialog").close();
      toast(editingDevice ? "Gerät gespeichert" : "Gerät angelegt", "success");
      await refreshView();
    } catch (e) {
      showError(form, e.message);
    }
  });
}

async function onDeviceDelete() {
  const device = editingDevice;
  const count = state.schedules.filter((s) => s.device_id === device.id).length;
  const text = count ? `Das Gerät und seine Zeitpläne (${count}) werden gelöscht.` : "Das Gerät wird gelöscht.";
  if (!(await confirmDialog(`${device.name} löschen?`, text, "Löschen", true))) return;
  try {
    await api("DELETE", `/api/devices/${device.id}`);
    $("#device-dialog").close();
    toast("Gerät gelöscht", "success");
    await refreshView();
  } catch (e) {
    toast(e.message, "error");
  }
}

// ---------- Zeitpläne ----------

function renderSchedules() {
  const list = $("#schedule-list");
  $("#server-time").textContent = state.serverNow
    ? `Serverzeit: ${fmtNext(state.serverNow.slice(0, 16)).replace("heute ", "")} Uhr`
    : " ";
  if (!state.devices.length) {
    list.replaceChildren(el("div", { class: "empty" },
      el("h3", {}, "Keine Geräte"),
      el("p", { class: "muted" }, "Jeder Zeitplan gehört zu einem Gerät. Geräte werden im Reiter „Geräte“ angelegt.")));
    return;
  }
  if (!state.schedules.length) {
    list.replaceChildren(el("div", { class: "empty" },
      el("h3", {}, "Keine Zeitpläne"),
      el("p", { class: "muted" }, "Geräte automatisch zu festen Zeiten aufwecken oder schlafen legen."),
      el("button", { class: "btn primary", type: "button", onclick: () => openScheduleDialog() }, "Zeitplan anlegen")));
    return;
  }
  list.replaceChildren(...state.schedules.map(scheduleRow));
}

function scheduleRow(s) {
  const device = deviceById(s.device_id);
  return el("div", { class: `row sched ${s.enabled ? "" : "disabled"}` },
    el("label", { class: "switch", title: s.enabled ? "Deaktivieren" : "Aktivieren" },
      el("input", { type: "checkbox", checked: s.enabled, "aria-label": "Aktiv", onchange: (e) => toggleSchedule(s, e.currentTarget) }),
      el("span", { class: "slider" })),
    el("div", { class: "sched-time mono" }, s.time),
    el("div", { class: "sched-main" },
      el("div", { class: "sched-title" },
        el("span", { class: `badge ${s.action}` }, s.action === "wake" ? icon("power") : icon("moon"), ACTIONS[s.action]),
        el("span", { class: "sched-device" }, device ? device.name : "?")),
      el("div", { class: "muted small" }, [fmtDays(s.days), s.label].filter(Boolean).join(" · "))),
    el("div", { class: "days", "aria-label": fmtDays(s.days) },
      DAYS.map((name, i) => el("span", { class: s.days.includes(i) ? "day on" : "day" }, name))),
    el("div", { class: "sched-next small" },
      s.enabled && s.next_run ? [el("span", { class: "muted" }, "Nächste: "), fmtNext(s.next_run)] : el("span", { class: "muted" }, "pausiert")),
    el("button", { class: "icon-btn", type: "button", title: "Bearbeiten", "aria-label": "Zeitplan bearbeiten", onclick: () => openScheduleDialog(s) }, icon("edit")));
}

async function toggleSchedule(schedule, input) {
  try {
    await api("PUT", `/api/schedules/${schedule.id}`, { ...schedule, enabled: input.checked });
    await loadSchedules();
    renderSchedules();
  } catch (e) {
    input.checked = !input.checked;
    toast(e.message, "error");
  }
}

let editingSchedule = null;

function buildDayPicker() {
  $("#daypicker").replaceChildren(...DAYS.map((name, i) =>
    el("label", { class: "daybox" }, el("input", { type: "checkbox", name: "days", value: String(i) }), el("span", {}, name))));
}

function setDays(days) {
  for (const box of $$("#daypicker input")) box.checked = days.includes(Number(box.value));
}

function openScheduleDialog(schedule = null) {
  editingSchedule = schedule;
  const form = $("#schedule-form");
  form.reset();
  showError(form, "");
  const select = form.elements.device_id;
  select.replaceChildren(...state.devices.map((d) => el("option", { value: d.id }, d.name)));
  const s = schedule || { device_id: state.devices[0]?.id, action: "wake", time: "07:00", days: [0, 1, 2, 3, 4], enabled: true, label: "" };
  select.value = s.device_id;
  for (const radio of form.querySelectorAll("input[name=action]")) radio.checked = radio.value === s.action;
  form.elements.time.value = s.time;
  form.elements.label.value = s.label || "";
  form.elements.enabled.checked = s.enabled;
  setDays(s.days);
  updateSleepAvailability();
  $("#schedule-dialog-title").textContent = schedule ? "Zeitplan bearbeiten" : "Neuer Zeitplan";
  $("#delete-schedule").hidden = !schedule;
  $("#schedule-dialog").showModal();
}

function updateSleepAvailability() {
  const form = $("#schedule-form");
  const device = deviceById(form.elements.device_id.value);
  const sleepRadio = form.querySelector("input[name=action][value=sleep]");
  const canSleep = device && device.sleep_method !== "none";
  sleepRadio.disabled = !canSleep;
  sleepRadio.closest("label").title = canSleep ? "" : "Für dieses Gerät ist keine Schlaf-Methode eingerichtet";
  if (!canSleep && sleepRadio.checked) form.querySelector("input[name=action][value=wake]").checked = true;
}

async function onScheduleSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = {
    device_id: form.elements.device_id.value,
    action: (form.querySelector("input[name=action]:checked") || {}).value,
    time: form.elements.time.value,
    label: form.elements.label.value,
    enabled: form.elements.enabled.checked,
    days: $$("#daypicker input:checked").map((b) => Number(b.value)),
  };
  await busy($("button[type=submit]", form), async () => {
    try {
      if (editingSchedule) await api("PUT", `/api/schedules/${editingSchedule.id}`, data);
      else await api("POST", "/api/schedules", data);
      $("#schedule-dialog").close();
      toast(editingSchedule ? "Zeitplan gespeichert" : "Zeitplan angelegt", "success");
      await refreshView();
    } catch (e) {
      showError(form, e.message);
    }
  });
}

async function onScheduleDelete() {
  if (!(await confirmDialog("Zeitplan löschen?", "Der Zeitplan wird dauerhaft entfernt.", "Löschen", true))) return;
  try {
    await api("DELETE", `/api/schedules/${editingSchedule.id}`);
    $("#schedule-dialog").close();
    toast("Zeitplan gelöscht", "success");
    await refreshView();
  } catch (e) {
    toast(e.message, "error");
  }
}

// ---------- Protokoll ----------

async function renderEvents() {
  const { events } = await api("GET", "/api/events");
  const list = $("#event-list");
  if (!events.length) {
    list.replaceChildren(el("div", { class: "empty" }, el("h3", {}, "Keine Einträge"),
      el("p", { class: "muted" }, "Das Protokoll listet manuelle und geplante Aktionen.")));
    return;
  }
  list.replaceChildren(...events.map((e) =>
    el("div", { class: `row event ${e.ok ? "ok" : "fail"}` },
      el("span", { class: "event-icon" }, e.ok ? icon("check") : icon("x")),
      el("span", { class: "event-time mono small" }, fmtDateTime(e.ts)),
      el("div", { class: "event-main" },
        el("div", {}, el("strong", {}, e.device), " · ", ACTIONS[e.action] || e.action,
          el("span", { class: "tag" }, e.source)),
        el("div", { class: "muted small" }, e.message)))));
}

async function onClearEvents() {
  if (!(await confirmDialog("Protokoll leeren?", "Alle Einträge werden gelöscht.", "Leeren", true))) return;
  await api("DELETE", "/api/events");
  await renderEvents();
}

// ---------- System ----------

async function renderSystem() {
  const [info] = await Promise.all([api("GET", "/api/info"), renderWifi(), renderVpn()]);
  state.info = info;
  const rows = [
    ["Hostname", info.hostname],
    ["Modell", info.model || "–"],
    ["Version", info.version],
    ["Serverzeit", `${info.time.replace("T", " ")} ${info.timezone}`],
    ["System läuft seit", fmtDuration(info.system_uptime)],
    ["Dienst läuft seit", fmtDuration(info.service_uptime)],
    ["CPU-Temperatur", info.cpu_temp != null ? `${info.cpu_temp.toFixed(1)} °C` : "–"],
    ["Last (1 min)", info.load.toFixed(2)],
  ];
  $("#sys-info").replaceChildren(...rows.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v))));
  renderSysNetwork(info.network);
  $("#sys-networks").replaceChildren(...info.allowed_networks.map((n) => el("span", { class: "chip mono" }, n)));
  $("#sys-client").textContent = info.client;
  $("#ssh-key").value = info.ssh_public_key || "(kein Schlüssel – ssh-keygen nicht verfügbar)";
  $("#pw-title").textContent = info.auth_required ? "Passwort ändern" : "Passwort festlegen";
  $("#pw-current-field").hidden = !info.auth_required;
}

const IFACE_KINDS = { wifi: "WLAN", lan: "LAN", vpn: "VPN" };

function signalLabel(dbm) {
  if (dbm >= -60) return ["gut", "good"];
  if (dbm >= -70) return ["mittel", "fair"];
  return ["schwach", "poor"];
}

function renderSysNetwork(info) {
  const ifaces = info.interfaces.filter((i) => i.up && i.address);
  const vpn = ifaces.filter((i) => i.kind === "vpn");
  const wifi = ifaces.find((i) => i.wifi);
  const port = info.port === 80 ? "" : `:${info.port}`;
  const rows = [];
  if (wifi) {
    const [label, cls] = signalLabel(wifi.wifi.signal_dbm);
    rows.push(["WLAN-Signal", el("span", { class: `signal ${cls}` }, `${wifi.wifi.signal_dbm} dBm · ${label}`)]);
  }
  rows.push(["VPN", vpn.length
    ? el("span", { class: "signal good" }, `verbunden · ${vpn.map((i) => i.address).join(", ")}`)
    : el("span", { class: "muted" }, "nicht verbunden")]);
  rows.push(["Magic Packets über", info.wol_interface || "Default-Route"]);
  $("#sys-network").replaceChildren(...rows.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v))));
  $("#sys-ifaces").replaceChildren(...ifaces.map((i) =>
    el("div", { class: "iface" },
      el("span", { class: `tag kind-${i.kind}` }, IFACE_KINDS[i.kind] || i.kind),
      el("span", { class: "mono" }, i.name),
      el("a", { class: "mono", href: `http://${i.address}${port}/` }, i.address))));
}

// ---------- WLAN ----------

async function renderWifi() {
  let wifi;
  try {
    wifi = await api("GET", "/api/wifi");
  } catch (e) {
    if (e.status === 401) throw e;
    state.wifi = null;
    $("#wifi-unavailable").hidden = false;
    $("#wifi-body").hidden = true;
    $("#wifi-add").hidden = true;
    return;
  }
  state.wifi = wifi;
  $("#wifi-unavailable").hidden = true;
  $("#wifi-body").hidden = false;
  $("#wifi-add").hidden = false;
  const hs = wifi.hotspot;
  let status;
  if (wifi.pending) status = el("span", {}, `verbinde mit „${wifi.pending}“ …`);
  else if (wifi.mode === "client") status = el("span", { class: "signal good" }, `verbunden mit „${wifi.ssid || "?"}“`);
  else if (wifi.mode === "hotspot") status = el("span", { class: "signal fair" }, `Hotspot „${hs.ssid}“ aktiv`);
  else status = el("span", { class: "signal poor" }, "nicht verbunden");
  const rows = [
    ["Status", status],
    ["Interface", wifi.address ? `${wifi.interface} · ${wifi.address}` : wifi.interface],
    ["Einrichtungs-Hotspot", hs.enabled ? `${hs.ssid} · ${hs.secured ? "mit Passwort" : "offen"}` : "abgeschaltet"],
  ];
  $("#wifi-info").replaceChildren(...rows.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v))));

  const error = $("#wifi-error");
  error.hidden = !wifi.last_error;
  if (wifi.last_error) error.textContent = `${fmtDateTime(wifi.last_error.ts)}: ${wifi.last_error.message}`;

  const current = wifi.mode === "client" ? wifi.ssid : null;
  $("#wifi-saved").replaceChildren(...(wifi.saved.length ? wifi.saved.map((ssid) =>
    el("div", { class: "saved-item" },
      el("span", {}, ssid),
      ssid === current && el("span", { class: "tag current" }, "verbunden"),
      el("button", {
        class: "icon-btn", type: "button", disabled: ssid === current,
        title: ssid === current ? "Das verbundene WLAN kann nicht entfernt werden" : "Entfernen",
        "aria-label": `${ssid} entfernen`, onclick: () => forgetWifi(ssid),
      }, icon("x")))) : [el("p", { class: "muted small" }, "Kein WLAN gespeichert.")]));

  $("#hotspot-text").textContent = hs.enabled
    ? `Ist ${hs.delay_minutes} Minuten lang keines der gespeicherten WLANs erreichbar (ohne gespeichertes WLAN sofort), ` +
      `öffnet der WOL-Server den Hotspot „${hs.ssid}“. Nach dem Verbinden öffnet sich die Einrichtungsseite automatisch, ` +
      `alternativ unter http://${hs.address}/. Während der Hotspot läuft, sucht der WOL-Server alle paar Minuten nach den gespeicherten WLANs.`
    : "Der Einrichtungs-Hotspot ist abgeschaltet (hotspot_enabled in /etc/wol-server/config.json).";
}

async function forgetWifi(ssid) {
  if (!(await confirmDialog(`„${ssid}“ entfernen?`, "Das WLAN-Profil samt Passwort wird gelöscht.", "Entfernen", true))) return;
  try {
    await api("POST", "/api/wifi/forget", { ssid });
    toast(`„${ssid}“ entfernt`, "success");
  } catch (e) {
    toast(e.message, "error");
  }
  await renderWifi();
}

function signalBars(strength) {
  const level = Math.max(1, Math.min(4, Math.ceil(strength / 25)));
  return el("span", { class: `bars b${level}`, title: `Signal ${strength} %` }, el("i"), el("i"), el("i"), el("i"));
}

function renderScan(networks, scanTime, cached) {
  const form = $("#wifi-form");
  const list = $("#wifi-scan");
  if (!networks.length) {
    list.replaceChildren(el("p", { class: "muted small" }, "Keine Netze gefunden – Namen unten selbst eingeben."));
  } else {
    list.replaceChildren(...networks.map((n) =>
      el("button", {
        type: "button", class: `scan-item${n.ssid === form.elements.ssid.value ? " selected" : ""}`,
        onclick: (e) => pickNetwork(n, e.currentTarget),
      },
      signalBars(n.signal),
      el("span", { class: "scan-ssid" }, n.ssid),
      n.security ? icon("lock") : el("span", { class: "muted small" }, "offen"))));
  }
  const when = scanTime ? new Date(scanTime * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }) : "";
  $("#wifi-scan-info").textContent = cached
    ? `Gefunden vor Start des Hotspots${when ? ` (${when})` : ""}`
    : when ? `Gefunden um ${when}` : "";
  // Im Hotspot-Betrieb kann der Zero nicht scannen.
  $("#wifi-rescan").hidden = cached;
}

function pickNetwork(network, button) {
  const form = $("#wifi-form");
  form.elements.ssid.value = network.ssid;
  form.elements.hidden_ssid.checked = false;
  for (const item of $$(".scan-item")) item.classList.toggle("selected", item === button);
  if (network.security) form.elements.password.focus();
  else form.elements.password.value = "";
}

async function scanWifi() {
  const form = $("#wifi-form");
  await busy($("#wifi-rescan"), async () => {
    try {
      const res = await api("POST", "/api/wifi/scan");
      renderScan(res.networks || [], res.scan_time, res.cached);
    } catch (e) {
      showError(form, e.message);
    }
  });
}

function openWifiDialog() {
  const form = $("#wifi-form");
  form.reset();
  showError(form, "");
  form.elements.password.type = "password";
  const wifi = state.wifi;
  $("#wifi-step-form").hidden = false;
  $("#wifi-step-done").hidden = true;
  $("#wifi-submit").hidden = false;
  $("#wifi-submit").textContent = wifi && wifi.mode === "client" ? "Speichern" : "Verbinden";
  $("#wifi-cancel").textContent = "Abbrechen";
  renderScan(wifi ? wifi.scan : [], wifi && wifi.scan_time, wifi && wifi.mode === "hotspot");
  $("#wifi-dialog").showModal();
  if (wifi && wifi.mode !== "hotspot") scanWifi();
}

function showWifiSwitching(ssid) {
  $("#wifi-step-form").hidden = true;
  $("#wifi-step-done").hidden = false;
  $("#wifi-submit").hidden = true;
  $("#wifi-cancel").textContent = "Schließen";
  $("#wifi-done-title").textContent = `Verbinde mit „${ssid}“ …`;
  const host = state.info ? state.info.hostname : "wol-server";
  const port = state.info && state.info.network && state.info.network.port !== 80 ? `:${state.info.network.port}` : "";
  const url = `http://${host}.local${port}/`;
  $("#wifi-done-text").replaceChildren(state.hotspot
    ? el("span", {}, "Der Hotspot wird beendet, die Verbindung zu diesem Gerät bricht ab. ",
      "Im Heimnetz ist die Oberfläche unter ", el("a", { href: url }, url),
      " erreichbar oder unter der Adresse, die der Router für den Pi anzeigt.")
    : el("span", {}, "Der Status aktualisiert sich automatisch."));
}

async function onWifiSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showError(form, "");
  const data = {
    ssid: form.elements.ssid.value,
    password: form.elements.password.value,
    hidden: form.elements.hidden_ssid.checked,
  };
  if (!data.ssid.trim()) return showError(form, "Bitte ein WLAN auswählen oder den Namen eingeben.");
  if (data.password && data.password.length < 8) return showError(form, "Das WLAN-Passwort hat mindestens 8 Zeichen.");
  await busy($("#wifi-submit"), async () => {
    try {
      const res = await api("POST", "/api/wifi/connect", data);
      if (res.switching || res.pending) {
        showWifiSwitching(data.ssid);
        setTimeout(() => renderWifi().catch(() => {}), 3000);
      } else {
        $("#wifi-dialog").close();
        toast(res.message || "WLAN gespeichert", "success");
        await renderWifi();
      }
    } catch (e) {
      showError(form, e.message);
    }
  });
}

async function onHotspotSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showError(form, "");
  const password = form.elements.password.value;
  if (password && (password.length < 8 || password.length > 63)) {
    return showError(form, "Das Passwort braucht 8 bis 63 Zeichen. Ohne Passwort ist der Hotspot offen.");
  }
  if (!password && !(await confirmDialog("Hotspot ohne Passwort?",
    "Ohne Passwort kann sich jedes Gerät in Funkreichweite mit dem Einrichtungs-Hotspot verbinden. Die Web-GUI bleibt durch ihr eigenes Passwort geschützt.",
    "Offen lassen"))) return;
  await busy($("button[type=submit]", form), async () => {
    try {
      const res = await api("PUT", "/api/wifi/hotspot", { password });
      form.reset();
      toast(res.message || "Gespeichert", "success");
      await renderWifi();
    } catch (e) {
      showError(form, e.message);
    }
  });
}

// ---------- VPN ----------

const VPN_STATES = { active: "verbunden", activating: "verbindet …", reloading: "verbindet …", deactivating: "trennt …" };

function vpnBusy(vpn) {
  return Boolean(vpn && ((vpn.job && vpn.job.state === "running") || (vpn.enabled && !vpn.address)));
}

async function renderVpn() {
  let vpn;
  try {
    vpn = await api("GET", "/api/vpn");
  } catch (e) {
    if (e.status === 401) throw e;
    state.vpn = null;
    $("#vpn-unavailable").hidden = false;
    $("#vpn-body").hidden = true;
    $("#vpn-upload").hidden = true;
    return;
  }
  state.vpn = vpn;
  $("#vpn-unavailable").hidden = true;
  $("#vpn-body").hidden = false;
  $("#vpn-upload").hidden = false;
  const job = vpn.job && vpn.job.state === "running" ? vpn.job : null;
  let status;
  if (job) status = el("span", {}, "wird eingerichtet …");
  else if (!vpn.configured) status = el("span", { class: "muted" }, "nicht eingerichtet");
  else if (vpn.address) status = el("span", { class: "signal good" }, `verbunden · ${vpn.address}`);
  else if (vpn.enabled) status = el("span", { class: "signal fair" }, VPN_STATES[vpn.state] || "verbindet …");
  else status = el("span", { class: "muted" }, "ausgeschaltet");
  const rows = [["Status", status]];
  if (vpn.remote) rows.push(["Server", el("span", { class: "mono" }, vpn.remote)]);
  if (vpn.address) {
    const port = state.info && state.info.network && state.info.network.port !== 80 ? `:${state.info.network.port}` : "";
    rows.push(["GUI über VPN", el("a", { class: "mono", href: `http://${vpn.address}${port}/` }, `http://${vpn.address}${port}/`)]);
  }
  if (vpn.uploaded) rows.push(["Hochgeladen", fmtDateTime(vpn.uploaded)]);
  $("#vpn-info").replaceChildren(...rows.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v))));

  const error = $("#vpn-error");
  const failed = vpn.job && vpn.job.state === "failed" ? vpn.job : null;
  error.hidden = !failed;
  if (failed) error.textContent = `${fmtDateTime(failed.ts)}: ${failed.message}`;

  $("#vpn-actions").hidden = !vpn.configured;
  const toggle = $("#vpn-toggle");
  toggle.textContent = vpn.enabled ? "Ausschalten" : "Einschalten";
  toggle.disabled = Boolean(job);
  $("#vpn-remove").disabled = Boolean(job);
  $("#vpn-upload").disabled = Boolean(job);
}

// Nur Vorschau im Browser – geprüft wird auf dem Pi.
function inspectOvpn(text) {
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter((l) => l && !/^[#;]/.test(l));
  const remote = lines.find((l) => /^remote\s/.test(l));
  return {
    remote: remote ? remote.split(/\s+/).slice(1, 3).join(":") : null,
    needsAuth: lines.some((l) => /^auth-user-pass(\s|$)/.test(l)),
    client: lines.some((l) => /^(client|tls-client)$/.test(l)),
  };
}

function openVpnDialog() {
  const form = $("#vpn-form");
  form.reset();
  showError(form, "");
  state.vpnFile = null;
  $("#vpn-file-info").textContent = "OpenVPN-Client-Konfiguration (.ovpn) mit eingebetteten Zertifikaten.";
  for (const field of $$("[data-auth]", form)) field.hidden = true;
  $("#vpn-dialog").showModal();
}

async function onVpnFile(event) {
  const form = $("#vpn-form");
  showError(form, "");
  const file = event.target.files[0];
  state.vpnFile = null;
  if (!file) return;
  if (file.size > 48 * 1024) return showError(form, "Die Datei ist zu groß (max. 48 KB).");
  const text = await file.text();
  const info = inspectOvpn(text);
  if (!info.client || !info.remote) {
    return showError(form, "Keine OpenVPN-Client-Konfiguration: „client“ oder „remote“ fehlt.");
  }
  state.vpnFile = { text, info };
  $("#vpn-file-info").textContent = `Server: ${info.remote}${info.needsAuth ? " · braucht Benutzername und Passwort" : ""}`;
  for (const field of $$("[data-auth]", form)) field.hidden = !info.needsAuth;
}

async function onVpnSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showError(form, "");
  const file = state.vpnFile;
  if (!file) return showError(form, "Bitte die .ovpn-Datei auswählen.");
  const data = { config: file.text };
  if (file.info.needsAuth) {
    data.username = form.elements.username.value;
    data.password = form.elements.password.value;
    if (!data.username || !data.password) return showError(form, "Diese Konfiguration braucht Benutzername und Passwort.");
  }
  await busy($("#vpn-submit"), async () => {
    try {
      const res = await api("POST", "/api/vpn/config", data);
      $("#vpn-dialog").close();
      state.vpnFile = null;
      toast(res.message || "Wird eingerichtet …", "success");
      await renderVpn();
    } catch (e) {
      showError(form, e.message);
    }
  });
}

// Über das VPN geöffnet: Die Seite wurde über die VPN-Adresse des Pi aufgerufen
function viaVpn() {
  return Boolean(state.vpn && state.vpn.address && location.hostname === state.vpn.address);
}

async function onVpnToggle() {
  const enable = !state.vpn.enabled;
  if (!enable && viaVpn() && !(await confirmDialog("VPN ausschalten?",
    "Diese Sitzung läuft über das VPN und wird getrennt. Ohne VPN ist die Oberfläche nur aus dem Heimnetz erreichbar.",
    "Ausschalten", true))) return;
  await busy($("#vpn-toggle"), async () => {
    try {
      const res = await api("PUT", "/api/vpn", { enabled: enable });
      toast(res.message, "success");
    } catch (e) {
      toast(e.message, "error");
    }
    await renderVpn();
  });
}

async function onVpnRemove() {
  if (!(await confirmDialog("VPN-Konfiguration entfernen?",
    "Zertifikate und Zugangsdaten werden gelöscht. Ohne VPN ist der WOL-Server nur aus dem Heimnetz erreichbar.",
    "Entfernen", true))) return;
  try {
    const res = await api("DELETE", "/api/vpn");
    toast(res.message, "success");
  } catch (e) {
    toast(e.message, "error");
  }
  await renderVpn();
}

async function copyKey() {
  const area = $("#ssh-key");
  try {
    await navigator.clipboard.writeText(area.value);
  } catch (e) {
    // Clipboard-API gibt es nur in sicheren Kontexten (HTTPS) – im Intranet per HTTP Fallback nutzen.
    area.select();
    document.execCommand("copy");
  }
  toast("Schlüssel kopiert", "success");
}

async function onPasswordSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showError(form, "");
  if (form.new.value.length < 8) return showError(form, "Mindestens 8 Zeichen");
  if (form.new.value !== form.repeat.value) return showError(form, "Passwörter stimmen nicht überein");
  await busy($("button[type=submit]", form), async () => {
    try {
      await api("POST", "/api/password", { current: form.current.value, new: form.new.value });
      form.reset();
      toast("Passwort gespeichert", "success");
      await updateSession();
      await renderSystem();
    } catch (e) {
      showError(form, e.message);
    }
  });
}

// ---------- Netzwerk (DHCP und DNS) ----------

const NET_SUBS = ["overview", "reservations", "leases", "dns", "settings", "tools"];
const LEASE_PRESETS = [[600, "10 Minuten"], [3600, "1 Stunde"], [14400, "4 Stunden"], [43200, "12 Stunden"], [86400, "1 Tag"], [604800, "7 Tage"]];
const REC_TYPES = { host: "Name → IP", alias: "Alias", mx: "MX", srv: "SRV", txt: "TXT" };
const net = {
  sub: "overview",
  status: null,
  config: null,
  leases: null,
  sort: { res: ["ip", 1], lease: ["ip", 1], rec: ["name", 1] },
  watching: false,
  importPreview: null,
};

const normMac = (value) => {
  const hex = String(value || "").toLowerCase().replace(/[^0-9a-f]/g, "");
  return hex.length === 12 ? hex.match(/../g).join(":") : "";
};

function ipKey(ip) {
  const parts = String(ip || "").split(".").map(Number);
  return parts.length === 4 ? parts.reduce((a, b) => a * 256 + b, 0) : Number.MAX_SAFE_INTEGER;
}

function fmtAgo(ts) {
  if (!ts) return "–";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "gerade eben";
  if (s < 3600) return `vor ${Math.round(s / 60)} min`;
  if (s < 86400) return `vor ${Math.round(s / 3600)} h`;
  const d = Math.round(s / 86400);
  return d === 1 ? "gestern" : `vor ${d} Tagen`;
}

function fmtIn(ts) {
  if (!ts) return "unbegrenzt";
  const s = ts - Date.now() / 1000;
  if (s <= 0) return "abgelaufen";
  if (s < 3600) return `noch ${Math.max(1, Math.round(s / 60))} min`;
  if (s < 86400) return `noch ${Math.round(s / 3600)} h`;
  return `noch ${Math.round(s / 86400)} d`;
}

function fmtLease(seconds) {
  const preset = LEASE_PRESETS.find(([v]) => v === seconds);
  if (preset) return preset[1];
  if (seconds % 86400 === 0) return `${seconds / 86400} Tage`;
  if (seconds % 3600 === 0) return `${seconds / 3600} h`;
  if (seconds % 60 === 0) return `${seconds / 60} min`;
  return `${seconds} s`;
}

// Auswahl der Lease-Zeit: Vorgaben oder eigener Wert („90m“, „2h“, „3d“). allowDefault: „Standard“ für Reservierungen.
function leasePicker(container, value, allowDefault = false) {
  const select = el("select", { "aria-label": "Lease-Zeit" },
    allowDefault && el("option", { value: "" }, "Standard des Servers"),
    LEASE_PRESETS.map(([v, label]) => el("option", { value: String(v) }, label)),
    el("option", { value: "custom" }, "Eigener Wert …"));
  const custom = el("input", { class: "mono", placeholder: "z. B. 90m, 2h, 3d", "aria-label": "Eigene Lease-Zeit" });
  const known = value == null ? "" : LEASE_PRESETS.some(([v]) => v === value) ? String(value) : "custom";
  select.value = allowDefault || value != null ? known : "43200";
  if (known === "custom") custom.value = fmtLease(value).replace(" Tage", "d").replace(" h", "h").replace(" min", "m").replace(" s", "");
  custom.hidden = select.value !== "custom";
  select.addEventListener("change", () => {
    custom.hidden = select.value !== "custom";
    if (!custom.hidden) custom.focus();
  });
  container.replaceChildren(select, custom);
  return () => (select.value === "custom" ? custom.value.trim() : select.value === "" ? null : Number(select.value));
}

// Tabelle mit sortierbaren Spalten. columns: [{key, label, sort?, render, cls?}]
function dataTable(name, columns, rows, emptyText) {
  if (!rows.length) return el("div", { class: "table-empty muted" }, emptyText);
  const [key, dir] = net.sort[name] || [columns[0].key, 1];
  const col = columns.find((c) => c.key === key) || columns[0];
  const sorted = col.sort ? [...rows].sort((a, b) => {
    const x = col.sort(a), y = col.sort(b);
    return (x < y ? -1 : x > y ? 1 : 0) * dir;
  }) : rows;
  const head = el("tr", {}, columns.map((c) => el("th", { class: c.cls || "", scope: "col" },
    c.sort ? el("button", {
      type: "button", class: `sort ${c.key === key ? (dir > 0 ? "asc" : "desc") : ""}`,
      onclick: () => {
        net.sort[name] = [c.key, c.key === key ? -dir : 1];
        renderNetSub();
      },
    }, c.label) : c.label)));
  return el("table", { class: "data" }, el("thead", {}, head),
    el("tbody", {}, sorted.map((row) => el("tr", { class: row._cls || "" },
      columns.map((c) => el("td", { class: c.cls || "", "data-label": c.label }, c.render(row)))))));
}

async function loadNetStatus() {
  net.status = await api("GET", "/api/net");
  return net.status;
}

async function loadNetConfig() {
  net.config = await api("GET", "/api/net/config");
  return net.config;
}

async function refreshNetBadge() {
  try {
    const status = await loadNetStatus();
    const badge = $("#net-tab-badge");
    badge.hidden = !status.counts.unknown;
    badge.textContent = status.counts.unknown || "";
    badge.title = `${status.counts.unknown} unbekannte Geräte`;
  } catch (e) {
    // Netzwerk-Verwaltung nicht installiert: Reiter bleibt ohne Zähler
  }
}

async function renderNetwork() {
  try {
    await Promise.all([loadNetStatus(), loadNetConfig()]);
  } catch (e) {
    if (e.status === 401) throw e;
    $("#net-unavailable").hidden = false;
    $("#net-body").hidden = true;
    $("#net-service").hidden = true;
    return;
  }
  $("#net-unavailable").hidden = true;
  $("#net-body").hidden = false;
  renderNetHeader();
  setNetSub(net.sub, false);
  if (net.status.running) watchNetJob();
}

function setNetSub(sub, push = true) {
  net.sub = sub;
  for (const tab of $$("#net-subtabs .subtab")) tab.classList.toggle("active", tab.dataset.sub === sub);
  for (const view of $$(".subview")) view.hidden = view.id !== `net-sub-${sub}`;
  if (push) history.replaceState(null, "", `#network/${sub}`);
  renderNetSub(true);
}

function renderNetSub(load = false) {
  const actions = {
    overview: renderNetOverview, reservations: renderReservations, leases: () => renderLeases(load),
    dns: () => renderDnsView(load), settings: renderNetSettings, tools: () => renderNetTools(load),
  };
  Promise.resolve(actions[net.sub]()).catch((e) => toast(e.message, "error"));
}

function renderNetHeader() {
  const st = net.status;
  const pill = $("#net-service");
  const on = st.service.state === "active";
  pill.hidden = false;
  pill.className = `service-pill ${on ? "on" : st.dns_enabled || st.dhcp_enabled ? "warn" : "off"}`;
  pill.replaceChildren(el("span", { class: "dot" }), on ? "dnsmasq läuft" : st.dns_enabled || st.dhcp_enabled ? "dnsmasq gestoppt" : "aus");
  $("#net-subtitle").textContent = [
    st.dhcp_enabled ? `DHCP an (${st.mode === "known-only" ? "nur bekannte Geräte" : "mit Bereich"})` : "DHCP aus",
    st.dns_enabled ? `DNS an · ${st.domain}` : "DNS aus",
  ].join(" · ");
  const pending = (net.config && net.config.pending) || st.pending;
  const bar = $("#net-pending");
  bar.hidden = !pending.length;
  $("#net-pending-title").textContent = pending.length === 1 ? "Eine offene Änderung" : `${pending.length} offene Änderungen`;
  $("#net-pending-list").replaceChildren(...pending.map((c) => el("li", {}, c)));
  $("#net-apply").disabled = st.running;
  $("#net-discard").disabled = st.running;
  const job = $("#net-job");
  if (st.running) {
    job.hidden = false;
    job.className = "job-bar running";
    job.replaceChildren(el("span", { class: "spinner" }), el("span", {}, st.queued > 1 ? `${st.queued} Aufträge laufen …` : "Wird ausgeführt …"));
  } else if (st.job && st.job.state === "failed" && Date.now() / 1000 - st.job.ts < 3600) {
    job.hidden = false;
    job.className = "job-bar failed";
    job.replaceChildren(icon("alert"), el("span", {}, el("strong", {}, "Fehlgeschlagen: "), st.job.message),
      el("button", { type: "button", class: "icon-btn", title: "Ausblenden", "aria-label": "Ausblenden", onclick: () => { job.hidden = true; } }, icon("x")));
  } else {
    job.hidden = true;
  }
  const cfg = net.config && net.config.config;
  $("#net-count-res").textContent = cfg ? cfg.reservations.length : "";
  $("#net-count-leases").textContent = st.counts.leases;
  const badge = $("#net-tab-badge");
  badge.hidden = !st.counts.unknown;
  badge.textContent = st.counts.unknown || "";
}

// Aufträge laufen asynchron (wol-dns-apply.service) – solange beobachten, bis sie fertig sind.
async function watchNetJob(success) {
  net.watchSuccess = success;
  if (net.watching) return;
  net.watching = true;
  try {
    for (let i = 0; i < 120; i++) {
      await new Promise((r) => setTimeout(r, i < 10 ? 800 : 2000));
      const st = await loadNetStatus();
      if (state.view === "network") renderNetHeader();
      if (!st.running) {
        if (st.job && st.job.state === "ok") toast(net.watchSuccess || st.job.message, "success");
        else if (st.job) toast(st.job.message, "error");
        break;
      }
    }
  } finally {
    net.watching = false;
  }
  if (state.view === "network") await renderNetwork();
}

async function netApply() {
  await busy($("#net-apply"), async () => {
    try {
      const res = await api("POST", "/api/net/apply");
      toast(res.message);
      await loadNetStatus();
      renderNetHeader();
      watchNetJob("Änderungen übernommen");
    } catch (e) {
      toast(e.message, "error");
    }
  });
}

async function netDiscard() {
  if (!(await confirmDialog("Änderungen verwerfen?", "Alle Änderungen im Entwurf werden verworfen. Der aktive Stand bleibt unverändert.", "Verwerfen", true))) return;
  await api("POST", "/api/net/discard");
  toast("Entwurf verworfen", "success");
  await renderNetwork();
}

function afterDraftChange(message) {
  toast(message, "success");
  return renderNetwork();
}

// Ohne offenen Entwurf wirken Änderungen sofort: Auftrag beobachten. Mit Entwurf landen sie dort.
async function afterNetChange(res, success) {
  toast(res.message, res.request ? "" : "success");
  await renderNetwork();
  if (res.request) watchNetJob(success);
}

// Änderungen laufen nacheinander: einen noch laufenden Auftrag abwarten.
async function netIdle() {
  for (let i = 0; i < 60; i++) {
    if (!(await loadNetStatus()).running) return;
    await new Promise((r) => setTimeout(r, 1000));
  }
}

const netHasDraft = () => Boolean(net.config && net.config.pending.length);
const netSubmitLabel = () => (netHasDraft() ? "In den Entwurf" : "Übernehmen");

// ---- Übersicht ----

function statCard(title, value, lines, cls = "") {
  return el("div", { class: `stat ${cls}` }, el("div", { class: "stat-title" }, title), el("div", { class: "stat-value" }, value),
    lines.filter(Boolean).map((l) => el("div", { class: "stat-line" }, l)));
}

function renderNetOverview() {
  const st = net.status;
  const cfg = net.config.config;
  const online = new Set(net.config.meta.online);
  const reservedOnline = cfg.reservations.filter((r) => online.has(r.mac)).length;
  $("#net-stats").replaceChildren(
    statCard("DHCP", st.dhcp_enabled ? "an" : "aus", [
      st.dhcp_enabled && (st.mode === "known-only" ? "Nur bekannte Geräte" : `Bereich ${cfg.dhcp.range_start} – ${cfg.dhcp.range_end}`),
      `Lease-Zeit ${fmtLease(st.lease_time)}`,
      `${st.counts.leases} aktive Leases`,
    ], st.dhcp_enabled ? "on" : "off"),
    statCard("DNS", st.dns_enabled ? "an" : "aus", [
      el("span", { class: "mono" }, st.domain),
      `Upstream ${cfg.dns.upstream.join(", ") || "–"}`,
      `${st.counts.records} eigene Einträge`,
    ], st.dns_enabled ? "on" : "off"),
    statCard("Reservierungen", String(st.counts.reservations), [
      `${reservedOnline} gerade im Netz`,
      `${cfg.reservations.filter((r) => !r.enabled).length} inaktiv`,
      `Nächste freie IP: ${net.config.meta.next_ip || "–"}`,
    ]),
    statCard("Server", el("span", { class: "mono" }, st.server.address || "–"), [
      st.server.method === "manual" ? "Feste Adresse" : st.server.method === "auto" ? "Adresse per DHCP" : null,
      `${st.server.interface}${st.server.connection ? ` · ${st.server.connection}` : ""}`,
      st.server.mac && el("span", { class: "mono" }, st.server.mac),
    ]));

  $("#net-warnings").replaceChildren(...st.warnings.map((w) => el("div", { class: "notice warn" }, icon("alert"), el("span", {}, w))));

  const list = $("#net-unknown-list");
  const unknown = st.unknown || [];
  $("#net-unknown-info").textContent = st.dhcp_enabled && st.mode === "known-only"
    ? "Abgewiesene Anfragen der letzten 24 Stunden" : "Nur im Modus „nur bekannte Geräte“";
  if (!unknown.length) {
    list.replaceChildren(el("p", { class: "muted small" }, st.dhcp_enabled ? "Keine abgewiesenen Anfragen." : "DHCP ist aus."));
  } else {
    list.replaceChildren(...unknown.map((u) => el("div", { class: "request" },
      el("span", { class: "request-icon" }, icon("device")),
      el("div", { class: "request-main" },
        el("div", {}, el("strong", {}, u.name || "ohne Namen"), u.vendor ? el("span", { class: "tag" }, u.vendor) : null),
        el("div", { class: "muted small" }, el("span", { class: "mono" }, u.mac), ` · ${u.count}× angefragt · ${fmtAgo(u.last)}`)),
      el("div", { class: "request-actions" },
        el("button", { type: "button", class: "btn small ghost", onclick: () => ignoreDevice(u) }, "Ignorieren"),
        el("button", { type: "button", class: "btn small primary", onclick: () => openApprove(u) }, "Freigeben")))));
  }

  const last = st.last_change;
  $("#net-last-panel").hidden = !last;
  if (last) {
    $("#net-last-meta").textContent = `${fmtDateTime(last.ts)}${last.client ? ` · von ${last.client}` : ""}`;
    const items = last.changes || [];
    $("#net-last-list").replaceChildren(...items.slice(0, 6).map((c) => el("li", {}, c)),
      ...(items.length > 6 ? [el("li", { class: "muted" }, `… und ${items.length - 6} weitere`)] : []));
  }
}

// ---- Unbekannte Geräte ----

let approving = null;

function openApprove(u) {
  approving = u;
  const form = $("#approve-form");
  form.reset();
  showError(form, "");
  $("#approve-info").replaceChildren(...[
    ["MAC", el("span", { class: "mono" }, u.mac)],
    ["Hersteller", u.vendor || "unbekannt"],
    ["Meldet sich als", u.name || "–"],
    ["Angefragt", `${u.count}× · zuletzt ${fmtAgo(u.last)}`],
  ].map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v))));
  form.elements.name.value = u.suggested_name || "";
  form.elements.ip.value = u.suggested_ip || "";
  $("#approve-name-hint").textContent = u.name && u.name !== u.suggested_name ? `vom Gerät gemeldet: „${u.name}“` : "";
  $("#approve-private").hidden = !u.private;
  $("#approve-dialog").showModal();
}

async function onApproveSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showError(form, "");
  const data = { name: form.elements.name.value, ip: form.elements.ip.value, comment: form.elements.comment.value };
  await busy($("#approve-submit"), async () => {
    try {
      const res = await api("POST", `/api/net/unknown/${approving.mac}/approve`, data);
      $("#approve-dialog").close();
      toast(res.message);
      await loadNetStatus();
      renderNetHeader();
      watchNetJob(`${data.name} freigegeben`);
    } catch (e) {
      showError(form, e.message);
    }
  });
}

async function ignoreDevice(u) {
  if (!(await confirmDialog(`${u.name || u.mac} ignorieren?`,
    "Das Gerät erhält keine Adresse und wird in der Übersicht nicht mehr angezeigt. Aufheben unter Werkzeuge → Ignorierte Geräte.", "Ignorieren"))) return;
  try {
    await api("POST", `/api/net/unknown/${u.mac}/ignore`, { comment: u.name || "" });
    toast("Wird ignoriert", "success");
    await renderNetwork();
  } catch (e) {
    toast(e.message, "error");
  }
}

// ---- Reservierungen ----

function renderReservations() {
  const cfg = net.config.config;
  const meta = net.config.meta;
  const devices = net.config.devices || {};
  const online = new Set(meta.online);
  const search = $("#res-search").value.trim().toLowerCase();
  const filter = ($("#res-filter input:checked") || {}).value || "all";
  const rows = cfg.reservations.map((r) => ({
    ...r, vendor: meta.vendors[r.mac] || "", seen: meta.last_seen[r.mac] || null, online: online.has(r.mac),
    lease: meta.leases[r.mac] || null, device: devices[r.mac] || null, _cls: r.enabled ? "" : "is-disabled",
  })).filter((r) => {
    if (filter === "online" && !r.online) return false;
    if (filter === "wol" && !r.device) return false;
    if (filter === "disabled" && r.enabled) return false;
    if (!search) return true;
    return [r.name, r.ip, r.mac, r.vendor, r.comment].some((v) => String(v || "").toLowerCase().includes(search));
  });
  const domain = cfg.dns.domain;
  $("#res-table").replaceChildren(dataTable("res", [
    { key: "name", label: "Name", sort: (r) => r.name || "~", render: (r) => el("div", { class: "cell-name" },
      el("span", { class: `dot small ${r.online ? "online" : ""}`, title: r.online ? "im Netz" : "nicht gesehen" }),
      el("div", {}, el("strong", {}, r.name || el("span", { class: "muted" }, "ohne Namen")),
        r.comment ? el("div", { class: "muted small" }, r.comment) : null,
        !r.enabled ? el("span", { class: "tag" }, "inaktiv") : null)) },
    { key: "ip", label: "IP", sort: (r) => ipKey(r.ip), render: (r) => el("span", { class: "mono" }, r.ip) },
    { key: "mac", label: "MAC", cls: "hide-sm", sort: (r) => r.mac, render: (r) => el("span", { class: "mono small" }, r.mac) },
    { key: "vendor", label: "Hersteller", cls: "hide-md", sort: (r) => r.vendor || "~", render: (r) => el("span", { class: "small" }, r.vendor || "–") },
    { key: "seen", label: "Zuletzt", cls: "hide-sm", sort: (r) => -(r.online ? Infinity : r.seen || 0), render: (r) => el("span", { class: "small" }, r.online ? "jetzt" : fmtAgo(r.seen)) },
    { key: "actions", label: "", cls: "actions", render: (r) => el("div", { class: "row-actions" },
      r.device ? el("button", { type: "button", class: "icon-btn wake-btn", title: `${r.device.name} aufwecken`, "aria-label": `${r.device.name} aufwecken`,
        onclick: (e) => deviceAction(r.device, "wake", e.currentTarget) }, icon("power")) : null,
      el("button", { type: "button", class: "icon-btn", title: "Bearbeiten", "aria-label": `${r.name || r.mac} bearbeiten`, onclick: () => openResDialog(r) }, icon("edit"))) },
  ], rows, search || filter !== "all" ? "Keine passenden Reservierungen." : "Keine Reservierungen. Anlegen über „Reservierung“ oder per Import unter Werkzeuge."));
  $("#res-foot").textContent = `${rows.length} von ${cfg.reservations.length} · erreichbar als name.${domain}`;
}

let editingRes = null;
let getResLease = null;

function openResDialog(r = null, prefill = {}) {
  editingRes = r;
  const form = $("#res-form");
  form.reset();
  showError(form, "");
  const data = r || { enabled: true, ip: net.config ? net.config.meta.next_ip : "", lease_time: null, ...prefill };
  for (const key of ["name", "mac", "ip", "comment"]) form.elements[key].value = data[key] || "";
  form.elements.enabled.checked = data.enabled !== false;
  getResLease = leasePicker($("#res-lease"), data.lease_time ?? null, true);
  $("#res-dialog-title").textContent = r ? "Reservierung bearbeiten" : "Neue Reservierung";
  $("#res-delete").hidden = !r;
  $("#res-submit").textContent = netSubmitLabel();
  updateResVendor();
  $("#res-dialog").showModal();
}

function updateResVendor() {
  const mac = normMac($("#res-form").elements.mac.value);
  const vendor = mac && net.config ? net.config.meta.vendors[mac] : "";
  const leased = mac && net.config ? net.config.meta.leases[mac] : "";
  $("#res-vendor").textContent = [vendor, leased && `aktuelle Lease: ${leased}`].filter(Boolean).join(" · ");
}

async function onResSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showError(form, "");
  const data = {
    name: form.elements.name.value, mac: form.elements.mac.value, ip: form.elements.ip.value,
    comment: form.elements.comment.value, enabled: form.elements.enabled.checked, lease_time: getResLease(),
  };
  await busy($("button[type=submit]", form), async () => {
    try {
      await netIdle();
      const res = editingRes ? await api("PUT", `/api/net/reservations/${editingRes.id}`, data)
        : await api("POST", "/api/net/reservations", data);
      $("#res-dialog").close();
      await afterNetChange(res, `Reservierung ${data.name || data.mac} übernommen`);
      if ($("#device-dialog").open) updateDeviceNetHint();
    } catch (e) {
      showError(form, e.message);
    }
  });
}

async function onResDelete() {
  const r = editingRes;
  if (!(await confirmDialog(`${r.name || r.mac} löschen?`, `Die Reservierung für ${r.ip} wird entfernt. Im Modus „nur bekannte Geräte“ erhält das Gerät ohne Reservierung keine Adresse.`, "Löschen", true))) return;
  try {
    await netIdle();
    const res = await api("DELETE", `/api/net/reservations/${r.id}`);
    $("#res-dialog").close();
    await afterNetChange(res, `Reservierung ${r.name || r.mac} gelöscht`);
  } catch (e) {
    toast(e.message, "error");
  }
}

async function nextFreeIp() {
  await loadNetConfig();
  $("#res-form").elements.ip.value = net.config.meta.next_ip || "";
}

// ---- Leases ----

async function renderLeases(load) {
  if (load || !net.leases) net.leases = await api("GET", "/api/net/leases");
  const search = $("#lease-search").value.trim().toLowerCase();
  const rows = net.leases.leases.filter((l) => !search ||
    [l.name, l.hostname, l.ip, l.mac, l.vendor].some((v) => String(v || "").toLowerCase().includes(search)));
  $("#lease-table").replaceChildren(dataTable("lease", [
    { key: "name", label: "Name", sort: (l) => l.name || "~", render: (l) => el("div", {},
      el("strong", {}, l.name || el("span", { class: "muted" }, "ohne Namen")),
      l.hostname && l.hostname !== l.name ? el("div", { class: "muted small" }, `meldet sich als ${l.hostname}`) : null) },
    { key: "ip", label: "IP", sort: (l) => ipKey(l.ip), render: (l) => el("span", { class: "mono" }, l.ip) },
    { key: "mac", label: "MAC", cls: "hide-sm", sort: (l) => l.mac, render: (l) => el("span", { class: "mono small" }, l.mac) },
    { key: "vendor", label: "Hersteller", cls: "hide-md", sort: (l) => l.vendor || "~", render: (l) => el("span", { class: "small" }, l.vendor || "–") },
    { key: "expires", label: "Gültig", sort: (l) => l.expires || Infinity, render: (l) => el("span", { class: "small nowrap" }, fmtIn(l.expires)) },
    { key: "kind", label: "Art", cls: "hide-sm", sort: (l) => (l.reservation ? 0 : 1), render: (l) => el("span", { class: `tag ${l.reservation ? "current" : ""}` }, l.reservation ? "reserviert" : "dynamisch") },
    { key: "actions", label: "", cls: "actions", render: (l) => el("div", { class: "row-actions" },
      !l.reservation ? el("button", { type: "button", class: "btn small ghost", onclick: () => openResDialog(null, { mac: l.mac, ip: l.ip, name: l.hostname }) }, "Reservieren") : null,
      !l.device ? el("button", { type: "button", class: "btn small ghost hide-sm", onclick: () => openDeviceDialog(null, { name: l.name || l.hostname || "", mac: l.mac.toUpperCase(), host: l.name && net.config ? `${l.name}.${net.config.config.dns.domain}` : l.ip }) }, "Als WOL-Gerät") : null,
      el("button", { type: "button", class: "icon-btn", title: "Lease freigeben", "aria-label": `Lease von ${l.ip} freigeben`, onclick: () => releaseLease(l) }, icon("x"))) },
  ], rows, search ? "Keine passenden Leases." : "Keine aktiven Leases."));
  $("#net-count-leases").textContent = net.leases.leases.length;
}

async function releaseLease(l) {
  if (!(await confirmDialog(`Lease von ${l.ip} freigeben?`, "Die Lease wird gelöscht. Bei der nächsten Erneuerung erhält das Gerät eine neue Adresse, mit Reservierung die reservierte.", "Freigeben", true))) return;
  try {
    const res = await api("POST", `/api/net/leases/${l.mac}/release`);
    toast(res.message);
    await loadNetStatus();
    renderNetHeader();
    watchNetJob();
  } catch (e) {
    toast(e.message, "error");
  }
}

// ---- DNS ----

async function renderDnsView(load) {
  const cfg = net.config.config;
  const domain = cfg.dns.domain;
  $("#rec-hint").textContent = `Reservierungen sind automatisch als name.${domain} erreichbar und brauchen hier keinen Eintrag.`;
  const recValue = (r) => r.type === "mx" ? `${r.value} (Priorität ${r.priority})`
    : r.type === "srv" ? `${r.value}:${r.port} (Prio ${r.priority}, Gewicht ${r.weight})` : r.value;
  $("#rec-table").replaceChildren(dataTable("rec", [
    { key: "type", label: "Art", sort: (r) => r.type, render: (r) => el("span", { class: "tag" }, REC_TYPES[r.type]) },
    { key: "name", label: "Name", sort: (r) => r.name, render: (r) => el("div", {},
      el("span", { class: "mono" }, r.name ? `${r.name}.${domain}` : domain),
      r.comment ? el("div", { class: "muted small" }, r.comment) : null) },
    { key: "value", label: "Ziel / Wert", sort: (r) => r.value, render: (r) => el("span", { class: "mono small" }, recValue(r)) },
    { key: "actions", label: "", cls: "actions", render: (r) => el("button", { type: "button", class: "icon-btn", title: "Bearbeiten", "aria-label": "Eintrag bearbeiten", onclick: () => openRecDialog(r) }, icon("edit")) },
  ], cfg.records, "Keine eigenen Einträge."));

  const querylogOn = net.status.dns_enabled && cfg.dns.log_queries;
  $("#dns-querylog-hint").textContent = querylogOn ? "Die letzten Anfragen an den DNS-Server."
    : "Abfrageprotokoll ausgeschaltet (Einstellungen → DNS).";
  $("#dns-querylog-refresh").hidden = !querylogOn;
  if (!querylogOn) $("#dns-querylog").replaceChildren();
  if (load) {
    loadDnsStats();
    if (querylogOn) loadQueryLog();
  }
}

async function loadDnsStats() {
  const box = $("#dns-stats");
  try {
    const s = await api("GET", "/api/net/stats");
    if (!s.available) {
      box.replaceChildren(el("p", { class: "muted small" }, s.error || "DNS ist aus."));
      return;
    }
    const total = (s.hits || 0) + (s.misses || 0);
    const rows = [
      ["Trefferquote", total ? `${Math.round((s.hits / total) * 100)} % (${s.hits} von ${total})` : "–"],
      ["Cache-Größe", s.cachesize],
      ["Eingefügt / verdrängt", `${s.insertions} / ${s.evictions}`],
      ...s.servers.map((sv) => [`Upstream ${sv.server}`, `${sv.queries} Anfragen${sv.failed ? `, ${sv.failed} fehlgeschlagen` : ""}`]),
    ];
    box.replaceChildren(...rows.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, String(v)))));
  } catch (e) {
    box.replaceChildren(el("p", { class: "muted small" }, e.message));
  }
}

async function loadQueryLog() {
  const box = $("#dns-querylog");
  try {
    const { entries } = await api("GET", "/api/net/logs/queries");
    box.replaceChildren(dataTable("querylog", [
      { key: "ts", label: "Zeit", render: (q) => el("span", { class: "mono small" }, fmtDateTime(q.ts)) },
      { key: "client", label: "Von", render: (q) => el("span", { class: "mono small" }, q.client) },
      { key: "type", label: "Typ", cls: "hide-sm", render: (q) => el("span", { class: "tag" }, q.type) },
      { key: "name", label: "Name", render: (q) => el("span", { class: "mono small break" }, q.name) },
    ], [...entries].reverse().slice(0, 100), "Keine Anfragen protokolliert."));
  } catch (e) {
    box.replaceChildren(el("p", { class: "muted small" }, e.message));
  }
}

async function onDnsTest(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showError(form, "");
  const box = $("#dns-test-result");
  await busy($("button[type=submit]", form), async () => {
    try {
      const res = await api("POST", "/api/net/test", { query: form.elements.query.value });
      box.hidden = false;
      box.replaceChildren(
        el("div", { class: "dns-result-head" }, el("span", { class: `tag ${res.rcode === 0 ? "current" : ""}` }, res.status),
          el("span", { class: "muted small" }, `${res.query} · ${res.qtype} · ${res.ms} ms${res.authoritative ? " · lokal" : ""}`)),
        res.answers.length ? el("ul", {}, res.answers.map((a) => el("li", {}, el("span", { class: "tag" }, a.type), el("span", { class: "mono" }, a.data))))
          : el("p", { class: "muted small" }, "Keine Antwort-Einträge."));
    } catch (e) {
      box.hidden = true;
      showError(form, e.message);
    }
  });
}

let editingRec = null;

function openRecDialog(r = null) {
  editingRec = r;
  const form = $("#rec-form");
  form.reset();
  showError(form, "");
  const data = r || { type: "host", name: "", value: "", priority: 10, port: "", weight: 0, comment: "" };
  for (const radio of $$("input[name=type]", form)) radio.checked = radio.value === data.type;
  for (const key of ["name", "value", "priority", "port", "weight", "comment"]) form.elements[key].value = data[key] ?? "";
  if (data.type !== "mx" && data.type !== "srv") form.elements.priority.value = data.type === "srv" ? 0 : 10;
  setRecType(data.type, false);
  $("#rec-dialog-title").textContent = r ? "Eintrag bearbeiten" : "Neuer Eintrag";
  $("#rec-delete").hidden = !r;
  $("#rec-submit").textContent = netSubmitLabel();
  $("#rec-dialog").showModal();
}

function setRecType(type, adjust = true) {
  const domain = net.config ? net.config.config.dns.domain : "domäne";
  // Übliche Vorgaben beim Wechsel der Art eines neuen Eintrags: MX 10, SRV 0
  if (adjust && !editingRec) $("#rec-form").elements.priority.value = type === "mx" ? 10 : 0;
  const labels = {
    host: ["Name", "IP-Adresse", `z. B. nas → nas.${domain}`, "IPv4 oder IPv6"],
    alias: ["Alias", "zeigt auf", `z. B. drucker → drucker.${domain}`, "ein vorhandener lokaler Name"],
    mx: ["Für (leer = Domäne)", "Mailserver", `leer = ${domain}`, "Name des Mailservers"],
    srv: ["Dienst", "Ziel-Host", "z. B. _ipp._tcp", "Name des Hosts, der den Dienst anbietet"],
    txt: ["Name", "Text", `z. B. info → info.${domain}`, "ohne Anführungszeichen"],
  }[type];
  $("#rec-name-label").textContent = labels[0];
  $("#rec-value-label").textContent = labels[1];
  $("#rec-name-hint").textContent = labels[2];
  $("#rec-value-hint").textContent = labels[3];
  for (const field of $$("[data-rec]", $("#rec-form"))) field.hidden = !field.dataset.rec.split(" ").includes(type);
}

async function onRecSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showError(form, "");
  const type = ($("input[name=type]:checked", form) || {}).value;
  const data = { type, name: form.elements.name.value, value: form.elements.value.value, comment: form.elements.comment.value };
  if (type === "mx" || type === "srv") data.priority = Number(form.elements.priority.value || 0);
  if (type === "srv") {
    data.port = Number(form.elements.port.value);
    data.weight = Number(form.elements.weight.value || 0);
  }
  await busy($("button[type=submit]", form), async () => {
    try {
      await netIdle();
      const res = editingRec ? await api("PUT", `/api/net/records/${editingRec.id}`, data)
        : await api("POST", "/api/net/records", data);
      $("#rec-dialog").close();
      await afterNetChange(res, "DNS-Eintrag übernommen");
    } catch (e) {
      showError(form, e.message);
    }
  });
}

async function onRecDelete() {
  if (!(await confirmDialog("Eintrag löschen?", "Der DNS-Eintrag wird entfernt.", "Löschen", true))) return;
  try {
    await netIdle();
    const res = await api("DELETE", `/api/net/records/${editingRec.id}`);
    $("#rec-dialog").close();
    await afterNetChange(res, "DNS-Eintrag gelöscht");
  } catch (e) {
    toast(e.message, "error");
  }
}

// ---- Einstellungen ----

let getSettingsLease = null;

function conditionalRow(item = { domain: "", server: "" }) {
  const row = el("div", { class: "editor-row" },
    el("input", { class: "mono", placeholder: "vpn.example.org", value: item.domain, "data-k": "domain", "aria-label": "Domäne" }),
    el("span", { class: "muted" }, "→"),
    el("input", { class: "mono", placeholder: "10.8.0.1", value: item.server, "data-k": "server", "aria-label": "DNS-Server" }),
    el("button", { type: "button", class: "icon-btn", title: "Entfernen", "aria-label": "Weiterleitung entfernen", onclick: () => row.remove() }, icon("x")));
  return row;
}

function renderNetSettings() {
  const cfg = net.config.config;
  const form = $("#net-settings-form");
  showError(form, "");
  const set = (name, value) => {
    const field = form.elements[name];
    if (!field) return;
    if (field instanceof RadioNodeList) for (const r of field) r.checked = r.value === value;
    else if (field.type === "checkbox") field.checked = Boolean(value);
    else field.value = Array.isArray(value) ? value.join(", ") : value ?? "";
  };
  for (const [section, values] of Object.entries({ server: cfg.server, dhcp: cfg.dhcp, dns: cfg.dns })) {
    for (const [key, value] of Object.entries(values)) set(`${section}.${key}`, value);
  }
  getSettingsLease = leasePicker($("#settings-lease"), cfg.dhcp.lease_time);
  $("#conditional-list").replaceChildren(...cfg.dns.conditional.map(conditionalRow));
  updateModeHint();
  const prefix = Number(cfg.server.prefix);
  $("#prefix-hint").textContent = net.config.meta.network ? `Netz ${net.config.meta.network}` : prefix === 24 ? "/24 = 255.255.255.0" : "";
  $("#settings-submit").textContent = netHasDraft() ? "In den Entwurf" : "Prüfen";
  $("#settings-foot").textContent = netHasDraft() ? "Ein Entwurf ist offen. Änderungen landen im Entwurf."
    : "„Prüfen“ zeigt alle Änderungen, bevor sie wirksam werden.";
  renderStaticIp();
}

function updateModeHint() {
  const form = $("#net-settings-form");
  const mode = (form.querySelector("input[name='dhcp.mode']:checked") || {}).value;
  for (const block of $$("[data-mode]", form)) block.hidden = block.dataset.mode !== mode;
  $("#mode-hint").textContent = mode === "known-only"
    ? "Nur Geräte mit Reservierung erhalten eine Adresse. Unbekannte Geräte erscheinen zur Freigabe in der Übersicht."
    : "Unbekannte Geräte erhalten eine Adresse aus dem Bereich. Reservierte Adressen liegen außerhalb des Bereichs.";
}

function renderStaticIp() {
  const st = net.status.server;
  const box = $("#static-ip");
  const fixed = st.method === "manual";
  box.replaceChildren(
    el("div", { class: "static-ip-text" },
      el("strong", {}, fixed ? "Feste Adresse" : st.method === "auto" ? "Adresse per DHCP" : "Adresse unbekannt"),
      el("span", { class: "muted small" }, ` · ${st.address || "–"} auf ${st.interface}${st.connection ? ` (${st.connection})` : ""}`),
      el("div", { class: "muted small" }, "Ein DHCP-Server benötigt eine feste eigene Adresse.")),
    st.connection ? el("button", { type: "button", class: "btn small", onclick: () => toggleStaticIp(!fixed) },
      fixed ? "Wieder per DHCP" : "Feste Adresse setzen") : null);
}

async function toggleStaticIp(enable) {
  const ip = net.config.config.server.ip || net.status.server.address;
  const text = enable
    ? `Das WLAN-Profil erhält die feste Adresse ${ip} mit Gateway ${net.config.config.dhcp.gateway || "?"}. Die Verbindung bleibt bestehen.`
    : "Der WOL-Server bezieht seine Adresse per DHCP. Bei eingeschaltetem DHCP auf dem WOL-Server selbst erhält er so keine Adresse.";
  if (!(await confirmDialog(enable ? "Feste Adresse setzen?" : "Wieder per DHCP?", text, enable ? "Setzen" : "Umstellen", !enable))) return;
  try {
    const res = await api("PUT", "/api/net/static-ip", { enabled: enable });
    toast(res.message, "success");
    await renderNetwork();
  } catch (e) {
    toast(e.message, "error");
  }
}

async function onSettingsSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showError(form, "");
  const v = (name) => form.elements[name].value;
  const list = (name) => v(name).split(/[\s,;]+/).filter(Boolean);
  const data = {
    server: { ip: v("server.ip"), prefix: Number(v("server.prefix")), name: v("server.name"), interface: v("server.interface"), port: Number(v("server.port")) },
    dhcp: {
      enabled: form.elements["dhcp.enabled"].checked,
      mode: (form.querySelector("input[name='dhcp.mode']:checked") || {}).value,
      range_start: v("dhcp.range_start"), range_end: v("dhcp.range_end"), lease_time: getSettingsLease(),
      gateway: v("dhcp.gateway"), dns: list("dhcp.dns"), ntp: list("dhcp.ntp"), search: list("dhcp.search"),
      boot_file: v("dhcp.boot_file"), boot_server: v("dhcp.boot_server"), tftp: form.elements["dhcp.tftp"].checked,
    },
    dns: {
      enabled: form.elements["dns.enabled"].checked, domain: v("dns.domain"), upstream: list("dns.upstream"),
      ttl: Number(v("dns.ttl")), cache_size: Number(v("dns.cache_size")), log_queries: form.elements["dns.log_queries"].checked,
      conditional: $$("#conditional-list .editor-row").map((row) => ({
        domain: $("[data-k=domain]", row).value.trim(), server: $("[data-k=server]", row).value.trim(),
      })).filter((c) => c.domain || c.server),
    },
  };
  if (data.dhcp.enabled && net.status.server.method === "auto" &&
      !(await confirmDialog("Ohne feste Adresse?", "Der WOL-Server bezieht seine eigene Adresse per DHCP. Ein DHCP-Server benötigt eine feste Adresse (Einstellungen → Server → „Feste Adresse setzen“).", "Trotzdem speichern"))) return;
  await busy($("#settings-submit"), async () => {
    try {
      const preview = await api("POST", "/api/net/settings/preview", data);
      if (!preview.changes.length) return toast("Keine Änderungen", "success");
      if (!preview.draft && !(await confirmDialog("Änderungen übernehmen?", preview.changes, "Übernehmen"))) return;
      await netIdle();
      const res = await api("PUT", "/api/net/settings", data);
      await afterNetChange(res, "Einstellungen übernommen");
    } catch (e) {
      showError(form, e.message);
      form.querySelector(".form-error").scrollIntoView({ block: "center", behavior: "smooth" });
    }
  });
}

// ---- Werkzeuge ----

async function renderNetTools(load) {
  const st = net.status;
  $("#service-info").replaceChildren(...[
    ["dnsmasq", st.service.state === "active" ? "läuft" : st.service.state],
    ["Beim Start", st.service.enabled ? "aktiviert" : "deaktiviert"],
    ["Letzter Auftrag", st.job ? `${st.job.message} (${fmtAgo(st.job.ts)})` : "–"],
  ].map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v))));
  renderProbe(st.probe);
  const cfg = net.config.config;
  $("#ignored-list").replaceChildren(...(cfg.ignored.length ? cfg.ignored.map((i) => el("div", { class: "saved-item" },
    el("span", {}, el("span", { class: "mono" }, i.mac), i.comment ? el("span", { class: "muted" }, ` · ${i.comment}`) : null),
    el("span", { class: "muted small" }, fmtAgo(i.ts)),
    el("button", { type: "button", class: "btn small ghost", onclick: () => unignore(i.mac) }, "Nicht mehr ignorieren")))
    : [el("p", { class: "muted small" }, "Keine.")]));
  if (load) {
    loadHistory();
    loadNetLog();
  }
}

function renderProbe(probe) {
  const box = $("#probe-result");
  if (!probe) {
    box.replaceChildren();
    return;
  }
  const own = net.config.config.server.ip;
  const items = probe.servers || [];
  box.replaceChildren(el("p", { class: "muted" }, `Zuletzt gesucht ${fmtAgo(probe.ts)}:`),
    items.length ? el("ul", { class: "change-list" }, items.map((s) => {
      const ip = s.server_id || s.source;
      return el("li", {}, el("span", { class: "mono" }, ip), ip === own ? " (dieser Server)" : el("strong", { class: "warn-text" }, " – anderer DHCP-Server"),
        s.offered ? el("span", { class: "muted" }, ` bot ${s.offered} an`) : null);
    })) : el("p", {}, probe.ok ? "Kein DHCP-Server hat geantwortet." : `Fehler: ${probe.error}`));
}

async function loadHistory() {
  const box = $("#history-list");
  try {
    const { history } = await api("GET", "/api/net/history");
    box.replaceChildren(...(history.length ? history.map((h, i) => el("details", { class: "history-item", open: i === 0 },
      el("summary", {},
        el("span", { class: "mono small" }, fmtDateTime(h.ts)),
        el("span", {}, h.changes.length ? h.changes[0] + (h.changes.length > 1 ? ` (+${h.changes.length - 1})` : "") : "ohne Änderung"),
        i === 0 ? el("span", { class: "tag current" }, "aktiv") : el("button", { type: "button", class: "btn small ghost", onclick: (e) => { e.preventDefault(); restoreHistory(h); } }, icon("undo"), "Zurückholen")),
      el("p", { class: "muted small" }, h.client ? `von ${h.client}` : ""),
      el("ul", { class: "change-list" }, h.changes.map((c) => el("li", {}, c)))))
      : [el("p", { class: "muted small" }, "Kein Verlauf vorhanden.")]));
  } catch (e) {
    box.replaceChildren(el("p", { class: "muted small" }, e.message));
  }
}

async function restoreHistory(h) {
  if (!(await confirmDialog("Diesen Stand zurückholen?", `Der Stand vom ${fmtDateTime(h.ts)} wird als Entwurf geladen und mit „Übernehmen“ wirksam.`, "Als Entwurf laden"))) return;
  try {
    const res = await api("POST", `/api/net/history/${h.id}/restore`);
    await afterDraftChange(res.message);
  } catch (e) {
    toast(e.message, "error");
  }
}

async function loadNetLog() {
  const kind = ($("#log-kind input:checked") || {}).value || "dhcp";
  const box = $("#log-table");
  try {
    const { entries } = await api("GET", `/api/net/logs/${kind}`);
    const rows = [...entries].reverse().slice(0, 100);
    box.replaceChildren(kind === "dhcp"
      ? dataTable("log", [
        { key: "ts", label: "Zeit", render: (e) => el("span", { class: "mono small" }, fmtDateTime(e.ts)) },
        { key: "type", label: "Meldung", render: (e) => el("span", { class: "tag" }, e.type) },
        { key: "detail", label: "Details", render: (e) => el("span", { class: "mono small break" }, e.detail) },
      ], rows, "Keine DHCP-Meldungen.")
      : dataTable("log", [
        { key: "ts", label: "Zeit", render: (e) => el("span", { class: "mono small" }, fmtDateTime(e.ts)) },
        { key: "text", label: "Meldung", render: (e) => el("span", { class: "mono small break" }, e.text) },
      ], rows, "Keine Meldungen."));
  } catch (e) {
    box.replaceChildren(el("p", { class: "muted small" }, e.message));
  }
}

async function unignore(mac) {
  try {
    await api("DELETE", `/api/net/ignored/${mac}`);
    toast("Wird wieder angezeigt", "success");
    await renderNetwork();
  } catch (e) {
    toast(e.message, "error");
  }
}

async function runNetTask(button, path, success) {
  await busy(button, async () => {
    try {
      const res = await api("POST", path);
      toast(res.message);
      await loadNetStatus();
      renderNetHeader();
      await watchNetJob(success);
    } catch (e) {
      toast(e.message, "error");
    }
  });
}

async function exportNet() {
  try {
    const data = await api("GET", "/api/net/export");
    const blob = new Blob([JSON.stringify(data, null, 1)], { type: "application/json" });
    const a = el("a", { href: URL.createObjectURL(blob), download: `wol-netzwerk-${new Date().toISOString().slice(0, 10)}.json` });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  } catch (e) {
    toast(e.message, "error");
  }
}

async function onRestoreFile(event) {
  const file = event.target.files[0];
  event.target.value = "";
  if (!file) return;
  let data;
  try {
    data = JSON.parse(await file.text());
  } catch (e) {
    toast("Keine gültige Sicherung (JSON erwartet)", "error");
    return;
  }
  try {
    const res = await api("POST", "/api/net/restore", { data });
    await afterDraftChange(res.message);
  } catch (e) {
    toast(e.message, "error");
  }
}

function openImport() {
  const form = $("#import-form");
  form.reset();
  showError(form, "");
  net.importPreview = null;
  $("#import-preview").hidden = true;
  $("#import-submit").disabled = true;
  $("#import-dialog").showModal();
}

async function onImportFile(event) {
  const form = $("#import-form");
  showError(form, "");
  const file = event.target.files[0];
  if (!file) return;
  if (file.size > 200 * 1024) return showError(form, "Die Datei ist zu groß.");
  try {
    const { preview } = await api("POST", "/api/net/import", { text: await file.text() });
    net.importPreview = preview;
    renderImportPreview();
  } catch (e) {
    showError(form, e.message);
  }
}

function renderImportPreview() {
  const p = net.importPreview;
  const st = p.settings;
  const labels = { network: "Netz", gateway: "Gateway", lease_time: "Lease-Zeit", mode: "Modus", domain: "Domäne (alt)", ntp: "NTP", dns: "DNS (alt)" };
  $("#import-settings").replaceChildren(...Object.entries(labels).filter(([k]) => st[k] != null).map(([k, label]) => {
    let value = st[k];
    if (k === "lease_time") value = fmtLease(value);
    if (k === "mode") value = value === "known-only" ? "nur bekannte Geräte" : "mit Adressbereich";
    if (Array.isArray(value)) value = value.join(", ");
    return el("div", {}, el("dt", {}, label), el("dd", {}, value));
  }));
  $("#import-warnings").replaceChildren(...p.warnings.map((w) => el("li", { class: "notice warn" }, icon("alert"), el("span", {}, w))));
  const ok = p.reservations.filter((r) => !r.problems.length).length;
  $("#import-summary").textContent = `${p.reservations.length} feste Zuordnungen gefunden: ${ok} werden übernommen, ${p.reservations.length - ok} übersprungen.`;
  $("#import-table").replaceChildren(dataTable("import", [
    { key: "name", label: "Name", render: (r) => el("div", {}, el("strong", {}, r.name || "–"),
      r.original_name !== r.name ? el("div", { class: "muted small" }, `war „${r.original_name}“`) : null) },
    { key: "ip", label: "IP", render: (r) => el("span", { class: "mono" }, r.ip || "–") },
    { key: "mac", label: "MAC", cls: "hide-sm", render: (r) => el("span", { class: "mono small" }, r.mac || "–") },
    { key: "state", label: "", render: (r) => r.problems.length
      ? el("span", { class: "import-skip" }, icon("x"), r.problems.join("; "))
      : el("span", { class: "import-ok" }, icon("check"), r.exists || "neu") },
  ], p.reservations.map((r) => ({ ...r, _cls: r.problems.length ? "is-disabled" : "" })), "Keine festen Zuordnungen gefunden."));
  $("#import-preview").hidden = false;
  $("#import-submit").disabled = !ok && !Object.keys(st).length;
}

async function onImportSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showError(form, "");
  if (form.elements.replace.checked && !(await confirmDialog("Vorhandene Reservierungen entfernen?",
    "Die importierten Reservierungen ersetzen alle vorhandenen (im Entwurf).", "Ersetzen", true))) return;
  await busy($("#import-submit"), async () => {
    try {
      const res = await api("POST", "/api/net/import/commit", {
        preview: net.importPreview, replace: form.elements.replace.checked, take_settings: form.elements.take_settings.checked,
      });
      $("#import-dialog").close();
      net.sub = "reservations";
      await afterDraftChange(res.message);
    } catch (e) {
      showError(form, e.message);
    }
  });
}

function bindNetwork() {
  for (const tab of $$("#net-subtabs .subtab")) tab.addEventListener("click", () => setNetSub(tab.dataset.sub));
  for (const btn of $$("[data-goto-sub]")) btn.addEventListener("click", () => setNetSub(btn.dataset.gotoSub));
  $("#net-apply").addEventListener("click", netApply);
  $("#net-discard").addEventListener("click", netDiscard);
  $("#approve-form").addEventListener("submit", onApproveSubmit);
  $("#res-search").addEventListener("input", renderReservations);
  for (const r of $$("#res-filter input")) r.addEventListener("change", renderReservations);
  $("#res-add").addEventListener("click", () => openResDialog());
  $("#res-form").addEventListener("submit", onResSubmit);
  $("#res-form").elements.mac.addEventListener("input", updateResVendor);
  $("#res-delete").addEventListener("click", onResDelete);
  $("#res-next-ip").addEventListener("click", nextFreeIp);
  $("#lease-search").addEventListener("input", () => renderLeases(false));
  $("#lease-refresh").addEventListener("click", () => renderLeases(true));
  $("#dns-test-form").addEventListener("submit", onDnsTest);
  $("#dns-stats-refresh").addEventListener("click", loadDnsStats);
  $("#dns-querylog-refresh").addEventListener("click", loadQueryLog);
  $("#rec-add").addEventListener("click", () => openRecDialog());
  $("#rec-form").addEventListener("submit", onRecSubmit);
  $("#rec-delete").addEventListener("click", onRecDelete);
  for (const r of $$("#rec-type input")) r.addEventListener("change", () => setRecType(r.value));
  $("#net-settings-form").addEventListener("submit", onSettingsSubmit);
  $("#net-settings-reset").addEventListener("click", renderNetSettings);
  for (const r of $$("#net-settings-form input[name='dhcp.mode']")) r.addEventListener("change", updateModeHint);
  $("#conditional-add").addEventListener("click", () => $("#conditional-list").append(conditionalRow()));
  $("#import-open").addEventListener("click", openImport);
  $("#import-form").elements.file.addEventListener("change", onImportFile);
  $("#import-form").addEventListener("submit", onImportSubmit);
  $("#export-btn").addEventListener("click", exportNet);
  $("#restore-file").addEventListener("change", onRestoreFile);
  $("#probe-btn").addEventListener("click", (e) => runNetTask(e.currentTarget, "/api/net/probe"));
  $("#restart-btn").addEventListener("click", (e) => runNetTask(e.currentTarget, "/api/net/restart", "dnsmasq neu gestartet"));
  for (const r of $$("#log-kind input")) r.addEventListener("change", loadNetLog);
  $("#device-form").elements.mac.addEventListener("change", updateDeviceNetHint);
}

// ---------- Start ----------

async function updateSession() {
  const session = await api("GET", "/api/session");
  state.authRequired = session.auth_required;
  state.hotspot = session.hotspot;
  $("#logout").hidden = !session.auth_required;
  const banner = $("#banner");
  if (session.hotspot) {
    banner.hidden = false;
    banner.replaceChildren(
      el("span", {}, el("strong", {}, "Einrichtungsmodus: "), "Der WOL-Server ist mit keinem WLAN verbunden."),
      el("button", { class: "btn small", type: "button", onclick: () => { setView("system"); openWifiDialog(); } }, "WLAN einrichten"));
    return session;
  }
  banner.hidden = session.auth_required;
  banner.replaceChildren(
    el("span", {}, el("strong", {}, "Kein Passwort gesetzt. "), "Jeder im lokalen Netz kann Geräte steuern."),
    el("button", { class: "btn small", type: "button", onclick: () => setView("system") }, "Passwort festlegen"));
  return session;
}

async function startApp() {
  const session = await updateSession();
  if (session.auth_required && !session.authenticated) return showLogin();
  const info = await api("GET", "/api/info");
  state.info = info;
  $("#hostname").textContent = info.hostname;
  document.title = `WOL-Server · ${info.hostname}`;
  $("#boot").hidden = true;
  $("#login").hidden = true;
  $("#app").hidden = false;
  if (session.hotspot) {
    // Aufruf über den Einrichtungs-Hotspot (Captive Portal): direkt zur WLAN-Auswahl
    setView("system");
    await renderWifi().catch(() => {});
    if (state.wifi) openWifiDialog();
    return;
  }
  const [fromHash, sub] = location.hash.slice(1).split("/");
  if (fromHash === "network" && NET_SUBS.includes(sub)) net.sub = sub;
  setView(["devices", "schedules", "events", "network", "system"].includes(fromHash) ? fromHash : "devices");
  refreshNetBadge();
}

function bind() {
  $("#login-form").addEventListener("submit", onLogin);
  $("#logout").addEventListener("click", onLogout);
  for (const tab of $$(".tab")) tab.addEventListener("click", () => setView(tab.dataset.view));
  $("#add-device").addEventListener("click", () => openDeviceDialog());
  $("#add-schedule").addEventListener("click", () => openScheduleDialog());
  $("#clear-events").addEventListener("click", onClearEvents);
  $("#device-form").addEventListener("submit", onDeviceSubmit);
  $("#delete-device").addEventListener("click", onDeviceDelete);
  $("#sleep-method").addEventListener("change", (e) => setSleepMethod(e.target.value));
  for (const btn of $$("[data-cmd]")) {
    btn.addEventListener("click", () => { $("#device-form").elements.ssh_command.value = btn.dataset.cmd; });
  }
  $("#schedule-form").addEventListener("submit", onScheduleSubmit);
  $("#schedule-form").elements.device_id.addEventListener("change", updateSleepAvailability);
  $("#delete-schedule").addEventListener("click", onScheduleDelete);
  for (const btn of $$("[data-days]")) {
    btn.addEventListener("click", () => setDays(btn.dataset.days.split(",").map(Number)));
  }
  for (const btn of $$("[data-close]")) btn.addEventListener("click", () => btn.closest("dialog").close());
  $("#copy-key").addEventListener("click", copyKey);
  $("#password-form").addEventListener("submit", onPasswordSubmit);
  $("#wifi-add").addEventListener("click", openWifiDialog);
  $("#wifi-rescan").addEventListener("click", scanWifi);
  $("#wifi-form").addEventListener("submit", onWifiSubmit);
  $("#wifi-form").elements.show_password.addEventListener("change", (e) => {
    $("#wifi-form").elements.password.type = e.target.checked ? "text" : "password";
  });
  $("#wifi-form").elements.ssid.addEventListener("input", () => {
    for (const item of $$(".scan-item")) item.classList.remove("selected");
  });
  $("#hotspot-form").addEventListener("submit", onHotspotSubmit);
  bindNetwork();
  $("#vpn-upload").addEventListener("click", openVpnDialog);
  $("#vpn-form").elements.file.addEventListener("change", onVpnFile);
  $("#vpn-form").addEventListener("submit", onVpnSubmit);
  $("#vpn-toggle").addEventListener("click", onVpnToggle);
  $("#vpn-remove").addEventListener("click", onVpnRemove);
  buildDayPicker();

  // Status nur abfragen, solange die Seite sichtbar ist – spart Last auf dem Pi.
  setInterval(() => {
    if (document.visibilityState !== "visible" || $("#app").hidden) return;
    if (state.view === "devices") refreshStatus();
    // Neue Einträge (z. B. von Zeitplänen) auch bei offenem Protokoll anzeigen
    if (state.view === "events") renderEvents().catch(() => {});
    // Netzwerk: Übersicht (unbekannte Geräte) nachziehen; laufende Aufträge beobachtet watchNetJob()
    if (state.view === "network" && net.sub === "overview" && !net.watching) {
      loadNetStatus().then(() => { renderNetHeader(); renderNetOverview(); }).catch(() => {});
    }
    // WLAN-Status nur nachziehen, solange sich etwas tut (Hotspot, Verbindungsaufbau)
    if (state.view === "system" && state.wifi && (state.wifi.mode !== "client" || state.wifi.pending)) {
      renderWifi().catch(() => {});
    }
    // VPN-Status nur während Einrichtung oder Verbindungsaufbau nachziehen
    if (state.view === "system" && vpnBusy(state.vpn)) renderVpn().catch(() => {});
  }, STATUS_INTERVAL);
  // Lesezeichen und Zurück/Vor im Browser: #network/leases usw.
  window.addEventListener("hashchange", () => {
    if ($("#app").hidden) return;
    const [view, sub] = location.hash.slice(1).split("/");
    if (view === "network" && NET_SUBS.includes(sub) && state.view === "network") {
      if (sub !== net.sub) setNetSub(sub, false);
    } else if (["devices", "schedules", "events", "network", "system"].includes(view) && view !== state.view) {
      if (view === "network" && NET_SUBS.includes(sub)) net.sub = sub;
      setView(view);
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && !$("#app").hidden) refreshView().catch(() => {});
  });
}

document.addEventListener("DOMContentLoaded", () => {
  bind();
  startApp().catch((e) => {
    $("#boot").textContent = `Fehler: ${e.message}`;
  });
});
