"""Listen-Aktionen: entfernen, leeren, erneut versuchen, Ziel umstellen."""
import os

from .qt import Qt, QtWidgets
from .db import guess_root
from .engine import part_path
from .model import COPIED
from .scanner import keep_existing


class ListMixin:
    def retry_failed(self):
        self.db.execute("UPDATE jobs SET status='pending', error=NULL WHERE status='failed'")
        self.model.load(self.db)

    def clear_done(self):
        self.db.execute("DELETE FROM jobs WHERE status IN ('done','skipped')")
        self.model.load(self.db)

    def clear_failed(self):
        self.db.execute("DELETE FROM jobs WHERE status='failed'")
        self.model.load(self.db)

    def remove_selected(self):
        ids = self.selected_ids()
        if ids:
            self.db.execute(f"DELETE FROM jobs WHERE status!='running' AND id IN ({','.join('?' * len(ids))})", ids)
            self.model.load(self.db)

    def clear_all(self):
        n = len(self.model.rows)
        if not n:
            return
        busy = self.engine.is_running() or bool(self.engine.active) or bool(self.scans)
        partial = sum(1 for r in self.model.rows
                      if r["status"] not in COPIED and (self.model.copied(r) or r["id"] in self.engine.active))
        box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Icon.Question, "Liste leeren",
                                    f"Alle {n} Einträge aus der Liste entfernen?", parent=self)
        box.setInformativeText(("Das Einlesen wird abgebrochen.\n" if self.scans else "")
                               + ("Das laufende Kopieren wird dabei angehalten.\n"
                                  if self.engine.is_running() or self.engine.active else "")
                               + "Fertig kopierte Dateien im Ziel bleiben erhalten.")
        cb = None
        if partial or busy:
            cb = QtWidgets.QCheckBox("Angefangene Hilfsdateien (.pcopy-part) im Ziel löschen")
            cb.setChecked(True)
            cb.setToolTip("Unfertige Kopien liegen als versteckte .pcopy-part-Dateien im Ziel.\n"
                          "Fertige Dateien und vorhandene Zieldateien werden nie gelöscht.")
            box.setCheckBox(cb)
        box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
        box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
        if box.exec() != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        QtWidgets.QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.scanner.abort(timeout=5.0)
            self.scans.clear()
            held = list(self.engine.active)
            self.engine.shutdown(timeout=5.0)
            self.tick()
            removed = 0
            if cb is not None and cb.isChecked():
                # nur pcopys eigene Hilfsdateien – die echten Zieldateien bleiben unberührt
                q = ",".join("?" * len(held))
                for jid, dst in self.db.execute("SELECT id,dst FROM jobs WHERE status NOT IN ('done','skipped') "
                                                f"AND (offset>0 OR status='written' OR id IN ({q}))", held).fetchall():
                    try:
                        os.remove(part_path(dst, jid))
                        removed += 1
                    except OSError:
                        pass
            self.db.execute("DELETE FROM jobs")
            self.model.load(self.db)
            self.shown_active.clear()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self.status.showMessage(f"Liste geleert ({n} Einträge)"
                                + (f", {removed} angefangene Hilfsdateien im Ziel gelöscht" if removed else ""), 10000)

    def selected_ids(self):
        return [self.model.rows[i.row()]["id"] for i in self.table.selectionModel().selectedRows()]

    def retarget_selected(self):
        """Markierte wartende Dateien auf den aktuellen Zielordner umstellen (Unterpfad bleibt)."""
        ids = self.selected_ids()
        if not ids:
            self.status.showMessage("Erst Dateien in der Liste markieren.", 6000)
            return
        target = self.ensure_target()
        if not target:
            return
        mode = self.existing.currentData()
        moved = skipped = same = conflict = 0
        stale = []
        q = ",".join("?" * len(ids))
        with self.engine.claim_lock:             # kein Worker darf dazwischen einen Job übernehmen
            self.db.execute("BEGIN")
            try:
                rows = self.db.execute(f"SELECT id,src,dst,root,offset,size FROM jobs "
                                       f"WHERE status='pending' AND id IN ({q})", ids).fetchall()
                for jid, src, dst, root, off, size in rows:
                    if jid in self.engine.active:
                        continue
                    new = os.path.join(target, os.path.relpath(dst, root or guess_root(src, dst)))
                    if os.path.normpath(new) == os.path.normpath(dst):
                        same += 1
                        continue
                    status, noff = "pending", 0
                    try:
                        if keep_existing(mode, os.stat(src), os.stat(new)):
                            status, noff = "skipped", size
                    except OSError:
                        pass
                    cur = self.db.execute("UPDATE OR IGNORE jobs SET dst=?, root=?, offset=?, status=?, error=NULL "
                                          "WHERE id=? AND status='pending'", (new, target, noff, status, jid))
                    if not cur.rowcount:
                        conflict += 1                # gleiche Quelle → gleiches Ziel steht schon in der Liste
                        continue
                    moved += 1
                    skipped += status == "skipped"
                    if off > 0:
                        stale.append(part_path(dst, jid))    # angefangene Hilfsdatei am alten Ziel
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        for f in stale:
            try:
                os.remove(f)
            except OSError:
                pass
        self.model.load(self.db)
        other = len(ids) - moved - same - conflict
        msg = f"{moved} Dateien auf „{target}“ umgestellt"
        if skipped:
            msg += f" ({skipped} davon dort schon vorhanden → übersprungen)"
        if same:
            msg += f", {same} hatten dieses Ziel schon"
        if conflict:
            msg += f", {conflict} stehen mit diesem Ziel schon in der Liste"
        if other:
            msg += f", {other} nicht umgestellt (schon kopiert, in Arbeit oder Fehler)"
        if stale:
            msg += f"; {len(stale)} angefangene Hilfsdateien am alten Ziel gelöscht"
        self.status.showMessage(msg, 15000)
