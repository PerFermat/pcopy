#!/usr/bin/env python3
"""
pcopy-gui – paralleler, wiederaufnehmbarer Datei-Kopierer mit Qt-Oberfläche (Startdatei).

  • Dateien/Ordner per „Dateien…“/„Ordner…“ oder per Drag & Drop aus Dolphin hinzufügen
  • auch während des Kopierens; Aufruf mit Pfaden (pcopy-gui DATEI…) übergibt sie an das
    bereits laufende Fenster (Dolphin-Kontextmenü)
  • mehrere Kopien gleichzeitig, Fortschritt pro Datei und gesamt, Tempo, Restzeit
  • Liste bleibt stehen oder läuft mit („Mitlaufen“: aktive Kopien in der Mitte)
  • vorhandene Zieldateien: überspringen, überschreiben oder nur durch neuere ersetzen
  • große Ordner werden im Hintergrund eingelesen: Liste füllt sich nach und nach,
    Kopieren beginnt schon mit den ersten gefundenen Dateien
  • kopiert erst nach manuellem „Start“ (auch beim Aufruf aus Dolphin)
  • Ziel wird pro Datei gemerkt; „Zielordner aktualisieren“ stellt wartende Dateien auf den
    aktuell eingestellten Zielordner um
  • Menüleiste: Datei, Kopieren, Liste (u. a. „Liste leeren“), Einstellungen, Hilfe
  • Tempo-Begrenzung (MB/s, gesamt), damit der Rechner nebenher flüssig bleibt
  • Pause/Abbruch/Absturz: wird beim nächsten Start exakt an der Byte-Position fortgesetzt

Der Programmcode liegt im Unterordner pcopy/, die Job-Liste (pcopy.db) neben dieser Datei.

Benötigt: Python 3.8+ und PyQt6 (sudo apt install python3-pyqt6) oder PySide6 (pip install pyside6)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from pcopy.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
