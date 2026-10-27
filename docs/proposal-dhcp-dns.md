# Proposal: DHCP and DNS in WOL-Server

As of: 2026-10-26 · Status: **implemented (phases 1–5), tested locally, not yet in production on the Pi**

> Usage and migration: README, section 5. Where the implementation deviates from this proposal:
> - Jobs go through a queue (`/var/lib/wol-wifi/dns/requests/`, `wol-dns-apply.path` with
>   `DirectoryNotEmpty=`) instead of a single job file, executed in Python (`wolserver/dnsjob.py`).
> - Reservations are also written to `hosts.dns`, so that sleeping devices without a lease can be resolved by
>   name as well (important for Wake-on-LAN).
> - Vendor detection uses the system's lists (`ieee-data`, nmap); no list of its own is shipped.
> - Approve and Ignore take effect immediately, regardless of an open draft.
> - Reservations, DNS records and settings take effect immediately (settings with a preview of the
>   changes). A draft exists only after an import, loading a backup or restoring from the history.

WOL-Server takes over DHCP and DNS for the home network. Both are kept in sync automatically: every device that
gets an address via DHCP is immediately reachable by its name, including the reverse lookup from IP to name.
Everything is managed in the WOL-Server web GUI, at least as conveniently as with Webmin.

## 1. Current situation

Today this is handled by the backup server `backup` (192.168.1.10, Debian 13) with Webmin:

| Area | Current state |
|---|---|
| DHCP | ISC dhcpd 4.4.3. ISC has not developed it further since 2022. One subnet 192.168.1.0/24. |
| Assignments | **46 fixed assignments** (MAC → IP, name). The dynamic range consists of .254 only, plus `deny unknown-clients`: in practice only known devices get an address. |
| Lease time | 600 s (default), at most 7200 s |
| Options | domain `oldnet.local`, router, DNS and NTP each 192.168.1.1 |
| DDNS | `ddns-updates on` is set, but `ddns-update-style` is commented out: **effectively off** |
| DNS | BIND 9.20 as a forwarder only, no zone of its own. The clients use the router as DNS anyway. **No device can be resolved by its name.** |

WOL-Server (Pi Zero W, 192.168.1.8) is reserved there as `wol`.

## 2. Decision: dnsmasq, controlled by WOL-Server

| Option | Result |
|---|---|
| **dnsmasq and a custom GUI in WOL-Server** | **chosen.** DHCP and DNS in one process, synchronization is built in. Small (a few MB of RAM), runs on the Zero, already installed as `dnsmasq-base`. The GUI fits the project: Python without extra packages, one interface for everything. |
| Technitium DNS | rejected: .NET (not wanted, and does not run on ARMv6 either) |
| Kea + Kea-DDNS + PowerDNS (+ Stork, PowerDNS-Admin) | rejected: five services, two GUIs, too heavy for the Pi. Standards-compliant DDNS per RFC 2136 is not needed on a private network. |
| Pi-hole v6 | rejected: DNS in the GUI only supports A and CNAME, and its main purpose is an ad blocker |
| Repair dhcpd and BIND on .10 | rejected: the GUI would remain Webmin, dhcpd has no future |

Not needed and therefore not planned: BIND-compatible zone files, zone transfers, TSIG, DNSSEC for the local
zone, DHCP failover, multiple scopes or VLANs, DHCP relay.

## 3. Features

### Essential (phases 1–3)

**DHCP: basic settings**
- Network, gateway, DNS servers (WOL-Server itself, optionally a second one), NTP server, domain or search domain
- Default lease time, **preset 12 h**. Selection list (10 min, 1 h, 4 h, 12 h, 1 day, 7 days) or free input in
  minutes, hours or days (2 min to 7 days). Can be set differently per reservation, e.g. short for devices that
  move around often.
- Mode **"Known devices only"** (as today) or **"Address range for unknown devices"** with start and end

**DHCP: reservations**
- MAC → IP → name, with comment, enabled/disabled and optionally a custom lease time
- Validation on input: duplicate MAC, IP or name; IP outside the network or inside the dynamic range; invalid name
- Table with search, sorting and filter (enabled, with WOL device, last seen)

**DHCP: approving unknown devices quickly**
- In "known devices only" mode, the server collects rejected requests: MAC, name reported by the device, vendor,
  time, number of attempts.
- Notice in the overview. **Approve with one click:** the name is prefilled (cleaned up), the next free IP is
  suggested, one click on *Apply* is enough. The device gets its address on its next attempt, usually within a
  few seconds.
- *Ignore* hides a MAC permanently, e.g. for neighbours' devices or visitors.
- Helps above all with phones that use a private, changing Wi-Fi address.

**DHCP: current leases**
- Name (reported by the device), IP, MAC, vendor, expiry, type (reserved or dynamic)
- Actions: turn into a reservation, turn into a WOL device, delete lease

**DNS**
- Local domain. Leases and reservations are entered automatically as `name.domain`, including reverse lookup, and disappear when the lease expires.
- Naming rule: the name from the reservation wins over the one the device reports. A name already in use is not overwritten. Reported names are cleaned up (`Anna's iPhone` → `annas-iphone`).
- Custom records for devices without DHCP (name → IP) and aliases (name → name)
- Upstream DNS (router, provider or public). The local domain and private reverse zones never go to the internet.

### Useful (phase 4)

- **Second DHCP server:** Before turning on, and regularly afterwards, the server asks for other DHCP servers via DHCPDISCOVER and warns if one responds.
- **Conditional forwarding:** one domain to a different DNS server, e.g. names in the VPN to the VPN server.
- **Export and import** as JSON, plus import from `dhcpd.conf`. Once for the migration, afterwards for backups.
- **Change log:** who, when, what. Recorded in the existing GUI log.

### Later, if wanted

Vendor detection via an offline list of MAC prefixes, "last seen", DNS statistics and a query log for
troubleshooting, network boot via PXE/TFTP.

## 4. User interface

New **Network** tab with four sections. As with pfSense or OPNsense, changes are first collected and then applied
together. This makes it possible to change several reservations in one go, and a mistake does not take the network
down in the middle of the work.

```
┌ WOL-Server ─ Devices · Schedules · Log · Network · System ─────────────────────────┐
│ ⚠ 3 changes not yet applied                          [Discard] [Apply]             │
│                                                                                    │
│ Overview │ Reservations │ Leases │ DNS │ Settings                                  │
│ ────────────────────────────────────────────────────────────────────────────────── │
│ DHCP  ● active · known devices only · 41 leases    DNS  ● active · home.arpa       │
│ ⚠ Unknown device: 5a:1f:…  "iPhone" · Apple · 2 min ago      [Approve] [Ignore]    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

**Reservations**

```
 Search: [office__________]   Filter: (●) all  ( ) enabled  ( ) with WOL    [+ Reservation]
 ┌──────────────────┬───────────────┬───────────────────┬─────────┬──────────┬─────┐
 │ Name             │ IP            │ MAC               │ Lease   │ Seen     │     │
 ├──────────────────┼───────────────┼───────────────────┼─────────┼──────────┼─────┤
 │ office-pc  ⏻     │ 192.168.1.9   │ 00:d8:61:d7:1d:36 │ Default │ 3 min ago│ ✎ ✕ │
 │ brother-mfc      │ 192.168.1.40  │ 94:dd:f8:bd:83:30 │ Default │ today    │ ✎ ✕ │
 │ repeater-lan     │ 192.168.1.2   │ d2:ce:1e:f7:77:47 │ 24 h    │ —        │ ✎ ✕ │
 └──────────────────┴───────────────┴───────────────────┴─────────┴──────────┴─────┘
 ⏻ = also a WOL device (wake directly from the list)
```

**Approve (dialog):**

```
 Approve unknown device
 MAC       5a:1f:3c:…  (private Wi-Fi address, Apple)   · 4 attempts since 21:14
 Name      [annas-iphone_____]   ← reported by the device: "Anna's iPhone"
 IP        [192.168.1.41_______]   ← next free address
 Comment   [_____________________]
                                   [Cancel]  [Approve and apply]
```

**Leases:** table as above, additionally with remaining time and the actions *Reserve*, *As WOL device* and
*Delete*.

**DNS:** domain, upstream servers, conditional forwarding, custom records and aliases. Next to it a
**test query**: enter a name or IP and see the answer from the local DNS.

**Settings:** mode (known devices only or address range), lease time, gateway, DNS, NTP, domain,
service on/off, export and import.

**Link to the WOL devices:** Both lists stay separate and are linked by MAC. The device dialog offers
"Reserve fixed IP". In the reservation list, ⏻ marks WOL devices and allows waking them. If the name of a
reservation changes, the WOL device remains untouched.

## 5. Technical design

### Processes and privileges

As with the VPN: the GUI keeps running without privileges, and small, separate parts do the work as root.

```
 Browser ──HTTP──▶ wol-server (user wol)
                    │  edit the draft, show leases
                    ▼ Unix socket
                   wol-wifi (root, without capabilities)        → better named "wol-system" in future
                    │  strictly validates the draft, generates the dnsmasq files into a staging area,
                    │  writes a job
                    ▼ wol-dns-apply.path
                   wol-dns-apply.service (root, oneshot)
                    │  dnsmasq --test on the staging area → apply → SIGHUP or restart
                    │  → self-test (DNS query to 127.0.0.1) → on failure, restore previous state
                    ▼
                   wol-dns.service = dnsmasq (starts as root, then runs as user dnsmasq)
```

- **No free text reaches dnsmasq.** The GUI only passes structured data (JSON). The configuration is generated
  exclusively by the root service, line by line from validated fields. Options such as `dhcp-script` or
  `conf-file`, which start programs or include files, cannot be injected this way. This is the same lesson as
  with the VPN validation.
- **Own unit instead of the Debian package `dnsmasq`.** We use the existing `dnsmasq-base` with our own unit. The
  `dnsmasq` package would bring along a second, automatically starting service.
- **Reservations without a restart.** dnsmasq rereads `dhcp-hostsfile` and `addn-hosts` on SIGHUP. New or
  changed reservations and custom DNS records therefore take effect immediately, without interruption. Only
  changes to the basic settings (range, lease time, domain) need a restart. The leases are preserved in the
  lease file.

### Files

| File | Contents | Written by |
|---|---|---|
| `/var/lib/wol-wifi/dns/network.json` | applied state (source of truth) | wol-wifi |
| `/var/lib/wol-wifi/dns/draft.json` | changes not yet applied | wol-wifi |
| `/etc/wol-dns/dnsmasq.conf` | basic settings | wol-dns-apply |
| `/etc/wol-dns/hosts.dhcp` | reservations (`dhcp-hostsfile`) | wol-dns-apply |
| `/etc/wol-dns/hosts.dns` | custom records (`addn-hosts`) | wol-dns-apply |
| `/var/lib/misc/dnsmasq.leases` | current leases | dnsmasq |
| `/var/lib/wol-wifi/dns/history/` | the last 10 applied states, for falling back | wol-wifi |

### Data model (`network.json`)

```json
{
  "version": 1,
  "dhcp": {
    "enabled": true,
    "mode": "known-only",
    "range": {"start": "192.168.1.200", "end": "192.168.1.249"},
    "lease_time": "12h",
    "gateway": "192.168.1.1",
    "dns": ["192.168.1.8", "192.168.1.1"],
    "ntp": ["192.168.1.1"]
  },
  "dns": {
    "domain": "home.arpa",
    "upstream": ["192.168.1.1"],
    "conditional": [{"domain": "vpn.example.org", "server": "10.1.1.1"}],
    "ttl": 60
  },
  "reservations": [
    {"mac": "00:d8:61:d7:1d:36", "ip": "192.168.1.9", "name": "office-pc",
     "comment": "Arbeitsplatz", "enabled": true, "lease_time": null}
  ],
  "records": [
    {"type": "host", "name": "router", "ip": "192.168.1.1"},
    {"type": "alias", "name": "drucker", "target": "brother-mfc"}
  ]
}
```

### Validation rules (in the root service, not just in the GUI)

- MAC: six hex pairs, no multicast address; IP: inside the network, not the network or broadcast address, not inside the dynamic range
- Names per RFC 1123: `a–z`, `0–9`, `-`, at most 63 characters, no `-` at the start or end; upper case is converted to lower case. **37 of the 46 names on .10 need this** (e.g. `LinuxCNC-Alfa` → `linuxcnc-alfa`, underscores → `-`).
- Uniqueness of MAC, IP and name across reservations, custom records and WOL-Server itself
- Lease time 2 min to 7 days; domain as a valid DNS name; upstream servers as IP addresses
- Limits: at most 500 reservations, 200 custom records

### Generated dnsmasq configuration (example)

```
# generated by WOL-Server - do not edit by hand
interface=wlan0
bind-dynamic                      # no conflict with the hotspot dnsmasq on 10.42.0.1
listen-address=192.168.1.8
domain-needed
bogus-priv                        # private reverse queries do not go to the internet
no-resolv
server=192.168.1.1
server=/vpn.example.org/10.1.1.1
local=/home.arpa/
domain=home.arpa
expand-hosts
local-ttl=60
dhcp-authoritative
dhcp-range=192.168.1.0,static,255.255.255.0,12h     # mode "known devices only"
dhcp-ignore=tag:!known
dhcp-option=option:router,192.168.1.1
dhcp-option=option:dns-server,192.168.1.8,192.168.1.1
dhcp-option=option:ntp-server,192.168.1.1
dhcp-hostsfile=/etc/wol-dns/hosts.dhcp
addn-hosts=/etc/wol-dns/hosts.dns
dhcp-leasefile=/var/lib/misc/dnsmasq.leases
log-dhcp
```

The root service reads the rejected requests of unknown devices from the journal of `wol-dns`
(`DHCPDISCOVER … ignored`) and makes them available to the GUI.

### Prerequisite: static IP for WOL-Server

A DHCP server cannot obtain its address from itself. The Pi's Wi-Fi profile is therefore switched to
`ipv4.method manual` with 192.168.1.8/24, gateway .1 and DNS 127.0.0.1. The setup hotspot remains unchanged.
While it is running, the Pi is not on the home network anyway, and `bind-dynamic` ensures that dnsmasq does not
interfere on 10.42.0.1.

## 6. Migrating from .10

The short lease time of 10 minutes on .10 helps here: all devices renew after 5 minutes at the latest and fall
back to a broadcast after 8¾ minutes. Then the new server answers.

1. **Preparation:** static IP for WOL-Server (see above). Import the `dhcpd.conf` from .10 via the GUI
   (upload the file). The preview shows each reservation with its cleaned-up name and marks conflicts.
   Apply; DHCP stays **off** for now.
2. **Test DNS:** dnsmasq runs as DNS only. Check on the laptop: `dig @192.168.1.8 office-pc.home.arpa`
   and `dig @192.168.1.8 -x 192.168.1.9`. The clients notice nothing.
3. **Switch over:** On .10 run `systemctl disable --now isc-dhcp-server`, turn on DHCP in WOL-Server. The
   assistant first checks via DHCPDISCOVER that no other DHCP server responds any more, and otherwise does not
   turn it on.
4. **Observe:** Over the next 10–15 minutes all active devices show up under *Leases*, with the same IP as
   before, but now with DNS names and with WOL-Server as their DNS.
5. **Fallback** (possible at any time): turn off DHCP in WOL-Server, on .10 run
   `systemctl enable --now isc-dhcp-server`. After 10 minutes at the latest, everything runs as before.
6. **Clean-up** after a few days without problems: increase the lease time to 12 h. On .10, also turn off BIND
   (`systemctl disable --now named`). dhcpd and BIND stay **installed but disabled** as a reserve for falling
   back.

## 7. Risks

| Risk | Mitigation |
|---|---|
| **Single point of failure:** If WOL-Server fails, devices no longer get an address, and name resolution fails. | Lease time 12 h instead of 10 min. Router as second DNS. Fallback via .10 documented and effective within 10 minutes. Hardware watchdog is active. |
| **Hardware:** The Zero W was unstable today (kernel oops, power supply suspected). As infrastructure for the whole network it is the weakest candidate. | First test deliberately on the Zero W. Before switching DHCP over (phase 3), wait for several days of stability with the new power supply. If it is not good enough: **Pi Zero 2 W** (same form factor, same image, about five times faster) or Pi 2 wired to `repeaterLAN`. The fallback via .10 stays open at all times. |
| Wi-Fi: DHCP and DNS for the whole network over the air | Not critical on the home network; the router is connected via Wi-Fi anyway. DNS answers from the cache are faster than via the router today. |
| Two DHCP servers at the same time | Check via DHCPDISCOVER before turning on and regularly afterwards, warning in the GUI |
| Phones with a private Wi-Fi address drop out in "known devices only" mode | List of rejected devices with "Approve"; hint in the GUI to turn off the private address on the home Wi-Fi |
| SD card: dnsmasq writes the lease file on every change | With a 12 h lease time and around 50 devices, that is only a few writes per day |
| Domain `.local` collides with mDNS | decided: `home.arpa` (section 8) |
| IPv6 | stays with the router (RA). Names are initially available for IPv4 only. |

## 8. Decisions

| Question | Decision |
|---|---|
| Domain | **`home.arpa`.** Unique, no collision with mDNS, `wol.local` keeps working. `oldnet.local` can be handed out as an additional search domain during the transition. |
| Hardware | **First test on the Pi Zero W.** Switch to Zero 2 W or Pi 2 if it is not stable enough. |
| Mode | **"Known devices only" as before**, with quick approval of unknown devices directly from the GUI (section 3) |
| .10 after the migration | **dhcpd and BIND disabled but installed** as a reserve for falling back |
| Lease time | **12 h after the migration, selectable in the GUI or freely configurable**, also per reservation. During the migration it stays at 10 min, so that switching over and falling back take effect quickly. |
| Upstream DNS | **Still the router (192.168.1.1).** Nothing changes for internet names compared to today. |

This settles all fundamental questions.

## 9. Phases

Each phase is usable and tested on its own (unit tests as before, plus a run on the Pi) before the next one
begins. The sizes are rough estimates.

| Phase | Contents | Size |
|---|---|---|
| 1 | dnsmasq unit, apply with validation and fallback, data model, **DNS** section (domain, upstream, custom records, test query). DHCP off. | large |
| 2 | **Reservations** with table, dialog and validation; **import from `dhcpd.conf`** with preview; link to the WOL devices | large |
| 3 | Turn on **DHCP**: settings, leases, **quick approval of unknown devices**, static IP for the Pi, check for a second DHCP server, migration assistant | large |
| 4 | Conditional forwarding, export/import, change log | medium |
| 5 (optional) | Vendor detection, "last seen", statistics, PXE | small to medium |

After phase 1 the new DNS can already be used and checked in parallel, without anything changing on the network.
Only phase 3 takes over DHCP from .10.
