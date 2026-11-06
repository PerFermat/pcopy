"""Menüleiste und Kontextmenü der Tabelle."""
import os

from .qt import QtCore, QtGui, QtWidgets
from .db import DB_PATH
from .scanner import EXISTING

EXCLUDE_EXAMPLES = """# Beispiele – Raute (#) am Zeilenanfang entfernen, um ein Muster zu aktivieren:
# *.tmp
# *.part
# *.crdownload
# .~lock.*#
# Thumbs.db
# .DS_Store
# .thumbnails
# .cache
# Nextcloud-Hilfsdateien:
# .sync_*.db*
# .owncloudsync.log*
# .nextcloudsync.log*
"""


class MenuMixin:
    def _build_menu(self):
        act, mb = self._acts, self.menuBar()

        m = mb.addMenu("&Datei")
        m.addAction(self.a_files)
        m.addAction(self.a_folder)
        m.addSeparator()
        m.addAction(act("Zielordner wählen…", "folder-new", self.pick_target, None, "Ctrl+Shift+T"))
        m.addAction(act("Zielordner öffnen", "folder-open",
                        lambda: self.target.text() and QtGui.QDesktopServices.openUrl(
                            QtCore.QUrl.fromLocalFile(self.target.text()))))
        m.addSeparator()
        m.addAction(act("Bericht…", "document-properties", self.show_report,
                        "Zusammenfassung, Fehler und übersprungene Dateien – speicherbar als Text oder CSV", "Ctrl+B"))
        m.addSeparator()
        m.addAction(act("Beenden", "application-exit", self.close, None, "Ctrl+Q"))

        m = mb.addMenu("&Kopieren")
        m.addAction(self.a_start)
        m.addAction(self.a_pause)
        m.addSeparator()
        m.addAction(act("Fehlerhafte erneut versuchen", "view-refresh", self.retry_failed,
                        "Alle Dateien mit Fehler wieder in die Warteschlange stellen", "Ctrl+R"))

        m = mb.addMenu("&Liste")
        m.addAction(act("Ausgewählte entfernen", "list-remove", self.remove_selected,
                        "Markierte Einträge aus der Liste nehmen (laufende bleiben)", "Del"))
        m.addAction(act("Fertige entfernen", "edit-clear-list", self.clear_done,
                        "Fertige und übersprungene Einträge aus der Liste nehmen"))
        m.addAction(act("Fehlerhafte entfernen", "dialog-error", self.clear_failed))
        m.addSeparator()
        m.addAction(act("Als Nächstes kopieren", "go-top", lambda: self.prioritize(True),
                        "Markierte wartende Dateien vorziehen", "Ctrl+Up"))
        m.addAction(act("Ausgewählte prüfen", "document-preview", self.check_selected,
                        "Markierte fertige Dateien von der Platte lesen und mit der Quelle vergleichen",
                        "Ctrl+Shift+P"))
        m.addSeparator()
        m.addAction(act("Alles auswählen", "edit-select-all", self.table.selectAll, None, "Ctrl+A"))
        m.addSeparator()
        self.a_retarget = act("Zielordner aktualisieren", "folder-sync", self.retarget_selected,
                              "Markierte, noch wartende Dateien auf den oben eingestellten Zielordner umstellen",
                              "Ctrl+Shift+U")
        m.addAction(self.a_retarget)
        m.addAction(act("Vorhandene Dateien entscheiden…", "dialog-question", lambda: self.decide_conflicts(start=False),
                        "Dateien, die es im Ziel schon gibt: überspringen, überschreiben oder beide behalten"))
        m.addSeparator()
        m.addAction(act("Liste leeren…", "edit-clear-all", self.clear_all,
                        "Alle Einträge entfernen (laufendes Kopieren wird angehalten)", "Ctrl+Shift+Del"))

        m = mb.addMenu("&Einstellungen")
        self.verify = m.addAction("Nach dem Kopieren prüfen (SHA-256)")
        self.verify.setCheckable(True)
        self.verify.setStatusTip("Jede Datei nach dem Sichern von der Platte zurücklesen und mit der Quelle "
                                 "vergleichen – erst dann bekommt sie ihren richtigen Namen")
        self.verify.setChecked(self.settings.value("verify", "true") == "true")
        self.verify.toggled.connect(lambda on: setattr(self.engine, "verify", on))
        self.hdd_serial = m.addAction("Festplatten nacheinander (je Festplatte 1 Datei gleichzeitig)")
        self.hdd_serial.setCheckable(True)
        self.hdd_serial.setStatusTip("Drehende Festplatten (Quelle oder Ziel) werden nur von einer Kopie zur Zeit "
                                     "benutzt – sonst springt der Lesekopf und alles wird langsamer. SSDs nutzen "
                                     "weiter „Gleichzeitig“.")
        self.hdd_serial.setChecked(self.settings.value("hdd_serial", "true") == "true")
        self.hdd_serial.toggled.connect(lambda on: setattr(self.engine, "hdd_serial", on))
        self.gentle = m.addAction("Rücksicht auf andere Programme (niedrige Priorität, Cache schonen)")
        self.gentle.setCheckable(True)
        self.gentle.setStatusTip("Plattenzugriffe mit niedrigster Priorität, Prüfsummen mit niedriger CPU-Priorität; "
                                 "kopierte Daten werden aus dem Seiten-Cache entfernt")
        self.gentle.setChecked(self.settings.value("gentle", "true") == "true")
        self.gentle.toggled.connect(lambda on: setattr(self.engine, "gentle", on))
        m.addSeparator()
        self.follow = m.addAction("Liste mitlaufen lassen")
        self.follow.setCheckable(True)
        self.follow.setStatusTip("Liste so verschieben, dass die gerade kopierten Dateien in der Mitte stehen")
        self.follow.setChecked(self.settings.value("follow", "true") == "true")
        self.follow.toggled.connect(lambda on: on and self.shown_active.clear())
        m.addSeparator()
        m.addAction(act("Ausschließen…", "view-filter", self.edit_excludes,
                        "Muster für Dateien und Ordner, die nicht kopiert werden (z. B. *.tmp, .thumbnails)"))
        sub = m.addMenu("Vorhandene Dateien")
        grp = QtGui.QActionGroup(self)
        modes = []
        for i, (_, text) in enumerate(EXISTING):
            a = sub.addAction(text)
            a.setCheckable(True)
            grp.addAction(a)
            a.triggered.connect(lambda _=False, i=i: self.existing.setCurrentIndex(i))
            modes.append(a)
        modes[self.existing.currentIndex()].setChecked(True)
        self.existing.currentIndexChanged.connect(lambda i: modes[i].setChecked(True))
        m.addSeparator()
        self.build_font_menu(m)

        m = mb.addMenu("&Hilfe")
        m.addAction(act("Über pcopy", "help-about", lambda: QtWidgets.QMessageBox.about(
            self, "Über pcopy",
            "<b>pcopy</b> – paralleler, wiederaufnehmbarer Datei-Kopierer<br><br>"
            "Abbruch, Pause oder Absturz: beim nächsten Start geht es an derselben Stelle weiter.<br>"
            f"Liste: <code>{DB_PATH}</code>")))

    def edit_excludes(self):
        text = self.settings.value("exclude", "") or EXCLUDE_EXAMPLES
        new, ok = QtWidgets.QInputDialog.getMultiLineText(
            self, "Ausschließen",
            "Muster, die beim Hinzufügen übersprungen werden – eins pro Zeile, # = Kommentar.\n"
            "Ohne „/“: gilt für Datei- und Ordnernamen überall (*, ? als Platzhalter).\n"
            "Mit „/“: gilt für den Pfad im hinzugefügten Ordner, z. B. Fotos/Cache\n"
            "Ausgeschlossene Dateien werden beim Spiegeln im Ziel auch nicht gelöscht.", text)
        if ok:
            self.settings.setValue("exclude", new)
            n = len(self.exclude_patterns())
            self.status.showMessage(f"{n} Ausschlussmuster aktiv – gilt für Dateien, die ab jetzt hinzugefügt werden",
                                    8000)

    def context_menu(self, pos):
        ids = self.selected_ids()
        if not ids:
            return
        m = QtWidgets.QMenu(self)
        r = self.model.rows[self.model.idx[ids[0]]]
        a_open = m.addAction(QtGui.QIcon.fromTheme("folder-open"), "Zielordner öffnen")
        a_src = m.addAction(QtGui.QIcon.fromTheme("folder"), "Quellordner öffnen")
        m.addSeparator()
        t = self.target.text().strip()
        a_tgt = m.addAction(QtGui.QIcon.fromTheme("folder-sync"),
                            f"Zielordner aktualisieren → {t}" if t else "Zielordner aktualisieren …")
        a_tgt.setEnabled(any(self.model.rows[self.model.idx[i]]["status"] in ("pending", "conflict") for i in ids))
        sel = [self.model.rows[self.model.idx[i]] for i in ids]
        a_conf = m.addAction(QtGui.QIcon.fromTheme("dialog-question"), "Vorhandene Dateien entscheiden…")
        a_conf.setEnabled(any(r["status"] == "conflict" for r in sel))
        a_prio = m.addAction(QtGui.QIcon.fromTheme("go-top"), "Als Nächstes kopieren")
        a_prio.setEnabled(any(r["status"] == "pending" for r in sel))
        a_unprio = m.addAction(QtGui.QIcon.fromTheme("go-bottom"), "Vorrang entfernen")
        a_unprio.setEnabled(any(r["status"] == "pending" and r.get("prio") for r in sel))
        m.addSeparator()
        a_check = m.addAction(QtGui.QIcon.fromTheme("document-preview"), "Prüfen (von der Platte lesen und vergleichen)")
        a_check.setEnabled(any(self.model.rows[self.model.idx[i]]["status"] in ("done", "skipped") for i in ids))
        a_retry = m.addAction(QtGui.QIcon.fromTheme("view-refresh"), "Erneut versuchen")
        a_del = m.addAction(QtGui.QIcon.fromTheme("list-remove"), "Aus Liste entfernen")
        chosen = m.exec(self.table.viewport().mapToGlobal(pos))
        q = ",".join("?" * len(ids))
        if chosen == a_open:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(os.path.dirname(r["dst"])))
        elif chosen == a_src:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(os.path.dirname(r["src"])))
        elif chosen == a_prio:
            self.prioritize(True)
        elif chosen == a_unprio:
            self.prioritize(False)
        elif chosen == a_check:
            self.check_selected()
        elif chosen == a_retry:
            self.db.execute("UPDATE jobs SET status='pending', error=NULL, tries=0, retry_at=NULL "
                            f"WHERE status='failed' AND id IN ({q})", ids)
            self.model.load(self.db)
        elif chosen == a_del:
            self.remove_selected()
        elif chosen == a_conf:
            self.decide_conflicts([r["id"] for r in sel if r["status"] == "conflict"], start=False)
        elif chosen == a_tgt:
            self.retarget_selected()
