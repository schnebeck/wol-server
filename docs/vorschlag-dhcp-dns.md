# Vorschlag: DHCP und DNS im WOL-Server

**Deutsch** · [English](proposal-dhcp-dns.md)

Stand: 26.10.2026 · Status: **umgesetzt (Phasen 1–5), lokal getestet, noch nicht auf dem Pi in Betrieb**

> Bedienung und Umzug: README, Abschnitt 5. Abweichungen der Umsetzung von diesem Vorschlag:
> - Aufträge laufen über eine Warteschlange (`/var/lib/wol-wifi/dns/requests/`, `wol-dns-apply.path` mit
>   `DirectoryNotEmpty=`) statt einer einzelnen Auftragsdatei, ausgeführt in Python (`wolserver/dnsjob.py`).
> - Reservierungen stehen zusätzlich in `hosts.dns`, damit auch schlafende Geräte ohne Lease per Name auflösbar
>   sind (wichtig für Wake-on-LAN).
> - Die Herstellererkennung nutzt die Listen des Systems (`ieee-data`, nmap), es wird keine eigene mitgeliefert.
> - Freigeben und Ignorieren wirken sofort, unabhängig von einem offenen Entwurf.
> - Reservierungen, DNS-Einträge und Einstellungen wirken sofort (Einstellungen mit Vorschau der
>   Änderungen). Einen Entwurf gibt es nur nach Import, Sicherung laden oder Verlauf zurückholen.

Der WOL-Server übernimmt DHCP und DNS für das Heimnetz. Beides ist automatisch abgeglichen: Jedes Gerät,
das per DHCP eine Adresse bekommt, ist sofort unter seinem Namen erreichbar, auch rückwärts von der IP zum
Namen. Verwaltet wird alles in der Web-GUI des WOL-Servers, mindestens so komfortabel wie mit Webmin.

## 1. Ausgangslage

Heute erledigt das der Backup-Server `backup` (192.168.1.10, Debian 13) mit Webmin:

| Bereich | Bestand |
|---|---|
| DHCP | ISC dhcpd 4.4.3. ISC entwickelt ihn seit 2022 nicht mehr weiter. Ein Subnetz 192.168.1.0/24. |
| Zuordnungen | **46 feste Zuordnungen** (MAC → IP, Name). Der dynamische Bereich besteht nur aus .254, dazu `deny unknown-clients`: In der Praxis bekommen nur bekannte Geräte eine Adresse. |
| Lease-Zeit | 600 s (Standard), höchstens 7200 s |
| Optionen | Domäne `oldnet.local`, Router, DNS und NTP jeweils 192.168.1.1 |
| DDNS | `ddns-updates on` ist gesetzt, `ddns-update-style` aber auskommentiert: **faktisch aus** |
| DNS | BIND 9.20 nur als Weiterleiter, keine eigene Zone. Die Clients nutzen ohnehin den Router als DNS. **Kein Gerät ist unter seinem Namen auflösbar.** |

Der WOL-Server (Pi Zero W, 192.168.1.8) ist dort als `wol` reserviert.

## 2. Entscheidung: dnsmasq, gesteuert vom WOL-Server

| Option | Ergebnis |
|---|---|
| **dnsmasq und eigene GUI im WOL-Server** | **gewählt.** DHCP und DNS in einem Prozess, der Abgleich ist eingebaut. Klein (wenige MB RAM), läuft auf dem Zero, als `dnsmasq-base` schon installiert. Die GUI passt zum Projekt: Python ohne Zusatzpakete, eine Oberfläche für alles. |
| Technitium DNS | verworfen: .NET (nicht gewünscht, auf ARMv6 auch nicht lauffähig) |
| Kea + Kea-DDNS + PowerDNS (+ Stork, PowerDNS-Admin) | verworfen: fünf Dienste, zwei GUIs, für den Pi zu schwer. Standardkonformes DDNS per RFC 2136 wird im Privatnetz nicht gebraucht. |
| Pi-hole v6 | verworfen: DNS in der GUI nur A und CNAME, dazu ein Werbefilter als Hauptzweck |
| dhcpd und BIND auf .10 reparieren | verworfen: Die GUI bliebe Webmin, dhcpd hat keine Zukunft |

Nicht gebraucht und deshalb nicht vorgesehen: BIND-konforme Zonendateien, Zonentransfers, TSIG, DNSSEC für die
lokale Zone, DHCP-Failover, mehrere Scopes oder VLANs, DHCP-Relay.

## 3. Funktionsumfang

### Unverzichtbar (Phasen 1–3)

**DHCP: Grundeinstellungen**
- Netz, Gateway, DNS-Server (der WOL-Server selbst, optional ein zweiter), NTP-Server, Domäne bzw. Suchdomäne
- Lease-Zeit als Standard, **Vorgabe 12 h**. Auswahlliste (10 min, 1 h, 4 h, 12 h, 1 Tag, 7 Tage) oder frei
  in Minuten, Stunden oder Tagen (2 min bis 7 Tage). Pro Reservierung abweichend einstellbar, z. B. kurz für
  Geräte, die oft umziehen.
- Modus **„Nur bekannte Geräte“** (wie heute) oder **„Adressbereich für unbekannte Geräte“** mit Start und Ende

**DHCP: Reservierungen**
- MAC → IP → Name, mit Kommentar, aktiv/inaktiv und optional eigener Lease-Zeit
- Prüfung beim Eingeben: doppelte MAC, IP oder Name; IP außerhalb des Netzes oder im dynamischen Bereich; ungültiger Name
- Tabelle mit Suche, Sortierung und Filter (aktiv, mit WOL-Gerät, zuletzt gesehen)

**DHCP: unbekannte Geräte schnell freigeben**
- Im Modus „nur bekannte Geräte“ sammelt der Server abgewiesene Anfragen: MAC, vom Gerät gemeldeter Name,
  Hersteller, Zeitpunkt, Anzahl der Versuche.
- Hinweis in der Übersicht. **Freigeben mit einem Klick:** Name ist vorbelegt (bereinigt), die nächste freie IP ist
  vorgeschlagen, ein Klick auf *Übernehmen* genügt. Das Gerät bekommt seine Adresse beim nächsten Versuch,
  meist innerhalb weniger Sekunden.
- *Ignorieren* blendet eine MAC dauerhaft aus, z. B. für Nachbargeräte oder Besuch.
- Hilft vor allem bei Handys mit privater, wechselnder WLAN-Adresse.

**DHCP: aktuelle Leases**
- Name (vom Gerät gemeldet), IP, MAC, Hersteller, Ablauf, Art (reserviert oder dynamisch)
- Aktionen: als Reservierung übernehmen, als WOL-Gerät übernehmen, Lease löschen

**DNS**
- Lokale Domäne. Leases und Reservierungen werden automatisch als `name.domäne` eingetragen, inklusive Reverse-Auflösung, und verschwinden beim Ablauf der Lease.
- Namensregel: Der Name aus der Reservierung schlägt den, den das Gerät meldet. Ein schon vergebener Name wird nicht überschrieben. Gemeldete Namen werden bereinigt (`Anna's iPhone` → `annas-iphone`).
- Eigene Einträge für Geräte ohne DHCP (Name → IP) und Aliasse (Name → Name)
- Upstream-DNS (Router, Provider oder öffentlich). Die eigene Domäne und private Reverse-Zonen gehen nie ins Internet.

### Sinnvoll (Phase 4)

- **Zweiter DHCP-Server:** Vor dem Einschalten und danach regelmäßig fragt der Server per DHCPDISCOVER nach anderen DHCP-Servern und warnt, falls einer antwortet.
- **Bedingte Weiterleitung:** eine Domäne an einen anderen DNS-Server, z. B. Namen im VPN an den VPN-Server.
- **Export und Import** als JSON, dazu der Import aus `dhcpd.conf`. Einmal für den Umzug, danach für Backups.
- **Änderungsprotokoll:** wer, wann, was. Steht im bestehenden Protokoll der GUI.

### Später, falls gewünscht

Herstellererkennung über eine Offline-Liste der MAC-Präfixe, „zuletzt gesehen“, DNS-Statistik und ein Abfrageprotokoll
zur Fehlersuche, Netzwerkstart per PXE/TFTP.

## 4. Oberfläche

Neuer Reiter **Netzwerk** mit vier Bereichen. Änderungen werden wie bei pfSense oder OPNsense erst gesammelt und
dann gemeinsam übernommen. So lassen sich mehrere Reservierungen in einem Rutsch ändern, und ein Fehler legt nicht
mitten in der Arbeit das Netz lahm.

```
┌ WOL-Server ─ Geräte · Zeitpläne · Protokoll · Netzwerk · System ───────────────────┐
│ ⚠ 3 Änderungen noch nicht übernommen               [Verwerfen] [Übernehmen]        │
│                                                                                    │
│ Übersicht │ Reservierungen │ Leases │ DNS │ Einstellungen                          │
│ ────────────────────────────────────────────────────────────────────────────────── │
│ DHCP  ● aktiv · nur bekannte Geräte · 41 Leases       DNS  ● aktiv · home.arpa       │
│ ⚠ Unbekanntes Gerät: 5a:1f:…  „iPhone“ · Apple · vor 2 min  [Freigeben] [Ignorieren]│
└────────────────────────────────────────────────────────────────────────────────────┘
```

**Reservierungen**

```
 Suche: [office__________]   Filter: (●) alle  ( ) aktiv  ( ) mit WOL     [+ Reservierung]
 ┌──────────────────┬───────────────┬───────────────────┬─────────┬──────────┬─────┐
 │ Name             │ IP            │ MAC               │ Lease   │ Gesehen  │     │
 ├──────────────────┼───────────────┼───────────────────┼─────────┼──────────┼─────┤
 │ office-pc  ⏻     │ 192.168.1.9   │ 00:d8:61:d7:1d:36 │ Standard│ vor 3 min│ ✎ ✕ │
 │ brother-mfc      │ 192.168.1.40  │ 94:dd:f8:bd:83:30 │ Standard│ heute    │ ✎ ✕ │
 │ repeater-lan     │ 192.168.1.2   │ d2:ce:1e:f7:77:47 │ 24 h    │ —        │ ✎ ✕ │
 └──────────────────┴───────────────┴───────────────────┴─────────┴──────────┴─────┘
 ⏻ = auch WOL-Gerät (Wecken direkt aus der Liste)
```

**Freigeben (Dialog):**

```
 Unbekanntes Gerät freigeben
 MAC       5a:1f:3c:…  (private WLAN-Adresse, Apple)    · 4 Versuche seit 21:14
 Name      [annas-iphone_____]   ← vom Gerät gemeldet: „Anna's iPhone“
 IP        [192.168.1.41_______]   ← nächste freie Adresse
 Kommentar [_____________________]
                                   [Abbrechen]  [Freigeben und übernehmen]
```

**Leases:** Tabelle wie oben, zusätzlich mit Restlaufzeit und den Aktionen *Reservieren*, *Als WOL-Gerät* und
*Löschen*.

**DNS:** Domäne, Upstream-Server, bedingte Weiterleitungen, eigene Einträge und Aliasse. Daneben eine
**Testabfrage**: Name oder IP eingeben, die Antwort des eigenen DNS sehen.

**Einstellungen:** Modus (nur bekannte Geräte oder Adressbereich), Lease-Zeit, Gateway, DNS, NTP, Domäne,
Dienst an/aus, Export und Import.

**Verbindung zu den WOL-Geräten:** Beide Listen bleiben getrennt und sind über die MAC verknüpft. Im
Geräte-Dialog gibt es „Feste IP reservieren“. In der Reservierungsliste kennzeichnet ⏻ WOL-Geräte und
erlaubt das Wecken. Ändert sich der Name einer Reservierung, bleibt das WOL-Gerät unberührt.

## 5. Technischer Aufbau

### Prozesse und Rechte

Wie beim VPN: Die GUI läuft weiter ohne Rechte, die Arbeit als root erledigen kleine, getrennte Teile.

```
 Browser ──HTTP──▶ wol-server (Benutzer wol)
                    │  Entwurf bearbeiten, Leases anzeigen
                    ▼ Unix-Socket
                   wol-wifi (root, ohne Capabilities)          → künftig besser „wol-system“
                    │  prüft den Entwurf streng, erzeugt die dnsmasq-Dateien in einen Zwischenstand,
                    │  schreibt einen Auftrag
                    ▼ wol-dns-apply.path
                   wol-dns-apply.service (root, oneshot)
                    │  dnsmasq --test mit dem Zwischenstand → übernehmen → SIGHUP oder Neustart
                    │  → Selbsttest (DNS-Abfrage an 127.0.0.1) → bei Fehler alten Stand zurück
                    ▼
                   wol-dns.service = dnsmasq (startet als root, läuft danach als Benutzer dnsmasq)
```

- **Kein Freitext bis zu dnsmasq.** Die GUI übergibt nur strukturierte Daten (JSON). Die Konfiguration erzeugt
  ausschließlich der Root-Dienst, und zwar Zeile für Zeile aus geprüften Feldern. Optionen wie `dhcp-script` oder
  `conf-file`, die Programme starten oder Dateien einbinden, lassen sich so nicht einschleusen. Das ist dieselbe
  Lehre wie bei der VPN-Prüfung.
- **Eigene Unit statt Debian-Paket `dnsmasq`.** Wir nutzen das vorhandene `dnsmasq-base` mit eigener Unit. Das
  Paket `dnsmasq` würde einen zweiten, automatisch startenden Dienst mitbringen.
- **Reservierungen ohne Neustart.** dnsmasq liest `dhcp-hostsfile` und `addn-hosts` bei SIGHUP neu ein. Neue oder
  geänderte Reservierungen und eigene DNS-Einträge greifen deshalb sofort, ohne Unterbrechung. Nur Änderungen an
  den Grundeinstellungen (Bereich, Lease-Zeit, Domäne) brauchen einen Neustart. Die Leases bleiben dabei in der
  Lease-Datei erhalten.

### Dateien

| Datei | Inhalt | Wer schreibt |
|---|---|---|
| `/var/lib/wol-wifi/dns/network.json` | übernommener Stand (Quelle der Wahrheit) | wol-wifi |
| `/var/lib/wol-wifi/dns/draft.json` | noch nicht übernommene Änderungen | wol-wifi |
| `/etc/wol-dns/dnsmasq.conf` | Grundeinstellungen | wol-dns-apply |
| `/etc/wol-dns/hosts.dhcp` | Reservierungen (`dhcp-hostsfile`) | wol-dns-apply |
| `/etc/wol-dns/hosts.dns` | eigene Einträge (`addn-hosts`) | wol-dns-apply |
| `/var/lib/misc/dnsmasq.leases` | aktuelle Leases | dnsmasq |
| `/var/lib/wol-wifi/dns/history/` | die letzten 10 übernommenen Stände für den Rückweg | wol-wifi |

### Datenmodell (`network.json`)

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

### Prüfregeln (im Root-Dienst, nicht nur in der GUI)

- MAC: sechs Hex-Paare, keine Multicast-Adresse; IP: im Netz, nicht Netz- oder Broadcast-Adresse, nicht im dynamischen Bereich
- Namen nach RFC 1123: `a–z`, `0–9`, `-`, höchstens 63 Zeichen, nicht mit `-` am Anfang oder Ende; Großbuchstaben werden klein geschrieben. **37 der 46 Namen auf .10 brauchen das** (z. B. `LinuxCNC-Alfa` → `linuxcnc-alfa`, Unterstriche → `-`).
- Eindeutigkeit von MAC, IP und Name über Reservierungen, eigene Einträge und den WOL-Server selbst
- Lease-Zeit 2 min bis 7 Tage; Domäne als gültiger DNS-Name; Upstream-Server als IP-Adressen
- Grenzen: höchstens 500 Reservierungen, 200 eigene Einträge

### Erzeugte dnsmasq-Konfiguration (Beispiel)

```
# erzeugt vom WOL-Server – nicht von Hand bearbeiten
interface=wlan0
bind-dynamic                      # kein Konflikt mit dem Hotspot-dnsmasq auf 10.42.0.1
listen-address=192.168.1.8
domain-needed
bogus-priv                        # private Reverse-Anfragen nicht ins Internet
no-resolv
server=192.168.1.1
server=/vpn.example.org/10.1.1.1
local=/home.arpa/
domain=home.arpa
expand-hosts
local-ttl=60
dhcp-authoritative
dhcp-range=192.168.1.0,static,255.255.255.0,12h     # Modus „nur bekannte Geräte“
dhcp-ignore=tag:!known
dhcp-option=option:router,192.168.1.1
dhcp-option=option:dns-server,192.168.1.8,192.168.1.1
dhcp-option=option:ntp-server,192.168.1.1
dhcp-hostsfile=/etc/wol-dns/hosts.dhcp
addn-hosts=/etc/wol-dns/hosts.dns
dhcp-leasefile=/var/lib/misc/dnsmasq.leases
log-dhcp
```

Die abgewiesenen Anfragen unbekannter Geräte liest der Root-Dienst aus dem Journal von `wol-dns`
(`DHCPDISCOVER … ignored`) und stellt sie der GUI bereit.

### Voraussetzung: feste IP für den WOL-Server

Ein DHCP-Server kann seine Adresse nicht von sich selbst holen. Das WLAN-Profil des Pi wird deshalb auf
`ipv4.method manual` mit 192.168.1.8/24, Gateway .1 und DNS 127.0.0.1 umgestellt. Der Einrichtungs-Hotspot
bleibt unverändert. Läuft er, ist der Pi ohnehin nicht im Heimnetz, und `bind-dynamic` sorgt dafür, dass
dnsmasq dann auf 10.42.0.1 nicht stört.

## 6. Umzug von .10

Die kurze Lease-Zeit von 10 Minuten auf .10 hilft hier: Alle Geräte erneuern spätestens nach 5 Minuten und
fallen nach 8¾ Minuten auf eine Rundfrage zurück. Dann antwortet der neue Server.

1. **Vorbereitung:** feste IP für den WOL-Server (siehe oben). Import der `dhcpd.conf` von .10 über die GUI
   (Datei hochladen). Die Vorschau zeigt jede Reservierung mit bereinigtem Namen und markiert Konflikte.
   Übernehmen, DHCP bleibt dabei noch **aus**.
2. **DNS testen:** dnsmasq läuft nur als DNS. Auf dem Laptop prüfen: `dig @192.168.1.8 office-pc.home.arpa`
   und `dig @192.168.1.8 -x 192.168.1.9`. Die Clients merken davon nichts.
3. **Umschalten:** Auf .10 `systemctl disable --now isc-dhcp-server`, im WOL-Server DHCP einschalten. Der
   Assistent prüft vorher per DHCPDISCOVER, dass kein anderer DHCP-Server mehr antwortet, und schaltet sonst
   nicht ein.
4. **Beobachten:** In den nächsten 10–15 Minuten tauchen alle aktiven Geräte unter *Leases* auf, mit derselben
   IP wie bisher, jetzt aber mit DNS-Namen und mit dem WOL-Server als DNS.
5. **Rückweg** (jederzeit möglich): DHCP im WOL-Server aus, auf .10 `systemctl enable --now isc-dhcp-server`.
   Nach spätestens 10 Minuten läuft alles wieder wie vorher.
6. **Aufräumen** nach ein paar Tagen ohne Probleme: Lease-Zeit auf 12 h erhöhen. Auf .10 auch BIND abschalten
   (`systemctl disable --now named`). dhcpd und BIND bleiben **installiert, aber deaktiviert** als Reserve für den
   Rückweg.

## 7. Risiken

| Risiko | Gegenmaßnahme |
|---|---|
| **Single Point of Failure:** Fällt der WOL-Server aus, bekommen Geräte keine Adresse mehr, und die Namensauflösung fällt aus. | Lease-Zeit 12 h statt 10 min. Router als zweiter DNS. Rückweg über .10 dokumentiert und in 10 Minuten wirksam. Hardware-Watchdog ist aktiv. |
| **Hardware:** Der Zero W war heute instabil (Kernel-Oops mit Verdacht auf Stromversorgung). Als Infrastruktur fürs ganze Netz ist er der schwächste Kandidat. | Erster Test bewusst auf dem Zero W. Vor dem Umschalten von DHCP (Phase 3) mehrere Tage Stabilität mit dem neuen Netzteil abwarten. Reicht er nicht: **Pi Zero 2 W** (gleiche Bauform, gleiches Image, etwa fünfmal schneller) oder Pi 2 per Kabel am `repeaterLAN`. Der Rückweg über .10 bleibt jederzeit offen. |
| WLAN: DHCP und DNS fürs ganze Netz über Funk | Im Heimnetz unkritisch, der Router hängt ohnehin am WLAN. DNS-Antworten aus dem Cache sind schneller als heute über den Router. |
| Zwei DHCP-Server gleichzeitig | Prüfung per DHCPDISCOVER vor dem Einschalten und danach regelmäßig, Warnung in der GUI |
| Handys mit privater WLAN-Adresse fallen im Modus „nur bekannte Geräte“ raus | Liste der abgewiesenen Geräte mit „Freigeben“; Hinweis in der GUI, die private Adresse im Heim-WLAN abzuschalten |
| SD-Karte: dnsmasq schreibt die Lease-Datei bei jeder Änderung | Bei 12 h Lease-Zeit und rund 50 Geräten sind das wenige Schreibzugriffe am Tag |
| Domäne `.local` kollidiert mit mDNS | entschieden: `home.arpa` (Abschnitt 8) |
| IPv6 | bleibt beim Router (RA). Namen gibt es zunächst nur für IPv4. |

## 8. Entscheidungen

| Frage | Entscheidung |
|---|---|
| Domäne | **`home.arpa`.** Eindeutig, keine Kollision mit mDNS, `wol.local` funktioniert weiter. `oldnet.local` kann für den Übergang als zusätzliche Suchdomäne verteilt werden. |
| Hardware | **Erster Test auf dem Pi Zero W.** Wechsel auf Zero 2 W oder Pi 2, falls er nicht stabil genug ist. |
| Modus | **„Nur bekannte Geräte“ wie bisher**, mit schneller Freigabe unbekannter Geräte direkt aus der GUI (Abschnitt 3) |
| .10 nach dem Umzug | **dhcpd und BIND deaktiviert, aber installiert** als Reserve für den Rückweg |
| Lease-Zeit | **12 h nach dem Umzug, in der GUI auswählbar oder frei einstellbar**, auch pro Reservierung. Während des Umzugs bleiben es 10 min, damit Umschalten und Rückweg schnell wirken. |
| Upstream-DNS | **Weiter der Router (192.168.1.1).** Für Internet-Namen ändert sich nichts gegenüber heute. |

Damit sind alle Grundsatzfragen geklärt.

## 9. Phasen

Jede Phase ist für sich nutzbar und getestet (Unit-Tests wie bisher, dazu ein Lauf auf dem Pi), bevor die
nächste beginnt. Die Größen sind grobe Einschätzungen.

| Phase | Inhalt | Größe |
|---|---|---|
| 1 | dnsmasq-Unit, Übernahme mit Prüfen und Rückweg, Datenmodell, **DNS**-Bereich (Domäne, Upstream, eigene Einträge, Testabfrage). DHCP aus. | groß |
| 2 | **Reservierungen** mit Tabelle, Dialog und Prüfung; **Import aus `dhcpd.conf`** mit Vorschau; Verknüpfung mit den WOL-Geräten | groß |
| 3 | **DHCP** einschalten: Einstellungen, Leases, **unbekannte Geräte schnell freigeben**, feste IP für den Pi, Prüfung auf einen zweiten DHCP-Server, Umzugsassistent | groß |
| 4 | Bedingte Weiterleitung, Export/Import, Änderungsprotokoll | mittel |
| 5 (optional) | Herstellererkennung, „zuletzt gesehen“, Statistik, PXE | klein bis mittel |

Nach Phase 1 lässt sich der neue DNS schon parallel nutzen und prüfen, ohne dass sich im Netz etwas ändert.
Erst Phase 3 übernimmt DHCP von .10.
