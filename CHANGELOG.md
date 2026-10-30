# Changelog

What changed in which version. The version is also shown in the GUI (*System → Info*) and by
`wol-server --version`.

## Unreleased

- **Plug passwords:** `plugs.json` no longer keeps the password of a Shelly. After the first successful login the
  server stores only the digest key derived from it (HA1, which the plug keeps itself). The key works for that one
  plug only, so the file does not give away a password that may be in use elsewhere. Plugs saved with their password
  are switched over at the next reading. A plug that has been reset or replaced asks for the password again.
- **Consumption charts:** the power curves get a time grid. Faint bands alternate every hour (every 5 minutes
  in the last hour), and every time on the axis has a gridline and a tick. The month chart shades the weekends,
  and its last day label no longer crowds the one before it.
- **Plug card:** "On" meant only the relay. For a plug with a device behind it the card now also names the
  device's state, as on the device card ("On · Standby", in gray while the device sleeps). Below about 1 W a Shelly
  reads 0 W although current flows; the card shows "< 1 W" then instead of a 0 that looks like no power.
- **Wake from the plug card:** a plug with a device behind it offers *Wake* instead of *On*, active while the device
  is not online - asleep behind a plug that is on, or without power. It wakes the device as its own card does.
- **Waking through a switched-off plug:** with the wake method "Wake-on-LAN" (the default) a wake sent only the magic
  packet, which reaches nothing without power. If the plug is off, the WOL-Server now switches it on first and sends
  the packet once the network card has power, as "Power on, then WOL" always did.

## 1.2.1 – 2026-10-29

- **Requests:** the answer to `POST /api/devices/<device>/requests` says in `woken` whether this
  request woke the device. A client can then ask for sleep only for a device it woke itself, and a
  device that someone switched on stays on. [snapshot-backup](https://github.com/schnebeck/snapshot-backup)
  19.0 and later do that by default.
- **Docs:** the README names snapshot-backup as a ready client for requests.
- **Version:** the version shown in the GUI, the log and `--version` was still 1.0.0 in 1.1.0 and
  1.2.0; it follows the release now. This changelog is new.

## 1.2.0 – 2026-10-29

- **Requests:** several clients share one device, for example backup clients and a backup server.
  - A client requests the device (`POST …/requests`), which wakes it, keeps the request alive with
    heartbeats (`PUT`) and ends it (`DELETE`, with `?then=sleep` to ask for sleep).
  - The device sleeps after the last open request and a grace period of 2 minutes; a request
    without heartbeats expires after its `ttl`.
  - `POST …/sleep?when=idle` asks for sleep without holding a request; `DELETE …/sleep` takes it
    back.
  - Only the API is needed (`curl`), in the LAN and over the VPN, with API tokens. A token can only
    renew and end its own requests.
- While requests are open, sleep schedules wait for the last one, *Sleep* and a plain
  `POST …/sleep` ask first (`force` overrides), and the device's plug is not switched off by a
  schedule.
- The device card shows who uses a device ("In use: …"); the details list every request and can
  end it.
- Requests survive a restart of the WOL-Server (`requests.json`).
- `then=sleep` for a device without a sleep method ends the request instead of refusing and leaving
  it open.
- The API docs were checked against the code: the example script keeps the request ID on 502 and
  504, the answers of every call are described.

## 1.1.0 – 2026-10-29

- **Wireless plugs** (Shelly Gen2+): switch and measure, with safety checks before power is cut.
- **Consumption** per hour (live, 5-second steps), day, month and year, with costs and standby
  share; CSV export.
- **Plugs linked to devices:** wake by power, cut after sleep, status from power (standby, no power,
  starting, not responding).
- **Alerts:** overtemperature, welded relay, relay wear, unreachable plug.
- The WOL-Server's own plug and plugs of network equipment are never switched off.
- **Securing plugs** from the GUI: access point, Bluetooth, Matter, cloud off, password set; settings
  that wait for a restart are shown and the plug can be restarted.
- Schedules for plugs.
- **Network:** rejected DHCP requests are shown again (dnsmasq's journal format with the doubled
  tag), suggested addresses are checked on the network before they are offered.
- **Devices:** *Wake* only for a device that is not online, *Sleep* only for one that is not off.

## 1.0.0 – 2026-10-27

The first release.

- **Wake-on-LAN** and **sleep** (SSH command) for the devices in the network, with schedules and a
  status per device; a web GUI in English with a German translation.
- **Raspberry Pi Zero W:** a ready SD card image with setup on first start, a setup hotspot for the
  Wi-Fi, Wi-Fi-only operation, a status LED, `lowmem.sh` and `lowpower.sh`.
- **OpenVPN client**, set up in the GUI (upload a `.ovpn`, on/off, remove).
- **DHCP and DNS** with dnsmasq ("Network" tab): reservations, records, leases, unknown devices,
  import, export, history and tests.
- **HTTPS** with a Let's Encrypt certificate obtained by a web server.
- **Script API:** API tokens, devices by ID or name, `?wait=` until the device answers or is gone.
- **Access:** `allowed_networks: "auto"` follows the WOL-Server's own interfaces.
- GPL-3.0-or-later with SPDX headers (REUSE).
