# Proposal: smart plugs in WOL-Server

As of: 2026-10-27 · Status: **draft, not implemented**

WOL-Server learns to use metering smart plugs. A plug is a device in the network with the type "wireless plug". Linking
it to a consumer tells WOL-Server what the plug measures and what it switches. If the consumer is a WOL device, the
device gets three new things:

- **Better status:** the power draw shows "off / standby / running", and also "hangs" (draws power but does not
  answer). A ping alone only knows "answers / does not answer".
- **Power as a switch:**
  - power on as a wake method for devices without WOL;
  - zero standby after a shutdown;
  - a hard power cycle as the last resort for a device that hangs.
- **Energy tracking:** consumption per day, month and year, optionally with costs.

The consumer can also be a network device without WOL, or just a name such as "Fridge".

## 1. Starting point

- Hardware: **Shelly Plug M Gen3** (13 A, Schuko, 10,000 switching cycles, internal temperature sensor). Four are
  ordered.
- All Shelly devices of generations 2 to 4 share the same local API (`/rpc/...`). It works without the cloud and
  without an account. Setup goes through the plug's own hotspot at `192.168.33.1`.
- Setup works like the first setup of WOL-Server: the plug opens a hotspot, you choose the Wi-Fi and enter the
  password, DHCP stays on. The plug then asks the WOL-Server DHCP for an address and appears in the GUI (section 4).

## 2. Decisions so far

| Question | Decision |
|---|---|
| Plug type | **Shelly Gen2+ with the original firmware** as the first driver. It is calibrated at the factory, local and documented. Tasmota (re-flashed) would lose the calibration of the power metering. |
| Other vendors | **Driver layer** (section 6): the core knows only a generic plug interface; each vendor is one driver. Requirement for a driver: a local API, no cloud. |
| Cloud | Off. WOL-Server talks to the plug directly over HTTP in the LAN. |
| Zero-cross switching | Not available on the plug and impossible from outside (Wi-Fi latency is longer than a half-wave). Not needed. The relay is protected by the rules in section 3 instead. |
| Welded contacts | No positively driven contacts in a plug. Detection by measurement instead (section 3). |
| Plug in the network | **A type at the reservation:** "wireless plug" next to the existing "WOL device". Name and fixed IP live in the reservation only, never on the plug. |
| Plugs behind a hub | A DECT plug (or later Zigbee) has no IP of its own. Then the **hub** is reserved (e.g. the FRITZ!Box); its plugs are added in the Plugs tab through the hub (driver + host of the hub + channel = device ID). Type, link and safety rules are the same. |
| Detection | **By asking the device, never by vendor prefix alone.** An Espressif MAC can be a Shelly, a Tasmota plug or any home-made ESP project. A driver's `probe` decides; the result is only a suggestion and can always be changed, including back to "normal device". |
| Linking | **Plug → consumer:** a WOL device, a reservation (network device without WOL) or just a name. One plug has at most one consumer; a consumer has at most one plug. |

## 3. Safety rules

These rules are enforced in WOL-Server itself, not only in the GUI.

1. **Power off only when the device is already off.** "Power off" is allowed without asking if the measured
   draw is at or below the device's standby threshold. Above it, the GUI shows the current draw and asks for an
   explicit confirmation ("The device is drawing 85 W. Cut the power anyway?"). The script API cannot cut the
   power of a running device.
2. **Cut standby only after a real shutdown.** The option "cut power after sleep" is only offered when the user
   confirms that the sleep command **shuts the device down**. A device in suspend (S3) keeps its RAM powered and
   draws about as much as one that is switched off. Cutting it loses unsaved data. WOL-Server cannot tell the two
   apart by power alone, so the user has to state it.
3. **Hard power cycle runs on the plug.** "Off, wait 10 s, on" is a single command with `toggle_after`. The plug
   switches back on by itself, even if the Wi-Fi or WOL-Server fails in between. A device is never left without
   power by accident.
4. **Welded contacts:** relay "off", but the measured draw stays above ~1 W for more than 30 s, so the alarm
   "Contacts may be welded" fires. The check runs all the time, not only right after switching off. A device with
   WOL draws standby power all the time, so this works reliably there.
5. **Temperature:** the plug's temperature and its own error flags (`overtemp`, `overpower`, `overvoltage`,
   `undervoltage`) are read along with the power. WOL-Server warns:
   - on any error flag;
   - above a fixed limit;
   - on a slow rise over days at the same load, which is a sign of a worn contact.
6. **No lock-out:** WOL-Server refuses to cut the power of a plug whose linked device is the WOL-Server itself
   or the gateway (router). The README warns against plugging the Pi or the router into a plug it controls.
7. **Relay wear:** the plug counts its switching cycles (`counts.switch_on`). The GUI shows "312 of 10,000
   cycles" and warns at 80 %.

## 4. Features

### Phase 1: plugs

- **Setup, step by step:**
  1. **On the plug's hotspot:** choose the Wi-Fi, enter the password, leave DHCP on.
  2. **Network → Overview → Unknown devices:** the plug appears with the name it reports. WOL-Server asks the
     drivers once, read-only (for Shelly: `GET /shelly`), and shows the result: "Shelly Plug M Gen3 detected".
  3. **Approve:** IP and name as for every reservation, plus the **type**. "wireless plug" with the detected driver is
     preselected; "normal device" or another driver can be chosen instead. Then WOL-Server asks for the plug's
     password.
- **Later changes:** the type of a reservation can be changed at any time. An existing reservation gets an
  *As plug* button next to *As WOL device*. "Do not detect" for a MAC stops all probing of that device, e.g. for a
  home-made ESP project that should not get unexpected HTTP requests.
- **Manual:** a plug outside the DHCP (e.g. a fixed IP) can be entered by host name or IP, with the driver chosen
  by hand.
- **Password:** digest authentication as the plug requires (user `admin`, SHA-256). The password is stored on the
  server only and is never sent back to the browser.
- **Live view:** relay state, power (W), voltage, current, temperature, energy counter, switching cycles, error
  flags.
- **Switch:** on, off and power cycle, with the rules from section 3.
- **Plug settings** that WOL-Server sets once and then checks:
  - state after a power outage (`initial_state`), recommended "restore last state";
  - name.

### Phase 2: plug and device together

- **Link:** from both sides, in the device dialog (choose a plug) and in the Plugs tab (choose a consumer). Both
  edit the same link. The thresholds are suggested from the current measurement ("Measured now: 1.3 W, use as
  standby?").
- **What the consumer gets:**

  | Consumer | Functions |
  |---|---|
  | WOL device | everything: status from power, wake methods, power cut after sleep, power cycle, safety rules |
  | Reservation (network device without WOL) | consumption, switching, status from ping and power |
  | Name only | consumption and switching |

- **Safety rules belong to the consumer, not to the button.** Switching a plug in the Plugs tab runs the same
  checks as the device card.
- **Status from power and ping:**

  | Ping | Power | Status |
  |---|---|---|
  | answers | any | **Online** |
  | no answer | above the running threshold for more than 5 min | **Hangs?** (offers a power cycle) |
  | no answer | between standby and running | **Starting / shutting down** |
  | no answer | at or below standby | **Standby** (WOL possible) |
  | no answer | relay off | **No power** |

- **Wake methods:** WOL (as before), **power on** (for devices without WOL whose BIOS starts on power return, and
  every Raspberry Pi), or **power on, then WOL**. The last one is needed after the standby was cut: the network
  card has no power until the plug is switched on again.
- **Sleep with power cut:** after the sleep command, wait until the draw falls to standby (at most a set time,
  e.g. 5 min), then switch off the plug. If the draw does not fall, do not switch off; log a warning instead.
- **Script API:** `wake` uses the device's wake method, so `POST /api/devices/<name>/wake` also switches the plug
  on if needed. `GET /api/status` includes the power draw.
- Alerts (welded contacts, temperature, plug unreachable) go to the log and appear as a notice in the GUI.

### Phase 3: energy tracking

- **Values:** the plug counts energy itself (`aenergy.total`, Wh). WOL-Server only reads the counter. A missed
  reading loses nothing, the next one includes the difference.
- **Views:** today as a curve (power over time), days of the month and months of the year as bars, per plug and
  in total.
- **Costs:** optional price per kWh; shows cost per day, month and year, and **what the standby costs per year**.
- **Export** as CSV.

### Later, if wanted

- More drivers: Tasmota (`/cm?cmnd=Status 8`), FRITZ!DECT via the FRITZ!Box, Shelly power strips with several
  outlets, ESPHome (its web server API)
- A **generic HTTP driver** for home-made devices: URLs for on, off and status, plus the JSON paths for power and
  energy, entered in the GUI
- Schedules for plugs without a device (e.g. coffee machine)
- Keep plugs off the internet: a DHCP option without a gateway, but with the local NTP server
- Notifications (e.g. via e-mail or a push service)

## 5. User interface

**Devices tab:** the card of a device with a plug shows the power and the extended status.

```
┌──────────────────────────────────────────────┐
│ ● Office PC                         ⚡ 92 W   │
│   Online                                     │
│   MAC 00:D8:61:D7:1D:36  Host office-pc…     │
│   ⏱ Wake today 07:30                          │
│ [ ⏻ Wake ] [ ☾ Sleep ] [ ⚡ Power ▾ ]          │
└──────────────────────────────────────────────┘
          Power ▾: Power on · Power off · Power cycle (off 10 s, on)
```

**Device dialog,** new section *Plug*:

```
 Plug               [plug-office-pc ▾]    measured now: 1.3 W
 Standby up to      [ 3   ] W      [use measured value]
 Running from       [ 20  ] W
 Wake method        (●) WOL  ( ) Power on  ( ) Power on, then WOL
 [ ] Cut power after sleep  — only if the sleep command shuts the device down
```

**Reservations:** a plug icon in the type column, and *As plug* next to *As WOL device*.

**New Plugs tab** with three sections:

```
 Plugs     Overview | Consumption | Settings

 ┌ Office PC ─────────┐ ┌ NAS ───────────────┐ ┌ Backup server ─────┐
 │ ⚡ 92 W   34 °C     │ │ ⚡ 18 W   29 °C     │ │ ⚡ 1.1 W  standby   │
 │ today   0.61 kWh   │ │ today   0.39 kWh   │ │ today   0.03 kWh   │
 │ month  14.2 kWh    │ │ month  12.8 kWh    │ │ month   1.9 kWh    │
 │ standby/year 4.10 €│ │                    │ │ standby/year 2.90 €│
 └────────────────────┘ └────────────────────┘ └────────────────────┘
```

- *Overview:* every plug with its consumer, live values and a switch.
- *Consumption:* the curve for today and bars for days or months, per plug and in total, with costs.
- *Settings* per plug: consumer, driver, password, state after a power outage, thresholds, switching cycles,
  firmware.

The main navigation then has six entries plus the language switch. On a phone they move into a menu if they do not
fit.

## 6. Technical design

### Processes and privileges

Everything runs in the existing unprivileged `wol-server` service. Talking to the plugs is plain HTTP in the LAN.
It needs no root rights and no new service.

### Driver layer

The plug control is split in two, like a hardware abstraction layer:

- **Drivers** only talk to the hardware and convert its answers into one common form (watts, watt-hours,
  °C, normalized error codes). They know nothing about devices, safety rules or statistics.
- **The core** (plug manager) contains all the logic: polling, safety rules, status, alarms, statistics, counter
  resets. It talks to every plug through the same interface, whatever the vendor.

```
 GUI / API / scheduler
          │
   plug manager  ── safety rules, status, alarms, statistics, energy.db
          │  common interface
   ┌──────┼──────────┬───────────────┬──────────────┐
 Shelly Gen2+    Tasmota      FRITZ!DECT (via     fake plug
 (phase 1)       (later)      FRITZ!Box, later)   (tests, demo)
```

**Interface** (`wolserver/plugs/base.py`):

| Method | Returns / does |
|---|---|
| `probe(host)` | class method: is this a device of this driver? → model, MAC, firmware, password needed |
| `status()` | `PlugStatus`: `on`, `power_w`, `voltage_v`, `current_a`, `energy_wh` (counter of the plug), `temperature_c`, `errors`, `switch_count`; fields the hardware cannot supply are `None` |
| `switch(on)` | switch on or off |
| `cycle(seconds)` | off, then on again after `seconds`, executed **on the device** |
| `settings()` / `configure(power_on_state)` | state after a power outage: `restore`, `on`, `off` |
| `capabilities` | what the hardware can do: `metering`, `energy_counter`, `temperature`, `switch_count`, `device_cycle`, `power_on_state`, `errors` |

- **Addressing:** a plug is `driver + host + channel`. This covers power strips with several outlets (channel
  0–3) and hubs such as a FRITZ!Box with several DECT plugs (channel = device ID).
- **Capabilities instead of assumptions:** the core adapts to what a driver can do.
  - Without `metering` there is no automatic safety check (rule 1), so "power off" for a linked device always asks
    for confirmation, and welded contacts cannot be detected.
  - Without `device_cycle` the core runs off/wait/on itself and the GUI points out that a Wi-Fi drop in between
    leaves the device off.
  - Without `temperature` there is no temperature warning.
- **Errors:** drivers raise `PlugUnreachable` or `PlugAuthError` only. The core turns them into status and alarms.
- **Shared helpers:** HTTP with timeouts and digest authentication (MD5 and SHA-256) in `wolserver/plugs/http.py`,
  so a new driver is usually 100–200 lines.
- **Registry:** each driver registers under a fixed name (`shelly-gen2`, later `tasmota`, `fritz-dect`). The
  plug stores the name. When a new device appears, WOL-Server tries the `probe` of every driver once; the result is
  a suggestion only.
- **Tests:** each driver is tested against recorded answers of the real device (JSON fixtures). The core is tested
  against the fake plug, which also runs in the demo.

### Polling

- **Background:** every 60 s, one status request per plug (timeout 3 s). It supplies power, counter,
  temperature, errors and switching cycles in one answer. With 4 plugs that is 4 small requests per minute.
- **GUI open:** the live values are refreshed every 5 s, from a cache that is at most 5 s old, so several
  browser tabs do not multiply the requests.
- **Load on the Zero:** negligible. Today it has about 300 MB of RAM available and a load of 0.2.

### First driver: Shelly Gen2+

| Purpose | Call |
|---|---|
| Identify (no password) | `GET /shelly` |
| Status | `GET /rpc/Switch.GetStatus?id=0` → `output`, `apower` (W), `voltage`, `current`, `aenergy.total` (Wh), `temperature.tC`, `errors`, `counts.switch_on` |
| Switch | `GET /rpc/Switch.Set?id=0&on=true` |
| Power cycle | `GET /rpc/Switch.Set?id=0&on=false&toggle_after=10` |
| Settings | `Switch.SetConfig` → `initial_state` |

Python's `urllib` only supports digest authentication with MD5 and SHA-1; the Shelly needs SHA-256. A small
implementation of RFC 7616 (about 30 lines with `hashlib`) in the shared HTTP helper takes care of that, so there
are still no extra packages. The Shelly driver supports all capabilities.

### Data

- **`plugs.json`** in the data directory, mode 0600 (contains the plug passwords):
  ```json
  {"id": "…", "name": "plug-office-pc", "driver": "shelly-gen2", "host": "plug-office-pc.home.arpa", "channel": 0,
   "model": "S3PL-30110EU",
   "mac": "…", "password": "…", "consumer": {"kind": "device", "id": "…"}, "standby_w": 3, "running_w": 20,
   "wake_method": "power_wol", "cut_after_sleep": false, "price_kwh": null}
  ```
  `consumer.kind` is `device` (WOL device, by ID), `reservation` (by MAC) or `label` (just a name), or the field is
  missing (no consumer). The type "wireless plug" at the reservation is the same entry seen from the network side.
- **`energy.db`** (SQLite, part of Python): one row per plug and hour (energy, maximum power, temperature).
  4 plugs over one year are about 35,000 rows, or about 2 MB.
- **SD card:** minute values stay in RAM (last 24 h for the curve). Hourly values are written once per hour in a
  single transaction, 24 small writes a day.
- **Counter reset:** if the counter of a plug goes down (firmware update, reset), the new value counts as
  consumption since the reset; nothing is lost and nothing is counted twice.

### Log

Log entries like the existing ones, with the source (GUI, schedule, API):
- "Power on: Office PC (plug-office-pc)"
- "Power cycle: NAS, device was not responding"
- "Warning: plug-office-pc 61 °C"

## 7. Risks

| Risk | Mitigation |
|---|---|
| **Data loss from cutting the power** of a running or suspended device | Rules 1 and 2 (section 3): only at standby level, cut after sleep only with a confirmed shutdown, the API cannot cut a running device |
| **Lock-out:** the plug of the Pi or the router is switched off | Rule 6; note in the README |
| Relay wear from inrush current, welded contacts | Switching under load is avoided; switching cycles shown; welded contacts detected by measurement (rule 4) |
| Worn contact gets hot | Temperature and error flags of the plug, warning on a slow rise (rule 5) |
| Device without power after an outage | Plug setting "restore last state"; WOL-Server shows the setting and warns if it is "off" |
| Wi-Fi drop during a power cycle | The plug switches back on by itself (`toggle_after`) |
| Measurement inaccurate at low power | Thresholds from real measurements of each device (suggestion in the device dialog) |
| Plug unreachable | Status "plug unreachable"; the device keeps working with ping and WOL as before |

## 8. Open questions

| Question | Suggestion |
|---|---|
| Price per kWh for the cost display | enter later in the GUI; without a price, only kWh are shown |
| Which device gets which plug | decided when the plugs arrive; the standby values are measured then |
| May the script API switch plugs directly (without a device)? | at first only via `wake` of a device; direct switching later, if needed |
| Keep the plugs off the internet | later (section 4); first set up with internet for the firmware update |

## 9. Phases

| Phase | Contents | Size |
|---|---|---|
| 1 | Driver layer with the Shelly Gen2+ driver and the fake plug. Type "wireless plug" at the reservation, detection when approving (changeable), password, Plugs tab with live view, switching with the safety rules and plug settings. Unit tests for core and driver. | medium |
| 2 | Link to consumers (WOL device, reservation, name): status from power and ping, wake methods, cut after sleep, power cycle for hanging devices, alerts | medium |
| 3 | Energy tracking: hourly values in SQLite, *Consumption* with curve and bars, costs, CSV export | medium |
| later | More drivers (Tasmota, FRITZ!DECT), schedules for plugs, plugs without internet, notifications | small each |

Phase 1 can be tested with the real plugs as soon as they are set up; it does not change anything for the
existing devices.
