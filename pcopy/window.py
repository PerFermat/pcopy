"""Hauptfenster: Aufbau, Start/Pause, laufende Aktualisierung, Beenden."""
import queue
import time
from collections import deque

from .qt import Qt, QtCore, QtGui, QtWidgets
from .adding import AddMixin
from .checker import Checker
from .db import APP_DIR, db_connect
from .engine import Engine
from .fontsize import FontMixin
from .listops import ListMixin
from .menus import MenuMixin
from . import precheck
from .report import ReportDialog
from .model import BarDelegate, CHECKING, DropTable, FINISHED, JobModel
from .scanner import EXISTING, Scanner
from .util import hsize, htime

APP = "pcopy"
LONG_AFTER = 5.0          # s: länger laufende Kopien rücken in der Anzeige an die Arbeitsfront


class MainWindow(AddMixin, ListMixin, MenuMixin, FontMixin, QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("pcopy – Dateien kopieren")
        self.setWindowIcon(QtGui.QIcon(str(APP_DIR / "pcopy.svg")))
        self.setAcceptDrops(True)
        self.settings = QtCore.QSettings(APP, "gui")
        self.db = db_connect()
        self.engine = Engine()
        self.checker = Checker(self.engine)
        self.checking = False     # läuft eine Prüfrunde (für die Abschlussmeldung)
        self.report = None
        self.fails_at_start = 0   # Fehler beim Start – neue Fehler öffnen am Ende den Bericht
        self.model = JobModel()
        self.samples = deque()
        self.shown_active = set()
        self.clean_exit = True
        self.scanner = Scanner()
        self.scans = {}           # Anfrage-Nr. -> Stand des Einlesens
        self.scan_nr = 0
        self.init_font()
        self._build()
        self.apply_font()
        self.db.execute("UPDATE jobs SET status='pending' WHERE status='running'")
        # kleine Dateien, die beim letzten Mal nicht mehr gesynct wurden: neu kopieren
        redo = self.db.execute("UPDATE jobs SET status='pending', offset=0 WHERE status='written'").rowcount
        self.model.load(self.db)
        if redo:
            self.status.showMessage(f"{redo} Dateien waren beim letzten Beenden noch nicht sicher auf der Platte "
                                    "und werden beim Start neu kopiert.", 20000)
        self.timer = QtCore.QTimer(self, interval=250, timeout=self.tick)
        self.timer.start()
        self.tick()
        g = self.settings.value("geometry")
        if g is not None:
            self.restoreGeometry(g)
        else:
            self.resize(1050, 620)

    # ---------------------------------------------------------------- Aufbau
    def _build(self):
        icon = QtGui.QIcon.fromTheme
        tb = self.addToolBar("Werkzeuge")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        def act(text, ic, slot, tip=None, key=None):
            a = QtGui.QAction(icon(ic), text, self)
            a.triggered.connect(slot)
            if tip:
                a.setToolTip(tip)
                a.setStatusTip(tip)
            if key:
                a.setShortcut(QtGui.QKeySequence(key))
            return a

        self.a_files = act("Dateien hinzufügen…", "document-open", self.pick_files,
                           "Eine oder mehrere Dateien hinzufügen", "Ctrl+O")
        self.a_folder = act("Ordner hinzufügen…", "folder-open", self.pick_folder,
                            "Einen Ordner (mit Unterordnern) hinzufügen", "Ctrl+Shift+O")
        self.a_start = act("Start", "media-playback-start", self.start,
                           "Kopieren starten bzw. fortsetzen", "Ctrl+Return")
        self.a_pause = act("Pause", "media-playback-pause", self.pause,
                           "Anhalten – wird später an exakt derselben Stelle fortgesetzt", "Ctrl+P")
        self._acts = act
        self.a_files.setIconText("Dateien…")          # kurzer Text in der Werkzeugleiste
        self.a_folder.setIconText("Ordner…")
        tb.addAction(self.a_files)
        tb.addAction(self.a_folder)
        tb.addSeparator()
        tb.addAction(self.a_start)
        tb.addAction(self.a_pause)
        tb.addSeparator()
        tb.addWidget(QtWidgets.QLabel(" Gleichzeitig: "))
        self.spin = QtWidgets.QSpinBox(minimum=1, maximum=16)
        self.spin.setValue(int(self.settings.value("parallel", 4)))
        self.spin.valueChanged.connect(self.engine.set_parallel)
        tb.addWidget(self.spin)
        tb.addWidget(QtWidgets.QLabel("  Max. Tempo: "))
        self.limit = QtWidgets.QSpinBox(minimum=0, maximum=10000, singleStep=5, suffix=" MB/s")
        self.limit.setSpecialValueText("unbegrenzt")
        self.limit.setToolTip("Gesamttempo aller Kopien begrenzen, damit der Rechner nebenher flüssig bleibt.\n"
                              "0 = unbegrenzt. Lässt sich auch während des Kopierens ändern.")
        self.limit.valueChanged.connect(lambda v: self.engine.throttle.set_rate(v * 1024 * 1024))
        self.limit.setValue(int(self.settings.value("limit", 0)))
        self.engine.throttle.set_rate(self.limit.value() * 1024 * 1024)
        tb.addWidget(self.limit)

        central = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(central)
        lay.setContentsMargins(10, 8, 10, 8)

        # Zielordner
        row = QtWidgets.QHBoxLayout()
        lbl = QtWidgets.QLabel("<b>Ziel:</b>")
        self.target = QtWidgets.QLineEdit(self.settings.value("target", ""))
        self.target.setPlaceholderText("Zielordner wählen – neue Dateien werden dorthin kopiert")
        b = QtWidgets.QPushButton(icon("folder-open"), "Ändern…")
        b.clicked.connect(self.pick_target)
        row.addWidget(lbl); row.addWidget(self.target, 1); row.addWidget(b)
        row.addSpacing(16)
        row.addWidget(QtWidgets.QLabel("Vorhandene Dateien:"))
        self.existing = QtWidgets.QComboBox()
        for key, text in EXISTING:
            self.existing.addItem(text, key)
        self.existing.setToolTip("Was passiert, wenn es die Datei im Ziel schon gibt.\n"
                                 "Gilt für Dateien, die ab jetzt hinzugefügt werden.\n"
                                 "„wenn neuer“: nur ersetzen, wenn die Quelle jünger ist als die Zieldatei.")
        i = self.existing.findData(self.settings.value("existing", "skip"))
        self.existing.setCurrentIndex(max(i, 0))
        row.addWidget(self.existing)
        lay.addLayout(row)

        # Tabelle
        self.table = DropTable(self.on_drop)
        self.table.setModel(self.model)
        self.table.setItemDelegateForColumn(3, BarDelegate(self.table))
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
        h.setStretchLastSection(False)
        h.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        lay.addWidget(self.table, 1)

        # Gesamtfortschritt
        box = QtWidgets.QGroupBox("Gesamt")
        g = QtWidgets.QGridLayout(box)
        self.total_bar = QtWidgets.QProgressBar(maximum=1000)
        g.addWidget(self.total_bar, 0, 0, 1, 4)
        self.l_files, self.l_bytes, self.l_speed, self.l_eta = (QtWidgets.QLabel() for _ in range(4))
        for i, (cap, l) in enumerate([("Dateien", self.l_files), ("Daten", self.l_bytes),
                                      ("Tempo", self.l_speed), ("Restzeit", self.l_eta)]):
            w = QtWidgets.QLabel(f"<span style='color:gray'>{cap}</span>")
            v = QtWidgets.QVBoxLayout(); v.setSpacing(0)
            v.addWidget(w); v.addWidget(l)
            g.addLayout(v, 1, i)
        self.total_values = (self.l_files, self.l_bytes, self.l_speed, self.l_eta)   # Schrift: apply_font
        lay.addWidget(box)
        self.setCentralWidget(central)
        self.status = self.statusBar()
        self.scan_label = QtWidgets.QLabel()
        self.status.addPermanentWidget(self.scan_label)
        self.par_label = QtWidgets.QLabel()
        self.par_label.setStyleSheet("color:#b8860b")
        self.status.addPermanentWidget(self.par_label)
        self._build_menu()

    # ---------------------------------------------------------------- Steuerung
    def start(self):
        if not self.engine.is_running() and not self.check_targets():
            return
        self.engine.verify = self.verify.isChecked()
        if not self.engine.is_running():
            self.fails_at_start = sum(r["status"] == "failed" for r in self.model.rows)
        self.engine.start(self.spin.value())
        self.tick()

    def show_report(self):
        if self.report is None:
            self.report = ReportDialog(self, self.db, self.target.text() or str(APP_DIR))
        else:
            self.report.refresh()
        self.report.show()
        self.report.raise_()
        self.report.activateWindow()

    def check_targets(self):
        """Platz, 4-GB-Grenze (FAT32) und erlaubte Zeichen vor dem Start prüfen.
        False = abgebrochen. Nicht anlegbare Dateien werden auf Wunsch als Fehler markiert."""
        try:
            msgs, affected = precheck.check(self.db)
        except Exception as e:                   # Prüfung darf das Kopieren nie verhindern
            self.status.showMessage(f"Prüfung vor dem Start nicht möglich: {e}", 8000)
            return True
        if not msgs:
            return True
        box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Icon.Warning, "Vor dem Start",
                                    "<br><br>".join(msgs), parent=self)
        box.setTextFormat(Qt.TextFormat.RichText)
        go = box.addButton("Betroffene überspringen und starten" if affected else "Trotzdem starten",
                           QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Abbrechen", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not go:
            return False
        if affected:
            self.db.execute("BEGIN")
            self.db.executemany("UPDATE jobs SET status='failed', error=? WHERE id=? AND status='pending'",
                                [(why, jid) for jid, why in affected.items()])
            self.db.execute("COMMIT")
            self.model.load(self.db)
        return True

    def pause(self):
        self.engine.pause()
        self.status.showMessage("Pausiert – Stand ist gesichert, „Start“ macht an derselben Stelle weiter.", 8000)

    # ---------------------------------------------------------------- Aktualisierung
    def tick(self):
        self.drain_scanner()                     # zuerst: neue Zeilen vor deren Statusmeldungen
        while True:
            try:
                jid, kw = self.engine.events.get_nowait()
            except queue.Empty:
                break
            self.model.update(jid, **kw)
        actives = list(self.engine.active.values())
        for a in actives:
            self.model.set_live(a.id, a.copied, a.speed(), a.verified)
        cur = self.checker.current
        if cur is not None:
            self.model.set_live(cur.id, cur.size, 0, cur.verified)
        elif self.checking and not self.checker.busy():
            self.checking = False
            ok, bad = self.checker.ok, self.checker.bad
            self.status.showMessage(f"Prüfung fertig: {ok} Dateien in Ordnung"
                                    + (f", {bad} mit Fehler (siehe Status/Tooltip)" if bad else ""), 20000)
        self.regroup_long(actives)
        self.follow_active({a.id for a in actives})

        rows = self.model.rows
        total = done = 0
        n_done = n_fail = n_all = 0
        wall = time.time()
        for i, r in enumerate(rows):
            if r["status"] == "failed":
                n_fail += 1
                continue
            if r.get("retry_at") and r["status"] == "pending" and r["retry_at"] > wall - 2:
                self.model.refresh(i, 4)                 # Countdown „Wiederholung in … s“
            n_all += 1
            total += r["size"]
            done += self.model.copied(r)
            n_done += r["status"] in FINISHED or r["status"] in CHECKING
        open_jobs = n_all - n_done                       # wartend, läuft oder noch nicht gesichert

        # Alles erledigt: anhalten. Später hinzugefügte Dateien warten dann auf „Start“.
        if self.engine.is_running() and not open_jobs and not self.engine.active \
                and not self.scans and not self.engine.unsynced() and not self.engine.dirs_pending():
            self.engine.pause()
            self.status.showMessage(f"Fertig – {n_done} Dateien" + (f", {n_fail} mit Fehler" if n_fail else "")
                                    + ". Neue Dateien werden erst nach „Start“ kopiert. Bericht: Strg+B", 15000)
            if n_fail > self.fails_at_start:
                self.show_report()                       # neue Fehler in diesem Durchlauf

        now = time.monotonic()
        self.samples.append((now, self.engine.session_bytes()))
        while self.samples and now - self.samples[0][0] > 5:
            self.samples.popleft()
        speed = 0.0
        if len(self.samples) >= 2:
            (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
            speed = (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0
        running = self.engine.is_running()
        if not running:
            speed = 0.0

        frac = done / total if total else 0.0
        self.total_bar.setValue(int(frac * 1000))
        self.total_bar.setFormat(f"{frac * 100:.1f} %".replace(".", ","))
        self.l_files.setText(f"{n_done} / {n_all}" + (f"  <span style='color:#d0342c'>({n_fail} Fehler)</span>"
                                                      if n_fail else ""))
        self.l_bytes.setText(f"{hsize(done)} / {hsize(total)}")
        self.l_speed.setText(f"{hsize(speed)}/s" if running else "–")
        self.l_eta.setText(htime((total - done) / speed) if running and speed else "–")

        # „Gleichzeitig“ verkleinert: Angefangenes läuft zu Ende, Neues startet erst danach
        n_act, n_max = len(self.engine.active), self.engine.n
        self.par_label.setVisible(running and n_act > n_max)
        if n_act > n_max:
            self.par_label.setText(f"{n_act} angefangene Kopien laufen zu Ende – danach {n_max} gleichzeitig")

        busy = running and bool(self.engine.active)
        self.a_start.setEnabled(not running)
        self.a_pause.setEnabled(running)
        state = ("kopiert" if busy else "wartet auf Dateien") if running \
            else ("fertig" if rows and not open_jobs else "angehalten")
        self.setWindowTitle(f"pcopy – {frac * 100:.0f} % – {state}" if busy else f"pcopy – {state}")

    def regroup_long(self, actives):
        """Kopien, die länger als LONG_AFTER s laufen, direkt vor die erste wartende Datei schieben:
        oben das Fertige, dann was gerade kopiert wird, darunter das noch Ausstehende.
        Nur die Anzeige wird umsortiert; kurze Kopien bleiben stehen, damit nichts springt."""
        now = time.monotonic()
        idx = self.model.idx
        long = sorted(a.id for a in actives if now - a.started >= LONG_AFTER and a.id in idx)
        if not long:
            return
        rows, ls = self.model.rows, set(long)
        b = next((i for i, r in enumerate(rows) if r["status"] == "pending" and r["id"] not in ls), len(rows))
        if all(idx[j] < b for j in long) and [r["id"] for r in rows[b - len(long):b]] == long:
            return                                       # steht schon richtig
        for jid in long:                                 # aufsteigend: Reihenfolge im Block = Kopierreihenfolge
            i = idx[jid]
            if i < b:
                self.model.move_row(i, b - 1)
            else:
                self.model.move_row(i, b)
                b += 1

    def follow_active(self, ids):
        """Bei „Mitlaufen“ die aktiven Kopien mittig zeigen – nur wenn eine neue Datei dazukommt,
        damit man zwischendurch trotzdem von Hand scrollen kann."""
        if not self.follow.isChecked() or not ids or ids <= self.shown_active:
            self.shown_active &= ids
            return
        self.shown_active = set(ids)
        rows = sorted(self.model.idx[i] for i in ids if i in self.model.idx)
        if rows:
            mid = (rows[0] + rows[-1]) // 2
            self.table.scrollTo(self.model.index(mid, 0), QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter)

    # ---------------------------------------------------------------- Beenden
    def closeEvent(self, e):
        copying = self.engine.is_running() and self.engine.active
        if copying or self.scans:
            r = QtWidgets.QMessageBox.question(
                self, "Kopieren läuft" if copying else "Einlesen läuft",
                ("Es wird noch kopiert." if copying else "Es werden noch Dateien eingelesen.")
                + " Anhalten und beenden?\n\n"
                "Beim nächsten Start geht es an genau derselben Stelle weiter."
                + ("\nNoch nicht eingelesene Dateien: den Ordner einfach noch einmal hinzufügen –\n"
                   "was schon in der Liste steht, wird erkannt." if self.scans else ""))
            if r != QtWidgets.QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        QtWidgets.QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.status.showMessage("Wird angehalten …")
        QtWidgets.QApplication.processEvents()
        # Hängt ein Worker (Ziel antwortet nicht), trotzdem beenden: der Stand bis zur letzten
        # Sicherung (≤ SYNC_EVERY s) steht in der DB, der Rest wird beim nächsten Start kopiert.
        self.checker.stop()
        scan_ok = self.scanner.abort(timeout=2.0)
        self.clean_exit = self.engine.shutdown(timeout=5.0) and scan_ok
        QtWidgets.QApplication.restoreOverrideCursor()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("parallel", self.spin.value())
        self.settings.setValue("limit", self.limit.value())
        self.settings.setValue("existing", self.existing.currentData())
        self.settings.setValue("follow", "true" if self.follow.isChecked() else "false")
        self.settings.setValue("verify", "true" if self.verify.isChecked() else "false")
        self.settings.setValue("target", self.target.text())
        e.accept()
