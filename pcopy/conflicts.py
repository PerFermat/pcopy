"""Vorhandene Zieldateien entscheiden (Einstellung „nachfragen“): pro Datei überspringen,
überschreiben oder beide behalten – vor dem Start, für einzelne oder markierte Dateien."""
import os
import time

from .qt import Qt, QtGui, QtWidgets
from .scanner import free_name
from .util import hsize

CHOICES = [("skip", "überspringen"), ("overwrite", "überschreiben"), ("rename", "beide behalten")]
TEXT = dict(CHOICES)


def apply(db, decisions):
    """decisions: {job-id: „skip“/„overwrite“/„rename“}. Nur Einträge, die noch „conflict“ sind.
    Rückgabe: Anzahl je Entscheidung."""
    count = dict.fromkeys(TEXT, 0)
    db.execute("BEGIN")
    try:
        for jid, choice in decisions.items():
            row = db.execute("SELECT dst,size FROM jobs WHERE id=? AND status='conflict'", (jid,)).fetchone()
            if row is None:
                continue
            dst, size = row
            if choice == "skip":
                db.execute("UPDATE jobs SET status='skipped', offset=? WHERE id=?", (size, jid))
            elif choice == "overwrite":
                db.execute("UPDATE jobs SET status='pending', offset=0 WHERE id=?", (jid,))
            else:
                new = free_name(dst, lambda p: db.execute("SELECT 1 FROM jobs WHERE dst=? LIMIT 1", (p,)).fetchone())
                db.execute("UPDATE jobs SET status='pending', offset=0, dst=? WHERE id=?", (new, jid))
            count[choice] += 1
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return count


def _stat(path):
    try:
        return os.lstat(path)
    except OSError:
        return None


def _desc(st):
    if st is None:
        return "fehlt"
    return f"{hsize(st.st_size)}, {time.strftime('%d.%m.%Y %H:%M', time.localtime(st.st_mtime))}"


class ConflictDialog(QtWidgets.QDialog):
    """Tabelle der Dateien, die es im Ziel schon gibt; Entscheidung gilt für die markierten Zeilen."""

    COLS = ["Datei", "Zielordner", "Quelle", "Ziel (vorhanden)", "Entscheidung"]

    def __init__(self, parent, db, ids=None, start=True):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Vorhandene Dateien")
        q = "SELECT id,src,dst FROM jobs WHERE status='conflict'"
        args = []
        if ids:
            q += f" AND id IN ({','.join('?' * len(ids))})"
            args = list(ids)
        self.rows = db.execute(q + " ORDER BY id", args).fetchall()
        self.choice = {}
        self.newer = {}           # job-id -> Quelle ist neuer als die Zieldatei

        lay = QtWidgets.QVBoxLayout(self)
        info = QtWidgets.QLabel(
            f"<b>{len(self.rows)} Dateien gibt es im Ziel schon</b> (mit anderer Größe oder Zeit).<br>"
            "Zeilen markieren und unten entscheiden. Die neuere Seite ist <b>fett</b>.<br>"
            "„Beide behalten“ kopiert unter neuem Namen, z. B. <i>Foto (2).jpg</i>.")
        lay.addWidget(info)

        self.table = QtWidgets.QTableWidget(len(self.rows), len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        bold = QtGui.QFont(self.table.font())
        bold.setBold(True)
        for i, (jid, src, dst) in enumerate(self.rows):
            ss, ds = _stat(src), _stat(dst)
            cells = [os.path.basename(src), os.path.dirname(dst), _desc(ss), _desc(ds), ""]
            for col, text in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(text)
                it.setToolTip(f"Quelle: {src}\nZiel:   {dst}")
                self.table.setItem(i, col, it)
            if ss and ds and abs(ss.st_mtime - ds.st_mtime) > 2:
                self.table.item(i, 2 if ss.st_mtime > ds.st_mtime else 3).setFont(bold)
            self.newer[jid] = bool(ss and ds and ss.st_mtime > ds.st_mtime + 2)
            self._set(i, "skip")
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.selectAll()
        lay.addWidget(self.table, 1)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Markierte:"))
        for key, text in CHOICES:
            b = QtWidgets.QPushButton(text.capitalize())
            b.clicked.connect(lambda _=False, k=key: self._apply_selected(k))
            row.addWidget(b)
        b = QtWidgets.QPushButton("Überschreiben, wenn neuer")
        b.setToolTip("Markierte Dateien überschreiben, wenn die Quelle neuer ist – sonst überspringen")
        b.clicked.connect(self._newer_selected)
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        bb = QtWidgets.QDialogButtonBox()
        ok = bb.addButton("Übernehmen und starten" if start else "Übernehmen",
                          QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setDefault(True)
        bb.addButton("Abbrechen", QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(1000, 520)

    def _set(self, i, key):
        self.choice[self.rows[i][0]] = key
        self.table.item(i, 4).setText(TEXT[key])

    def _selected(self):
        return sorted({ix.row() for ix in self.table.selectionModel().selectedRows()})

    def _apply_selected(self, key):
        for i in self._selected():
            self._set(i, key)

    def _newer_selected(self):
        for i in self._selected():
            self._set(i, "overwrite" if self.newer[self.rows[i][0]] else "skip")

    def decisions(self):
        return dict(self.choice)
