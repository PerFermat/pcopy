"""Bericht über die Liste: Zusammenfassung, Fehler, übersprungene Dateien; speicherbar als Text/CSV."""
import csv
import os
import time

from .qt import QtGui, QtWidgets
from .util import hsize

SHOW_MAX = 500            # so viele Fehler/Übersprungene zeigt das Fenster (die Datei enthält alle)


def collect(db):
    rows = db.execute("SELECT status,src,dst,size,hash,error,tries,kind,action FROM jobs ORDER BY id").fetchall()
    s = dict(verified=[0, 0], unverified=[0, 0], skipped=[0, 0], skipped_ok=0, failed=[], open=[0, 0],
             moved=0, deleted=0, notes=[])
    skipped = []
    for status, src, dst, size, h, err, tries, kind, action in rows:
        size = size or 0
        if status == "done" and kind == "delete":
            s["deleted"] += 1
            continue
        if status == "done":
            s["moved"] += action == "move"
            if err:
                s["notes"].append((dst, err))
            k = "verified" if h else "unverified"
            s[k][0] += 1
            s[k][1] += size
        elif status == "skipped":
            s["skipped"][0] += 1
            s["skipped"][1] += size
            s["skipped_ok"] += bool(h)
            skipped.append(dst)
        elif status == "failed":
            s["failed"].append((dst, err or "unbekannter Fehler", src))
        else:
            s["open"][0] += 1
            s["open"][1] += size
    return rows, s, skipped


def as_text(db, limit=None):
    rows, s, skipped = collect(db)
    out = [f"pcopy – Bericht vom {time.strftime('%d.%m.%Y %H:%M')}", "",
           f"Einträge in der Liste:      {len(rows)}",
           f"  geprüft (SHA-256):        {s['verified'][0]:>7}  ({hsize(s['verified'][1])})",
           f"  fertig, nicht geprüft:    {s['unverified'][0]:>7}  ({hsize(s['unverified'][1])})",
           f"  übersprungen (vorhanden): {s['skipped'][0]:>7}  ({hsize(s['skipped'][1])})"
           + (f", davon {s['skipped_ok']} geprüft" if s["skipped_ok"] else ""),
           f"  davon verschoben:         {s['moved']:>7}",
           f"  im Ziel gelöscht (Spiegeln, Papierkorb): {s['deleted']}",
           f"  Fehler:                   {len(s['failed']):>7}",
           f"  noch offen:               {s['open'][0]:>7}  ({hsize(s['open'][1])})", ""]
    if s["failed"]:
        out.append(f"Fehler ({len(s['failed'])}):")
        for dst, err, src in s["failed"][:limit]:
            out.append(f"  {dst}\n      {err}\n      Quelle: {src}")
        if limit and len(s["failed"]) > limit:
            out.append(f"  … und {len(s['failed']) - limit} weitere (vollständig in der gespeicherten Datei)")
        out.append("")
    if s["notes"]:
        out.append(f"Hinweise ({len(s['notes'])}):")
        out += [f"  {d}\n      {e}" for d, e in s["notes"][:limit]]
        out.append("")
    if skipped:
        out.append(f"Übersprungen, weil im Ziel schon vorhanden ({len(skipped)}):")
        out += [f"  {d}" for d in skipped[:limit]]
        if limit and len(skipped) > limit:
            out.append(f"  … und {len(skipped) - limit} weitere")
    return "\n".join(out) + "\n", s


def save_csv(db, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Status", "Art", "Aktion", "Quelle", "Ziel", "Größe (Bytes)", "SHA-256", "Fehler", "Wiederholungen"])
        for status, src, dst, size, h, err, tries, kind, action in collect(db)[0]:
            w.writerow([status, kind or "file", action or "copy", src, dst, size, h or "", err or "", tries or 0])


class ReportDialog(QtWidgets.QDialog):
    def __init__(self, parent, db, start_dir):
        super().__init__(parent)
        self.db, self.start_dir = db, start_dir
        self.setWindowTitle("pcopy – Bericht")
        self.resize(900, 560)
        lay = QtWidgets.QVBoxLayout(self)
        self.text = QtWidgets.QPlainTextEdit(readOnly=True)
        self.text.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        lay.addWidget(self.text, 1)
        row = QtWidgets.QHBoxLayout()
        for label, slot in (("Als Text speichern…", self.save_txt), ("Als CSV speichern…", self.save_csv),
                            ("Aktualisieren", self.refresh)):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        close = QtWidgets.QPushButton("Schließen")
        close.clicked.connect(self.close)
        row.addWidget(close)
        lay.addLayout(row)
        self.refresh()

    def refresh(self):
        self.text.setPlainText(as_text(self.db, limit=SHOW_MAX)[0])

    def _ask(self, ext, kind):
        name = os.path.join(self.start_dir, f"pcopy-bericht-{time.strftime('%Y-%m-%d_%H%M')}.{ext}")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Bericht speichern", name, kind)
        return path

    def save_txt(self):
        path = self._ask("txt", "Text (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(as_text(self.db)[0])

    def save_csv(self):
        path = self._ask("csv", "CSV (*.csv)")
        if path:
            save_csv(self.db, path)
