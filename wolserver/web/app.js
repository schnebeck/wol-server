"use strict";

// ---------- Hilfsfunktionen ----------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const DAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
const ACTIONS = { wake: "Aufwecken", sleep: "Schlafen legen" };
const SLEEP_METHODS = { none: "–", ssh: "SSH", http: "HTTP", sol: "Sleep-on-LAN" };
const STATUS_INTERVAL = 10000;

const state = {
  view: "devices",
  devices: [],
  schedules: [],
  status: {},
  serverNow: null,
  authRequired: false,
  info: null,
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
  $("#confirm-text").textContent = text;
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
  history.replaceState(null, "", `#${view}`);
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
      el("h3", {}, "Noch keine Geräte"),
      el("p", { class: "muted" }, "Lege ein Gerät mit seiner MAC-Adresse an, um es per Wake-on-LAN aufzuwecken."),
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

function openDeviceDialog(device = null) {
  editingDevice = device;
  const form = $("#device-form");
  form.reset();
  showError(form, "");
  const d = device || { broadcast: "255.255.255.255", wol_port: 9, sleep_method: "none", ssh_port: 22, ssh_command: "sudo systemctl suspend", http_method: "POST" };
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
  const extra = count ? ` Zugehörige Zeitpläne (${count}) werden ebenfalls gelöscht.` : "";
  if (!(await confirmDialog(`${device.name} löschen?`, `Das Gerät wird entfernt.${extra}`, "Löschen", true))) return;
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
      el("h3", {}, "Erst ein Gerät anlegen"),
      el("p", { class: "muted" }, "Zeitpläne beziehen sich immer auf ein Gerät.")));
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
    list.replaceChildren(el("div", { class: "empty" }, el("h3", {}, "Noch keine Einträge"),
      el("p", { class: "muted" }, "Hier erscheinen manuelle und geplante Aktionen.")));
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
  const info = await api("GET", "/api/info");
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
  renderNetwork(info.network);
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

function renderNetwork(net) {
  const ifaces = net.interfaces.filter((i) => i.up && i.address);
  const vpn = ifaces.filter((i) => i.kind === "vpn");
  const wifi = ifaces.find((i) => i.wifi);
  const port = net.port === 80 ? "" : `:${net.port}`;
  const rows = [];
  if (wifi) {
    const [label, cls] = signalLabel(wifi.wifi.signal_dbm);
    rows.push(["WLAN-Signal", el("span", { class: `signal ${cls}` }, `${wifi.wifi.signal_dbm} dBm · ${label}`)]);
  }
  rows.push(["VPN", vpn.length
    ? el("span", { class: "signal good" }, `verbunden · ${vpn.map((i) => i.address).join(", ")}`)
    : el("span", { class: "muted" }, "nicht verbunden")]);
  rows.push(["Magic Packets über", net.wol_interface || "Default-Route"]);
  $("#sys-network").replaceChildren(...rows.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v))));
  $("#sys-ifaces").replaceChildren(...ifaces.map((i) =>
    el("div", { class: "iface" },
      el("span", { class: `tag kind-${i.kind}` }, IFACE_KINDS[i.kind] || i.kind),
      el("span", { class: "mono" }, i.name),
      el("a", { class: "mono", href: `http://${i.address}${port}/` }, i.address))));
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

// ---------- Start ----------

async function updateSession() {
  const session = await api("GET", "/api/session");
  state.authRequired = session.auth_required;
  $("#logout").hidden = !session.auth_required;
  const banner = $("#banner");
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
  $("#hostname").textContent = info.hostname;
  document.title = `WOL-Server · ${info.hostname}`;
  $("#boot").hidden = true;
  $("#login").hidden = true;
  $("#app").hidden = false;
  const fromHash = location.hash.slice(1);
  setView(["devices", "schedules", "events", "system"].includes(fromHash) ? fromHash : "devices");
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
  buildDayPicker();

  // Status nur abfragen, solange die Seite sichtbar ist – spart Last auf dem Pi.
  setInterval(() => {
    if (document.visibilityState === "visible" && state.view === "devices" && !$("#app").hidden) refreshStatus();
  }, STATUS_INTERVAL);
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
