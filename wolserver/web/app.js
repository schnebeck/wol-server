/*
 * WOL-Server - wolserver/web/app.js
 * The web GUI: devices, schedules, log, network and system.
 *
 *   Copyright (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
 *   Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
 *   Written by Anthropic Claude Opus 5.5 - AI generated content.
 *
 *   Free software under the GNU General Public License, version 3 or later.
 *   There is no warranty, to the extent permitted by law. The full text is in
 *   LICENSES/GPL-3.0-or-later.txt.
 *
 * SPDX-FileCopyrightText: (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
 * SPDX-FileContributor: Anthropic Claude Opus 5.5 (AI generated content)
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

"use strict";

// ---------- Helpers ----------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"].map((d) => t(d));
const ACTIONS = { wake: t("Wake"), sleep: t("Put to sleep"), net: t("Change"), power_on: t("Power on"), power_off: t("Power off"), power_cycle: t("Power cycle"), alert: t("Warning"), secure: t("Secured"), request: t("Request") };
const SLEEP_METHODS = { none: "–", ssh: "SSH", http: "HTTP", sol: "Sleep-on-LAN" };
const STATUS_INTERVAL = 10000;
const PLUG_INTERVAL = 5000;
const VIEWS = ["devices", "plugs", "schedules", "events", "network", "system"];
const PLUG_SUBS = ["overview", "energy"];
const ENERGY_INTERVAL = 60000;

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
  plugs: [],
  plugDrivers: null,
  plugSettings: { price_kwh: null, currency: "€" },
  power: {},
  requests: {},
  holds: {},
  conflicts: {},
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
  users: ["M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z", "M2.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6", "M16 4.6a3.5 3.5 0 0 1 0 6.8", "M18.5 14.4c1.8.8 3 2.8 3 5.6"],
  alert: ["M12 4l9 16H3z", "M12 10v4.5", "M12 17.5v.01"],
  info: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M12 11v5.5", "M12 7.5v.01"],
  device: ["M4 5h16v11H4z", "M9 20h6", "M12 16v4"],
  undo: ["M9 14L4 9l5-5", "M4 9h10a6 6 0 0 1 0 12h-3"],
  plug: ["M9 3v5", "M15 3v5", "M6 8h12v3a6 6 0 0 1-12 0z", "M12 17v4"],
  log: ["M9 6h11", "M9 12h11", "M9 18h11", "M4.5 6h.01", "M4.5 12h.01", "M4.5 18h.01"],
  network: ["M9.5 3h5v5h-5z", "M12 8v4", "M5.5 12h13", "M5.5 12v4", "M18.5 12v4", "M3 16h5v5H3z", "M16 16h5v5h-5z"],
  settings: ["M4 7h9", "M17 7h3", "M15 5v4", "M4 17h3", "M11 17h9", "M9 15v4"],
  bolt: ["M13 3L5 13.5h6L10 21l8-10.5h-6z"],
  chevron: ["M7 10l5 5 5-5"],
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
    throw new HttpError(0, t("Server not reachable"));
  }
  let data = null;
  try {
    data = await res.json();
  } catch (e) {
    data = null;
  }
  if (res.status === 401 && path !== "/api/login") showLogin();
  // Server messages are English; show them in the chosen language
  if (data && typeof data.message === "string") data.message = tr(data.message);
  if (data && typeof data.error === "string") data.error = tr(data.error);
  if (!res.ok && !(data && "ok" in data)) {
    throw new HttpError(res.status, (data && data.error) || t("Error {0}", res.status));
  }
  return data;
}

function toast(message, kind = "info") {
  const node = el("div", { class: `toast ${kind}`, role: "status" },
    kind === "error" ? icon("x") : icon("check"), el("span", {}, tr(message)));
  $("#toasts").append(node);
  setTimeout(() => node.classList.add("leaving"), 4200);
  setTimeout(() => node.remove(), 4600);
}

function confirmDialog(title, text, okLabel = "OK", danger = false) {
  const dlg = $("#confirm-dialog");
  $("#confirm-title").textContent = title;
  // Text or list (e.g. the changes before applying)
  $("#confirm-text").replaceChildren(Array.isArray(text) ? el("ul", { class: "change-list" }, text.map((item) => el("li", {}, tr(item)))) : text);
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
  box.textContent = tr(message) || "";
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

// The server sends local time without a zone ("2026-10-24T07:30"), so compare strings.
function fmtNext(iso) {
  if (!iso) return "";
  const [date, time] = iso.split("T");
  const today = (state.serverNow || "").slice(0, 10);
  if (date === today) return t("today {0}", time);
  const d = new Date(`${date}T00:00:00`);
  const now = new Date(`${today}T00:00:00`);
  const diff = Math.round((d - now) / 86400000);
  if (diff === 1) return t("tomorrow {0}", time);
  return `${DAYS[(d.getDay() + 6) % 7]}, ${d.toLocaleDateString(I18N.locale, { day: "numeric", month: "numeric" })} ${time}`;
}

function fmtDateTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleString(I18N.locale, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
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
  if (key === "0,1,2,3,4,5,6") return t("daily");
  if (key === "0,1,2,3,4") return t("weekdays");
  if (key === "5,6") return t("at the weekend");
  return days.map((d) => DAYS[d]).join(", ");
}

const deviceById = (id) => state.devices.find((d) => d.id === id);
const plugById = (id) => state.plugs.find((p) => p.id === id);

// ---------- Sign-in ----------

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
  history.replaceState(null, "", view === "network" ? `#network/${net.sub}` : view === "plugs" ? `#plugs/${plugView.sub}` : `#${view}`);
  refreshView().catch((e) => toast(e.message, "error"));
}

async function refreshView() {
  if (state.view === "devices") {
    await Promise.all([loadDevices(), loadSchedules(), loadPlugs(false).catch(() => {})]);
    renderDevices();
    refreshStatus();
  } else if (state.view === "schedules") {
    await Promise.all([loadDevices(), loadSchedules(), loadPlugs(false)]);
    renderSchedules();
  } else if (state.view === "plugs") {
    showPlugSub();
    if (plugView.sub === "energy") {
      await loadPlugs();
      renderPlugsLine();
      await renderEnergy();
    } else {
      await Promise.all([loadPlugs(), loadSchedules(), loadDevices()]);
      renderPlugs();
      if (plugsNeedStatus()) refreshStatus();
    }
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

// ---------- Devices ----------

let statusBusy = false;

async function refreshStatus() {
  if (statusBusy || !state.devices.length) return;
  statusBusy = true;
  try {
    const data = await api("GET", "/api/status");
    state.status = data.status;
    state.power = data.power || {};
    state.requests = data.requests || {};
    state.holds = data.holds || {};
    state.conflicts = data.conflicts || {};
    const online = Object.values(data.status).filter((s) => s === true).length;
    $("#status-line").textContent =
      t("{0} of {1} online · checked {2}", online, state.devices.length, new Date(data.checked * 1000).toLocaleTimeString(I18N.locale));
    renderDevices();
    if (plugsOverviewShown()) renderPlugs(); // the plug cards name the state of their devices
  } catch (e) {
    if (e.status !== 401) $("#status-line").textContent = e.message;
  } finally {
    statusBusy = false;
  }
}

function nextActionFor(id, key = "device_id") {
  return state.schedules
    .filter((s) => s[key] === id && s.next_run)
    .sort((a, b) => a.next_run.localeCompare(b.next_run))[0];
}

function renderDevices() {
  const list = $("#device-list");
  if ($("details.menu[open]", list)) return; // do not close an open power menu with a status refresh
  if (!state.devices.length) {
    $("#status-line").textContent = " ";
    list.replaceChildren(el("div", { class: "empty" },
      el("h3", {}, t("No devices")),
      el("p", { class: "muted" }, t("Devices are woken via Wake-on-LAN using their MAC address.")),
      el("button", { class: "btn primary", type: "button", onclick: () => openDeviceDialog() }, t("Add first device"))));
    return;
  }
  list.replaceChildren(...state.devices.map(deviceCard));
}

// Since when a device draws running power without answering (to tell "starting" from "hangs")
const silentSince = new Map();
const HANG_SECONDS = 300;

// Status from ping and power: [css class, label]
function deviceState(d) {
  const st = state.status[d.id];
  const p = state.power[d.id];
  if (st === true || !p || !p.reachable) {
    silentSince.delete(d.id);
    if (st === true) return ["online", t("Online")];
    return st === false ? ["offline", t("Offline")] : ["unknown", d.host ? t("Checking …") : t("No host")];
  }
  if (!p.on) {
    silentSince.delete(d.id);
    return ["offline", t("No power")];
  }
  if (p.power_w == null || p.power_w <= p.standby_w) {
    silentSince.delete(d.id);
    return ["offline", st === false ? t("Standby") : t("Checking …")];
  }
  if (st !== false) return ["unknown", t("Checking …")];
  if (p.running_w != null && p.power_w > p.running_w) {
    if (!silentSince.has(d.id)) silentSince.set(d.id, Date.now());
    if (Date.now() - silentSince.get(d.id) > HANG_SECONDS * 1000) return ["warn", t("Not responding")];
  } else {
    silentSince.delete(d.id);
  }
  return ["unknown", t("Starting or shutting down")];
}

function deviceCard(d) {
  const [cls, label] = deviceState(d);
  const power = state.power[d.id];
  const next = nextActionFor(d.id);
  const canSleep = d.sleep_method !== "none";
  const online = state.status[d.id] === true;
  const offline = state.status[d.id] === false;
  return el("article", { class: `card device ${cls}` },
    el("header", { class: "card-head" },
      el("span", { class: `dot ${cls}`, "aria-hidden": "true" }),
      el("div", { class: "card-title" },
        el("h3", {}, d.name),
        el("span", { class: `status-label ${cls}` }, label)),
      power ? powerMenu({ id: power.plug_id, name: power.plug }, power) : null,
      el("button", { class: "icon-btn", type: "button", title: t("Edit"), "aria-label": t("Edit {0}", d.name), onclick: () => openDeviceDialog(d) }, icon("edit"))),
    el("dl", { class: "meta" },
      el("div", {}, el("dt", {}, "MAC"), el("dd", { class: "mono" }, d.mac)),
      el("div", {}, el("dt", {}, "Host"), el("dd", { class: "mono" }, d.host || "–")),
      el("div", {}, el("dt", {}, t("Sleep")), el("dd", {}, SLEEP_METHODS[d.sleep_method]))),
    el("p", { class: next ? "next" : "next muted" }, icon("clock"),
      next ? el("span", {}, el("strong", {}, ACTIONS[next.action]), " ", fmtNext(next.next_run)) : t("No active schedule")),
    requestsLine(d),
    // What the server found contradictory in the state shown here (also in its log)
    (state.conflicts[d.id] || []).map((message) => el("p", { class: "plug-warn" }, icon("alert"), " ", tr(message))),
    // Wake only what is not online, sleep only what is not known to be off (ping status, not the guess)
    el("div", { class: "card-actions" },
      el("button", {
        class: "btn wake", type: "button", disabled: online, title: online ? t("Already online") : "",
        onclick: (e) => deviceAction(d, "wake", e.currentTarget),
      }, icon("power"), t("Wake")),
      el("button", {
        class: "btn sleep", type: "button", disabled: !canSleep || offline,
        title: !canSleep ? t("No sleep method configured") : offline ? t("Already off or asleep") : "",
        onclick: (e) => deviceAction(d, "sleep", e.currentTarget),
      }, icon("moon"), t("Sleep"))));
}

// Requests: clients that hold the device through the API (they keep it awake), and a remembered sleep
function requestsLine(d) {
  const r = state.requests && state.requests[d.id];
  if (!r) return null;
  const clients = r.requests.map((q) => q.client);
  const hold = state.holds && state.holds[d.id];
  // Held on the device: the clients' work runs. Otherwise the hold's state, if there is one
  const running = hold && hold.state === "held";
  const who = running ? t(clients.length > 1 ? "{0} are running" : "{0} is running", clients.join(", ")) : clients.join(", ");
  const holdText = !hold || running ? "" : ` · ${{ waiting: t("hold waits for the device"), failed: t("hold failed"),
    asleep: t("asleep, woken again") }[hold.state] || ""}`;
  const text = clients.length
    ? t("In use: {0}", who) + holdText + (r.sleep ? ` · ${t("sleep requested")}` : "")
    : t("Sleep requested by {0}", tr(r.sleep.by));
  return el("p", { class: "next in-use", title: hold ? (hold.error ? tr(hold.error) : running ? t("held on the device") : "") : "" },
    icon("users"), el("span", {}, text),
    el("button", { type: "button", class: "linkish", onclick: () => openRequestsDialog(d) }, t("Details")));
}

let requestsDevice = null;

async function openRequestsDialog(device) {
  requestsDevice = device;
  $("#requests-title").textContent = t("Requests for {0}", device.name);
  $("#requests-dialog").showModal();
  await loadRequests();
}

async function loadRequests() {
  const device = requestsDevice;
  let data;
  try {
    data = await api("GET", `/api/devices/${device.id}/requests`);
  } catch (e) {
    $("#requests-list").replaceChildren(el("div", { class: "table-empty muted" }, e.message));
    return;
  }
  const time = (ts) => new Date(ts * 1000).toLocaleString(I18N.locale, { weekday: "short", hour: "2-digit", minute: "2-digit" });
  const fact = (label, value) => el("span", {}, el("span", { class: "muted" }, `${label} `), value);
  $("#requests-list").replaceChildren(data.requests.length ? el("ul", { class: "request-list" },
    data.requests.map((q) => el("li", {},
      el("div", { class: "request-main" },
        el("strong", {}, q.client), el("span", { class: "muted small" }, tr(q.source)),
        el("div", { class: "request-facts small" },
          fact(t("Since"), time(q.created)),
          q.ttl ? fact(t("Last heartbeat"), fmtAgo(q.renewed)) : null,
          fact(t("Expires"), q.expires ? time(q.expires) : t("until released")))),
      el("button", { type: "button", class: "btn small danger ghost", onclick: (e) => endRequest(q, e.currentTarget) }, t("End")))))
    : el("div", { class: "table-empty muted" }, t("No open requests.")));
  const sleep = $("#requests-sleep");
  sleep.hidden = !data.sleep;
  if (data.sleep) {
    const when = data.sleep.at ? t("runs at {0}", time(data.sleep.at)) : t("waits for the open requests");
    sleep.replaceChildren(el("span", {}, icon("moon"), " ", t("Sleep requested by {0}", tr(data.sleep.by)), ", ", when),
      el("button", { type: "button", class: "btn small", onclick: (e) => cancelRemoteSleep(e.currentTarget) }, t("Cancel")));
  }
}

async function endRequest(q, button) {
  if (!(await confirmDialog(t("End the request of {0}?", q.client),
    t("The client may still be working; without its request the device can go to sleep."), t("End"), true))) return;
  await busy(button, async () => {
    try {
      await api("DELETE", `/api/devices/${requestsDevice.id}/requests/${q.id}`);
    } catch (e) {
      toast(e.message, "error");
    }
  });
  await loadRequests();
  refreshStatus();
}

async function cancelRemoteSleep(button) {
  await busy(button, async () => {
    try {
      await api("DELETE", `/api/devices/${requestsDevice.id}/sleep`);
    } catch (e) {
      toast(e.message, "error");
    }
  });
  await loadRequests();
  refreshStatus();
}

// The power of the plug in the card head, and its switch behind it: same safety rules as in the Plugs tab
function powerMenu(plug, power) {
  const off = !power.reachable || !power.on || power.locked;
  const item = (action, label, disabled, title) => el("button", {
    type: "button", class: "menu-item", disabled, title,
    onclick: (e) => {
      e.currentTarget.closest("details").open = false;
      plugAction(plug, action, e.currentTarget.closest("details").querySelector("summary"));
    },
  }, label);
  const value = power.reachable ? (power.on ? `${fmtWatt(power.power_w)} W` : t("Power off")) : "–";
  return el("details", { class: "menu power-menu" },
    el("summary", { class: "power-badge", title: t("Plug {0}", plug.name), "aria-label": `${t("Power")}: ${t("Plug {0}", plug.name)}, ${value}` },
      icon("bolt"), value, icon("chevron")),
    el("div", { class: "menu-list" },
      item("on", t("Power on"), !power.reachable || power.on),
      item("off", t("Power off"), off),
      item("cycle", t("Power cycle"), off, t("Off, on again after 10 s - runs on the plug itself"))));
}

async function deviceAction(device, action, button) {
  if (action === "sleep" &&
      !(await confirmDialog(t("Put {0} to sleep?", device.name), t("Unsaved work on the device may be lost."), t("Put to sleep")))) {
    return;
  }
  const send = (body) => busy(button, async () => {
    try {
      const res = await api("POST", `/api/devices/${device.id}/${action}`, body);
      if (!res.confirm) toast(`${device.name}: ${res.message}`, res.ok ? "success" : "error");
      return res;
    } catch (e) {
      toast(`${device.name}: ${e.message}`, "error");
      return null;
    }
  });
  const res = await send();
  // Open requests: clients still work on the device
  if (res && res.confirm && await confirmDialog(t("Put {0} to sleep anyway?", device.name),
    `${res.message}. ${t("Their work on the device may be lost; the requests stay open.")}`, t("Put to sleep"), true)) {
    await send({ force: true });
  }
  // After waking, a PC usually needs 10-60 s: refresh the status at short intervals.
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
  form.elements.hold.checked = Boolean(d.hold);
  form.elements.hold_command.value = d.hold_command || DEFAULT_HOLD_COMMAND;
  $("#hold-command-field").hidden = !d.hold;
  setSleepMethod(d.sleep_method || "none");
  $("#device-dialog-title").textContent = device ? t("Edit device") : t("New device");
  $("#delete-device").hidden = !device;
  fillDevicePlug(device);
  $("#device-dialog").showModal();
  updateDeviceNetHint();
  if (!state.plugDrivers) loadPlugs(false).then(() => editingDevice === device && fillDevicePlug(device)).catch(() => {});
}

// Keep awake while requested (hold.py): as models.DEFAULT_HOLD_COMMAND
const DEFAULT_HOLD_COMMAND = 'systemd-inhibit --what=sleep:idle --who=WOL-Server --why="Requested via the WOL-Server" --mode=block cat';

const WAKE_HINTS = {
  wol: t("Magic packet only, as without a plug."),
  power: t("Switches the plug on; for devices that start when the power returns (BIOS setting, Raspberry Pi). With the plug already on and the device in standby, a short power cycle starts it."),
  power_wol: t("Switches the plug on if it is off, waits 15 s for the network card and sends the magic packet."),
};

// The plug of a device: chosen in the device dialog, the same link as "Consumer" in the plug dialog
function fillDevicePlug(device) {
  const form = $("#device-form");
  const linked = device ? state.plugs.find((p) => p.consumer && p.consumer.kind === "device" && p.consumer.id === device.id) : null;
  $("#device-plug-group").hidden = !state.plugs.length;
  $("#device-plug").replaceChildren(el("option", { value: "" }, t("– none –")), ...state.plugs.map((p) => {
    const other = p.consumer && (p.consumer.kind === "self" || (p.consumer.kind === "device" && (!device || p.consumer.id !== device.id)))
      ? p.consumer_name || deviceById(p.consumer.id)?.name : null;
    return el("option", { value: p.id }, other ? `${p.name} (${t("now: {0}", other)})` : p.name);
  }));
  form.elements.plug_id.value = linked ? linked.id : "";
  const method = (linked && linked.wake_method) || "wol";
  for (const radio of form.querySelectorAll("input[name=wake_method]")) radio.checked = radio.value === method;
  form.elements.cut_after_sleep.checked = Boolean(linked && linked.cut_after_sleep);
  updateDevicePlug();
}

function updateDevicePlug() {
  const form = $("#device-form");
  const plug = plugById(form.elements.plug_id.value);
  $("#device-plug-options").hidden = !plug;
  const method = form.querySelector("input[name=wake_method]:checked")?.value || "wol";
  $("#wake-method-hint").textContent = WAKE_HINTS[method];
  const power = editingDevice && state.power[editingDevice.id];
  $("#device-plug-hint").textContent = plug && power && power.plug_id === plug.id && power.reachable
    ? t("Measured now: {0} W", fmtWatt(power.power_w)) : "";
}

// Show the device's static IP in the network (DHCP reservation), only if network management is running.
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
    ? el("span", {}, icon("check"), t("Static IP in the network:"), " ", el("strong", { class: "mono" }, res.ip),
      res.name ? ` (${res.name}.${domain})` : "")
    : el("span", {}, t("No static IP in the network."), " ",
      el("button", { type: "button", class: "linkish", disabled: !mac, onclick: () => openResDialog(null, {
        mac, name: $("#device-form").elements.name.value }) }, t("Reserve …"))));
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
  // Only with SSH: the hold uses the same access (the server refuses it otherwise)
  data.hold = data.sleep_method === "ssh" && form.elements.hold.checked;
  const plugLink = {
    plug_id: data.plug_id || null, wake_method: data.wake_method || "wol", cut_after_sleep: Boolean(form.elements.cut_after_sleep.checked),
  };
  for (const key of ["plug_id", "wake_method", "cut_after_sleep"]) delete data[key];
  await busy($("button[type=submit]", form), async () => {
    try {
      const saved = editingDevice ? await api("PUT", `/api/devices/${editingDevice.id}`, data) : await api("POST", "/api/devices", data);
      if (state.plugs.length) await api("PUT", `/api/devices/${saved.id}/plug`, plugLink);
      $("#device-dialog").close();
      toast(editingDevice ? t("Device saved") : t("Device added"), "success");
      await refreshView();
    } catch (e) {
      showError(form, e.message);
    }
  });
}

async function onDeviceDelete() {
  const device = editingDevice;
  const count = state.schedules.filter((s) => s.device_id === device.id).length;
  const text = count ? t("The device and its schedules ({0}) will be deleted.", count) : t("The device will be deleted.");
  if (!(await confirmDialog(t("Delete {0}?", device.name), text, t("Delete"), true))) return;
  try {
    await api("DELETE", `/api/devices/${device.id}`);
    $("#device-dialog").close();
    toast(t("Device deleted"), "success");
    await refreshView();
  } catch (e) {
    toast(e.message, "error");
  }
}

// ---------- Schedules ----------

function renderSchedules() {
  const list = $("#schedule-list");
  $("#server-time").textContent = state.serverNow
    ? t("Server time: {0}", state.serverNow.slice(11, 16))
    : " ";
  if (!state.devices.length && !state.plugs.length) {
    list.replaceChildren(el("div", { class: "empty" },
      el("h3", {}, t("No devices")),
      el("p", { class: "muted" }, t("Every schedule belongs to a device or a plug. They are added in the \"Devices\" and \"Plugs\" tabs."))));
    return;
  }
  if (!state.schedules.length) {
    list.replaceChildren(el("div", { class: "empty" },
      el("h3", {}, t("No schedules")),
      el("p", { class: "muted" }, t("Wake devices, put them to sleep or switch plugs automatically at fixed times.")),
      el("button", { class: "btn primary", type: "button", onclick: () => openScheduleDialog() }, t("Create schedule"))));
    return;
  }
  list.replaceChildren(...state.schedules.map(scheduleRow));
}

const ACTION_ICONS = { wake: "power", sleep: "moon", power_on: "plug", power_off: "plug", power_cycle: "undo" };

function scheduleRow(s) {
  const target = s.plug_id ? plugById(s.plug_id) : deviceById(s.device_id);
  return el("div", { class: `row sched ${s.enabled ? "" : "disabled"}` },
    el("label", { class: "switch", title: s.enabled ? t("Disable") : t("Enable") },
      el("input", { type: "checkbox", checked: s.enabled, "aria-label": t("Enabled"), onchange: (e) => toggleSchedule(s, e.currentTarget) }),
      el("span", { class: "slider" })),
    el("div", { class: "sched-time mono" }, s.time),
    el("div", { class: "sched-main" },
      el("div", { class: "sched-title" },
        el("span", { class: `badge ${s.action}` }, icon(ACTION_ICONS[s.action] || "clock"), ACTIONS[s.action]),
        el("span", { class: "sched-device" }, !target ? "?" : s.plug_id && target.consumer ? `${target.name} (${target.consumer.label})` : target.name)),
      el("div", { class: "muted small" }, [fmtDays(s.days), s.label, s.force && t("switches even under load")].filter(Boolean).join(" · "))),
    el("div", { class: "days", "aria-label": fmtDays(s.days) },
      DAYS.map((name, i) => el("span", { class: s.days.includes(i) ? "day on" : "day" }, name))),
    el("div", { class: "sched-next small" },
      s.enabled && s.next_run ? [el("span", { class: "muted" }, t("Next:"), " "), fmtNext(s.next_run)] : el("span", { class: "muted" }, t("paused"))),
    el("button", { class: "icon-btn", type: "button", title: t("Edit"), "aria-label": t("Edit schedule"), onclick: () => openScheduleDialog(s) }, icon("edit")));
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
  const select = form.elements.target;
  select.replaceChildren(...[
    state.devices.length && el("optgroup", { label: t("Devices") }, state.devices.map((d) => el("option", { value: `device:${d.id}` }, d.name))),
    state.plugs.length && el("optgroup", { label: t("Plugs") }, state.plugs.map((p) => el("option", { value: `plug:${p.id}` },
      p.consumer ? `${p.name} (${p.consumer.label})` : p.name))),
  ].filter(Boolean));
  const first = state.devices.length ? `device:${state.devices[0].id}` : state.plugs.length ? `plug:${state.plugs[0].id}` : "";
  const s = schedule || { action: "", time: "07:00", days: [0, 1, 2, 3, 4], enabled: true, label: "", force: false };
  select.value = schedule ? (s.plug_id ? `plug:${s.plug_id}` : `device:${s.device_id}`) : first;
  renderScheduleActions(s.action);
  form.elements.force.checked = Boolean(s.force);
  updateForceRow();
  form.elements.time.value = s.time;
  form.elements.label.value = s.label || "";
  form.elements.enabled.checked = s.enabled;
  setDays(s.days);
  $("#schedule-dialog-title").textContent = schedule ? t("Edit schedule") : t("New schedule");
  $("#delete-schedule").hidden = !schedule;
  $("#schedule-dialog").showModal();
}

// The actions depend on the target: wake/sleep for devices, on/off/restart for plugs
function renderScheduleActions(selected) {
  const form = $("#schedule-form");
  const [kind, id] = form.elements.target.value.split(":");
  const current = selected || (form.querySelector("input[name=action]:checked") || {}).value;
  let actions;
  if (kind === "plug") {
    const locked = plugById(id)?.locked && t("Never switched off: supplies the WOL-Server or the network");
    actions = [["power_on", t("Power on")], ["power_off", t("Power off"), locked], ["power_cycle", t("Restart"), locked]];
  } else {
    const device = deviceById(id);
    const canSleep = device && device.sleep_method !== "none";
    actions = [["wake", t("Wake")], ["sleep", t("Put to sleep"), !canSleep && t("No sleep method is configured for this device")]];
  }
  const fallback = actions[0][0];
  const value = actions.some(([v, , off]) => v === current && !off) ? current : fallback;
  $("#schedule-actions").replaceChildren(...actions.map(([v, label, off]) => el("label", { title: off || "" },
    el("input", { type: "radio", name: "action", value: v, checked: v === value, disabled: Boolean(off), onchange: updateForceRow }),
    el("span", {}, label))));
  updateForceRow();
}

function updateForceRow() {
  const form = $("#schedule-form");
  const action = (form.querySelector("input[name=action]:checked") || {}).value;
  $("#schedule-force-row").hidden = !["power_off", "power_cycle"].includes(action);
}

async function onScheduleSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const [kind, id] = form.elements.target.value.split(":");
  const data = {
    [kind === "plug" ? "plug_id" : "device_id"]: id,
    force: kind === "plug" && form.elements.force.checked,
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
      toast(editingSchedule ? t("Schedule saved") : t("Schedule added"), "success");
      await refreshView();
    } catch (e) {
      showError(form, e.message);
    }
  });
}

async function onScheduleDelete() {
  if (!(await confirmDialog(t("Delete schedule?"), t("The schedule will be removed permanently."), t("Delete"), true))) return;
  try {
    await api("DELETE", `/api/schedules/${editingSchedule.id}`);
    $("#schedule-dialog").close();
    toast(t("Schedule deleted"), "success");
    await refreshView();
  } catch (e) {
    toast(e.message, "error");
  }
}

// ---------- Log ----------

async function renderEvents() {
  const { events } = await api("GET", "/api/events");
  const list = $("#event-list");
  if (!events.length) {
    list.replaceChildren(el("div", { class: "empty" }, el("h3", {}, t("No entries")),
      el("p", { class: "muted" }, t("The log lists manual and scheduled actions."))));
    return;
  }
  list.replaceChildren(...events.map((e) =>
    el("div", { class: `row event ${e.ok ? "ok" : "fail"}` },
      el("span", { class: "event-icon" }, e.ok ? icon("check") : icon("x")),
      el("span", { class: "event-time mono small" }, fmtDateTime(e.ts)),
      el("div", { class: "event-main" },
        el("div", {}, el("strong", {}, tr(e.device)), " · ", ACTIONS[e.action] || e.action,
          el("span", { class: "tag" }, tr(e.source))),
        el("div", { class: "muted small" }, tr(e.message))))));
}

async function onClearEvents() {
  if (!(await confirmDialog(t("Clear log?"), t("All entries will be deleted."), t("Clear"), true))) return;
  await api("DELETE", "/api/events");
  await renderEvents();
}

// ---------- Wireless plugs ----------

const POWER_ON_STATES = { restore: t("Restore last state"), on: t("On"), off: t("Off"), other: t("other (set on the plug)") };

async function loadPlugs(live = true) {
  const data = await api("GET", live ? "/api/plugs" : "/api/plugs?live=0");
  state.plugs = data.plugs;
  state.plugDrivers = data.drivers;
  state.plugSettings = data.settings || state.plugSettings;
}

const plugView = { sub: "overview", period: "day", date: isoDate(new Date()), plug: "all", data: null };

function isoDate(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function setPlugSub(sub, push = true) {
  plugView.sub = sub;
  showPlugSub();
  if (push) history.replaceState(null, "", `#plugs/${sub}`);
  refreshView().catch((e) => toast(e.message, "error"));
}

function showPlugSub() {
  for (const tab of $$("#plug-subtabs .subtab")) tab.classList.toggle("active", tab.dataset.sub === plugView.sub);
  for (const view of $$("#view-plugs .subview")) view.hidden = view.id !== `plug-sub-${plugView.sub}`;
}

function fmtMoney(value) {
  if (value == null) return "–";
  return `${value.toLocaleString(I18N.locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${state.plugSettings.currency || "€"}`;
}

// Costs of an energy in Wh, or null without a price
function costOf(wh) {
  const price = state.plugSettings.price_kwh;
  return price == null || wh == null ? null : (wh / 1000) * price;
}

function fmtWatt(w) {
  if (w == null) return "–";
  return w < 10 ? w.toLocaleString(I18N.locale, { maximumFractionDigits: 1 }) : Math.round(w).toLocaleString(I18N.locale);
}

function fmtKwh(wh) {
  return wh == null ? "–" : `${(wh / 1000).toLocaleString(I18N.locale, { maximumFractionDigits: wh < 10000 ? 2 : 1 })} kWh`;
}

function renderPlugsLine() {
  const reachable = state.plugs.filter((p) => p.live && p.live.status);
  const total = reachable.reduce((sum, p) => sum + (p.live.status.power_w || 0), 0);
  $("#plugs-line").textContent = state.plugs.length
    ? t("{0} plugs · together {1} W", state.plugs.length, fmtWatt(total)) : " ";
}

function renderPlugs() {
  const list = $("#plug-list");
  renderPlugsLine();
  $("#plug-subtabs").hidden = !state.plugs.length;
  $("#plug-alerts").replaceChildren(...state.plugs.flatMap((p) => (p.alerts || []).map((a) =>
    el("li", { class: `notice ${a.key === "unreachable" || a.key === "wear" ? "warn" : "danger"}` }, icon("alert"),
      el("span", {}, el("strong", {}, p.name), ": ", tr(a.message))))));
  if (!state.plugs.length) {
    list.replaceChildren(el("div", { class: "empty" },
      el("h3", {}, t("No plugs")),
      el("p", { class: "muted" }, t("Metering wireless plugs switch and measure consumers. Approve a new plug under Network → Overview, or add one by its address.")),
      el("button", { class: "btn primary", type: "button", onclick: () => openPlugDialog() }, t("Add plug"))));
    return;
  }
  list.replaceChildren(...state.plugs.map(plugCard));
}

function plugCard(p) {
  const live = p.live || {};
  const st = live.status;
  const cls = !st ? "unknown" : st.on ? "online" : "offline";
  // "Power on/off", not "On/Off": the relay, which a device behind it may sleep through
  let label = !st ? (live.unreachable ? t("Not reachable") : t("Error")) : st.on ? t("Power on") : t("Power off");
  // "On" is the relay. Behind it a device may be asleep: say so, from ping and power as on its own card,
  // or from the power alone while its status is not known yet.
  // The dot stays the relay's; the label takes the device's color.
  let labelCls = cls;
  const device = (p.consumer || {}).kind === "device" ? deviceById(p.consumer.id) : null;
  const deviceCls = device ? deviceState(device)[0] : null;
  if (device && st && st.on) {
    const deviceLabel = deviceState(device)[1];
    if (deviceCls !== "unknown") [labelCls, label] = [deviceCls, `${t("Power on")} · ${deviceLabel}`];
    else if (st.power_w != null && st.power_w <= p.standby_w) [labelCls, label] = ["offline", `${t("Power on")} · ${t("Standby")}`];
  }
  // Below about 1 W a Shelly reads 0 W although current flows: "< 1" instead of a 0 that looks like no power
  const below = st && st.on && st.power_w === 0 && st.current_a > 0;
  const consumer = p.consumer_name;
  const energy = p.energy;
  const meta = st ? [
    [t("Consumer"), consumer || "–"],
    [t("Today"), energy ? fmtKwh(energy.day) : "–"],
    [t("This month"), energy ? fmtKwh(energy.month) : "–"],
    [t("Voltage"), st.voltage_v == null ? "–" : `${Math.round(st.voltage_v)} V`],
    [t("Current"), st.current_a == null ? "–" : `${st.current_a.toLocaleString(I18N.locale, { maximumFractionDigits: st.current_a < 0.1 ? 3 : 2 })} A`],
    [t("Temperature"), st.temperature_c == null ? "–" : `${Math.round(st.temperature_c)} °C`],
    [t("Energy counter"), fmtKwh(st.energy_wh)],
    [t("Switch cycles"), st.switch_count == null ? "–" : st.switch_count.toLocaleString(I18N.locale)],
  ] : [[t("Consumer"), consumer || "–"], [t("Address"), p.host]];
  const standby = energy && energy.standby_year;
  if (st && standby != null) {
    const cost = costOf(standby);
    meta.push([t("Standby per year"), cost == null ? fmtKwh(standby) : `${fmtKwh(standby)} · ${fmtMoney(cost)}`]);
  }
  return el("article", { class: `card plug ${st ? (st.on ? "on" : "off") : "unknown"}` },
    el("header", { class: "card-head" },
      el("span", { class: `dot ${cls}`, "aria-hidden": "true" }),
      el("div", { class: "card-title" },
        el("h3", {}, p.name),
        el("span", { class: `status-label ${labelCls}` }, label)),
      el("button", { class: "icon-btn", type: "button", title: t("Edit"), "aria-label": t("Edit {0}", p.name), onclick: () => openPlugDialog(p) }, icon("edit"))),
    st ? el("div", { class: "plug-power" }, el("strong", {}, below ? "< 1" : fmtWatt(st.power_w)), el("span", {}, "W")) : null,
    (p.alerts || []).filter((a) => a.key !== "unreachable").map((a) => el("p", { class: "plug-error" }, icon("alert"), " ", tr(a.message))),
    !st && live.error ? el("p", { class: "plug-error" }, tr(live.error)) : null,
    securityWarning(p),
    el("dl", { class: "meta" }, meta.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v)))),
    plugNextLine(p),
    p.locked ? el("p", { class: "next muted" }, icon("lock"), t("Never switched off: supplies the WOL-Server or the network")) : null,
    el("div", { class: "card-actions" },
      // A device behind the plug is woken, not only powered: its wake method switches the power on if needed
      device
        ? el("button", {
          class: "btn wake", type: "button", disabled: deviceCls === "online", title: deviceCls === "online" ? t("Already online") : "",
          onclick: (e) => deviceAction(device, "wake", e.currentTarget).then(refreshPlugsSoon),
        }, icon("power"), t("Wake"))
        : el("button", { class: "btn wake", type: "button", disabled: !st || st.on, onclick: (e) => plugAction(p, "on", e.currentTarget) }, icon("power"), t("Power on")),
      el("button", { class: "btn", type: "button", disabled: !st || !st.on || p.locked, onclick: (e) => plugAction(p, "off", e.currentTarget) }, t("Power off")),
      el("button", { class: "btn", type: "button", disabled: !st || !st.on || p.locked, title: t("Off, on again after 10 s - runs on the plug itself"), onclick: (e) => plugAction(p, "cycle", e.currentTarget) }, icon("undo"), t("Power cycle"))));
}

const EXPOSURE_TEXTS = {
  access_point: t("own Wi-Fi access point"), bluetooth: t("Bluetooth"), matter: t("Matter"), cloud: t("vendor cloud"),
};

function securityIssues(sec) {
  return sec ? [...(sec.password ? [] : [t("no password")]), ...sec.open.map((e) => EXPOSURE_TEXTS[e] || e)] : [];
}

function securityWarning(p) {
  const issues = securityIssues(p.security);
  if (!issues.length) return null;
  const pending = p.security.restart_required ? ` (${t("restart pending")})` : "";
  return el("p", { class: "plug-warn" }, icon("alert"), " ", t("Open: {0}", issues.join(", ")) + pending, " ",
    el("button", { type: "button", class: "linkish", onclick: () => openPlugDialog(p, {}, true) }, t("Secure")));
}

function plugNextLine(p) {
  const next = nextActionFor(p.id, "plug_id");
  return el("p", { class: next ? "next" : "next muted" }, icon("clock"),
    next ? el("span", {}, el("strong", {}, ACTIONS[next.action]), " ", fmtNext(next.next_run)) : t("No active schedule"));
}

async function plugAction(plug, action, button, confirm = false) {
  let res;
  await busy(button, async () => {
    try {
      res = await api("POST", `/api/plugs/${plug.id}/${action}`, { confirm });
    } catch (e) {
      toast(`${plug.name}: ${e.message}`, "error");
    }
  });
  if (!res) return;
  if (res.confirm) {
    const title = action === "cycle" ? t("Restart {0} the hard way?", plug.name) : t("Switch off {0}?", plug.name);
    const text = `${res.message}. ${t("Cutting the power of a running device can lose unsaved data.")}`;
    if (await confirmDialog(title, text, action === "cycle" ? t("Restart") : t("Switch off"), true)) {
      await plugAction(plug, action, button, true);
    }
    return;
  }
  toast(`${plug.name}: ${res.message}`, "success");
  for (const delay of action === "cycle" ? [500, 12000] : [500]) {
    setTimeout(() => {
      if (state.view === "plugs" && plugView.sub === "overview") refreshPlugs();
      else if (state.view === "devices") refreshStatus();
    }, delay);
  }
}

let plugsBusy = false;

const plugsOverviewShown = () => state.view === "plugs" && plugView.sub === "overview";
// The plug cards show and wake the devices behind them: they need the device status as the device list does
const plugsNeedStatus = () => plugsOverviewShown() && state.plugs.some((p) => (p.consumer || {}).kind === "device");

// After a wake from a plug card: power and status change while the device starts (10-60 s)
function refreshPlugsSoon() {
  for (const delay of [3000, 15000, 40000]) {
    setTimeout(() => {
      if (plugsOverviewShown()) refreshPlugs().then(refreshStatus);
    }, delay);
  }
}

async function refreshPlugs() {
  if (plugsBusy) return;
  plugsBusy = true;
  try {
    await loadPlugs();
    renderPlugs();
  } catch (e) {
    if (e.status !== 401) $("#plugs-line").textContent = e.message;
  } finally {
    plugsBusy = false;
  }
}

let editingPlug = null;
let plugPowerOnState = null;

function openPlugDialog(plug = null, prefill = {}, focusSecurity = false) {
  editingPlug = plug;
  plugPowerOnState = null;
  const form = $("#plug-form");
  form.reset();
  showError(form, "");
  const data = plug ? { ...plug } : { channel: 0, standby_w: 5, ...prefill };
  for (const key of ["host", "name", "channel", "standby_w", "running_w"]) form.elements[key].value = data[key] ?? "";
  fillPlugConsumer(plug);
  const power = plug && plug.live && plug.live.status ? plug.live.status.power_w : null;
  const measured = $("#plug-measured");
  measured.hidden = power == null;
  if (power != null) {
    // Suggest the thresholds from the current measurement
    const standby = power <= 15;
    const value = standby ? Math.ceil(power + 1) : Math.round(power / 2);
    measured.replaceChildren(t("Measured now: {0} W", fmtWatt(power)), " ", el("button", {
      type: "button", class: "linkish",
      onclick: () => { form.elements[standby ? "standby_w" : "running_w"].value = value; },
    }, standby ? t("Use {0} W as standby", value) : t("Use {0} W as running threshold", value)));
  }
  $("#plug-driver").replaceChildren(...[
    !plug && el("option", { value: "auto" }, t("Detect automatically")),
    ...(state.plugDrivers || []).map((d) => el("option", { value: d.name }, tr(d.label))),
  ].filter(Boolean));
  form.elements.driver.value = plug ? plug.driver : "auto";
  form.elements.password.placeholder = plug && plug.has_password ? t("unchanged") : "";
  $("#plug-detected").textContent = plug ? [plug.model, plug.firmware && t("firmware {0}", plug.firmware), plug.mac].filter(Boolean).join(" · ") : "";
  $("#plug-wait").hidden = !prefill.waiting;
  const pos = form.elements.power_on_state;
  pos.disabled = true;
  $("#plug-pos-hint").textContent = plug ? t("Reading from the plug …") : t("Can be set after adding.");
  if (plug) {
    api("GET", `/api/plugs/${plug.id}/power-on-state`).then((res) => {
      if (editingPlug !== plug) return;
      plugPowerOnState = res.state;
      pos.querySelector('option[value="other"]')?.remove();
      if (res.state === "other") pos.append(el("option", { value: "other" }, POWER_ON_STATES.other));
      pos.value = res.state;
      pos.disabled = false;
      $("#plug-pos-hint").textContent = res.state === "off" ? t("Devices stay without power after an outage.")
        : res.state === "other" ? t("The plug uses a setting of its own. Choose one to be sure.") : "";
    }).catch((e) => { $("#plug-pos-hint").textContent = e.message; });
  }
  $("#plug-dialog-title").textContent = plug ? t("Edit plug") : t("New wireless plug");
  $("#plug-delete").hidden = !plug;
  $("#plug-submit").textContent = plug ? t("Save") : t("Add");
  fillPlugSecurity(plug);
  $("#plug-dialog").showModal();
  if (focusSecurity) $("#plug-security").scrollIntoView({ block: "nearest" });
}

// Security: password and the ways into the plug besides its local API (own access point, Bluetooth, ...)
function fillPlugSecurity(plug) {
  const box = $("#plug-security");
  const driver = plug && (state.plugDrivers || []).find((d) => d.name === plug.driver);
  box.hidden = !driver || !driver.capabilities.includes("security");
  if (box.hidden) return;
  $("#plug-security-items").replaceChildren();
  $("#plug-secure").disabled = true;
  $("#plug-secure-hint").textContent = "";
  $("#plug-security-state").textContent = t("Reading from the plug …");
  $("#plug-restart-row").hidden = true;
  api("GET", `/api/plugs/${plug.id}/security`).then((sec) => {
    rememberSecurity(plug, sec);
    if (editingPlug === plug) renderPlugSecurity(plug, sec);
  }).catch((e) => { $("#plug-security-state").textContent = e.message; });
}

// The card behind the dialog shows the same state as the dialog
function rememberSecurity(plug, sec) {
  plug.security = { password: sec.password, open: sec.open, restart_required: Boolean(sec.restart_required) };
  if (state.view === "plugs" && plugView.sub === "overview") renderPlugs();
}

function renderPlugSecurity(plug, sec) {
  const issues = securityIssues(sec);
  // Waiting for a restart: what is still listed goes off with it, offering it again would only confuse
  const waiting = sec.restart_required && sec.open.length;
  $("#plug-security-state").replaceChildren(...(waiting
    ? [icon("clock"), " ", t("Waiting for a restart: {0}", sec.open.map((e) => EXPOSURE_TEXTS[e] || e).join(", "))]
    : issues.length
      ? [icon("alert"), " ", t("Open: {0}", issues.join(", "))]
      : [el("span", { class: "ok" }, icon("check")), " ", t("Secured: password set, no other ways in.")]));
  $("#plug-security-items").replaceChildren(...(waiting ? [] : sec.open).map((e) => el("label", { class: "check" },
    el("input", { type: "checkbox", name: "disable", value: e, checked: true }),
    el("span", {}, t("Switch off: {0}", EXPOSURE_TEXTS[e] || e)))));
  $("#plug-secure").disabled = false;
  $("#plug-restart-row").hidden = !sec.restart_required;
  $("#plug-secure-hint").textContent = sec.password || plug.has_password
    ? t("Empty: the password stays as it is.")
    : t("Empty: the WOL-Server chooses a random password. Only the WOL-Server needs it; a reset of the plug removes it.");
}

async function onPlugRestart() {
  const plug = editingPlug;
  const form = $("#plug-form");
  showError(form, "");
  if (!(await confirmDialog(t("Restart {0}?", plug.name),
    t("The plug is not reachable for about 10 s. Afterwards its output follows the setting \"After a power outage\"."), t("Restart")))) return;
  await busy($("#plug-restart"), async () => {
    try {
      const res = await api("POST", `/api/plugs/${plug.id}/restart`);
      toast(res.message, "success");
      $("#plug-security-state").textContent = t("Reading from the plug …");
      setTimeout(() => { if (editingPlug === plug && $("#plug-dialog").open) fillPlugSecurity(plug); }, 15000);
    } catch (e) {
      showError(form, e.message);
    }
  });
}

async function onPlugSecure() {
  const plug = editingPlug;
  const form = $("#plug-form");
  showError(form, "");
  const password = form.elements.secure_password.value;
  if (password && password.length < 8) return showError(form, t("Password: 8 to 128 characters"));
  const disable = [...form.querySelectorAll('input[name="disable"]:checked')].map((i) => i.value);
  await busy($("#plug-secure"), async () => {
    try {
      const res = await api("POST", `/api/plugs/${plug.id}/security`, { password, disable });
      form.elements.secure_password.value = "";
      plug.has_password = true;
      rememberSecurity(plug, res);
      renderPlugSecurity(plug, res);
      toast(t("Plug secured"), "success");
    } catch (e) {
      showError(form, e.message);
    }
  });
}

const CONSUMER_OTHER = "__label";
const CONSUMER_SELF = "__self";

function fillPlugConsumer(plug) {
  const form = $("#plug-form");
  const consumer = (plug && plug.consumer) || null;
  const taken = new Map(state.plugs.filter((p) => p !== plug && p.consumer && p.consumer.kind === "device").map((p) => [p.consumer.id, p.name]));
  const selfPlug = state.plugs.find((p) => p !== plug && p.consumer && p.consumer.kind === "self");
  $("#plug-consumer").replaceChildren(
    el("option", { value: "" }, t("– nothing –")),
    el("option", { value: CONSUMER_SELF, disabled: Boolean(selfPlug) }, selfPlug ? `${t("This WOL-Server")} (${selfPlug.name})` : t("This WOL-Server")),
    state.devices.length ? el("optgroup", { label: t("Devices") }, state.devices.map((d) => el("option", { value: d.id, disabled: taken.has(d.id) },
      taken.has(d.id) ? `${d.name} (${taken.get(d.id)})` : d.name))) : null,
    el("option", { value: CONSUMER_OTHER }, t("Something else …")));
  form.elements.consumer_device.value = !consumer ? "" : consumer.kind === "device" ? consumer.id : consumer.kind === "self" ? CONSUMER_SELF : CONSUMER_OTHER;
  form.elements.consumer_label.value = consumer && consumer.kind === "label" ? consumer.label : "";
  form.elements.locked.checked = Boolean(plug && plug.locked);
  updatePlugConsumer();
}

// The plug of the WOL-Server itself is always locked: switching it off would cut the server that switches it on again
function updatePlugConsumer() {
  const form = $("#plug-form");
  const value = form.elements.consumer_device.value;
  $("#plug-consumer-label").hidden = value !== CONSUMER_OTHER;
  form.elements.locked.disabled = value === CONSUMER_SELF;
  if (value === CONSUMER_SELF) form.elements.locked.checked = true;
}

async function detectPlug() {
  const form = $("#plug-form");
  showError(form, "");
  const host = form.elements.host.value.trim();
  if (!host) return showError(form, t("Enter a host name or an IP address"));
  await busy($("#plug-detect"), async () => {
    try {
      const { info } = await api("POST", "/api/plugs/probe", { host, driver: form.elements.driver.value });
      const driver = (state.plugDrivers || []).find((d) => d.name === info.driver);
      $("#plug-detected").replaceChildren(icon("check"), " ", [tr(driver ? driver.label : info.driver), info.model,
        info.firmware && t("firmware {0}", info.firmware), info.auth ? t("password protected") : t("no password")].filter(Boolean).join(" · "));
      if (!form.elements.name.value.trim() && info.name) form.elements.name.value = info.name;
      $("#plug-wait").hidden = true;
    } catch (e) {
      $("#plug-detected").textContent = "";
      showError(form, e.message);
    }
  });
}

async function onPlugSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showError(form, "");
  const consumer = form.elements.consumer_device.value;
  const data = {
    host: form.elements.host.value.trim(), name: form.elements.name.value.trim(),
    consumer_device: consumer && consumer !== CONSUMER_OTHER && consumer !== CONSUMER_SELF ? consumer : null,
    consumer_label: consumer === CONSUMER_OTHER ? form.elements.consumer_label.value.trim() : "",
    consumer_self: consumer === CONSUMER_SELF, locked: form.elements.locked.checked,
    password: form.elements.password.value, driver: form.elements.driver.value, channel: Number(form.elements.channel.value) || 0,
    standby_w: form.elements.standby_w.value, running_w: form.elements.running_w.value,
  };
  if (!editingPlug && openPlugDialog.prefillMac) data.mac = openPlugDialog.prefillMac;
  await busy($("#plug-submit"), async () => {
    try {
      if (editingPlug) {
        await api("PUT", `/api/plugs/${editingPlug.id}`, data);
        const pos = form.elements.power_on_state;
        if (!pos.disabled && plugPowerOnState !== pos.value) {
          await api("PUT", `/api/plugs/${editingPlug.id}/power-on-state`, { state: pos.value });
        }
      } else {
        await api("POST", "/api/plugs", data);
      }
      $("#plug-dialog").close();
      toast(editingPlug ? t("Plug saved") : t("Plug added"), "success");
      if (state.view === "plugs") await refreshPlugs();
      else if (state.view === "network") await renderNetwork();
    } catch (e) {
      showError(form, e.message);
    }
  });
}

async function onPlugDelete() {
  const plug = editingPlug;
  if (!(await confirmDialog(t("Remove {0}?", plug.name), t("The WOL-Server forgets the plug. The plug itself and its reservation stay unchanged."), t("Remove"), true))) return;
  try {
    await api("DELETE", `/api/plugs/${plug.id}`);
    $("#plug-dialog").close();
    toast(t("Plug removed"), "success");
    await refreshPlugs();
  } catch (e) {
    toast(e.message, "error");
  }
}

// "Add as wireless plug" from a reservation (host: name.domain or IP)
function plugAddButton(name, mac, ip) {
  const label = t("Add as wireless plug");
  return el("button", {
    type: "button", class: "icon-btn", title: label, "aria-label": `${label}: ${name || mac}`,
    onclick: () => openPlugFor(name, mac, name && net.config ? `${name}.${net.config.config.dns.domain}` : ip),
  }, icon("plug"));
}

async function openPlugFor(name, mac, host, waiting = false) {
  if (!state.plugDrivers) await loadPlugs().catch(() => {});
  if (!state.devices.length) await loadDevices().catch(() => {});
  openPlugDialog.prefillMac = mac;
  openPlugDialog(null, { name: name || "", host, waiting });
}

// ---------- Consumption (energy of the plugs) ----------

const SERIES_SLOTS = 8; // colors --series-1 … --series-8, in fixed order; more plugs fold into "Other"

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) if (value != null) node.setAttribute(key, value);
  for (const child of children.flat()) if (child) node.append(child);
  return node;
}

function niceTicks(max, count = 4) {
  if (!(max > 0)) return [0, 1];
  const raw = max / count;
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * power).find((v) => v >= raw);
  const ticks = [];
  for (let v = 0; v < max + step * 0.999; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
  return ticks;
}

function fmtNum(value, digits = 1) {
  return value.toLocaleString(I18N.locale, { maximumFractionDigits: digits });
}

async function renderEnergy(quiet = false) {
  const box = $("#plug-sub-energy");
  if (!quiet) box.classList.add("loading");
  try {
    const q = new URLSearchParams(plugView.period === "hour" ? { period: "hour" } : { period: plugView.period, date: plugView.date });
    plugView.data = await api("GET", `/api/energy?${q}`);
  } finally {
    box.classList.remove("loading");
  }
  state.plugSettings = plugView.data.settings;
  drawEnergy();
}

// Series of the chosen plugs: color slot by the plug's position (it follows the plug, not the selection)
function energySeries(data) {
  let series = data.plugs.map((p, i) => ({ id: p.id, name: p.name, consumer: p.consumer_name, standby_w: p.standby_w, slot: i + 1 }));
  if (series.length > SERIES_SLOTS) {
    const rest = series.slice(SERIES_SLOTS - 1);
    series = [...series.slice(0, SERIES_SLOTS - 1), { id: "__other", name: t("Other"), slot: SERIES_SLOTS, members: rest.map((r) => r.id) }];
  }
  if (plugView.plug !== "all") series = series.filter((sr) => sr.id === plugView.plug || (sr.members || []).includes(plugView.plug));
  const pick = (values) => (sr) => {
    if (!sr.members) return values[sr.id] || [];
    const lists = sr.members.map((id) => values[id] || []);
    const n = Math.max(0, ...lists.map((l) => l.length));
    return Array.from({ length: n }, (_, i) => (lists.some((l) => l[i] != null) ? lists.reduce((sum, l) => sum + (l[i] || 0), 0) : null));
  };
  const nowById = Object.fromEntries(data.plugs.map((p) => [p.id, p.power_w]));
  for (const sr of series) {
    const ids0 = sr.members || [sr.id];
    sr.now = ids0.some((id) => nowById[id] != null) ? ids0.reduce((sum, id) => sum + (nowById[id] || 0), 0) : null;
    sr.energy = data.values ? pick(data.values)(sr) : [];
    sr.curve = data.curve ? pick(data.curve.plugs)(sr) : null;
    sr.total = sr.energy.reduce((sum, v) => sum + (v || 0), 0);
    const ids = sr.members || [sr.id];
    const standby = ids.map((id) => data.summary[id] && data.summary[id].standby_year);
    sr.standby_year = standby.some((v) => v != null) ? standby.reduce((sum, v) => sum + (v || 0), 0) : null;
  }
  return series;
}

function periodLabel(period, iso) {
  const d = new Date(`${iso}T12:00:00`);
  if (period === "day") return d.toLocaleDateString(I18N.locale, { weekday: "short", day: "numeric", month: "long", year: "numeric" });
  if (period === "month") return d.toLocaleDateString(I18N.locale, { month: "long", year: "numeric" });
  return String(d.getFullYear());
}

function shiftPeriod(step) {
  const d = new Date(`${plugView.date}T12:00:00`);
  if (plugView.period === "day") d.setDate(d.getDate() + step);
  else if (plugView.period === "month") d.setMonth(d.getMonth() + step, 1);
  else d.setFullYear(d.getFullYear() + step, 0, 1);
  plugView.date = isoDate(d);
  renderEnergy().catch((e) => toast(e.message, "error"));
}

function isCurrentPeriod() {
  if (plugView.period === "hour") return true;
  const today = isoDate(new Date());
  const n = { day: 10, month: 7, year: 4 }[plugView.period];
  return plugView.date.slice(0, n) >= today.slice(0, n);
}

function bucketLabel(period, data, i, long = false) {
  const b = data.buckets[i];
  if (period === "day") return long ? `${b}:00–${String((Number(b) + 1) % 24).padStart(2, "0")}:00` : b;
  if (period === "month") {
    if (!long) return String(Number(b));
    return new Date(`${data.date.slice(0, 8)}${b}T12:00:00`).toLocaleDateString(I18N.locale, { weekday: "short", day: "numeric", month: "short" });
  }
  return new Date(Number(data.date.slice(0, 4)), Number(b) - 1, 1).toLocaleDateString(I18N.locale, { month: long ? "long" : "short" });
}

function drawEnergy() {
  const data = plugView.data;
  if (!data) return;
  const period = plugView.period;
  for (const radio of $$("#energy-period input")) radio.checked = radio.value === period;
  const live = period === "hour";
  $("#energy-label").textContent = live ? t("Last 60 minutes") : periodLabel(period, plugView.date);
  $("#energy-prev").hidden = live;
  $("#energy-next").hidden = live;
  $("#energy-next").disabled = isCurrentPeriod();
  $("#energy-today").hidden = isCurrentPeriod();
  const select = $("#energy-plug");
  select.replaceChildren(el("option", { value: "all" }, t("All plugs")), ...data.plugs.map((p) => el("option", { value: p.id }, p.consumer_name ? `${p.name} · ${p.consumer_name}` : p.name)));
  if (!data.plugs.some((p) => p.id === plugView.plug)) plugView.plug = "all";
  select.value = plugView.plug;
  $("#energy-currency").textContent = state.plugSettings.currency || "€";
  const priceInput = $("#energy-price-form").elements.price_kwh;
  if (document.activeElement !== priceInput) priceInput.value = state.plugSettings.price_kwh ?? "";

  if (!data.plugs.length) {
    $("#energy-stats").replaceChildren();
    $("#energy-chart-panel").hidden = true;
    $("#energy-table").replaceChildren(el("div", { class: "table-empty muted" }, t("No plugs yet.")));
    return;
  }
  $("#energy-chart-panel").hidden = false;
  const series = energySeries(data);
  if (live) return drawLive(data, series);
  const total = series.reduce((sum, sr) => sum + sr.total, 0);
  const hasData = series.some((sr) => sr.energy.some((v) => v != null));
  const standby = series.filter((sr) => sr.standby_year != null);
  const standbyWh = standby.reduce((sum, sr) => sum + sr.standby_year, 0);
  const periodName = isCurrentPeriod() ? { day: t("today so far"), month: t("this month so far"), year: t("this year so far") }[period]
    : periodLabel(period, plugView.date);
  const price = state.plugSettings.price_kwh;
  $("#energy-stats").replaceChildren(
    statCard(t("Energy"), hasData ? fmtKwh(total) : "–", [periodName]),
    statCard(t("Costs"), price == null ? "–" : fmtMoney(costOf(total)), [price == null ? t("Enter a price below") : t("at {0} per kWh", fmtMoney(price))]),
    statCard(t("Standby per year"), standby.length ? fmtKwh(standbyWh) : "–",
      [standby.length ? (price == null ? t("estimated from the last 30 days") : fmtMoney(costOf(standbyWh))) : t("known after one day")]));

  const legend = $("#energy-legend");
  legend.replaceChildren(...(series.length > 1 && period !== "day" ? series.map((sr) => el("span", { class: "legend-item" },
    el("span", { class: `key ${period === "day" ? "line" : "box"} s${sr.slot}`, "aria-hidden": "true" }), sr.name)) : []),
  ...(period === "month" ? [el("span", { class: "legend-item" }, el("span", { class: "key box band", "aria-hidden": "true" }), t("Weekend"))] : []));
  $("#energy-chart-title").textContent = period === "day"
    ? (series.length === 1 ? t("Power of {0} in W", series[0].name) : t("Power in W, each plug on its own scale"))
    : (series.length === 1 ? t("Energy of {0} in kWh", series[0].name) : t("Energy in kWh"));
  const chart = $("#energy-chart");
  if (!hasData && !(period === "day" && series.some((sr) => sr.curve && sr.curve.some((v) => v != null)))) {
    chart.replaceChildren(el("div", { class: "table-empty muted" }, t("No values for this period yet.")));
  } else if (period === "day") {
    drawCurve(chart, data, series);
  } else {
    drawBars(chart, data, series, period);
  }
  drawEnergyTable(series, total, period);
}

// The last hour: power right now, and the curve in 5 s steps while the view is open (once a minute before)
function drawLive(data, series) {
  const known = (sr) => sr.curve.filter((v) => v != null);
  for (const sr of series) {
    const values = known(sr);
    sr.avg = values.length ? values.reduce((a, v) => a + v, 0) / values.length : null;
    sr.peak = values.length ? Math.max(...values) : null;
    sr.wh = values.length ? values.reduce((a, v) => a + v, 0) * data.curve.step / 3600 : null;
  }
  const sum = (key) => (series.some((sr) => sr[key] != null) ? series.reduce((a, sr) => a + (sr[key] || 0), 0) : null);
  const now = sum("now"), wh = sum("wh");
  // Peak of the sum: the highest step of all plugs together
  const n = series[0].curve.length;
  let peak = null;
  for (let i = 0; i < n; i++) {
    if (!series.some((sr) => sr.curve[i] != null)) continue;
    peak = Math.max(peak ?? 0, series.reduce((a, sr) => a + (sr.curve[i] || 0), 0));
  }
  const price = state.plugSettings.price_kwh;
  $("#energy-stats").replaceChildren(
    statCard(t("Power now"), now == null ? "–" : `${fmtWatt(now)} W`, [series.length > 1 ? t("all shown plugs together") : series[0].name], "live"),
    statCard(t("Peak"), peak == null ? "–" : `${fmtWatt(peak)} W`, [t("in the last 60 minutes")]),
    statCard(t("Energy"), wh == null ? "–" : `${fmtNum(wh, wh < 10 ? 1 : 0)} Wh`,
      [wh != null && price != null ? fmtMoney(costOf(wh)) : t("in the last 60 minutes")]));
  $("#energy-legend").replaceChildren();
  $("#energy-chart-title").textContent = series.length === 1 ? t("Power of {0} in W", series[0].name) : t("Power in W, each plug on its own scale");
  const chart = $("#energy-chart");
  if (!series.some((sr) => known(sr).length)) {
    chart.replaceChildren(el("div", { class: "table-empty muted" }, t("No readings yet: they arrive every few seconds while this view is open.")));
  } else {
    const start = data.curve.start, step = data.curve.step;
    const fmtTime = (ts, seconds) => new Date(ts * 1000).toLocaleTimeString(I18N.locale, seconds ? { hour: "2-digit", minute: "2-digit", second: "2-digit" } : { hour: "2-digit", minute: "2-digit" });
    drawCurve(chart, data, series, {
      // a label every 10 (narrow: 20) minutes, on the full ten
      axis: (narrow) => {
        const every = (narrow ? 1200 : 600);
        const out = [];
        for (let ts = Math.ceil(start / every) * every; ts <= start + (n - 1) * step; ts += every) out.push([(ts - start) / step, fmtTime(ts)]);
        return out;
      },
      bands: () => {
        const out = [];
        for (let ts = Math.floor(start / 600) * 600 + 300; ts < start + n * step; ts += 600) out.push([(ts - start) / step, (ts + 300 - start) / step]);
        return out;
      },
      time: (i) => fmtTime(start + i * step, true),
      headValue: (sr) => (sr.now == null ? null : `${fmtWatt(sr.now)} W`),
    });
  }
  const head = [t("Plug"), t("Consumer"), t("Now"), t("Average"), t("Peak")];
  const cell = (v) => el("td", { class: "num" }, v == null ? "–" : `${fmtWatt(v)} W`);
  const row = (sr) => [el("td", {}, el("span", { class: `key line s${sr.slot}`, "aria-hidden": "true" }), sr.name),
    el("td", { class: "hide-sm" }, sr.consumer || "–"), cell(sr.now), cell(sr.avg), cell(sr.peak)];
  const tr_ = (cells) => el("tr", {}, cells.map((c, k) => { c.dataset.label = head[k]; return c; }));
  $("#energy-table").replaceChildren(el("table", { class: "data energy-table" },
    el("caption", { class: "sr-only" }, t("Power per plug in the last 60 minutes")),
    el("thead", {}, el("tr", {}, head.map((h, k) => el("th", { scope: "col", class: k >= 2 ? "num" : k === 1 ? "hide-sm" : "" }, h)))),
    el("tbody", {}, series.map((sr) => tr_(row(sr))))));
}

// Frame of a chart: SVG with gridlines and y-axis (the caller places it)
function chartFrame(chart, max, fmtTick, opts = {}) {
  const width = Math.max(280, chart.clientWidth);
  const height = opts.height || (width < 520 ? 210 : 260);
  const ticks = niceTicks(max, opts.ticks || 4);
  const top = ticks[ticks.length - 1];
  const left = opts.left || 10 + Math.max(...ticks.map((v) => fmtTick(v).length)) * 7;
  const m = { left, right: 10, top: opts.top ?? 10, bottom: opts.bottom ?? 26 };
  const plotW = width - m.left - m.right;
  const plotH = height - m.top - m.bottom;
  const y = (v) => m.top + plotH - (v / top) * plotH;
  const root = svg("svg", { class: "chart-svg", width, height, viewBox: `0 0 ${width} ${height}`, role: "img" });
  const grid = svg("g", { class: "grid" });
  for (const v of ticks) {
    grid.append(svg("line", { x1: m.left, x2: width - m.right, y1: y(v), y2: y(v), class: v === 0 ? "baseline" : "" }));
    const label = svg("text", { x: m.left - 6, y: y(v) + 4, "text-anchor": "end", class: "tick" });
    label.textContent = fmtTick(v);
    grid.append(label);
  }
  root.append(grid);
  return { root, width, height, m, plotW, plotH, y, ticks };
}

// Time grid behind a curve: faint alternating bands (hours, or 5 minutes in the last hour) to read a time off,
// a vertical hairline at every labeled time, and a short tick under the baseline where a label stands.
// bands: shaded [from, to] in curve positions; marks: positions of the labels.
function timeGrid(frame, x, n, bands, marks, ticks) {
  const { m, plotH, root } = frame;
  const bottom = m.top + plotH;
  const g = svg("g", { class: "time-grid" });
  for (const [from, to] of bands) {
    const a = Math.max(0, from), b = Math.min(n - 1, to);
    if (b > a) g.append(svg("rect", { class: "band", x: x(a), y: m.top, width: x(b) - x(a), height: plotH }));
  }
  for (const pos of marks) if (pos > 0 && pos < n - 1) g.append(svg("line", { x1: x(pos), x2: x(pos), y1: m.top, y2: bottom }));
  if (ticks) for (const pos of marks) {
    const xi = x(Math.min(n - 1, pos));
    g.append(svg("line", { class: "tick-mark", x1: xi, x2: xi, y1: bottom, y2: bottom + 4 }));
  }
  root.insertBefore(g, root.firstChild);
}

// Hover and keyboard: every position of the x-axis shows all series. targets: the SVGs that react
// (one per panel); the tooltip stands beside the position, next to the panel the pointer is in.
function chartHover(chart, { tip, width, buckets, targets, xOf, indexAt, show }) {
  let current = null;
  let panelTop = 0;
  const move = (i) => {
    if (i == null || i < 0 || i >= buckets) return;
    current = i;
    tip.replaceChildren(...show(i));
    tip.hidden = false;
    const x = xOf(i);
    const tipW = tip.offsetWidth;
    tip.style.left = `${x + 16 + tipW <= width ? x + 16 : Math.max(0, x - 16 - tipW)}px`;
    tip.style.top = `${Math.min(panelTop, Math.max(0, chart.offsetHeight - tip.offsetHeight))}px`;
  };
  const hide = () => {
    current = null;
    tip.hidden = true;
    show(null);
  };
  for (const target of targets) {
    target.addEventListener("pointermove", (e) => {
      const rect = target.getBoundingClientRect();
      panelTop = rect.top - chart.getBoundingClientRect().top + 8;
      move(indexAt(e.clientX - rect.left));
    });
    target.addEventListener("pointerleave", (e) => {
      if (!targets.some((t2) => t2.contains(e.relatedTarget))) hide();
    });
  }
  chart.tabIndex = 0;
  chart.onkeydown = (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const step = e.key === "ArrowLeft" ? -1 : 1;
    move(current == null ? (step > 0 ? 0 : buckets - 1) : Math.min(buckets - 1, Math.max(0, current + step)));
  };
  chart.onblur = hide;
}

function tipRow(sr, value, unit, cls = "line") {
  return el("div", { class: "tip-row" }, el("span", { class: `key ${cls} s${sr.slot}`, "aria-hidden": "true" }),
    el("strong", {}, value == null ? "–" : `${value} ${unit}`), el("span", { class: "muted" }, sr.name));
}

// Power over the day. Several plugs: one panel each with its own scale (small multiples) and a shared time
// axis - a 100 W PC would otherwise press a 1 W standby flat onto the baseline.
function drawCurve(chart, data, series, opts = {}) {
  const n = data.curve.plugs[data.plugs[0].id].length;
  const step = data.curve.step;
  const start = data.curve.start;
  const multiple = series.length > 1;
  const fmtTick = (v) => fmtNum(v, v < 10 ? 1 : 0);
  const maxOf = (sr) => Math.max(1, ...sr.curve.filter((v) => v != null));
  const groups = multiple ? series.map((sr) => [sr]) : [series];
  const tickSets = groups.map((g) => niceTicks(Math.max(...g.map(maxOf)), multiple ? 3 : 4));
  const left = 10 + Math.max(...tickSets.flat().map((v) => fmtTick(v).length)) * 7;
  const narrow = chart.clientWidth < 520;
  const axis = opts.axis ? opts.axis(narrow)
    : (narrow ? [0, 6, 12, 18, 24] : [0, 3, 6, 9, 12, 15, 18, 21, 24]).map((h) => [(h * 3600) / step, `${String(h).padStart(2, "0")}:00`]);
  // every other hour of the day shaded (01-02, 03-04, ...)
  const bands = opts.bands ? opts.bands()
    : Array.from({ length: 12 }, (_, k) => [((2 * k + 1) * 3600) / step, ((2 * k + 2) * 3600) / step]);
  const panels = groups.map((group, k) => {
    const last = k === groups.length - 1;
    const frame = chartFrame(chart, Math.max(...group.map(maxOf)), fmtTick, multiple
      ? { height: (narrow ? 84 : 96) + (last ? 20 : 0), ticks: 3, left, top: 8, bottom: last ? 26 : 6 }
      : { left });
    const { m, plotW, y, root } = frame;
    const x = (i) => m.left + (i / (n - 1)) * plotW;
    timeGrid(frame, x, n, bands, axis.map(([pos]) => pos), last);
    if (last) {
      for (const [pos, text] of axis) {
        const i = Math.min(n - 1, pos);
        const label = svg("text", { x: x(i), y: frame.height - 6, "text-anchor": i <= 0 ? "start" : i >= n - 1 ? "end" : "middle", class: "tick" });
        label.textContent = text;
        root.append(label);
      }
    }
    for (const sr of group) {
      // Line with gaps where nothing is known; a wash below it when the panel has one series
      let d = "", area = "", run = [];
      const flush = () => {
        if (run.length > 1 && group.length === 1) area += `M${x(run[0])},${y(0)}${run.map((i) => `L${x(i)},${y(sr.curve[i])}`).join("")}L${x(run[run.length - 1])},${y(0)}Z`;
        run = [];
      };
      sr.curve.forEach((v, i) => {
        if (v == null) return flush();
        d += `${run.length ? "L" : "M"}${x(i)},${y(v)}`;
        run.push(i);
      });
      flush();
      if (area) root.append(svg("path", { d: area, class: `area s${sr.slot}` }));
      root.append(svg("path", { d, class: `line s${sr.slot}` }));
    }
    const cross = svg("line", { class: "crosshair", y1: m.top, y2: m.top + frame.plotH, visibility: "hidden" });
    const dots = svg("g", {});
    root.append(cross, dots);
    const show = (i) => {
      dots.replaceChildren();
      cross.setAttribute("visibility", i == null ? "hidden" : "visible");
      if (i == null) return;
      cross.setAttribute("x1", x(i));
      cross.setAttribute("x2", x(i));
      for (const sr of group) if (sr.curve[i] != null) dots.append(svg("circle", { cx: x(i), cy: y(sr.curve[i]), r: 4, class: `dot s${sr.slot}` }));
    };
    const value = multiple && opts.headValue ? opts.headValue(group[0]) : null;
    const head = multiple ? el("div", { class: "multiple-head" }, el("span", { class: `key line s${group[0].slot}`, "aria-hidden": "true" }),
      group[0].name, group[0].consumer ? el("span", { class: "muted" }, group[0].consumer) : null,
      value ? el("strong", { class: "head-value" }, value) : null) : null;
    return { frame, x, show, node: el("div", { class: "multiple" }, head, root) };
  });
  const tip = el("div", { class: "chart-tip", hidden: true, role: "status" });
  chart.replaceChildren(...panels.map((p) => p.node), tip);
  const { frame, x } = panels[0];
  chartHover(chart, {
    tip, width: frame.width, buckets: n, targets: panels.map((p) => p.frame.root), xOf: x,
    indexAt: (px) => Math.round(((px - frame.m.left) / frame.plotW) * (n - 1)),
    show: (i) => {
      for (const p of panels) p.show(i);
      if (i == null) return null;
      const time = opts.time ? opts.time(i) : new Date((start + i * step) * 1000).toLocaleTimeString(I18N.locale, { hour: "2-digit", minute: "2-digit" });
      return [el("div", { class: "tip-head" }, time), ...series.map((sr) => tipRow(sr, sr.curve[i] == null ? null : fmtWatt(sr.curve[i]), "W"))];
    },
  });
}

function drawBars(chart, data, series, period) {
  const n = data.buckets.length;
  const totals = data.buckets.map((_, i) => series.reduce((sum, sr) => sum + (sr.energy[i] || 0), 0) / 1000);
  const max = Math.max(0.1, ...totals);
  const digits = max < 1 ? 2 : max < 10 ? 1 : 0;
  const frame = chartFrame(chart, max, (v) => fmtNum(v, 2));
  const { m, plotW, y, root } = frame;
  const band = plotW / n;
  const barW = Math.max(3, Math.min(24, band - 4));
  const cx = (i) => m.left + band * (i + 0.5);
  if (period === "month") { // weekends shaded behind the columns
    const g = svg("g", { class: "time-grid" });
    data.buckets.forEach((b, i) => {
      const day = new Date(`${data.date.slice(0, 8)}${b}T12:00:00`).getDay();
      if (day === 0 || day === 6) g.append(svg("rect", { class: "band", x: m.left + band * i, y: m.top, width: band, height: frame.plotH }));
    });
    root.insertBefore(g, root.firstChild);
  }
  const every = Math.ceil(n / Math.max(1, Math.floor(plotW / 34)));
  data.buckets.forEach((_, i) => {
    if (i !== n - 1 && (i % every || n - 1 - i < every)) return; // the last label keeps its neighbor at a distance
    const label = svg("text", { x: cx(i), y: frame.height - 8, "text-anchor": "middle", class: "tick" });
    label.textContent = bucketLabel(period, data, i);
    root.append(label);
  });
  const columns = svg("g", {});
  const peak = totals.indexOf(Math.max(...totals));
  data.buckets.forEach((_, i) => {
    const col = svg("g", { class: "column" });
    let base = 0;
    const segments = series.map((sr) => ({ sr, v: (sr.energy[i] || 0) / 1000 })).filter((sg) => sg.v > 0);
    segments.forEach((sg, k) => {
      const y0 = y(base), y1 = y(base + sg.v);
      const gap = k ? 2 : 0; // surface gap between stacked segments
      const h = Math.max(0, y0 - y1 - gap);
      const x0 = cx(i) - barW / 2;
      const top = y0 - gap - h;
      if (k === segments.length - 1 && h > 0) {
        const r = Math.min(4, h, barW / 2); // rounded data end, square at the baseline
        col.append(svg("path", { class: `bar s${sg.sr.slot}`, d: `M${x0},${y0 - gap}V${top + r}Q${x0},${top} ${x0 + r},${top}H${x0 + barW - r}Q${x0 + barW},${top} ${x0 + barW},${top + r}V${y0 - gap}Z` }));
      } else if (h > 0) {
        col.append(svg("rect", { class: `bar s${sg.sr.slot}`, x: x0, y: top, width: barW, height: h }));
      }
      base += sg.v;
    });
    if (i === peak && totals[i] > 0) {
      const label = svg("text", { x: cx(i), y: y(totals[i]) - 6, "text-anchor": "middle", class: "value" });
      label.textContent = fmtNum(totals[i], digits);
      col.append(label);
    }
    columns.append(col);
  });
  root.append(columns);
  const cols = [...columns.children];
  const tip = el("div", { class: "chart-tip", hidden: true, role: "status" });
  chart.replaceChildren(root, tip);
  chartHover(chart, {
    tip, width: frame.width, buckets: n, targets: [root], xOf: cx, indexAt: (px) => Math.floor((px - m.left) / band),
    show: (i) => {
      cols.forEach((c, k) => c.classList.toggle("dim", i != null && k !== i));
      if (i == null) return null;
      const rows = series.map((sr) => tipRow(sr, sr.energy[i] == null ? null : fmtNum(sr.energy[i] / 1000, 2), "kWh", "box"));
      const cost = costOf(totals[i] * 1000);
      return [el("div", { class: "tip-head" }, bucketLabel(period, data, i, true)),
        el("div", { class: "tip-total" }, el("strong", {}, `${fmtNum(totals[i], 2)} kWh`), cost == null ? null : el("span", { class: "muted" }, fmtMoney(cost))),
        ...(series.length > 1 ? rows : [])];
    },
  });
}

function drawEnergyTable(series, total, period) {
  const price = state.plugSettings.price_kwh;
  const head = [t("Plug"), t("Consumer"), t("Energy"), t("Share"), price != null && t("Costs"), t("Standby per year")].filter(Boolean);
  const row = (sr) => [
    el("td", {}, el("span", { class: `key box s${sr.slot}`, "aria-hidden": "true" }), sr.name),
    el("td", { class: "hide-sm" }, sr.consumer || "–"),
    el("td", { class: "num" }, fmtKwh(sr.total)),
    el("td", { class: "num" }, total > 0 ? `${fmtNum((sr.total / total) * 100, 0)} %` : "–"),
    price != null && el("td", { class: "num" }, fmtMoney(costOf(sr.total))),
    el("td", { class: "num" }, sr.standby_year == null ? "–" : price == null ? fmtKwh(sr.standby_year) : `${fmtKwh(sr.standby_year)} · ${fmtMoney(costOf(sr.standby_year))}`),
  ].filter(Boolean);
  const labels = head;
  const tr_ = (cells, cls = "") => el("tr", { class: cls }, cells.map((c, k) => { c.dataset.label = labels[k]; return c; }));
  const standbyAll = series.filter((sr) => sr.standby_year != null).reduce((sum, sr) => sum + sr.standby_year, 0);
  const foot = series.length > 1 ? tr_([
    el("td", {}, el("strong", {}, t("Together"))), el("td", { class: "hide-sm" }),
    el("td", { class: "num" }, el("strong", {}, fmtKwh(total))), el("td", {}),
    price != null && el("td", { class: "num" }, el("strong", {}, fmtMoney(costOf(total)))),
    el("td", { class: "num" }, standbyAll ? fmtKwh(standbyAll) : "–"),
  ].filter(Boolean), "total") : null;
  $("#energy-table").replaceChildren(el("table", { class: "data energy-table" },
    el("caption", { class: "sr-only" }, t("Energy per plug, {0}", periodLabel(period, plugView.date))),
    el("thead", {}, el("tr", {}, head.map((h, k) => el("th", { scope: "col", class: k >= 2 ? "num" : k === 1 ? "hide-sm" : "" }, h)))),
    el("tbody", {}, series.map((sr) => tr_(row(sr))), foot)));
}

// CSV of the shown period: one row per hour, day or month, one column per plug (kWh)
function exportEnergyCsv() {
  const data = plugView.data;
  if (!data) return;
  const series = energySeries(data);
  const de = I18N.lang === "de";
  const sep = de ? ";" : ",";
  if (plugView.period === "hour") return exportLiveCsv(data, series, de, sep);
  const num = (wh) => (wh == null ? "" : (wh / 1000).toFixed(3).replace(".", de ? "," : "."));
  const quote = (text) => `"${String(text).replace(/"/g, '""')}"`;
  const first = { day: t("Hour"), month: t("Day"), year: t("Month") }[plugView.period];
  const lines = [[first, ...series.map((sr) => quote(`${sr.name} (kWh)`)), quote(`${t("Together")} (kWh)`)].join(sep)];
  data.buckets.forEach((b, i) => {
    const values = series.map((sr) => sr.energy[i]);
    const sum = values.some((v) => v != null) ? values.reduce((a, v) => a + (v || 0), 0) : null;
    const key = plugView.period === "day" ? `${data.date} ${b}:00` : plugView.period === "month" ? `${data.date.slice(0, 8)}${b}` : `${data.date.slice(0, 4)}-${b}`;
    lines.push([key, ...values.map(num), num(sum)].join(sep));
  });
  const name = `wol-energy-${plugView.period}-${plugView.period === "day" ? data.date : plugView.period === "month" ? data.date.slice(0, 7) : data.date.slice(0, 4)}.csv`;
  downloadCsv(lines, name);
}

// CSV of the last hour: one row per 5 s step, one column per plug (W)
function exportLiveCsv(data, series, de, sep) {
  const { start, step } = data.curve;
  const num = (w) => (w == null ? "" : String(w).replace(".", de ? "," : "."));
  const quote = (text) => `"${String(text).replace(/"/g, '""')}"`;
  const lines = [[t("Time"), ...series.map((sr) => quote(`${sr.name} (W)`))].join(sep)];
  const pad = (v) => String(v).padStart(2, "0");
  series[0].curve.forEach((_, i) => {
    const d = new Date((start + i * step) * 1000);
    const key = `${isoDate(d)} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    lines.push([key, ...series.map((sr) => num(sr.curve[i]))].join(sep));
  });
  downloadCsv(lines, `wol-power-${isoDate(new Date())}.csv`);
}

function downloadCsv(lines, name) {
  const blob = new Blob(["\ufeff" + lines.join("\r\n") + "\r\n"], { type: "text/csv;charset=utf-8" });
  const a = el("a", { href: URL.createObjectURL(blob), download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

async function onPriceSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await busy($("button[type=submit]", form), async () => {
    try {
      const res = await api("PUT", "/api/energy/settings", { price_kwh: form.elements.price_kwh.value.trim() });
      state.plugSettings = res.settings;
      toast(t("Price saved"), "success");
      drawEnergy();
    } catch (e) {
      toast(e.message, "error");
    }
  });
}

// ---------- System ----------

async function renderSystem() {
  const [info] = await Promise.all([api("GET", "/api/info"), renderWifi(), renderVpn(), renderTokens()]);
  state.info = info;
  const rows = [
    [t("Hostname"), info.hostname],
    [t("Model"), info.model || "–"],
    [t("Version"), info.version],
    [t("Server time"), `${info.time.replace("T", " ")} ${info.timezone}`],
    [t("System up for"), fmtDuration(info.system_uptime)],
    [t("Service up for"), fmtDuration(info.service_uptime)],
    [t("CPU temperature"), info.cpu_temp != null ? `${info.cpu_temp.toFixed(1)} °C` : "–"],
    [t("Load (1 min)"), info.load.toFixed(2)],
    ["HTTPS", httpsLabel(info.https)],
  ];
  $("#sys-info").replaceChildren(...rows.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v))));
  renderSysNetwork(info.network);
  $("#sys-networks").replaceChildren(...info.allowed_networks.map((n) => el("span", { class: "chip mono" }, n.network,
    n.source !== "config" && el("span", { class: "chip-src" }, n.source === "auto" ? t("always") : n.source))));
  $("#sys-client").textContent = info.client;
  $("#ssh-key").value = info.ssh_public_key || t("(no key: ssh-keygen not available)");
  $("#pw-title").textContent = info.auth_required ? t("Change password") : t("Set password");
  $("#pw-current-field").hidden = !info.auth_required;
}

function httpsLabel(https) {
  if (!https || !https.port) return t("off");
  if (!https.names) return t("no certificate");
  const until = new Date(https.not_after * 1000).toLocaleDateString(I18N.locale);
  return https.active ? t("{0}, valid until {1}", https.names[0], until) : t("Certificate expired ({0})", until);
}

const IFACE_KINDS = { wifi: t("Wi-Fi"), lan: "LAN", vpn: "VPN" };

function signalLabel(dbm) {
  if (dbm >= -60) return [t("good"), "good"];
  if (dbm >= -70) return [t("fair"), "fair"];
  return [t("weak"), "poor"];
}

function renderSysNetwork(info) {
  const ifaces = info.interfaces.filter((i) => i.up && i.address);
  const vpn = ifaces.filter((i) => i.kind === "vpn");
  const wifi = ifaces.find((i) => i.wifi);
  const port = info.port === 80 ? "" : `:${info.port}`;
  const rows = [];
  if (wifi) {
    const [label, cls] = signalLabel(wifi.wifi.signal_dbm);
    rows.push([t("Wi-Fi signal"), el("span", { class: `signal ${cls}` }, `${wifi.wifi.signal_dbm} dBm · ${label}`)]);
  }
  rows.push(["VPN", vpn.length
    ? el("span", { class: "signal good" }, t("connected · {0}", vpn.map((i) => i.address).join(", ")))
    : el("span", { class: "muted" }, t("not connected"))]);
  rows.push([t("Magic packets via"), info.wol_interface || t("default route")]);
  $("#sys-network").replaceChildren(...rows.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v))));
  $("#sys-ifaces").replaceChildren(...ifaces.map((i) =>
    el("div", { class: "iface" },
      el("span", { class: `tag kind-${i.kind}` }, IFACE_KINDS[i.kind] || i.kind),
      el("span", { class: "mono" }, i.name),
      el("a", { class: "mono", href: `http://${i.address}${port}/` }, i.address))));
}

// ---------- Wi-Fi ----------

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
  if (wifi.pending) status = el("span", {}, t("connecting to \"{0}\" …", wifi.pending));
  else if (wifi.mode === "client") status = el("span", { class: "signal good" }, t("connected to \"{0}\"", wifi.ssid || "?"));
  else if (wifi.mode === "hotspot") status = el("span", { class: "signal fair" }, t("Hotspot \"{0}\" active", hs.ssid));
  else status = el("span", { class: "signal poor" }, t("not connected"));
  const rows = [
    [t("Status"), status],
    [t("Interface"), wifi.address ? `${wifi.interface} · ${wifi.address}` : wifi.interface],
    [t("Setup hotspot"), hs.enabled ? `${hs.ssid} · ${hs.secured ? t("with password") : t("open")}` : t("disabled")],
  ];
  $("#wifi-info").replaceChildren(...rows.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v))));

  const error = $("#wifi-error");
  error.hidden = !wifi.last_error;
  if (wifi.last_error) error.textContent = `${fmtDateTime(wifi.last_error.ts)}: ${tr(wifi.last_error.message)}`;

  const current = wifi.mode === "client" ? wifi.ssid : null;
  $("#wifi-saved").replaceChildren(...(wifi.saved.length ? wifi.saved.map((ssid) =>
    el("div", { class: "saved-item" },
      el("span", {}, ssid),
      ssid === current && el("span", { class: "tag current" }, t("connected")),
      el("button", {
        class: "icon-btn", type: "button", disabled: ssid === current,
        title: ssid === current ? t("The connected Wi-Fi cannot be removed") : t("Remove"),
        "aria-label": t("Remove {0}", ssid), onclick: () => forgetWifi(ssid),
      }, icon("x")))) : [el("p", { class: "muted small" }, t("No Wi-Fi saved."))]));

  $("#hotspot-text").textContent = hs.enabled
    ? t("If none of the saved Wi-Fi networks is reachable for {0} minutes (immediately without a saved network), the WOL-Server opens the hotspot \"{1}\". After connecting, the setup page opens automatically, alternatively at http://{2}/. While the hotspot is running, the WOL-Server looks for the saved networks every few minutes.", hs.delay_minutes, hs.ssid, hs.address)
    : t("The setup hotspot is disabled (hotspot_enabled in /etc/wol-server/config.json).");
}

async function forgetWifi(ssid) {
  if (!(await confirmDialog(t("Remove \"{0}\"?", ssid), t("The Wi-Fi profile including its password will be deleted."), t("Remove"), true))) return;
  try {
    await api("POST", "/api/wifi/forget", { ssid });
    toast(t("\"{0}\" removed", ssid), "success");
  } catch (e) {
    toast(e.message, "error");
  }
  await renderWifi();
}

function signalBars(strength) {
  const level = Math.max(1, Math.min(4, Math.ceil(strength / 25)));
  return el("span", { class: `bars b${level}`, title: t("Signal {0} %", strength) }, el("i"), el("i"), el("i"), el("i"));
}

function renderScan(networks, scanTime, cached) {
  const form = $("#wifi-form");
  const list = $("#wifi-scan");
  if (!networks.length) {
    list.replaceChildren(el("p", { class: "muted small" }, t("No networks found. Enter the name below.")));
  } else {
    list.replaceChildren(...networks.map((n) =>
      el("button", {
        type: "button", class: `scan-item${n.ssid === form.elements.ssid.value ? " selected" : ""}`,
        onclick: (e) => pickNetwork(n, e.currentTarget),
      },
      signalBars(n.signal),
      el("span", { class: "scan-ssid" }, n.ssid),
      n.security ? icon("lock") : el("span", { class: "muted small" }, t("open")))));
  }
  const when = scanTime ? new Date(scanTime * 1000).toLocaleTimeString(I18N.locale, { hour: "2-digit", minute: "2-digit" }) : "";
  $("#wifi-scan-info").textContent = cached
    ? (when ? t("Found before the hotspot started ({0})", when) : t("Found before the hotspot started"))
    : when ? t("Found at {0}", when) : "";
  // In hotspot mode the Zero cannot scan.
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
  $("#wifi-static").hidden = true;
  const wifi = state.wifi;
  $("#wifi-step-form").hidden = false;
  $("#wifi-step-done").hidden = true;
  $("#wifi-submit").hidden = false;
  $("#wifi-submit").textContent = wifi && wifi.mode === "client" ? t("Save") : t("Connect");
  $("#wifi-cancel").textContent = t("Cancel");
  renderScan(wifi ? wifi.scan : [], wifi && wifi.scan_time, wifi && wifi.mode === "hotspot");
  $("#wifi-dialog").showModal();
  if (wifi && wifi.mode !== "hotspot") scanWifi();
}

function showWifiSwitching(ssid, ip) {
  $("#wifi-step-form").hidden = true;
  $("#wifi-step-done").hidden = false;
  $("#wifi-submit").hidden = true;
  $("#wifi-cancel").textContent = t("Close");
  $("#wifi-done-title").textContent = t("Connecting to \"{0}\" …", ssid);
  const host = state.info ? state.info.hostname : "wol-server";
  const port = state.info && state.info.network && state.info.network.port !== 80 ? `:${state.info.network.port}` : "";
  const url = `http://${host}.local${port}/`;
  const ipUrl = ip && `http://${ip}${port}/`;
  $("#wifi-done-text").replaceChildren(state.hotspot
    ? el("span", {}, t("The hotspot is shutting down; the connection to this device will drop."), " ",
      t("In the home network the interface is available at"), " ", el("a", { href: url }, url),
      ...(ipUrl ? [" ", t("or"), " ", el("a", { href: ipUrl }, ipUrl), "."]
        : [" ", t("or at the address the router shows for the Pi.")]))
    : el("span", {}, t("The status updates automatically.")));
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
  if (form.elements.static_ip.checked) {
    data.ipv4 = { address: form.elements.ip_address.value, gateway: form.elements.ip_gateway.value, dns: form.elements.ip_dns.value };
    if (!data.ipv4.address.trim()) return showError(form, t("Enter the static address, e.g. 192.168.1.8/24."));
  }
  if (!data.ssid.trim()) return showError(form, t("Select a Wi-Fi network or enter its name."));
  if (data.password && data.password.length < 8) return showError(form, t("The Wi-Fi password has at least 8 characters."));
  await busy($("#wifi-submit"), async () => {
    try {
      const res = await api("POST", "/api/wifi/connect", data);
      if (res.switching || res.pending) {
        showWifiSwitching(data.ssid, data.ipv4 && data.ipv4.address.trim().split("/")[0]);
        setTimeout(() => renderWifi().catch(() => {}), 3000);
      } else {
        $("#wifi-dialog").close();
        toast(res.message || t("Wi-Fi saved"), "success");
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
    return showError(form, t("The password needs 8 to 63 characters. Without a password the hotspot is open."));
  }
  if (!password && !(await confirmDialog(t("Hotspot without a password?"),
    t("Without a password, any device within radio range can connect to the setup hotspot. The web GUI stays protected by its own password."),
    t("Leave open")))) return;
  await busy($("button[type=submit]", form), async () => {
    try {
      const res = await api("PUT", "/api/wifi/hotspot", { password });
      form.reset();
      toast(res.message || t("Saved"), "success");
      await renderWifi();
    } catch (e) {
      showError(form, e.message);
    }
  });
}

// ---------- VPN ----------

const VPN_STATES = { active: t("connected"), activating: t("connecting …"), reloading: t("connecting …"), deactivating: t("disconnecting …") };

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
  if (job) status = el("span", {}, t("setting up …"));
  else if (!vpn.configured) status = el("span", { class: "muted" }, t("not set up"));
  else if (vpn.address) status = el("span", { class: "signal good" }, t("connected · {0}", vpn.address));
  else if (vpn.enabled) status = el("span", { class: "signal fair" }, VPN_STATES[vpn.state] || t("connecting …"));
  else status = el("span", { class: "muted" }, t("switched off"));
  const rows = [[t("Status"), status]];
  if (vpn.remote) rows.push([t("Server"), el("span", { class: "mono" }, vpn.remote)]);
  if (vpn.address) {
    const port = state.info && state.info.network && state.info.network.port !== 80 ? `:${state.info.network.port}` : "";
    rows.push([t("GUI via VPN"), el("a", { class: "mono", href: `http://${vpn.address}${port}/` }, `http://${vpn.address}${port}/`)]);
  }
  if (vpn.uploaded) rows.push([t("Uploaded"), fmtDateTime(vpn.uploaded)]);
  $("#vpn-info").replaceChildren(...rows.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v))));

  const error = $("#vpn-error");
  const failed = vpn.job && vpn.job.state === "failed" ? vpn.job : null;
  error.hidden = !failed;
  if (failed) error.textContent = `${fmtDateTime(failed.ts)}: ${tr(failed.message)}`;

  $("#vpn-actions").hidden = !vpn.configured;
  const toggle = $("#vpn-toggle");
  toggle.textContent = vpn.enabled ? t("Switch off") : t("Switch on");
  toggle.disabled = Boolean(job);
  $("#vpn-remove").disabled = Boolean(job);
  $("#vpn-upload").disabled = Boolean(job);
}

// Preview in the browser only; the Pi does the real check.
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
  $("#vpn-file-info").textContent = t("OpenVPN client configuration (.ovpn) with embedded certificates.");
  for (const field of $$("[data-auth]", form)) field.hidden = true;
  $("#vpn-dialog").showModal();
}

async function onVpnFile(event) {
  const form = $("#vpn-form");
  showError(form, "");
  const file = event.target.files[0];
  state.vpnFile = null;
  if (!file) return;
  if (file.size > 48 * 1024) return showError(form, t("The file is too large (max. 48 KB)."));
  const text = await file.text();
  const info = inspectOvpn(text);
  if (!info.client || !info.remote) {
    return showError(form, t("Not an OpenVPN client configuration: \"client\" or \"remote\" is missing."));
  }
  state.vpnFile = { text, info };
  $("#vpn-file-info").textContent = info.needsAuth ? t("Server: {0} · needs user name and password", info.remote) : t("Server: {0}", info.remote);
  for (const field of $$("[data-auth]", form)) field.hidden = !info.needsAuth;
}

async function onVpnSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showError(form, "");
  const file = state.vpnFile;
  if (!file) return showError(form, t("Select the .ovpn file."));
  const data = { config: file.text };
  if (file.info.needsAuth) {
    data.username = form.elements.username.value;
    data.password = form.elements.password.value;
    if (!data.username || !data.password) return showError(form, t("This configuration needs a user name and password."));
  }
  await busy($("#vpn-submit"), async () => {
    try {
      const res = await api("POST", "/api/vpn/config", data);
      $("#vpn-dialog").close();
      state.vpnFile = null;
      toast(res.message || t("Setting up …"), "success");
      await renderVpn();
    } catch (e) {
      showError(form, e.message);
    }
  });
}

// Opened via the VPN: the page was loaded from the Pi's VPN address
function viaVpn() {
  return Boolean(state.vpn && state.vpn.address && location.hostname === state.vpn.address);
}

async function onVpnToggle() {
  const enable = !state.vpn.enabled;
  if (!enable && viaVpn() && !(await confirmDialog(t("Switch off VPN?"),
    t("This session runs over the VPN and will be disconnected. Without VPN the interface is only reachable from the home network."),
    t("Switch off"), true))) return;
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
  if (!(await confirmDialog(t("Remove VPN configuration?"),
    t("Certificates and credentials will be deleted. Without VPN the WOL-Server is only reachable from the home network."),
    t("Remove"), true))) return;
  try {
    const res = await api("DELETE", "/api/vpn");
    toast(res.message, "success");
  } catch (e) {
    toast(e.message, "error");
  }
  await renderVpn();
}

// ---- API tokens ----

async function renderTokens() {
  const box = $("#token-list");
  try {
    const { tokens } = await api("GET", "/api/tokens");
    box.replaceChildren(...(tokens.length ? tokens.map((tok) => el("div", { class: "saved-item" },
      el("span", {}, el("strong", {}, tok.name)),
      el("span", { class: "muted small" }, tok.last_used ? t("used {0}", fmtAgo(tok.last_used)) : t("never used"),
        " · ", t("created {0}", fmtDateTime(tok.created))),
      el("button", { type: "button", class: "btn small ghost danger", onclick: () => revokeToken(tok) }, t("Revoke"))))
      : [el("p", { class: "muted small" }, t("No API tokens."))]));
  } catch (e) {
    box.replaceChildren(el("p", { class: "muted small" }, e.message));
  }
}

async function onTokenSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showError(form, "");
  await busy($("button[type=submit]", form), async () => {
    try {
      const res = await api("POST", "/api/tokens", { name: form.elements.name.value });
      form.reset();
      const device = (state.devices[0] && state.devices[0].name) || "backup-server";
      $("#token-value").value = res.token;
      $("#token-example").textContent = `curl -fsS -X POST -H "Authorization: Bearer ${res.token}" \\\n` +
        `  "${location.origin}/api/devices/${encodeURIComponent(device.toLowerCase())}/wake?wait=180"`;
      $("#token-new").hidden = false;
      await renderTokens();
    } catch (e) {
      showError(form, e.message);
    }
  });
}

async function revokeToken(tok) {
  if (!(await confirmDialog(t("Revoke token \"{0}\"?", tok.name), t("Scripts using this token stop working immediately."), t("Revoke"), true))) return;
  try {
    await api("DELETE", `/api/tokens/${tok.id}`);
    $("#token-new").hidden = true;
    toast(t("Token revoked"), "success");
    await renderTokens();
  } catch (e) {
    toast(e.message, "error");
  }
}

async function copyToken() {
  const area = $("#token-value");
  try {
    await navigator.clipboard.writeText(area.value);
  } catch (e) {
    area.select();
    document.execCommand("copy");
  }
  toast(t("Token copied"), "success");
}

async function copyKey() {
  const area = $("#ssh-key");
  try {
    await navigator.clipboard.writeText(area.value);
  } catch (e) {
    // The Clipboard API exists only in secure contexts (HTTPS); use the fallback over plain HTTP.
    area.select();
    document.execCommand("copy");
  }
  toast(t("Key copied"), "success");
}

async function onPasswordSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showError(form, "");
  if (form.new.value.length < 8) return showError(form, t("At least 8 characters"));
  if (form.new.value !== form.repeat.value) return showError(form, t("Passwords do not match"));
  await busy($("button[type=submit]", form), async () => {
    try {
      await api("POST", "/api/password", { current: form.current.value, new: form.new.value });
      form.reset();
      toast(t("Password saved"), "success");
      await updateSession();
      await renderSystem();
    } catch (e) {
      showError(form, e.message);
    }
  });
}

// ---------- Network (DHCP and DNS) ----------

const NET_SUBS = ["overview", "reservations", "leases", "dns", "settings", "tools"];
const LEASE_PRESETS = [[600, t("10 minutes")], [3600, t("1 hour")], [14400, t("4 hours")], [43200, t("12 hours")], [86400, t("1 day")], [604800, t("7 days")]];
const REC_TYPES = { host: t("Name → IP"), alias: t("Alias"), mx: "MX", srv: "SRV", txt: "TXT" };
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
  if (s < 90) return t("just now");
  if (s < 3600) return t("{0} min ago", Math.round(s / 60));
  if (s < 86400) return t("{0} h ago", Math.round(s / 3600));
  const d = Math.round(s / 86400);
  return d === 1 ? t("yesterday") : t("{0} days ago", d);
}

function fmtIn(ts) {
  if (!ts) return t("unlimited");
  const s = ts - Date.now() / 1000;
  if (s <= 0) return t("expired");
  if (s < 3600) return t("{0} min left", Math.max(1, Math.round(s / 60)));
  if (s < 86400) return t("{0} h left", Math.round(s / 3600));
  return t("{0} d left", Math.round(s / 86400));
}

function fmtLease(seconds) {
  const preset = LEASE_PRESETS.find(([v]) => v === seconds);
  if (preset) return preset[1];
  if (seconds % 86400 === 0) return t("{0} days", seconds / 86400);
  if (seconds % 3600 === 0) return `${seconds / 3600} h`;
  if (seconds % 60 === 0) return `${seconds / 60} min`;
  return `${seconds} s`;
}

// Compact form for the input field, independent of the language: "3d", "2h", "90m", "45"
function compactLease(seconds) {
  if (seconds % 86400 === 0) return `${seconds / 86400}d`;
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return String(seconds);
}

// Lease time picker: presets or a custom value ("90m", "2h", "3d"). allowDefault: "server default" for reservations.
function leasePicker(container, value, allowDefault = false) {
  const select = el("select", { "aria-label": t("Lease time") },
    allowDefault && el("option", { value: "" }, t("Server default")),
    LEASE_PRESETS.map(([v, label]) => el("option", { value: String(v) }, label)),
    el("option", { value: "custom" }, t("Custom value …")));
  const custom = el("input", { class: "mono", placeholder: t("e.g. 90m, 2h, 3d"), "aria-label": t("Custom lease time") });
  const known = value == null ? "" : LEASE_PRESETS.some(([v]) => v === value) ? String(value) : "custom";
  select.value = allowDefault || value != null ? known : "43200";
  if (known === "custom") custom.value = compactLease(value);
  custom.hidden = select.value !== "custom";
  select.addEventListener("change", () => {
    custom.hidden = select.value !== "custom";
    if (!custom.hidden) custom.focus();
  });
  container.replaceChildren(select, custom);
  return () => (select.value === "custom" ? custom.value.trim() : select.value === "" ? null : Number(select.value));
}

// Table with sortable columns. columns: [{key, label, sort?, render, cls?}]
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
    badge.title = t("{0} unknown devices", status.counts.unknown);
  } catch (e) {
    // Network management not installed: the tab stays without a counter
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
  for (const view of $$("#view-network .subview")) view.hidden = view.id !== `net-sub-${sub}`;
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
  pill.replaceChildren(el("span", { class: "dot" }), on ? t("dnsmasq running") : st.dns_enabled || st.dhcp_enabled ? t("dnsmasq stopped") : t("off"));
  $("#net-subtitle").textContent = [
    st.dhcp_enabled ? (st.mode === "known-only" ? t("DHCP on (known devices only)") : t("DHCP on (with range)")) : t("DHCP off"),
    st.dns_enabled ? t("DNS on · {0}", st.domain) : t("DNS off"),
  ].join(" · ");
  const pending = (net.config && net.config.pending) || st.pending;
  const bar = $("#net-pending");
  bar.hidden = !pending.length;
  $("#net-pending-title").textContent = pending.length === 1 ? t("One pending change") : t("{0} pending changes", pending.length);
  $("#net-pending-list").replaceChildren(...pending.map((c) => el("li", {}, tr(c))));
  $("#net-apply").disabled = st.running;
  $("#net-discard").disabled = st.running;
  const job = $("#net-job");
  if (st.running) {
    job.hidden = false;
    job.className = "job-bar running";
    job.replaceChildren(el("span", { class: "spinner" }), el("span", {}, st.queued > 1 ? t("{0} jobs running …", st.queued) : t("Running …")));
  } else if (st.job && st.job.state === "failed" && Date.now() / 1000 - st.job.ts < 3600) {
    job.hidden = false;
    job.className = "job-bar failed";
    job.replaceChildren(icon("alert"), el("span", {}, el("strong", {}, t("Failed:")), " ", tr(st.job.message)),
      el("button", { type: "button", class: "icon-btn", title: t("Hide"), "aria-label": t("Hide"), onclick: () => { job.hidden = true; } }, icon("x")));
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

// Jobs run asynchronously (wol-dns-apply.service): watch them until they finish.
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
      watchNetJob(t("Changes applied"));
    } catch (e) {
      toast(e.message, "error");
    }
  });
}

async function netDiscard() {
  if (!(await confirmDialog(t("Discard changes?"), t("All changes in the draft will be discarded. The active configuration stays unchanged."), t("Discard"), true))) return;
  await api("POST", "/api/net/discard");
  toast(t("Draft discarded"), "success");
  await renderNetwork();
}

function afterDraftChange(message) {
  toast(message, "success");
  return renderNetwork();
}

// Without an open draft, changes take effect immediately: watch the job. With a draft they go there.
async function afterNetChange(res, success) {
  toast(res.message, res.request ? "" : "success");
  await renderNetwork();
  if (res.request) watchNetJob(success);
}

// Changes run one after another: wait for a job that is still running.
async function netIdle() {
  for (let i = 0; i < 60; i++) {
    if (!(await loadNetStatus()).running) return;
    await new Promise((r) => setTimeout(r, 1000));
  }
}

const netHasDraft = () => Boolean(net.config && net.config.pending.length);
const netSubmitLabel = () => (netHasDraft() ? t("Add to draft") : t("Apply"));

// ---- Overview ----

function statCard(title, value, lines, cls = "") {
  return el("div", { class: `stat ${cls}` }, el("div", { class: "stat-title" }, title), el("div", { class: "stat-value" }, value),
    lines.filter(Boolean).map((l) => el("div", { class: "stat-line" }, l)));
}

function renderNetOverview() {
  const st = net.status;
  const cfg = net.config.config;
  const leased = net.config.meta.leases;
  const reservedLeased = cfg.reservations.filter((r) => leased[r.mac]).length;
  const disabled = cfg.reservations.filter((r) => !r.enabled).length;
  $("#net-stats").replaceChildren(
    statCard("DHCP", st.dhcp_enabled ? t("on") : t("off"), [
      st.dhcp_enabled && (st.mode === "known-only" ? t("Known devices only") : t("Range {0} – {1}", cfg.dhcp.range_start, cfg.dhcp.range_end)),
      t("Lease time {0}", fmtLease(st.lease_time)),
      t("{0} active leases", st.counts.leases),
    ], st.dhcp_enabled ? "on" : "off"),
    statCard("DNS", st.dns_enabled ? t("on") : t("off"), [
      el("span", { class: "mono" }, st.domain),
      t("Upstream {0}", cfg.dns.upstream.join(", ") || "–"),
      t("{0} custom records", st.counts.records),
    ], st.dns_enabled ? "on" : "off"),
    statCard(t("Reservations"), String(st.counts.reservations), [
      st.dhcp_enabled && t("{0} with a lease from the WOL-Server", reservedLeased),
      disabled && t("{0} disabled", disabled),
      t("Next free IP: {0}", net.config.meta.next_ip || "–"),
    ]),
    statCard("Server", el("span", { class: "mono" }, st.server.address || "–"), [
      st.server.method === "manual" ? t("Static address") : st.server.method === "auto" ? t("Address via DHCP") : null,
      `${st.server.interface}${st.server.connection ? ` · ${st.server.connection}` : ""}`,
      st.server.mac && el("span", { class: "mono" }, st.server.mac),
    ]));

  $("#net-warnings").replaceChildren(...st.warnings.map((w) => el("div", { class: "notice warn" }, icon("alert"), el("span", {}, tr(w)))));

  const list = $("#net-unknown-list");
  const unknown = st.unknown || [];
  $("#net-unknown-info").textContent = st.dhcp_enabled && st.mode === "known-only"
    ? t("Rejected requests of the last 24 hours") : t("Only in \"known devices only\" mode");
  if (!unknown.length) {
    list.replaceChildren(el("p", { class: "muted small" }, st.dhcp_enabled ? t("No rejected requests.") : t("DHCP is off.")));
  } else {
    list.replaceChildren(...unknown.map((u) => el("div", { class: "request" },
      el("span", { class: "request-icon" }, icon("device")),
      el("div", { class: "request-main" },
        el("div", {}, el("strong", {}, u.name || t("no name")), u.vendor ? el("span", { class: "tag" }, tr(u.vendor)) : null),
        el("div", { class: "muted small" }, el("span", { class: "mono" }, u.mac), " · ", t("{0}× requested", u.count), " · ", fmtAgo(u.last))),
      el("div", { class: "request-actions" },
        el("button", { type: "button", class: "btn small ghost", onclick: () => ignoreDevice(u) }, t("Ignore")),
        el("button", { type: "button", class: "btn small primary", onclick: () => openApprove(u) }, t("Approve"))))));
  }

  const last = st.last_change;
  $("#net-last-panel").hidden = !last;
  if (last) {
    $("#net-last-meta").textContent = last.client ? t("{0} · by {1}", fmtDateTime(last.ts), last.client) : fmtDateTime(last.ts);
    const items = last.changes || [];
    $("#net-last-list").replaceChildren(...items.slice(0, 6).map((c) => el("li", {}, tr(c))),
      ...(items.length > 6 ? [el("li", { class: "muted" }, t("… and {0} more", items.length - 6))] : []));
  }
}

// ---- Unknown devices ----

let approving = null;

function openApprove(u) {
  approving = u;
  const form = $("#approve-form");
  form.reset();
  showError(form, "");
  $("#approve-info").replaceChildren(...[
    ["MAC", el("span", { class: "mono" }, u.mac)],
    [t("Vendor"), tr(u.vendor) || t("unknown")],
    [t("Reports itself as"), u.name || "–"],
    [t("Requested"), t("{0}× · last {1}", u.count, fmtAgo(u.last))],
  ].map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v))));
  form.elements.name.value = u.suggested_name || "";
  form.elements.ip.value = u.suggested_ip || "";
  $("#approve-name-hint").textContent = u.name && u.name !== u.suggested_name ? t("reported by the device: \"{0}\"", u.name) : "";
  $("#approve-private").hidden = !u.private;
  // Only a hint from the reported name; whether it really is a plug is checked after approving ("Detect")
  const looksLikePlug = /shelly|tasmota|plug/i.test(u.name || "");
  for (const r of $$("#approve-type input")) r.checked = r.value === (looksLikePlug ? "plug" : "device");
  $("#approve-type-hint").textContent = looksLikePlug ? t("The reported name suggests a wireless plug.") : "";
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
      watchNetJob(t("{0} approved", data.name));
      if (($("#approve-type input:checked") || {}).value === "plug") openPlugFor(data.name, approving.mac, data.ip, true);
    } catch (e) {
      showError(form, e.message);
    }
  });
}

async function ignoreDevice(u) {
  if (!(await confirmDialog(t("Ignore {0}?", u.name || u.mac),
    t("The device gets no address and is no longer shown in the overview. Undo under Tools → Ignored devices."), t("Ignore")))) return;
  try {
    await api("POST", `/api/net/unknown/${u.mac}/ignore`, { comment: u.name || "" });
    toast(t("Ignored"), "success");
    await renderNetwork();
  } catch (e) {
    toast(e.message, "error");
  }
}

// ---- Reservations ----

function renderReservations() {
  const cfg = net.config.config;
  const meta = net.config.meta;
  const devices = net.config.devices || {};
  const plugsByMac = net.config.plugs || {};
  const online = new Set(meta.online);
  const search = $("#res-search").value.trim().toLowerCase();
  const filter = ($("#res-filter input:checked") || {}).value || "all";
  const rows = cfg.reservations.map((r) => ({
    ...r, vendor: meta.vendors[r.mac] || "", seen: meta.last_seen[r.mac] || null, online: online.has(r.mac),
    lease: meta.leases[r.mac] || null, device: devices[r.mac] || null, plug: plugsByMac[r.mac] || null, _cls: r.enabled ? "" : "is-disabled",
  })).filter((r) => {
    if (filter === "online" && !r.online) return false;
    if (filter === "wol" && !r.device) return false;
    if (filter === "disabled" && r.enabled) return false;
    if (!search) return true;
    return [r.name, r.ip, r.mac, r.vendor, r.comment].some((v) => String(v || "").toLowerCase().includes(search));
  });
  const domain = cfg.dns.domain;
  $("#res-table").replaceChildren(dataTable("res", [
    { key: "name", label: t("Name"), sort: (r) => r.name || "~", render: (r) => el("div", { class: "cell-name" },
      el("span", { class: `dot small ${r.online ? "online" : ""}`, title: r.online ? t("in the network") : t("not seen") }),
      el("div", {}, el("strong", {}, r.name || el("span", { class: "muted" }, t("no name"))),
        r.comment ? el("div", { class: "muted small" }, r.comment) : null,
        !r.enabled ? el("span", { class: "tag" }, t("inactive")) : null)) },
    { key: "ip", label: "IP", sort: (r) => ipKey(r.ip), render: (r) => el("span", { class: "mono" }, r.ip) },
    { key: "mac", label: "MAC", cls: "hide-sm", sort: (r) => r.mac, render: (r) => el("span", { class: "mono small" }, r.mac) },
    { key: "vendor", label: t("Vendor"), cls: "hide-md", sort: (r) => r.vendor || "~", render: (r) => el("span", { class: "small" }, tr(r.vendor) || "–") },
    { key: "seen", label: t("Last seen"), cls: "hide-sm", sort: (r) => -(r.online ? Infinity : r.seen || 0), render: (r) => el("span", { class: "small" }, r.online ? t("now") : fmtAgo(r.seen)) },
    { key: "actions", label: "", cls: "actions", render: (r) => el("div", { class: "row-actions" },
      r.plug ? el("button", { type: "button", class: "icon-btn plug-link", title: t("Wireless plug {0}", r.plug.name), "aria-label": t("Wireless plug {0}", r.plug.name),
        onclick: () => setView("plugs") }, icon("plug"))
        : r.device ? el("button", { type: "button", class: "icon-btn wake-btn", title: t("Wake {0}", r.device.name), "aria-label": t("Wake {0}", r.device.name),
          onclick: (e) => deviceAction(r.device, "wake", e.currentTarget) }, icon("power"))
          : [wolDeviceButton(r.name, r.mac, r.ip), plugAddButton(r.name, r.mac, r.ip)],
      el("button", { type: "button", class: "icon-btn", title: t("Edit"), "aria-label": t("Edit {0}", r.name || r.mac), onclick: () => openResDialog(r) }, icon("edit"))) },
  ], rows, search || filter !== "all" ? t("No matching reservations.") : t("No reservations. Add one with \"Reservation\" or import under Tools.")));
  $("#res-foot").textContent = t("{0} of {1} · reachable as name.{2}", rows.length, cfg.reservations.length, domain);
}

// "Add as WOL device": prefills the device dialog from a reservation or lease (host: name.domain or IP)
function wolDeviceButton(name, mac, ip, named = Boolean(name)) {
  const label = t("Add as WOL device");
  const host = named && name && net.config ? `${name}.${net.config.config.dns.domain}` : ip;
  return el("button", {
    type: "button", class: "btn small wol-add", title: label, "aria-label": `${label}: ${name || mac}`,
    onclick: () => openDeviceDialog(null, { name: name || "", mac: mac.toUpperCase(), host }),
  }, icon("plus"), el("span", { class: "wol-add-text" }, label));
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
  $("#res-dialog-title").textContent = r ? t("Edit reservation") : t("New reservation");
  $("#res-delete").hidden = !r;
  $("#res-submit").textContent = netSubmitLabel();
  updateResVendor();
  $("#res-dialog").showModal();
}

function updateResVendor() {
  const mac = normMac($("#res-form").elements.mac.value);
  const vendor = mac && net.config ? net.config.meta.vendors[mac] : "";
  const leased = mac && net.config ? net.config.meta.leases[mac] : "";
  $("#res-vendor").textContent = [tr(vendor), leased && t("current lease: {0}", leased)].filter(Boolean).join(" · ");
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
      await afterNetChange(res, t("Reservation {0} applied", data.name || data.mac));
      if ($("#device-dialog").open) updateDeviceNetHint();
    } catch (e) {
      showError(form, e.message);
    }
  });
}

async function onResDelete() {
  const r = editingRes;
  if (!(await confirmDialog(t("Delete {0}?", r.name || r.mac), t("The reservation for {0} will be removed. In \"known devices only\" mode the device gets no address without a reservation.", r.ip), t("Delete"), true))) return;
  try {
    await netIdle();
    const res = await api("DELETE", `/api/net/reservations/${r.id}`);
    $("#res-dialog").close();
    await afterNetChange(res, t("Reservation {0} deleted", r.name || r.mac));
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
    { key: "name", label: t("Name"), sort: (l) => l.name || "~", render: (l) => el("div", {},
      el("strong", {}, l.name || el("span", { class: "muted" }, t("no name"))),
      l.hostname && l.hostname !== l.name ? el("div", { class: "muted small" }, t("reports itself as {0}", l.hostname)) : null) },
    { key: "ip", label: "IP", sort: (l) => ipKey(l.ip), render: (l) => el("span", { class: "mono" }, l.ip) },
    { key: "mac", label: "MAC", cls: "hide-sm", sort: (l) => l.mac, render: (l) => el("span", { class: "mono small" }, l.mac) },
    { key: "vendor", label: t("Vendor"), cls: "hide-md", sort: (l) => l.vendor || "~", render: (l) => el("span", { class: "small" }, tr(l.vendor) || "–") },
    { key: "expires", label: t("Valid"), sort: (l) => l.expires || Infinity, render: (l) => el("span", { class: "small nowrap" }, fmtIn(l.expires)) },
    { key: "kind", label: t("Type"), cls: "hide-sm", sort: (l) => (l.reservation ? 0 : 1), render: (l) => el("span", { class: `tag ${l.reservation ? "current" : ""}` }, l.reservation ? t("reserved") : t("dynamic")) },
    { key: "actions", label: "", cls: "actions", render: (l) => el("div", { class: "row-actions" },
      !l.reservation ? el("button", { type: "button", class: "btn small ghost", onclick: () => openResDialog(null, { mac: l.mac, ip: l.ip, name: l.hostname }) }, t("Reserve")) : null,
      !l.device ? wolDeviceButton(l.name || l.hostname, l.mac, l.ip, Boolean(l.name)) : null,
      el("button", { type: "button", class: "icon-btn", title: t("Release lease"), "aria-label": t("Release lease of {0}", l.ip), onclick: () => releaseLease(l) }, icon("x"))) },
  ], rows, search ? t("No matching leases.") : t("No active leases.")));
  $("#net-count-leases").textContent = net.leases.leases.length;
}

async function releaseLease(l) {
  if (!(await confirmDialog(t("Release lease of {0}?", l.ip), t("The lease will be deleted. At the next renewal the device gets a new address, or the reserved one if it has a reservation."), t("Release"), true))) return;
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
  $("#rec-hint").textContent = t("Reservations are reachable as name.{0} automatically and need no record here.", domain);
  const recValue = (r) => r.type === "mx" ? t("{0} (priority {1})", r.value, r.priority)
    : r.type === "srv" ? t("{0}:{1} (prio {2}, weight {3})", r.value, r.port, r.priority, r.weight) : r.value;
  $("#rec-table").replaceChildren(dataTable("rec", [
    { key: "type", label: t("Type"), sort: (r) => r.type, render: (r) => el("span", { class: "tag" }, REC_TYPES[r.type]) },
    { key: "name", label: t("Name"), sort: (r) => r.name, render: (r) => el("div", {},
      el("span", { class: "mono" }, r.name ? `${r.name}.${domain}` : domain),
      r.comment ? el("div", { class: "muted small" }, r.comment) : null) },
    { key: "value", label: t("Target / value"), sort: (r) => r.value, render: (r) => el("span", { class: "mono small" }, recValue(r)) },
    { key: "actions", label: "", cls: "actions", render: (r) => el("button", { type: "button", class: "icon-btn", title: t("Edit"), "aria-label": t("Edit record"), onclick: () => openRecDialog(r) }, icon("edit")) },
  ], cfg.records, t("No custom records.")));

  const querylogOn = net.status.dns_enabled && cfg.dns.log_queries;
  $("#dns-querylog-hint").textContent = querylogOn ? t("The latest queries to the DNS server.")
    : t("Query log switched off (Settings → DNS).");
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
      box.replaceChildren(el("p", { class: "muted small" }, tr(s.error) || t("DNS is off.")));
      return;
    }
    const total = (s.hits || 0) + (s.misses || 0);
    const rows = [
      [t("Hit rate"), total ? t("{0} % ({1} of {2})", Math.round((s.hits / total) * 100), s.hits, total) : "–"],
      [t("Cache size"), s.cachesize],
      [t("Inserted / evicted"), `${s.insertions} / ${s.evictions}`],
      ...s.servers.map((sv) => [t("Upstream {0}", sv.server), sv.failed ? t("{0} queries, {1} failed", sv.queries, sv.failed) : t("{0} queries", sv.queries)]),
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
      { key: "ts", label: t("Date & time"), render: (q) => el("span", { class: "mono small" }, fmtDateTime(q.ts)) },
      { key: "client", label: t("From"), render: (q) => el("span", { class: "mono small" }, q.client) },
      { key: "type", label: t("Type"), cls: "hide-sm", render: (q) => el("span", { class: "tag" }, q.type) },
      { key: "name", label: t("Name"), render: (q) => el("span", { class: "mono small break" }, q.name) },
    ], [...entries].reverse().slice(0, 100), t("No queries logged.")));
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
        el("div", { class: "dns-result-head" }, el("span", { class: `tag ${res.rcode === 0 ? "current" : ""}` }, tr(res.status)),
          el("span", { class: "muted small" }, `${res.query} · ${res.qtype} · ${res.ms} ms${res.authoritative ? ` · ${t("local")}` : ""}`)),
        res.answers.length ? el("ul", {}, res.answers.map((a) => el("li", {}, el("span", { class: "tag" }, a.type), el("span", { class: "mono" }, a.data))))
          : el("p", { class: "muted small" }, t("No answer records.")));
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
  $("#rec-dialog-title").textContent = r ? t("Edit record") : t("New record");
  $("#rec-delete").hidden = !r;
  $("#rec-submit").textContent = netSubmitLabel();
  $("#rec-dialog").showModal();
}

function setRecType(type, adjust = true) {
  const domain = net.config ? net.config.config.dns.domain : t("domain");
  // Usual defaults when changing the type of a new record: MX 10, SRV 0
  if (adjust && !editingRec) $("#rec-form").elements.priority.value = type === "mx" ? 10 : 0;
  const labels = {
    host: [t("Name"), t("IP address"), t("e.g. nas → nas.{0}", domain), t("IPv4 or IPv6")],
    alias: [t("Alias"), t("points to"), t("e.g. printer → printer.{0}", domain), t("an existing local name")],
    mx: [t("For (empty = domain)"), t("Mail server"), t("empty = {0}", domain), t("Name of the mail server")],
    srv: [t("Service"), t("Target host"), t("e.g. _ipp._tcp"), t("Name of the host that offers the service")],
    txt: [t("Name"), t("Text"), t("e.g. info → info.{0}", domain), t("without quotation marks")],
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
      await afterNetChange(res, t("DNS record applied"));
    } catch (e) {
      showError(form, e.message);
    }
  });
}

async function onRecDelete() {
  if (!(await confirmDialog(t("Delete record?"), t("The DNS record will be removed."), t("Delete"), true))) return;
  try {
    await netIdle();
    const res = await api("DELETE", `/api/net/records/${editingRec.id}`);
    $("#rec-dialog").close();
    await afterNetChange(res, t("DNS record deleted"));
  } catch (e) {
    toast(e.message, "error");
  }
}

// ---- Settings ----

let getSettingsLease = null;

function conditionalRow(item = { domain: "", server: "" }) {
  const row = el("div", { class: "editor-row" },
    el("input", { class: "mono", placeholder: "vpn.example.org", value: item.domain, "data-k": "domain", "aria-label": t("Domain") }),
    el("span", { class: "muted" }, "→"),
    el("input", { class: "mono", placeholder: "10.8.0.1", value: item.server, "data-k": "server", "aria-label": t("DNS server") }),
    el("button", { type: "button", class: "icon-btn", title: t("Remove"), "aria-label": t("Remove forwarding"), onclick: () => row.remove() }, icon("x")));
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
  $("#prefix-hint").textContent = net.config.meta.network ? t("Network {0}", net.config.meta.network) : prefix === 24 ? "/24 = 255.255.255.0" : "";
  $("#settings-submit").textContent = netHasDraft() ? t("Add to draft") : t("Review");
  $("#settings-foot").textContent = netHasDraft() ? t("A draft is open. Changes go into the draft.")
    : t("\"Review\" shows all changes before they take effect.");
  renderStaticIp();
}

function updateModeHint() {
  const form = $("#net-settings-form");
  const mode = (form.querySelector("input[name='dhcp.mode']:checked") || {}).value;
  for (const block of $$("[data-mode]", form)) block.hidden = block.dataset.mode !== mode;
  $("#mode-hint").textContent = mode === "known-only"
    ? t("Only devices with a reservation get an address. Unknown devices appear in the overview for approval.")
    : t("Unknown devices get an address from the range. Reserved addresses lie outside the range.");
}

function renderStaticIp() {
  const st = net.status.server;
  const box = $("#static-ip");
  const fixed = st.method === "manual";
  box.replaceChildren(
    el("div", { class: "static-ip-text" },
      el("strong", {}, fixed ? t("Static address") : st.method === "auto" ? t("Address via DHCP") : t("Address unknown")),
      el("span", { class: "muted small" }, " · ", t("{0} on {1}", st.address || "–", st.interface), st.connection ? ` (${st.connection})` : ""),
      el("div", { class: "muted small" }, t("A DHCP server needs a static address of its own."))),
    st.connection ? el("button", { type: "button", class: "btn small", onclick: () => toggleStaticIp(!fixed) },
      fixed ? t("Back to DHCP") : t("Set static address")) : null);
}

async function toggleStaticIp(enable) {
  const ip = net.config.config.server.ip || net.status.server.address;
  const text = enable
    ? t("The Wi-Fi profile gets the static address {0} with gateway {1}. The connection stays up.", ip, net.config.config.dhcp.gateway || "?")
    : t("The WOL-Server gets its address via DHCP. With DHCP enabled on the WOL-Server itself, it then gets no address.");
  if (!(await confirmDialog(enable ? t("Set static address?") : t("Back to DHCP?"), text, enable ? t("Set") : t("Switch"), !enable))) return;
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
      !(await confirmDialog(t("Without a static address?"), t("The WOL-Server gets its own address via DHCP. A DHCP server needs a static address (Settings → Server → \"Set static address\")."), t("Save anyway")))) return;
  await busy($("#settings-submit"), async () => {
    try {
      const preview = await api("POST", "/api/net/settings/preview", data);
      if (!preview.changes.length) return toast(t("No changes"), "success");
      if (!preview.draft && !(await confirmDialog(t("Apply changes?"), preview.changes, t("Apply")))) return;
      await netIdle();
      const res = await api("PUT", "/api/net/settings", data);
      await afterNetChange(res, t("Settings applied"));
    } catch (e) {
      showError(form, e.message);
      form.querySelector(".form-error").scrollIntoView({ block: "center", behavior: "smooth" });
    }
  });
}

// ---- Tools ----

async function renderNetTools(load) {
  const st = net.status;
  $("#service-info").replaceChildren(...[
    ["dnsmasq", st.service.state === "active" ? t("running") : st.service.state],
    [t("At boot"), st.service.enabled ? t("enabled") : t("disabled")],
    [t("Last job"), st.job ? `${tr(st.job.message)} (${fmtAgo(st.job.ts)})` : "–"],
  ].map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v))));
  renderProbe(st.probe);
  const cfg = net.config.config;
  $("#ignored-list").replaceChildren(...(cfg.ignored.length ? cfg.ignored.map((i) => el("div", { class: "saved-item" },
    el("span", {}, el("span", { class: "mono" }, i.mac), i.comment ? el("span", { class: "muted" }, ` · ${i.comment}`) : null),
    el("span", { class: "muted small" }, fmtAgo(i.ts)),
    el("button", { type: "button", class: "btn small ghost", onclick: () => unignore(i.mac) }, t("Stop ignoring"))))
    : [el("p", { class: "muted small" }, t("None."))]));
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
  box.replaceChildren(el("p", { class: "muted" }, t("Last searched {0}:", fmtAgo(probe.ts))),
    items.length ? el("ul", { class: "change-list" }, items.map((s) => {
      const ip = s.server_id || s.source;
      return el("li", {}, el("span", { class: "mono" }, ip), " ", ip === own ? t("(this server)") : el("strong", { class: "warn-text" }, t("– other DHCP server")),
        s.offered ? el("span", { class: "muted" }, " ", t("offered {0}", s.offered)) : null);
    })) : el("p", {}, probe.ok ? t("No DHCP server answered.") : t("Error: {0}", tr(probe.error))));
}

async function loadHistory() {
  const box = $("#history-list");
  try {
    const { history } = await api("GET", "/api/net/history");
    box.replaceChildren(...(history.length ? history.map((h, i) => el("details", { class: "history-item", open: i === 0 },
      el("summary", {},
        el("span", { class: "mono small" }, fmtDateTime(h.ts)),
        el("span", {}, h.changes.length ? tr(h.changes[0]) + (h.changes.length > 1 ? ` (+${h.changes.length - 1})` : "") : t("no change")),
        i === 0 ? el("span", { class: "tag current" }, t("active")) : el("button", { type: "button", class: "btn small ghost", onclick: (e) => { e.preventDefault(); restoreHistory(h); } }, icon("undo"), t("Restore"))),
      el("p", { class: "muted small" }, h.client ? t("by {0}", h.client) : ""),
      el("ul", { class: "change-list" }, h.changes.map((c) => el("li", {}, tr(c))))))
      : [el("p", { class: "muted small" }, t("No history yet."))]));
  } catch (e) {
    box.replaceChildren(el("p", { class: "muted small" }, e.message));
  }
}

async function restoreHistory(h) {
  if (!(await confirmDialog(t("Restore this configuration?"), t("The configuration from {0} is loaded as a draft and takes effect with \"Apply\".", fmtDateTime(h.ts)), t("Load as draft")))) return;
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
        { key: "ts", label: t("Date & time"), render: (e) => el("span", { class: "mono small" }, fmtDateTime(e.ts)) },
        { key: "type", label: t("Message"), render: (e) => el("span", { class: "tag" }, e.type) },
        { key: "detail", label: t("Details"), render: (e) => el("span", { class: "mono small break" }, e.detail) },
      ], rows, t("No DHCP messages."))
      : dataTable("log", [
        { key: "ts", label: t("Date & time"), render: (e) => el("span", { class: "mono small" }, fmtDateTime(e.ts)) },
        { key: "text", label: t("Message"), render: (e) => el("span", { class: "mono small break" }, e.text) },
      ], rows, t("No messages.")));
  } catch (e) {
    box.replaceChildren(el("p", { class: "muted small" }, e.message));
  }
}

async function unignore(mac) {
  try {
    await api("DELETE", `/api/net/ignored/${mac}`);
    toast(t("Shown again"), "success");
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
    const a = el("a", { href: URL.createObjectURL(blob), download: `wol-network-${new Date().toISOString().slice(0, 10)}.json` });
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
    toast(t("Not a valid backup (JSON expected)"), "error");
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
  if (file.size > 200 * 1024) return showError(form, t("The file is too large."));
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
  const labels = { network: t("Subnet"), gateway: t("Gateway"), lease_time: t("Lease time"), mode: t("Mode"), domain: t("Domain (old)"), ntp: "NTP", dns: t("DNS (old)") };
  $("#import-settings").replaceChildren(...Object.entries(labels).filter(([k]) => st[k] != null).map(([k, label]) => {
    let value = st[k];
    if (k === "lease_time") value = fmtLease(value);
    if (k === "mode") value = value === "known-only" ? t("known devices only") : t("with address range");
    if (Array.isArray(value)) value = value.join(", ");
    return el("div", {}, el("dt", {}, label), el("dd", {}, value));
  }));
  $("#import-warnings").replaceChildren(...p.warnings.map((w) => el("li", { class: "notice warn" }, icon("alert"), el("span", {}, tr(w)))));
  const ok = p.reservations.filter((r) => !r.problems.length).length;
  $("#import-summary").textContent = t("{0} fixed assignments found: {1} will be imported, {2} skipped.", p.reservations.length, ok, p.reservations.length - ok);
  $("#import-table").replaceChildren(dataTable("import", [
    { key: "name", label: t("Name"), render: (r) => el("div", {}, el("strong", {}, r.name || "–"),
      r.original_name !== r.name ? el("div", { class: "muted small" }, t("was \"{0}\"", r.original_name)) : null) },
    { key: "ip", label: "IP", render: (r) => el("span", { class: "mono" }, r.ip || "–") },
    { key: "mac", label: "MAC", cls: "hide-sm", render: (r) => el("span", { class: "mono small" }, r.mac || "–") },
    { key: "state", label: "", render: (r) => r.problems.length
      ? el("span", { class: "import-skip" }, icon("x"), r.problems.map(tr).join("; "))
      : el("span", { class: "import-ok" }, icon("check"), tr(r.exists) || t("new")) },
  ], p.reservations.map((r) => ({ ...r, _cls: r.problems.length ? "is-disabled" : "" })), t("No fixed assignments found.")));
  $("#import-preview").hidden = false;
  $("#import-submit").disabled = !ok && !Object.keys(st).length;
}

async function onImportSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  showError(form, "");
  if (form.elements.replace.checked && !(await confirmDialog(t("Remove existing reservations?"),
    t("The imported reservations replace all existing ones (in the draft)."), t("Replace"), true))) return;
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
  $("#restart-btn").addEventListener("click", (e) => runNetTask(e.currentTarget, "/api/net/restart", t("dnsmasq restarted")));
  for (const r of $$("#log-kind input")) r.addEventListener("change", loadNetLog);
  $("#device-form").elements.mac.addEventListener("change", updateDeviceNetHint);
}

// ---------- Start-up ----------

async function updateSession() {
  const session = await api("GET", "/api/session");
  state.authRequired = session.auth_required;
  state.hotspot = session.hotspot;
  $("#logout").hidden = !session.auth_required;
  const banner = $("#banner");
  if (session.hotspot) {
    banner.hidden = false;
    banner.replaceChildren(
      el("span", {}, el("strong", {}, t("Setup mode:")), " ", t("The WOL-Server is not connected to any Wi-Fi.")),
      el("button", { class: "btn small", type: "button", onclick: () => { setView("system"); openWifiDialog(); } }, t("Set up Wi-Fi")));
    return session;
  }
  banner.hidden = session.auth_required;
  banner.replaceChildren(
    el("span", {}, el("strong", {}, t("No password set.")), " ", t("Anyone in the local network can control devices.")),
    el("button", { class: "btn small", type: "button", onclick: () => setView("system") }, t("Set password")));
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
    // Opened via the setup hotspot (captive portal): go straight to the Wi-Fi selection
    setView("system");
    await renderWifi().catch(() => {});
    if (state.wifi) openWifiDialog();
    return;
  }
  const [fromHash, sub] = location.hash.slice(1).split("/");
  if (fromHash === "network" && NET_SUBS.includes(sub)) net.sub = sub;
  if (fromHash === "plugs" && PLUG_SUBS.includes(sub)) plugView.sub = sub;
  setView(VIEWS.includes(fromHash) ? fromHash : "devices");
  refreshNetBadge();
}

// Small language switcher (EN | DE) in the header and on the login page; the choice is remembered.
function renderLangSwitch() {
  for (const box of $$("[data-lang-switch]")) {
    box.replaceChildren(...Object.entries(I18N.langs).map(([code, name]) => el("button", {
      type: "button", class: code === I18N.lang ? "active" : "", lang: code, title: name,
      "aria-pressed": String(code === I18N.lang), onclick: () => code !== I18N.lang && I18N.setLang(code),
    }, code.toUpperCase())));
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", t("Language"));
  }
}

function bind() {
  renderLangSwitch();
  $("#login-form").addEventListener("submit", onLogin);
  $("#logout").addEventListener("click", onLogout);
  for (const tab of $$(".tab")) {
    // Icons for the menu bar; on narrow screens the labels shrink or disappear, the name stays for screen readers
    const label = $(".tab-label", tab).textContent;
    tab.prepend(icon(tab.dataset.icon));
    tab.setAttribute("aria-label", label);
    tab.title = label;
    tab.addEventListener("click", () => setView(tab.dataset.view));
  }
  $("#add-device").addEventListener("click", () => openDeviceDialog());
  $("#add-schedule").addEventListener("click", () => openScheduleDialog());
  $("#clear-events").addEventListener("click", onClearEvents);
  $("#device-form").addEventListener("submit", onDeviceSubmit);
  $("#delete-device").addEventListener("click", onDeviceDelete);
  $("#sleep-method").addEventListener("change", (e) => setSleepMethod(e.target.value));
  $("#device-form").elements.hold.addEventListener("change", (e) => { $("#hold-command-field").hidden = !e.target.checked; });
  for (const btn of $$("[data-cmd]")) {
    btn.addEventListener("click", () => { $("#device-form").elements.ssh_command.value = btn.dataset.cmd; });
  }
  $("#schedule-form").addEventListener("submit", onScheduleSubmit);
  $("#schedule-form").elements.target.addEventListener("change", () => renderScheduleActions());
  $("#delete-schedule").addEventListener("click", onScheduleDelete);
  for (const btn of $$("[data-days]")) {
    btn.addEventListener("click", () => setDays(btn.dataset.days.split(",").map(Number)));
  }
  for (const btn of $$("[data-close]")) btn.addEventListener("click", () => btn.closest("dialog").close());
  $("#copy-key").addEventListener("click", copyKey);
  $("#token-form").addEventListener("submit", onTokenSubmit);
  $("#token-copy").addEventListener("click", copyToken);
  $("#password-form").addEventListener("submit", onPasswordSubmit);
  $("#wifi-add").addEventListener("click", openWifiDialog);
  $("#wifi-rescan").addEventListener("click", scanWifi);
  $("#wifi-form").addEventListener("submit", onWifiSubmit);
  $("#wifi-form").elements.show_password.addEventListener("change", (e) => {
    $("#wifi-form").elements.password.type = e.target.checked ? "text" : "password";
  });
  $("#wifi-form").elements.static_ip.addEventListener("change", (e) => {
    $("#wifi-static").hidden = !e.target.checked;
    if (e.target.checked) $("#wifi-form").elements.ip_address.focus();
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
  $("#add-plug").addEventListener("click", () => openPlugFor("", "", ""));
  $("#plug-form").addEventListener("submit", onPlugSubmit);
  $("#plug-detect").addEventListener("click", detectPlug);
  $("#plug-delete").addEventListener("click", onPlugDelete);
  $("#plug-secure").addEventListener("click", onPlugSecure);
  $("#plug-restart").addEventListener("click", onPlugRestart);
  $("#plug-consumer").addEventListener("change", (e) => {
    updatePlugConsumer();
    if (e.target.value === CONSUMER_OTHER) $("#plug-consumer-label").focus();
  });
  $("#device-plug").addEventListener("change", updateDevicePlug);
  for (const radio of $$("#device-form input[name=wake_method]")) radio.addEventListener("change", updateDevicePlug);
  for (const tab of $$("#plug-subtabs .subtab")) tab.addEventListener("click", () => setPlugSub(tab.dataset.sub));
  $("#energy-period").addEventListener("change", (e) => {
    plugView.period = e.target.value;
    renderEnergy().catch((err) => toast(err.message, "error"));
  });
  $("#energy-prev").addEventListener("click", () => shiftPeriod(-1));
  $("#energy-next").addEventListener("click", () => shiftPeriod(1));
  $("#energy-today").addEventListener("click", () => {
    plugView.date = isoDate(new Date());
    renderEnergy().catch((e) => toast(e.message, "error"));
  });
  $("#energy-plug").addEventListener("change", (e) => {
    plugView.plug = e.target.value;
    drawEnergy();
  });
  $("#energy-csv").addEventListener("click", exportEnergyCsv);
  $("#energy-price-form").addEventListener("submit", onPriceSubmit);
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => state.view === "plugs" && plugView.sub === "energy" && drawEnergy(), 150);
  });
  // Close open menus (Power on the device cards) on a click elsewhere
  document.addEventListener("click", (e) => {
    for (const menu of $$("details.menu[open]")) if (!menu.contains(e.target)) menu.open = false;
  });
  buildDayPicker();
  setInterval(() => {
    if (document.visibilityState !== "visible" || $("#app").hidden || state.view !== "plugs") return;
    if (plugView.sub === "overview" && !$("#plug-dialog").open) refreshPlugs();
    // the live hour: not while a tooltip is open, it would jump away under the pointer
    if (plugView.sub === "energy" && plugView.period === "hour" && !$("#energy-chart .chart-tip:not([hidden])")) renderEnergy(true).catch(() => {});
  }, PLUG_INTERVAL);
  setInterval(() => {
    if (document.visibilityState !== "visible" || $("#app").hidden || state.view !== "plugs") return;
    if (plugView.sub === "energy" && plugView.period !== "hour" && isCurrentPeriod()) renderEnergy().catch(() => {});
  }, ENERGY_INTERVAL);

  // Poll the status only while the page is visible; this saves load on the Pi.
  setInterval(() => {
    if (document.visibilityState !== "visible" || $("#app").hidden) return;
    if (state.view === "devices" || plugsNeedStatus()) refreshStatus();
    // Show new entries (e.g. from schedules) while the log is open
    if (state.view === "events") renderEvents().catch(() => {});
    // Network: refresh the overview (unknown devices); watchNetJob() watches running jobs
    if (state.view === "network" && net.sub === "overview" && !net.watching) {
      loadNetStatus().then(() => { renderNetHeader(); renderNetOverview(); }).catch(() => {});
    }
    // Refresh the Wi-Fi status only while something is happening (hotspot, connecting)
    if (state.view === "system" && state.wifi && (state.wifi.mode !== "client" || state.wifi.pending)) {
      renderWifi().catch(() => {});
    }
    // Refresh the VPN status only while setting up or connecting
    if (state.view === "system" && vpnBusy(state.vpn)) renderVpn().catch(() => {});
  }, STATUS_INTERVAL);
  // Bookmarks and back/forward in the browser: #network/leases etc.
  window.addEventListener("hashchange", () => {
    if ($("#app").hidden) return;
    const [view, sub] = location.hash.slice(1).split("/");
    if (view === "network" && NET_SUBS.includes(sub) && state.view === "network") {
      if (sub !== net.sub) setNetSub(sub, false);
    } else if (view === "plugs" && PLUG_SUBS.includes(sub) && state.view === "plugs") {
      if (sub !== plugView.sub) setPlugSub(sub, false);
    } else if (VIEWS.includes(view) && view !== state.view) {
      if (view === "network" && NET_SUBS.includes(sub)) net.sub = sub;
      if (view === "plugs" && PLUG_SUBS.includes(sub)) plugView.sub = sub;
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
    $("#boot").textContent = t("Error: {0}", e.message);
  });
});
