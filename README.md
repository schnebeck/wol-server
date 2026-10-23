# WOL-Server für den Raspberry Pi Zero W

Ein schlanker, headless Wake-on-LAN-Manager mit Web-GUI. Er läuft dauerhaft auf einem Raspberry Pi Zero W
(ca. 0,4–0,7 W) und weckt Rechner im Heimnetz auf oder legt sie schlafen – manuell oder nach Zeitplan,
aus dem Intranet und über VPN.

- **Geräte verwalten:** Name, MAC, Host/IP, Broadcast-Adresse, WOL-Port, Schlaf-Methode
- **Aufwecken** per Magic Packet, **Schlafen legen** per SSH-Befehl, HTTP-Aufruf oder Sleep-on-LAN
- **Zeitpläne:** pro Gerät Aufwecken/Schlafen zu einer Uhrzeit an gewählten Wochentagen
- **Online-Status** (ICMP-Ping bzw. TCP-Probe, erkennt auch Windows-PCs mit ICMP-Firewall)
- **Protokoll** aller manuellen und geplanten Aktionen
- **OpenVPN-Client auf dem Pi:** Er ist über das VPN erreichbar, ohne Portfreigabe zu Hause (auch bei DS-Lite/CGNAT)
- **Reiner WLAN-Betrieb:** Powersave aus, WLAN-Watchdog, Hardware-Watchdog; Magic Packets gehen immer ins WLAN,
  nie in den Tunnel
- **Einrichtungs-Hotspot wie bei einer Appliance:** Findet der Pi kein bekanntes WLAN, öffnet er einen eigenen
  Hotspot mit Einrichtungsseite. WLAN wechseln geht ohne Imager, Tastatur oder SD-Karte.
- **Zugriff nur aus LAN/VPN** (Netz-Allowlist), Passwort-Login, CSRF-Schutz, gehärteter systemd-Dienst
- **Keine Abhängigkeiten** außer Python 3 (Standardbibliothek) – kein pip, kein Node, keine Datenbank, kein CDN

```
 Handy unterwegs ──► OpenVPN-Server ◄──(tun0, ausgehend)── Pi Zero W ──(wlan0)──► WLAN-Router ──(LAN)──► PC / NAS
 Laptop zu Hause ─────────────(WLAN/LAN)──────────────────►   :80                  Magic Packet (Broadcast)
                                                                └──(SSH / HTTP / Sleep-on-LAN)──► schlafen legen
```

Warum ein Pi? Magic Packets sind Layer-2-Broadcasts und kommen nicht durch einen VPN-Tunnel. Der Pi steht im
Heimnetz und schickt das Paket dort stellvertretend ab. Dafür baut er selbst den OpenVPN-Tunnel nach außen auf.

---

## 1. SD-Karte vorbereiten

Mit dem **Raspberry Pi Imager**:

1. Gerät: *Raspberry Pi Zero* · Betriebssystem: **Raspberry Pi OS Lite (32-bit)**
   (der Zero W hat eine ARMv6-CPU; 64-bit läuft nur auf dem Zero 2 W)
2. Unter „Einstellungen bearbeiten“:
   - Hostname, z. B. `wol` (die GUI ist dann unter `http://wol.local/` erreichbar)
   - Benutzer und Passwort
   - WLAN (SSID, Passwort, Land `DE`): nur für die Erstinstallation per SSH nötig, siehe unten
   - Zeitzone `Europe/Berlin`. **Wichtig für die Zeitpläne**, denn sie laufen in der Zeitzone des Pi.
   - Unter „Dienste“: **SSH aktivieren**
3. Karte schreiben, in den Pi stecken, booten (der erste Start dauert ein paar Minuten).

Der Imager wird nur dieses eine Mal gebraucht. Nach der Installation richtet der Pi neue oder geänderte WLANs
über seinen **Einrichtungs-Hotspot** ein (siehe [WLAN einrichten](#wlan-einrichten-und-wechseln-einrichtungs-hotspot)).

> **Tipp:** Im Router dem Pi eine feste IP zuweisen (z. B. FRITZ!Box: *Heimnetz → Netzwerk → Gerät → „Diesem
> Netzwerkgerät immer die gleiche IPv4-Adresse zuweisen“*).

## 2. Installieren

Vom eigenen Rechner aus:

```bash
scp -r WOL-Server <benutzer>@wol.local:~/
ssh <benutzer>@wol.local
cd ~/WOL-Server
sudo ./deploy/install.sh --openvpn ~/client.ovpn   # fragt nach GUI-Passwort (und ggf. VPN-Zugang)
sudo ./deploy/lowpower.sh                          # optional, empfohlen – danach: sudo reboot
```

`install.sh` legt den Systembenutzer `wol` an, kopiert das Programm nach `/opt/wol-server`, schreibt
`/etc/wol-server/config.json`, erzeugt einen SSH-Schlüssel und startet den Dienst `wol-server`.
Außerdem richtet es die WLAN-Absicherung (`wlan-setup.sh`) und den Einrichtungs-Hotspot (`hotspot-setup.sh`)
ein, beide siehe unten, und mit `--openvpn` den VPN-Client (`openvpn-setup.sh`, siehe Abschnitt 4).
Das Skript lässt sich gefahrlos erneut ausführen, so werden auch **Updates** eingespielt.
Daten und Konfiguration bleiben dabei erhalten.

Optionen:
- `--port 8080`: anderer Port
- `--no-password`: keine Passwortabfrage
- `--openvpn <datei.ovpn>`: OpenVPN-Client einrichten
- `--country AT`: WLAN-Land (Standard `DE`), wichtig für erlaubte Kanäle und Sendeleistung
- `--no-wlan-setup`: ohne WLAN-Absicherung
- `--no-hotspot`: ohne Einrichtungs-Hotspot

### WLAN-Betrieb: `wlan-setup.sh`

Der Zero W hat nur WLAN. Damit der Server trotzdem zuverlässig erreichbar bleibt, richtet `install.sh`
automatisch Folgendes ein. Einzeln lässt es sich mit `sudo ./deploy/wlan-setup.sh` ausführen.

| Maßnahme | Wirkung |
|---|---|
| WLAN-Powersave aus | Ohne diese Einstellung verpasst der Zero eingehende Verbindungen und Broadcasts. Kostet nur wenige mA. |
| WLAN-Watchdog (Timer, jede Minute) | Pingt das Gateway. Nach 3 Fehlschlägen wird das WLAN neu verbunden, nach 30 min ohne Netz startet der Pi neu (frühestens 1 h nach dem Boot). Läuft der Einrichtungs-Hotspot, hält sich der Watchdog heraus. Einstellbar in `/etc/default/wol-wlan-watchdog`. |
| Hardware-Watchdog | Startet den Pi neu, falls das System komplett hängt |

Der Watchdog protokolliert ins Journal: `journalctl -t wlan-watchdog`.

### WLAN einrichten und wechseln: Einrichtungs-Hotspot

Neuer Router, geändertes WLAN-Passwort, Umzug: Findet der Pi keines seiner gespeicherten WLANs, öffnet er nach
5 Minuten einen eigenen Hotspot. Ist noch gar kein WLAN gespeichert, geschieht das schon 30 Sekunden nach dem Start.

1. Mit dem Handy oder Laptop mit dem WLAN **`WOL-Setup-XXXX`** verbinden. `XXXX` sind die letzten Stellen der
   WLAN-MAC-Adresse.
2. Die Einrichtungsseite öffnet sich von selbst (Captive Portal). Falls nicht: **`http://10.42.0.1/`** aufrufen.
3. Anmelden (sofern ein GUI-Passwort gesetzt ist), WLAN aus der Liste wählen, Passwort eingeben, *Verbinden*.
4. Der Pi beendet den Hotspot und verbindet sich. Danach ist die GUI wieder im Heimnetz erreichbar, z. B. unter
   `http://wol.local/`. Klappt die Verbindung nicht, etwa weil das Passwort falsch ist, öffnet sich der Hotspot
   nach etwa einer Minute wieder und die Seite zeigt den Fehler.

Im Normalbetrieb verwaltet *System → WLAN* die gespeicherten Netze. Zusätzliche WLANs lassen sich vorab
eintragen, etwa für einen Umzug. Der Pi nutzt sie automatisch, sobald das aktuelle WLAN nicht mehr erreichbar
ist.

Gut zu wissen:
- Der Zero W hat nur einen 2,4-GHz-Funkchip. Hotspot und WLAN-Client gehen **nicht gleichzeitig**. Während der
  Hotspot läuft, schaltet der Pi ihn deshalb alle 5 Minuten kurz ab und sucht nach den gespeicherten WLANs. Das
  passiert nicht, solange ein Gerät mit dem Hotspot verbunden ist oder die Einrichtungsseite benutzt wird.
- Die WLAN-Liste im Hotspot stammt aus der Suche direkt vor dem Start, denn im Hotspot-Betrieb kann der Chip
  nicht scannen. Fehlt ein Netz, den Namen von Hand eingeben.
- **Sicherheit:** Der Hotspot ist anfangs offen, damit die Ersteinrichtung ohne Vorwissen klappt. Die GUI ist
  durch ihr Passwort geschützt. Unter *System → WLAN → Einrichtungs-Hotspot* lässt sich ein WPA2-Passwort
  setzen. Hotspot-Clients dürfen die GUI immer nutzen, auch wenn `10.42.0.0/24` nicht in `allowed_networks`
  steht.
- Während der Hotspot läuft, ist der Pi nicht im Heimnetz. Fällige Zeitpläne werden dann nicht ausgeführt und
  als Fehler protokolliert.
- Technik: Ein eigener Dienst `wol-wifi` steuert den Hotspot als root über NetworkManager. Der Web-Server
  spricht mit ihm über einen Unix-Socket und braucht selbst keine Rechte am Netzwerk. Einzeln einrichten:
  `sudo ./deploy/hotspot-setup.sh --country DE`.

**Als fertiges Gerät weitergeben oder ein Image erstellen:** Nach der Installation die gespeicherten WLANs löschen
(`sudo nmcli connection delete <Name>`, Namen mit `nmcli connection show`) und den Pi herunterfahren. Beim nächsten
Start öffnet er sofort den Hotspot. Die SD-Karte lässt sich in diesem Zustand auch als Image für weitere Geräte
sichern.

### Stromsparen: `lowpower.sh`

| Maßnahme | Wirkung |
|---|---|
| HDMI aus, KMS-Grafiktreiber aus, `gpu_mem=16` | ca. 25 mA weniger, mehr RAM |
| Bluetooth aus (`disable-bt`, `hciuart`, `bluetooth`) | weniger Strom, weniger Dienste |
| Audio, Kamera-/Display-Autoerkennung aus | weniger Treiber |
| `triggerhappy`, `ModemManager` aus | weniger Hintergrundprozesse |
| Journal nur im RAM, SD-Swap aus | schont die SD-Karte |
| `--led-off` | grüne Aktivitäts-LED aus |

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

**Der Pi im WLAN:** Der Router bzw. Access Point reicht den Broadcast aus dem WLAN ins Kabelnetz weiter. Dafür
gilt:
- Der Pi muss im **selben Netz** wie die Zielgeräte sein, also **nicht im Gast-WLAN**.
- Am Access Point darf **keine Client-Isolation** aktiv sein.
- Mesh-Repeater (FRITZ!Repeater u. ä.) leiten Broadcasts normalerweise weiter.
- WLAN-Broadcasts werden nicht bestätigt; der Server sendet jedes Magic Packet deshalb 5-mal.
- Die Signalstärke zeigt die GUI unter *System → Netzwerk* (besser als ca. −70 dBm ist gut).

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

## 4. Zugriff über VPN (OpenVPN-Client auf dem Pi)

Der Pi verbindet sich als **OpenVPN-Client** mit einem OpenVPN-Server, z. B. auf einem VPS, im Router oder auf
einem NAS. Andere VPN-Teilnehmer (Handy, Laptop) erreichen ihn dann über seine VPN-Adresse. Weil der Tunnel vom
Pi nach außen aufgebaut wird, braucht es zu Hause keine Portfreigabe; das funktioniert auch hinter DS-Lite/CGNAT.

```bash
sudo ./deploy/openvpn-setup.sh ~/client.ovpn      # oder gleich: install.sh --openvpn ~/client.ovpn
```

Das Skript erledigt Folgendes:
- installiert `openvpn` und legt die Konfiguration als `/etc/openvpn/client/wol.conf` ab (Modus 0600)
- fragt Benutzername und Passwort ab, falls die Konfiguration `auth-user-pass` nutzt; die Zugangsdaten landen
  in `/etc/openvpn/client/wol.auth`, denn headless kann OpenVPN nicht nachfragen
- kopiert referenzierte Dateien (`ca ca.crt` …) mit; Konfigurationen mit Inline-Zertifikaten gehen direkt
- sorgt für unbegrenzte Neuverbindung nach WLAN- oder Server-Ausfällen (`connect-retry`, `ping-restart`,
  systemd `Restart=always`)
- **ignoriert `redirect-gateway` und gepushte DNS-Server.** Der Pi soll über das VPN nur *erreichbar* sein,
  nicht seinen gesamten Verkehr durch den Tunnel schicken. Das spart CPU auf dem Single-Core-Zero.
  `--allow-redirect-gateway` schaltet das ab. Magic Packets gehen in jedem Fall ins WLAN, weil der Server sie an
  die WLAN-Adresse bindet.
- zeigt am Ende die URL im VPN an, z. B. `http://10.8.0.6/`

### Externer OpenVPN-Server (VPS o. ä.)

Pi und Handy/Laptop sind beide **Clients** desselben Servers. Damit sie sich gegenseitig erreichen, braucht die
Server-Konfiguration (z. B. `/etc/openvpn/server/server.conf`) diese Einstellungen:

```
topology subnet
server 10.8.0.0 255.255.255.0
client-to-client                 # Clients dürfen untereinander sprechen (ohne Kernel-Forwarding)
client-config-dir /etc/openvpn/ccd
keepalive 10 60                  # Abbrüche (WLAN!) schnell erkennen, Client verbindet neu
```

Feste Adresse für den Pi, damit die URL stabil bleibt. Der Dateiname ist der **Common Name** des
Pi-Zertifikats bzw. bei `username-as-common-name` der VPN-Benutzername:

```
# /etc/openvpn/ccd/wol-pi
ifconfig-push 10.8.0.6 255.255.255.0
```

Danach den Server neu starten. Die GUI ist dann von jedem VPN-Client aus unter `http://10.8.0.6/` erreichbar.

Hinweise:
- **Jeder Client braucht ein eigenes Zertifikat** bzw. einen eigenen Benutzer. Bei geteilten Zertifikaten
  (`duplicate-cn`) funktioniert die feste Adresse nicht zuverlässig.
- **Kommerzielle VPN-Dienste** (NordVPN, ProtonVPN usw.) eignen sich nicht: Sie isolieren Clients
  voneinander, der Pi wäre über sie nicht erreichbar.
- Der Pi kann über das VPN nur sich selbst anbieten. Die Geräte im Heimnetz sind über das VPN nicht direkt
  erreichbar, und das ist für den WOL-Server auch nicht nötig. Wer das trotzdem will, braucht auf dem Server
  `route`/`iroute` für das Heimnetz und auf dem Pi IP-Forwarding mit NAT. Das ist nicht Teil dieses Projekts.
- Das VPN-Subnetz darf sich nicht mit dem Heimnetz oder dem Netz unterwegs überschneiden (z. B. VPN
  `10.8.0.0/24`, Heimnetz `192.168.178.0/24`).
- Firewall des VPS: Nur der OpenVPN-Port (Standard UDP 1194) muss offen sein.
- Pi-Zertifikat widerrufen bei Verlust der SD-Karte: Die Schlüssel liegen dort unverschlüsselt in
  `/etc/openvpn/client/`.

Die GUI ist nur aus den Netzen in `allowed_networks` erreichbar. Standardmäßig sind das alle privaten Netze
(RFC 1918, damit auch die üblichen OpenVPN-Netze `10.8.0.0/24` …), `100.64.0.0/10`, IPv6-ULA und Link-Local.
Liegt das VPN-Netz woanders, warnt `openvpn-setup.sh`. Dann das Netz in `/etc/wol-server/config.json`
ergänzen und `sudo systemctl restart wol-server` ausführen. Den VPN-Status samt Adresse zeigt die GUI unter
*System → Netzwerk*.

> Die GUI spricht bewusst nur HTTP: Sie ist ausschließlich im LAN/VPN erreichbar, und der VPN-Verkehr ist
> verschlüsselt. **Den Port niemals per Portfreigabe ins Internet öffnen.**

Ein OpenVPN-Client verursacht auf dem Zero W im Leerlauf kaum Last (nur Keepalives). Für die wenigen Kilobyte
der GUI reicht die Leistung problemlos.

## 5. Bedienung

| Bereich | Funktion |
|---|---|
| **Geräte** | Karten mit Online-Status, nächster geplanter Aktion, Buttons *Aufwecken* / *Schlafen* |
| **Zeitpläne** | Uhrzeit + Wochentage + Aktion; per Schalter pausierbar; Vorlagen „Werktags/Wochenende/Täglich“ |
| **Protokoll** | Die letzten 300 Aktionen mit Ergebnis (manuell / Zeitplan) |
| **System** | WLAN (Status, gespeicherte Netze, hinzufügen, Hotspot-Passwort), Hostname, Uhrzeit, CPU-Temperatur, WLAN-Signal, VPN-Status, Interfaces, erlaubte Netze, SSH-Schlüssel, Passwort |

Der Online-Status wird nur abgefragt, solange die Seite offen und sichtbar ist (alle 10 s). Ist niemand auf
der GUI, schläft der Pi bis zur nächsten Zeitplan-Minute.

### Kommandozeile auf dem Pi

```bash
sudo wol-server passwd                       # GUI-Passwort setzen/ändern (meldet alle Sitzungen ab)
wol-server wake AA:BB:CC:DD:EE:FF            # Magic Packet direkt senden
journalctl -u wol-server -f                  # Logs
journalctl -u wol-wifi -f                    # WLAN-Verwaltung / Hotspot
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
| `wol_interface` | `""` (automatisch) | Interface für Magic Packets, z. B. `"wlan0"`; automatisch heißt: das Interface der Default-Route, VPN-Interfaces ausgenommen |
| `wifi_interface` | `"wlan0"` | WLAN-Interface für Client-Betrieb und Hotspot |
| `hotspot_enabled` | `true` | Einrichtungs-Hotspot, wenn kein bekanntes WLAN erreichbar ist |
| `hotspot_ssid` | `""` | Name des Hotspots; leer heißt `WOL-Setup-XXXX` |
| `hotspot_delay_minutes` | `5` | Minuten ohne WLAN, bis der Hotspot startet |
| `hotspot_retry_minutes` | `5` | Abstand, in dem der Hotspot nach den gespeicherten WLANs sucht |
| `wifi_socket` | `/run/wol-wifi/control.sock` | Socket zwischen Web-Server und `wol-wifi` |
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
/etc/openvpn/client/wol.conf     OpenVPN-Client (Dienst openvpn-client@wol)
/etc/default/wol-wlan-watchdog   Einstellungen des WLAN-Watchdogs
/var/lib/wol-wifi/hotspot.json   Hotspot-Passwort (nur root)
/etc/NetworkManager/system-connections/   gespeicherte WLANs (NetworkManager)
/etc/NetworkManager/dnsmasq-shared.d/60-wol-server-captive.conf   DNS-Umleitung im Hotspot
```

**Backup:** `/var/lib/wol-server/` sichern.

## 7. Technische Details

- **Zeitplaner:** Er wacht einmal pro Minute auf. Der Zero hat keine Echtzeituhr: Springt die Uhr nach dem Boot
  per NTP nach vorne, werden höchstens die letzten 10 Minuten nachgeholt. Rücksprünge und die doppelte Stunde
  bei der Zeitumstellung lösen nichts doppelt aus.
- **Magic Packets und VPN:** Pusht ein VPN-Server `redirect-gateway def1`, gehören `0.0.0.0/1` und
  `128.0.0.0/1` zu `tun0`, und ein Broadcast an `255.255.255.255` würde im Tunnel landen. Deshalb bindet der
  Server den Socket an die Adresse des WLAN/LAN-Interfaces. Linux schickt einen Limited Broadcast dann über
  genau dieses Interface. Das Protokoll zeigt, über welches Interface gesendet wurde.
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
GET    /api/wifi                    POST /api/wifi/scan             POST /api/wifi/connect {ssid, password, hidden}
POST   /api/wifi/forget {ssid}      PUT  /api/wifi/hotspot {password}
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
| VPN verbindet nicht | `journalctl -u openvpn-client@wol -e`; Zugangsdaten in `/etc/openvpn/client/wol.auth` prüfen |
| VPN steht, GUI über VPN nicht erreichbar | Auf dem Server `client-to-client`/Routing prüfen; VPN-Subnetz in `allowed_networks`? |
| Pi zeitweise weg | `journalctl -t wlan-watchdog`; Signal unter *System* prüfen (schlechter als −75 dBm: Pi näher an den Router oder Repeater nutzen) |
| Hotspot erscheint nicht | `journalctl -u wol-wifi -e`; WLAN-Land gesetzt (`sudo raspi-config nonint do_wifi_country DE`)? `rfkill list` |
| Handy öffnet die Einrichtungsseite nicht | `http://10.42.0.1/` von Hand aufrufen; manche Handys trennen WLANs ohne Internet, dann „Verbunden bleiben“ wählen |
| *System → WLAN*: „nicht erreichbar“ | `systemctl status wol-wifi`; erneut einrichten mit `sudo ./deploy/hotspot-setup.sh` |
| Zeitpläne laufen zur falschen Zeit | Zeitzone setzen: `sudo timedatectl set-timezone Europe/Berlin` |

## Deinstallation

```bash
sudo ./deploy/uninstall.sh            # Programm entfernen, Daten behalten (OpenVPN-Client und WLANs bleiben)
sudo ./deploy/uninstall.sh --purge    # alles entfernen
```
