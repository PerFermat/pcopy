"""Tabellenmodell, Fortschrittsbalken und Drop-Tabelle."""
import os

from .qt import Qt, QtCore, QtGui, QtWidgets
from .util import hsize, htime

COLS = ["Datei", "Zielordner", "Größe", "Fortschritt", "Status", "Tempo", "Rest"]
STATUS_TXT = {"pending": "wartend", "running": "kopiert …", "done": "fertig", "failed": "Fehler",
              "skipped": "übersprungen", "written": "sichern …", "verifying": "prüft …",
              "checking": "prüft …", "check_wait": "Prüfung wartet"}
FINISHED = ("done", "skipped")
CHECKING = ("check_wait", "checking")  # nachträgliche Prüfung fertiger Dateien (Rechtsklick → Prüfen)
COPIED = FINISHED + ("written",) + CHECKING   # Daten vollständig im Ziel (written: noch nicht gesynct)


class JobModel(QtCore.QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.idx = {}
        self.live = {}            # id -> (kopiert, tempo)

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return len(COLS)

    def headerData(self, s, o, role=Qt.ItemDataRole.DisplayRole):
        if o == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLS[s]

    def load(self, c):
        self.beginResetModel()
        self.rows = [dict(id=r[0], src=r[1], dst=r[2], size=r[3] or 0, offset=r[4] or 0, status=r[5], error=r[6],
                          speed=r[7], hash=r[8])
                     for r in c.execute("SELECT id,src,dst,size,offset,status,error,speed,hash FROM jobs ORDER BY id")]
        self.idx = {r["id"]: i for i, r in enumerate(self.rows)}
        self.live.clear()
        self.endResetModel()

    def append(self, new):
        new = [r for r in new if r["id"] not in self.idx]
        if not new:
            return
        n = len(self.rows)
        self.beginInsertRows(QtCore.QModelIndex(), n, n + len(new) - 1)
        for r in new:
            self.idx[r["id"]] = len(self.rows)
            self.rows.append(r)
        self.endInsertRows()

    def move_row(self, i, j):
        """Zeile i an Position j verschieben (nur Anzeige; Auswahl bleibt erhalten)."""
        if i == j:
            return
        if not self.beginMoveRows(QtCore.QModelIndex(), i, i, QtCore.QModelIndex(), j + 1 if j > i else j):
            return
        self.rows.insert(j, self.rows.pop(i))
        for k in range(min(i, j), max(i, j) + 1):
            self.idx[self.rows[k]["id"]] = k
        self.endMoveRows()

    def update(self, jid, **kw):
        i = self.idx.get(jid)
        if i is None:
            return
        self.rows[i].update(kw)
        if kw.get("status") not in ("running", "verifying", "checking"):
            self.live.pop(jid, None)
        self.dataChanged.emit(self.index(i, 0), self.index(i, len(COLS) - 1))

    def set_live(self, jid, copied, speed, verified=None):
        i = self.idx.get(jid)
        if i is None:
            return
        self.live[jid] = (copied, speed, verified)
        self.dataChanged.emit(self.index(i, 2), self.index(i, len(COLS) - 1))

    def copied(self, r):
        if r["status"] in COPIED:
            return r["size"]
        if r["id"] in self.live:
            return self.live[r["id"]][0]
        return r["offset"]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r, col = self.rows[index.row()], index.column()
        st = r["status"]
        done = self.copied(r)
        frac = done / r["size"] if r["size"] else (1.0 if st in FINISHED else 0.0)
        _, speed, verified = self.live.get(r["id"], (0, 0, None))
        if st in ("verifying", "checking") and verified is not None and r["size"]:
            frac = verified / r["size"]              # Balken zeigt den Fortschritt des Prüfens
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0: return os.path.basename(r["src"])
            if col == 1: return os.path.dirname(r["dst"])
            if col == 2: return hsize(r["size"])
            if col == 3:
                return ("prüft " if st in ("verifying", "checking") else "") + f"{frac * 100:.1f} %".replace(".", ",")
            if col == 4:
                if st == "pending" and r["offset"]:
                    return "pausiert"
                if st == "done" and r.get("hash"):
                    return "geprüft"
                if st == "skipped" and r.get("hash"):
                    return "übersprungen, geprüft"
                return STATUS_TXT.get(st, st)
            if col == 5:
                if st in ("done", "written") and r.get("speed"):
                    return f"Ø {hsize(r['speed'])}/s"        # Durchschnitt über die ganze Kopie
                return f"{hsize(speed)}/s" if st == "running" and speed else ""
            if col == 6: return htime((r["size"] - done) / speed) if st == "running" and speed else ""
        elif role == Qt.ItemDataRole.UserRole:
            return frac
        elif role == Qt.ItemDataRole.ToolTipRole:
            t = f"Quelle: {r['src']}\nZiel:   {r['dst']}"
            if r.get("hash") and st in FINISHED:
                t += f"\nSHA-256: {r['hash']}\n(nach dem Kopieren von der Platte gelesen und verglichen)"
            return t + (f"\nFehler: {r['error']}" if r.get("error") else "")
        elif role == Qt.ItemDataRole.ForegroundRole and col == 4:
            return QtGui.QBrush(QtGui.QColor({"done": "#2e9e44", "failed": "#d0342c", "running": "#2a7ad5", "skipped": "#2e9e44",
                                              "written": "#2a7ad5", "verifying": "#2a7ad5",
                                              "checking": "#2a7ad5", "check_wait": "#2a7ad5"}
                                             .get(st, "#888888")))
        elif role == Qt.ItemDataRole.TextAlignmentRole and col in (2, 5, 6):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        elif role == Qt.ItemDataRole.DecorationRole and col == 0:
            return QtGui.QIcon.fromTheme({"done": "dialog-ok-apply", "failed": "dialog-error", "skipped": "go-jump", "written": "document-save",
                                          "running": "go-next", "verifying": "document-preview",
                                          "checking": "document-preview", "check_wait": "document-preview"}.get(st, "text-x-generic"))
        return None


class BarDelegate(QtWidgets.QStyledItemDelegate):
    def paint(self, p, opt, index):
        frac = index.data(Qt.ItemDataRole.UserRole) or 0.0
        o = QtWidgets.QStyleOptionProgressBar()
        o.rect = opt.rect.adjusted(3, 4, -3, -4)
        o.minimum, o.maximum, o.progress = 0, 1000, int(frac * 1000)
        o.text = index.data(Qt.ItemDataRole.DisplayRole)
        o.textVisible = True
        o.textAlignment = Qt.AlignmentFlag.AlignCenter
        o.state = QtWidgets.QStyle.StateFlag.State_Enabled | QtWidgets.QStyle.StateFlag.State_Horizontal
        o.palette = opt.palette
        st = QtWidgets.QApplication.style()
        st.drawControl(QtWidgets.QStyle.ControlElement.CE_ProgressBar, o, p)


class DropTable(QtWidgets.QTableView):
    def __init__(self, on_drop):
        super().__init__()
        self.on_drop = on_drop
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.on_drop(e.mimeData().urls())
        e.acceptProposedAction()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.model() is not None and self.model().rowCount() == 0:
            p = QtGui.QPainter(self.viewport())
            p.setPen(self.palette().color(QtGui.QPalette.ColorRole.PlaceholderText))
            f = p.font(); f.setPointSizeF(f.pointSizeF() * 1.3); p.setFont(f)
            p.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter,
                       "Dateien oder Ordner aus Dolphin hierher ziehen\n"
                       "oder oben „Dateien…“ / „Ordner…“ wählen")
