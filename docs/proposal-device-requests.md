# Proposal: requests – several clients share one device

As of: 2026-10-29 · Status: **implemented, released in v1.2.0**

## 1. Problem

A backup server serves several clients: laptops, PCs and servers out on the internet behind the VPN. Each one
backs up at its own time. Before, every client script woke the server and put it to sleep afterwards. The
first client to finish then cut off the others, and a sleep schedule might hit a running backup. The guard
on the target (refuse while `rsync` runs) only looks at one moment. It is no agreement.

Long jobs that run on a device itself need the same thing, for example an AI task that takes days.

## 2. Idea

The WOL-Server keeps a list of **open requests** per device and decides on its own when the device may sleep.
Clients talk only to the API. There is no script to distribute and `curl` is enough, both in the LAN and over
the VPN.

1. **Request:** `POST /api/devices/<device>/requests {client, ttl}`. The device is woken if it does not
   answer; with `?wait=s` the answer comes once it is online. `woken` in the answer says whether this request
   woke it, so a client can ask for sleep only for a device it woke itself.
2. **Heartbeat:** `PUT …/requests/<id>`, every few minutes. It extends the request by its `ttl` (default 15
   minutes).
   - Without heartbeats the request expires, so a crashed client does not keep the device awake forever.
   - There is no upper limit: as long as heartbeats come, a request lives for days.
   - `ttl: 0` means "until released".
3. **Finish:** `DELETE …/requests/<id>?then=sleep`.
   - With `then=sleep` the client asks for sleep.
   - If no other request is open, the device sleeps after a **grace period** of 2 minutes. A new request in
     the meantime cancels that.
   - Otherwise the sleep is remembered and runs when the last request is done.
4. **Sleep when idle:** `POST …/sleep?when=idle` asks for sleep without holding a request: at once if nothing
   is open, otherwise remembered.

## 3. Rules

- **Scheduled sleep:** a sleep schedule that falls into an open request is **deferred**, not dropped. It runs
  when the last request is done.
- **The Sleep button** and the old `POST …/sleep` without `when` ask first while requests are open (409 with
  `confirm`); `force` puts the device to sleep anyway.
- **The plug of a device in use:**
  - *Off* and *Restart* by hand ask first.
  - A plug schedule is skipped.
- **Failed sleep:** if the sleep that runs after the last request fails (for example the guard on the target
  refuses), it is retried every 5 minutes, at most 6 times.
- **Storage:** requests survive a restart of the WOL-Server (`requests.json`). Heartbeats only touch RAM, which
  is easy on the SD card. After a restart every request gets one `ttl` for its next heartbeat.
- **Log:**
  - requested;
  - finished;
  - expired;
  - ended in the GUI;
  - sleep remembered, deferred, cancelled;
  - sleep done or failed: the run itself, as for every sleep.

## 4. API (also with API tokens)

```
GET    /api/devices/<id|name>/requests                 open requests and a remembered sleep
POST   /api/devices/<id|name>/requests[?wait=s]        {client, ttl}  -> 201 {id, …, online, woken} · 502/504: stays open
PUT    /api/devices/<id|name>/requests/<rid>           heartbeat -> 200 · 404 expired or unknown
DELETE /api/devices/<id|name>/requests/<rid>[?then=sleep]
POST   /api/devices/<id|name>/sleep?when=idle          -> 200 asleep · 202 remembered
DELETE /api/devices/<id|name>/sleep                    cancel a remembered sleep
```

A token can only renew and end its own requests (403 otherwise); the GUI can end any. `GET /api/status` also
returns the requests per device, so the GUI can show them on the device card ("In use:
aida, mail · sleep requested"). The dialog behind it lists every request and can end it.

## 5. Over the VPN

The API is reachable over the VPN: the allowlist `auto` contains the network of the tunnel. The HTTPS name,
however, points publicly to the web server that obtains the certificate. A client outside the home network
therefore pins the name to the Pi's VPN address, for example with `10.1.1.8 wol.example.org` in `/etc/hosts`,
or with `curl --resolve wol.example.org:443:10.1.1.8`.
