# Proposal: hold – a requested device stays awake on its own side too

As of: 2026-10-30 · Status: **implemented** (section 7)

## 1. Problem

A request (see [proposal-device-requests.md](proposal-device-requests.md)) tells the WOL-Server: "I need this
device, do not put it to sleep." The WOL-Server keeps to that. The device itself does not know about it: its own
power management (a desktop's idle timer, logind) puts it to sleep whenever it sees nothing to do. The device card
then shows "Standby" and "In use" at the same time.

A client that works *on* the device announces that work there itself, for example snapshot-backup runs rsync under
`systemd-inhibit`. A client that only needs the device to be there - it reaches it over the network, or it waits for
a person - has no way to do that. Its request is a promise the device does not hear.

## 2. Idea

Waking itself cannot go over SSH: a sleeping device takes no connection. But once it answers, the WOL-Server can use
the SSH access it already has for the sleep method and hold the device on the device's own side:

1. A device has an open request and **Keep awake while requested** switched on.
2. As soon as it answers, the WOL-Server opens an SSH connection and starts the **hold command** there, by default
   `systemd-inhibit --what=sleep:idle --who=WOL-Server --why="Requested via the WOL-Server" --mode=block cat`.
   The inhibitor holds as long as the connection is open (`cat` waits for its input, which never comes).
3. When the last request is done, the WOL-Server closes the connection and the inhibitor is gone. Only then does a
   requested sleep run, as before.

The hold is fail-safe: if the WOL-Server stops or the network breaks, the connection ends and the inhibitor goes with
it. No device stays awake forever because of a WOL-Server that is gone.

## 3. Rules

- **Only while requested.** Waking a device by hand, from a schedule or by `POST …/wake` holds nothing: a person who
  wakes a PC uses it, and its own power management is right.
- **Only with the SSH sleep method.** The hold uses the same host, user, port and key. A device that sleeps over HTTP
  or Sleep-on-LAN has no SSH access to hold it with.
- **Only when the device answers.** A device that is asleep is woken by the request; the hold follows once it is up.
- **Before every sleep the hold is released**, also for a sleep forced over open requests. Otherwise the device's own
  sleep guard (e.g. `wol-sleep`, which refuses while a block inhibitor is held) would refuse the WOL-Server's own
  sleep.
- **A broken hold is tried again**, at most once a minute, as long as the request is open; for example after the
  device rebooted. Each start and each loss goes into the log once.
- **The reason stays fixed.** Client names come from outside (API clients); they are not put into a command that
  runs on another machine. Who requests a device is shown by the WOL-Server.

## 4. On the device

- A device with plain SSH access runs the default command. It needs `systemd-inhibit` (systemd systems); desktops
  (KDE, GNOME) and logind respect a `sleep`/`idle` block inhibitor.
- A key that is bound to a forced command (`command="…"` in `authorized_keys`), as a sleep-only key should be, never
  runs the command sent; the forced command sees it in `SSH_ORIGINAL_COMMAND`. Such a script gets a second case: for
  `hold` it starts the inhibitor and waits, otherwise it does what it did. The hold command of that device is then
  just `hold`.
- **Order matters:** a forced command that does not know `hold` yet does what it always does - it puts the device
  to sleep or switches it off. Extend the script first, then switch the hold on. The switch is off by default, and
  the dialog says so.
- Windows, older NAS and anything without `systemd-inhibit`: leave the switch off.

## 5. GUI and API

- Device dialog, sleep method SSH: switch **Keep awake while requested (SSH)** and the hold command, prefilled.
- Device card: the request line says whether the hold stands ("held on the device") or why not.
- `GET /api/status` and `GET …/requests` report `hold`: `null`, or `{state: "held"|"waiting"|"failed", since, error}`.

## 6. Cost

One idle SSH connection per held device on the Pi: a few MB of memory, no CPU while nothing happens. Keepalives
(every 30 s) notice a dead connection.

## 7. Implemented

- `wolserver/hold.py`; the switch and the hold command per device (`hold`, `hold_command`).
- Added during the work: the WOL-Server checks the state it shows for contradictions - a requested device that is
  asleep, one that answers although its plug is off, one that answers at standby power - and shows them on the
  card and in its log (journal, RAM) once they have lasted 30 s, once when they appear and once when they are gone.
  A requested device is given 5 minutes to start before it counts as asleep. With the hold switched on, a requested
  device that falls asleep anyway is woken again, up to three times, 5 minutes apart.
