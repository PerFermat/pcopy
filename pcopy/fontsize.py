"""Einstellbare Schriftgröße für das ganze Fenster (wird gespeichert)."""
from .qt import QtGui, QtWidgets

PRESETS = [9, 10, 11, 12, 14, 16, 18, 20]
MIN_PT, MAX_PT = 6, 32
COL_WIDTHS = [260, 230, 80, 170, 80, 90, 65]      # bei Standardgröße


class FontMixin:
    def init_font(self):
        """Vor dem Aufbau aufrufen: merkt sich die Systemschrift als „Standard“."""
        self.base_font = QtGui.QFont(QtWidgets.QApplication.font())
        self.base_pt = self.base_font.pointSizeF() if self.base_font.pointSizeF() > 0 else 10.0
        try:
            self.font_pt = float(self.settings.value("fontsize", 0)) or self.base_pt
        except (TypeError, ValueError):
            self.font_pt = self.base_pt

    def build_font_menu(self, menu):
        self.font_menu = menu.addMenu(QtGui.QIcon.fromTheme("preferences-desktop-font"), "Schriftgröße")
        for text, ic, keys, slot in (
                ("Größer", "zoom-in", ["Ctrl++", "Ctrl+="], lambda: self.set_font_size(self.font_pt + 1)),
                ("Kleiner", "zoom-out", ["Ctrl+-"], lambda: self.set_font_size(self.font_pt - 1)),
                ("Standard", "zoom-original", ["Ctrl+0"], lambda: self.set_font_size(self.base_pt))):
            a = self.font_menu.addAction(QtGui.QIcon.fromTheme(ic), text)
            a.setShortcuts([QtGui.QKeySequence(k) for k in keys])
            a.triggered.connect(slot)
            self.addAction(a)                      # Tastenkürzel auch bei geschlossenem Menü
        self.font_menu.addSeparator()
        grp = QtGui.QActionGroup(self)
        self.font_presets = {}
        for pt in PRESETS:
            a = self.font_menu.addAction(f"{pt} pt")
            a.setCheckable(True)
            grp.addAction(a)
            a.triggered.connect(lambda _=False, pt=pt: self.set_font_size(pt))
            self.font_presets[pt] = a

    def set_font_size(self, pt):
        pt = max(MIN_PT, min(MAX_PT, round(pt * 2) / 2))
        self.font_pt = pt
        self.settings.setValue("fontsize", 0 if pt == self.base_pt else pt)
        self.apply_font()
        self.status.showMessage(f"Schriftgröße {pt:g} pt" + (" (Standard)" if pt == self.base_pt else ""), 3000)

    def apply_font(self):
        pt = self.font_pt
        f = QtGui.QFont(self.base_font)
        f.setPointSizeF(pt)
        QtWidgets.QApplication.setFont(f)          # gilt für alle Widgets ohne eigene Schrift
        big = QtGui.QFont(f)
        big.setPointSizeF(pt * 1.15)
        big.setBold(True)
        for l in self.total_values:
            l.setFont(big)
        h = QtGui.QFontMetrics(f).height()
        scale = pt / self.base_pt
        self.table.verticalHeader().setDefaultSectionSize(h + 10)
        self.table.horizontalHeader().setMinimumHeight(int(h * 1.5))
        for col, w in enumerate(COL_WIDTHS):
            if col != 1:                           # Spalte 1 (Zielordner) füllt den Rest
                self.table.setColumnWidth(col, int(w * scale))
        self.total_bar.setMinimumHeight(int(h * 1.5))
        self.font_menu.setTitle(f"Schriftgröße ({pt:g} pt)")
        for p, a in self.font_presets.items():
            a.setChecked(p == pt)
