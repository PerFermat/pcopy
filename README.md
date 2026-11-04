# pcopy

Paralleler, wiederaufnehmbarer Datei-Kopierer mit Qt-Oberfläche für Linux (KDE/Dolphin).
Gedacht für große Kopieraktionen – z. B. zehntausende Fotos auf eine USB-Platte –, die auch nach
Pause, Absturz oder abgezogener Platte exakt an der richtigen Stelle weitergehen.

![pcopy beim Kopieren](docs/screenshot.png)

## Funktionen

- **Hinzufügen** per „Dateien…“/„Ordner…“, Drag & Drop aus Dolphin oder Rechtsklick in Dolphin
  → „Mit pcopy kopieren“. Große Ordner werden im Hintergrund eingelesen; die Liste füllt sich
  nach und nach.
- **Mehrere Kopien gleichzeitig**, Fortschritt pro Datei und gesamt, Tempo und Restzeit;
  fertige Dateien zeigen ihr Durchschnittstempo (Ø).
- **Geprüft:** Jede Datei wird nach dem Sichern von der Platte zurückgelesen (am Seiten-Cache
  vorbei) und per SHA-256 mit der Quelle verglichen. Die Prüfsumme der Quelle entsteht beim
  Kopieren, die Quelle wird dafür nicht zusätzlich gelesen. Abschaltbar unter Einstellungen.
- **Nachträglich prüfen:** Rechtsklick auf fertige oder übersprungene Dateien → „Prüfen“ liest
  das Ziel erneut von der Platte und vergleicht es mit der gespeicherten Prüfsumme bzw. mit der
  Quelle. Abweichende oder fehlende Dateien werden als Fehler markiert und lassen sich mit
  „Erneut versuchen“ neu kopieren.
- **Nie halbe Dateien unter echtem Namen:** Kopiert wird in eine versteckte Hilfsdatei
  `.NAME.pcopy-part`; erst wenn sie vollständig, gesichert und geprüft ist, wird sie in einem
  Schritt umbenannt. Eine Zieldatei, die ersetzt wird, bleibt bis dahin unverändert.
- **Prüfung vor dem Start:** freier Platz je Ziel-Laufwerk, Dateien ab 4 GB auf FAT32 und
  Zeichen, die FAT32/exFAT in Namen nicht erlauben (`< > : " \ | ? *`) sowie symbolische Links,
  die FAT32/exFAT nicht speichern können. Betroffene Dateien
  lassen sich überspringen, der Rest wird kopiert.
- **Vollständige Ordner:** leere Ordner werden mitkopiert, symbolische Links als Links (auch
  Links auf Ordner und Links ins Leere), und am Ende übernimmt pcopy Zeitstempel und Rechte der
  Ordner – von innen nach außen, damit das Anlegen der Dateien sie nicht wieder verändert.
- **Automatisch wiederholen:** Bei vorübergehenden Fehlern (E/A-Fehler, Gerät kurz weg,
  Zeitüberschreitung, fehlgeschlagene Prüfung) bis zu 3 weitere Versuche nach 10, 30 und 60 s –
  ab dem zuletzt gesicherten Stand. Dauerhafte Fehler (Datei fehlt, keine Berechtigung, Platte
  voll) werden sofort gemeldet.
- **Bericht** (Datei → Bericht…, Strg+B): Zusammenfassung, alle Fehler mit Grund und die
  übersprungenen Dateien; speicherbar als Text oder CSV. Öffnet sich am Ende von selbst, wenn
  neue Fehler aufgetreten sind.
- **Wiederaufnehmbar:** Pause, Beenden oder Absturz – beim nächsten Start geht es an der
  gesicherten Byte-Position weiter. Das Fenster lässt sich auch schließen, wenn die Zielplatte
  hängt.
- **Schnell bei vielen kleinen Dateien:** Dateien unter 8 MB werden gesammelt per `syncfs`
  gesichert statt einzeln. Was beim Beenden noch nicht sicher auf der Platte war, wird beim
  nächsten Start automatisch neu kopiert.
- **Kopiert erst nach „Start“** – auch beim Aufruf aus Dolphin. Ist alles fertig, hält pcopy an;
  neu hinzugefügte Dateien warten wieder auf „Start“.
- **Tempo-Begrenzung** (MB/s, gesamt), damit der Rechner nebenher flüssig bleibt.
- **Vorhandene Zieldateien:** überspringen, überschreiben oder nur durch neuere ersetzen.
- **Ziel pro Datei:** Ein neuer Zielordner gilt für danach hinzugefügte Dateien; per Rechtsklick
  → „Zielordner aktualisieren“ lassen sich wartende Dateien auf das aktuelle Ziel umstellen.
- **Übersichtliche Liste:** optional mitlaufend; Kopien, die länger als 5 s brauchen, rücken
  zwischen fertige und wartende Dateien, damit große Dateien nicht untergehen.
- **Einstellbare Schriftgröße** (Strg++ / Strg+- / Strg+0).

## Voraussetzungen

- Linux, Python 3.8+
- PyQt6 (`sudo apt install python3-pyqt6`) oder PySide6 (`pip install pyside6`)

## Installation

```bash
git clone https://github.com/PerFermat/pcopy.git
cd pcopy
./install-pcopy.sh
```

Das Skript kopiert nichts, sondern legt nur Verweise auf den Programmordner an:
Symbol auf dem Schreibtisch, Startmenü-Eintrag, Dolphin-Kontextmenü „Mit pcopy kopieren“ und
den Befehl `pcopy-gui`. Änderungen am Programm (z. B. nach `git pull`) wirken sofort.

Starten lässt sich pcopy auch direkt:

```bash
./pcopy-gui.py [DATEI|ORDNER …]
```

Läuft schon ein Fenster, werden die Pfade dorthin übergeben.

## Daten

Die Job-Liste liegt als SQLite-Datenbank `pcopy.db` **neben dem Programm**. Einstellungen
(Fenstergröße, Zielordner, Tempo-Limit, Schriftgröße …) speichert Qt unter
`~/.config/pcopy/gui.conf`.

## Aufbau

| Datei | Inhalt |
|---|---|
| `pcopy-gui.py` | Startdatei |
| `pcopy/app.py` | Programmstart, Einzelinstanz, Übergabe von Pfaden |
| `pcopy/window.py` | Hauptfenster, Start/Pause, Anzeige, Beenden |
| `pcopy/menus.py` | Menüleiste und Kontextmenü |
| `pcopy/adding.py` | Dateien hinzufügen, Drag & Drop |
| `pcopy/listops.py` | Entfernen, Leeren, erneut versuchen, Ziel umstellen |
| `pcopy/engine.py` | Kopier-Threads, Tempo-Limit, Sichern, Prüfen, Hilfsdateien |
| `pcopy/precheck.py` | Prüfung vor dem Start (Platz, FAT32-Grenzen) |
| `pcopy/checker.py` | Nachträgliches Prüfen fertiger Dateien |
| `pcopy/report.py` | Bericht (Anzeige, Text, CSV) |
| `pcopy/scanner.py` | Einlesen im Hintergrund, Regeln für vorhandene Dateien |
| `pcopy/model.py` | Tabellenmodell und Fortschrittsbalken |
| `pcopy/db.py` | Datenbank |
| `pcopy/fontsize.py` | Schriftgröße |
| `pcopy/util.py`, `pcopy/qt.py` | Hilfsfunktionen, Qt-Einbindung |

## Lizenz

GNU General Public License v3.0 – siehe [LICENSE](LICENSE).
