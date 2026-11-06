"""Dateien hinzufügen: Dialoge, Drag & Drop, Einlesen im Hintergrund."""
import os
import queue
from pathlib import Path

from .qt import QtWidgets
from .util import hsize


class AddMixin:
    def ensure_target(self):
        t = self.target.text().strip()
        if t and os.path.isdir(t):
            return t
        if t and not os.path.exists(t):
            if QtWidgets.QMessageBox.question(self, "Zielordner", f"„{t}“ existiert nicht. Anlegen?") \
                    == QtWidgets.QMessageBox.StandardButton.Yes:
                os.makedirs(t, exist_ok=True)
                return t
        return self.pick_target()

    def pick_target(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Zielordner wählen", self.target.text() or str(Path.home()))
        if d:
            self.target.setText(d)
            self.settings.setValue("target", d)
        return d or None

    def pick_files(self):
        start = self.settings.value("lastsrc", str(Path.home()))
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Dateien hinzufügen", start)
        if files:
            self.settings.setValue("lastsrc", os.path.dirname(files[0]))
            self.add_paths(files)

    def pick_folder(self):
        start = self.settings.value("lastsrc", str(Path.home()))
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Ordner hinzufügen", start)
        if d:
            self.settings.setValue("lastsrc", os.path.dirname(d))
            self.add_paths([d])

    def on_drop(self, urls):
        local = [u.toLocalFile() for u in urls if u.isLocalFile()]
        if len(local) < len(urls):
            self.status.showMessage(f"{len(urls) - len(local)} nicht-lokale Einträge ignoriert "
                                    "(Netzlaufwerk bitte einhängen)", 8000)
        if local:
            self.add_paths(local)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.on_drop(e.mimeData().urls())

    def add_paths(self, paths):
        target = self.ensure_target()
        if not target:
            return
        self.scan_nr += 1
        self.scans[self.scan_nr] = dict(seen=0, new=0)
        self.scanner.add(self.scan_nr, list(paths), target, self.existing.currentData(),
                         self.action.currentData(), self.exclude_patterns())
        self.tick()

    def drain_scanner(self):
        while True:
            try:
                gen, rid, kind, data = self.scanner.out.get_nowait()
            except queue.Empty:
                break
            if gen != self.scanner.gen or rid not in self.scans:
                continue                         # verworfen (Liste geleert)
            if kind == "rows":
                rows, self.scans[rid] = data
                self.model.append(rows)        # läuft gerade ein Kopiervorgang, reihen sie sich ein
                continue
            st = data
            del self.scans[rid]
            verb = "zum Verschieben" if self.action.currentData() == "move" else "hinzugefügt"
            msg = f"{st['new'] - st['skipped'] - st['delete']} Dateien ({hsize(st['total'])}) {verb}"
            if st["delete"]:
                msg += f", {st['delete']} Einträge im Ziel werden gelöscht (Spiegeln – vor dem Start prüfen!)"
            if st["excluded"]:
                msg += f", {st['excluded']} ausgeschlossen"
            if st["skipped"]:
                msg += f", {st['skipped']} übersprungen (im Ziel schon vorhanden)"
            if st.get("hardlinks"):
                msg += f", {st['hardlinks']} harte Links (werden im Ziel wieder verlinkt)"
            if st.get("renamed"):
                msg += f", {st['renamed']} unter neuem Namen (im Ziel gab es den Namen schon)"
            if st.get("conflict"):
                msg += f", {st['conflict']} gibt es im Ziel schon – Entscheidung beim Start"
            if st["dup"]:
                msg += f", {st['dup']} waren schon in der Liste"
            if st["error"]:
                msg += f" – Einlesen abgebrochen: {st['error']}"
            elif not self.engine.is_running() and st["new"] > st["skipped"]:
                msg += " – zum Kopieren „Start“ drücken"
            self.status.showMessage(msg, 10000)
            if st.get("conflict") and self.engine.is_running():
                self.decide_conflicts(start=False)     # läuft schon: gleich fragen, nicht erst beim nächsten Start
        if self.scans:
            seen = sum(v["seen"] for v in self.scans.values())
            self.scan_label.setText(f"Lese ein … {seen:,} Dateien gefunden".replace(",", "."))
        self.scan_label.setVisible(bool(self.scans))
