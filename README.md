# WOL-Server für den Raspberry Pi Zero W

Ein schlanker, headless Wake-on-LAN-Manager mit Web-GUI. Er läuft dauerhaft auf einem Raspberry Pi Zero W
(ca. 0,4–0,7 W) und weckt Rechner im Heimnetz auf oder legt sie schlafen – manuell oder nach Zeitplan,
aus dem Intranet und über VPN.

- **Geräte verwalten:** Name, MAC, Host/IP, Broadcast-Adresse, WOL-Port, Schlaf-Methode
- **Aufwecken** per Magic Packet, **Schlafen legen** per SSH-Befehl, HTTP-Aufruf oder Sleep-on-LAN
- **Zeitpläne:** pro Gerät Aufwecken/Schlafen zu einer Uhrzeit an gewählten Wochentagen
- **Online-Status** (ICMP-Ping bzw. TCP-Probe, erkennt auch Windows-PCs mit ICMP-Firewall)
- **Protokoll** aller manuellen und geplanten Aktionen
- **Zugriff nur aus LAN/VPN** (Netz-Allowlist), Passwort-Login, CSRF-Schutz, gehärteter systemd-Dienst
- **Keine Abhängigkeiten** außer Python 3 (Standardbibliothek) – kein pip, kein Node, keine Datenbank, kein CDN

```
 Handy / Laptop ──(LAN oder VPN)──► Pi Zero W :80 ──(UDP-Broadcast, Magic Packet)──► PC / NAS / Server
                                       │
                                       └──(SSH / HTTP / Sleep-on-LAN)──► Gerät schlafen legen
```

Warum ein Pi? Magic Packets sind Layer-2-Broadcasts und kommen nicht durch einen VPN-Tunnel. Der Pi steht im
Heimnetz und schickt das Paket dort stellvertretend ab.

---

## 1. SD-Karte vorbereiten

Mit dem **Raspberry Pi Imager**:

1. Gerät: *Raspberry Pi Zero* · Betriebssystem: **Raspberry Pi OS Lite (32-bit)**
   (der Zero W hat eine ARMv6-CPU; 64-bit läuft nur auf dem Zero 2 W)
2. Unter „Einstellungen bearbeiten“:
   - Hostname, z. B. `wol` (die GUI ist dann unter `http://wol.local/` erreichbar)
   - Benutzer und Passwort
   - WLAN (SSID, Passwort, Land `DE`)
   - Zeitzone `Europe/Berlin`. **Wichtig für die Zeitpläne**, denn sie laufen in der Zeitzone des Pi.
   - Unter „Dienste“: **SSH aktivieren**
3. Karte schreiben, in den Pi stecken, booten (der erste Start dauert ein paar Minuten).

> **Tipp:** Im Router dem Pi eine feste IP zuweisen (z. B. FRITZ!Box: *Heimnetz → Netzwerk → Gerät → „Diesem
> Netzwerkgerät immer die gleiche IPv4-Adresse zuweisen“*).

## 2. Installieren

Vom eigenen Rechner aus:

```bash
scp -r WOL-Server <benutzer>@wol.local:~/
ssh <benutzer>@wol.local
cd ~/WOL-Server
sudo ./deploy/install.sh          # fragt nach einem GUI-Passwort
sudo ./deploy/lowpower.sh         # optional, empfohlen – danach: sudo reboot
```

`install.sh` legt den Systembenutzer `wol` an, kopiert das Programm nach `/opt/wol-server`, schreibt
`/etc/wol-server/config.json`, erzeugt einen SSH-Schlüssel und startet den Dienst `wol-server`.
Das Skript lässt sich gefahrlos erneut ausführen, so werden auch **Updates** eingespielt.
Daten und Konfiguration bleiben dabei erhalten.

Optionen: `--port 8080` (anderer Port), `--no-password` (keine Passwortabfrage).

### Stromsparen: `lowpower.sh`

| Maßnahme | Wirkung |
|---|---|
| HDMI aus, KMS-Grafiktreiber aus, `gpu_mem=16` | ca. 25 mA weniger, mehr RAM |
| Bluetooth aus (`disable-bt`, `hciuart`, `bluetooth`) | weniger Strom, weniger Dienste |
| Audio, Kamera-/Display-Autoerkennung aus | weniger Treiber |
| `triggerhappy`, `ModemManager` aus | weniger Hintergrundprozesse |
| Journal nur im RAM, SD-Swap aus | schont die SD-Karte |
| `--led-off` | grüne Aktivitäts-LED aus |
| **WLAN-Powersave aus** | Bewusst so gewählt, denn mit Powersave verpasst der Zero eingehende Verbindungen. Kostet nur wenige mA. |

Vorher wird `config.txt` gesichert (`config.txt.bak-<datum>`). Mit `--keep-hdmi` / `--keep-bluetooth`
lassen sich einzelne Maßnahmen überspringen.

## 3. Zielgeräte vorbereiten

### Wake-on-LAN aktivieren

- **BIOS/UEFI:** „Wake on LAN“, „Power On by PCI-E“ o. ä. aktivieren, „ErP/EuP“ deaktivieren.
- **Linux:** `sudo ethtool -s eth0 wol g`, dauerhaft mit NetworkManager:
  `nmcli connection modify "<Verbindung>" 802-3-ethernet.wake-on-lan magic`
- **Windows:** Geräte-Manager → Netzwerkadapter → Eigenschaften
  - Energieverwaltung: „Gerät kann den Computer aus dem Ruhezustand aktivieren“ und „Nur Magic Packet …“
  - Erweitert: „Wake on Magic Packet“ = Aktiviert
  - Zum Aufwecken aus dem *ausgeschalteten* Zustand den **Schnellstart deaktivieren**
    (Energieoptionen → „Auswahl der Netzschaltertasten“)
- Das Zielgerät sollte **per Kabel** angeschlossen sein, denn WOL über WLAN unterstützen nur wenige Geräte.
  Der Pi selbst darf im WLAN hängen; der Router reicht den Broadcast ins LAN weiter.

Kommt das Paket nicht an, in der GUI als Broadcast-Adresse die des eigenen Netzes eintragen
(z. B. `192.168.178.255` statt `255.255.255.255`).

### Schlafen legen

Wake-on-LAN kennt kein „Schlafen“. Der WOL-Server bietet drei Wege:

**SSH** (empfohlen für Linux, macOS, Windows mit OpenSSH-Server). Den öffentlichen Schlüssel aus
*System → SSH-Schlüssel* auf dem Ziel eintragen:

```bash
# Linux-Zielgerät, als der in der GUI eingetragene Benutzer:
echo 'ssh-ed25519 AAAA… wol-server@wol' >> ~/.ssh/authorized_keys
echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl suspend, /usr/bin/systemctl poweroff" \
  | sudo tee /etc/sudoers.d/wol-server
```

Befehl in der GUI: `sudo systemctl suspend`.

Windows: „Optionale Features“ → *OpenSSH-Server* installieren. Bei Administrator-Konten gehört der Schlüssel in
`C:\ProgramData\ssh\administrators_authorized_keys`. Befehl: `shutdown /h` (Ruhezustand) oder
`rundll32.exe powrprof.dll,SetSuspendState 0,1,0` (Standby, wenn der Ruhezustand deaktiviert ist).

Beim ersten Verbinden merkt sich der Server den Host-Key des Ziels (`accept-new`).

**HTTP:** Ruft eine URL auf (GET/POST/PUT, optional mit Body). Das passt z. B. für Home Assistant, ein NAS-API
oder einen eigenen Sleep-Endpunkt.

**Sleep-on-LAN:** Sendet ein Magic Packet mit umgekehrter MAC. Dafür muss auf dem Ziel ein passender Dienst
laufen, z. B. [sleep-on-lan](https://github.com/SR-G/sleep-on-lan) (Windows/Linux).

## 4. Zugriff über VPN

Die GUI ist nur aus den Netzen in `allowed_networks` erreichbar. Standardmäßig sind das alle privaten Netze
(RFC 1918), `100.64.0.0/10` (Tailscale/CGNAT), IPv6-ULA und Link-Local. Andere Quellen bekommen `403`.

- **VPN im Router** (FRITZ!Box-VPN/WireGuard, OpenVPN/WireGuard auf dem Router): Ist das der schonendste Weg
  für den Zero, denn VPN-Clients erhalten meist eine Adresse im Heimnetz oder in einem privaten Netz. Damit ist
  der Zugriff ohne weitere Konfiguration möglich: `http://<IP-des-Pi>/`.
- **Tailscale/WireGuard direkt auf dem Pi:** funktioniert, kostet auf dem Single-Core-Zero aber spürbar CPU.
- Liegt dein VPN-Netz außerhalb der Standardnetze, ergänze es in `/etc/wol-server/config.json` und führe
  `sudo systemctl restart wol-server` aus.

> Die GUI spricht bewusst nur HTTP: Sie ist ausschließlich im LAN/VPN erreichbar, und VPN-Verkehr ist bereits
> verschlüsselt. **Den Port niemals per Portfreigabe ins Internet öffnen.**

## 5. Bedienung

| Bereich | Funktion |
|---|---|
| **Geräte** | Karten mit Online-Status, nächster geplanter Aktion, Buttons *Aufwecken* / *Schlafen* |
| **Zeitpläne** | Uhrzeit + Wochentage + Aktion; per Schalter pausierbar; Vorlagen „Werktags/Wochenende/Täglich“ |
| **Protokoll** | Die letzten 300 Aktionen mit Ergebnis (manuell / Zeitplan) |
| **System** | Hostname, Uhrzeit, CPU-Temperatur, Laufzeit, erlaubte Netze, SSH-Schlüssel, Passwort ändern |

Der Online-Status wird nur abgefragt, solange die Seite offen und sichtbar ist (alle 10 s). Ist niemand auf
der GUI, schläft der Pi bis zur nächsten Zeitplan-Minute.

### Kommandozeile auf dem Pi

```bash
sudo wol-server passwd                       # GUI-Passwort setzen/ändern (meldet alle Sitzungen ab)
wol-server wake AA:BB:CC:DD:EE:FF            # Magic Packet direkt senden
journalctl -u wol-server -f                  # Logs
sudo systemctl restart wol-server            # nach Änderung der config.json
```

## 6. Konfiguration

`/etc/wol-server/config.json`:

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `listen` | `"::"` | Adresse (dual-stack; fällt ohne IPv6 auf `0.0.0.0` zurück) |
| `port` | `80` | HTTP-Port |
| `data_dir` | `/var/lib/wol-server` | Geräte, Zeitpläne, Protokoll, Passwort-Hash, SSH-Schlüssel |
| `allowed_networks` | private Netze | Quellnetze mit Zugriff |
| `session_days` | `30` | Gültigkeit des Login-Cookies |
| `status_cache_seconds` | `5` | Cache für Online-Prüfungen |
| `log_level` | `INFO` | `DEBUG` loggt jede Anfrage |

Dateien:

```
/opt/wol-server/wolserver/       Programm (Python-Paket inkl. web/)
/etc/wol-server/config.json      Konfiguration
/var/lib/wol-server/data.json    Geräte, Zeitpläne, Protokoll (atomar geschrieben)
/var/lib/wol-server/auth.json    Passwort-Hash (PBKDF2) + Cookie-Secret, Modus 0600
/var/lib/wol-server/.ssh/        SSH-Schlüssel und known_hosts für das Schlafenlegen
```

**Backup:** `/var/lib/wol-server/` sichern.

## 7. Technische Details

- **Zeitplaner:** Er wacht einmal pro Minute auf. Der Zero hat keine Echtzeituhr: Springt die Uhr nach dem Boot
  per NTP nach vorne, werden höchstens die letzten 10 Minuten nachgeholt. Rücksprünge und die doppelte Stunde
  bei der Zeitumstellung lösen nichts doppelt aus.
- **Online-Prüfung:** ICMP-Echo über unprivilegierte Sockets (dafür setzt `install.sh`
  `net.ipv4.ping_group_range`). Antwortet das Gerät nicht, folgen TCP-Probes auf 22/445/3389/80/443/139. Auch
  ein „Connection refused“ bedeutet: Das Gerät ist an.
- **Sicherheit:**
  - Netz-Allowlist
  - PBKDF2-Passwort-Hash und HMAC-signierte, `HttpOnly`/`SameSite=Strict`-Cookies
  - Bremse gegen Login-Brute-Force
  - CSRF-Schutz über einen Pflicht-Header
  - strikte Content-Security-Policy
  - Eingabevalidierung (u. a. gegen SSH-Option-Injection)
  - der Dienst läuft als unprivilegierter Benutzer mit `ProtectSystem=strict`, `NoNewPrivileges` u. v. m.
- **Ressourcen:** ein Python-Prozess, ca. 15–20 MB RAM, im Leerlauf praktisch 0 % CPU.

### API

Alle Endpunkte liefern JSON. Schreibende Anfragen brauchen den Header `X-Requested-With: wol-server`.

```
GET    /api/session                 POST /api/login {password}      POST /api/logout
GET    /api/devices                 POST /api/devices               PUT/DELETE /api/devices/<id>
POST   /api/devices/<id>/wake       POST /api/devices/<id>/sleep    GET  /api/status
GET    /api/schedules               POST /api/schedules             PUT/DELETE /api/schedules/<id>
GET    /api/events                  DELETE /api/events
GET    /api/info                    POST /api/password {current, new}
```

## 8. Entwicklung

```bash
python3 -m wolserver -c config.dev.json      # http://localhost:8080, Daten in ./data
python3 -m unittest discover -s tests -t .   # Tests
```

## 9. Fehlersuche

| Problem | Lösung |
|---|---|
| GUI nicht erreichbar | `systemctl status wol-server`, `journalctl -u wol-server -e`; `.local`-Namen brauchen mDNS, sonst die IP nutzen |
| `403 Zugriff nur aus dem lokalen Netz` | Das Client-Netz fehlt in `allowed_networks` (die eigene Adresse steht im Log) |
| Gerät wacht nicht auf | BIOS/NIC-Einstellungen prüfen, Broadcast-Adresse des Netzes eintragen, Gerät per Kabel anschließen, Windows-Schnellstart aus |
| Status immer „Offline“ | Host/IP prüfen; Firewall blockt ICMP *und* droppt TCP: z. B. ICMP-Echo in der Windows-Firewall erlauben |
| SSH: `Permission denied (publickey)` | Schlüssel aus *System* in die richtige `authorized_keys` eintragen |
| SSH: `sudo: a password is required` | sudoers-Regel aus Abschnitt 3 anlegen |
| Zeitpläne laufen zur falschen Zeit | Zeitzone setzen: `sudo timedatectl set-timezone Europe/Berlin` |

## Deinstallation

```bash
sudo ./deploy/uninstall.sh            # Programm entfernen, Daten behalten
sudo ./deploy/uninstall.sh --purge    # alles entfernen
```
