"""Einlesen hinzugefügter Dateien/Ordner im Hintergrund."""
import os
import queue
import threading
import time
from pathlib import Path

from .db import db_connect
from .engine import is_part

EXISTING = [("skip", "überspringen"), ("overwrite", "überschreiben"), ("newer", "überschreiben, wenn neuer")]


def keep_existing(mode, st, ds):
    """True, wenn die vorhandene Zieldatei (ds) laut Einstellung bleiben soll.
    Toleranz 2 s, weil FAT-Datenträger Zeiten nur auf 2 s genau speichern."""
    if mode == "overwrite":
        return False
    if mode == "newer":
        return st.st_mtime <= ds.st_mtime + 2
    return True


def collect(paths, dest):
    """(quelle, ziel, art)-Tripel; Ordner werden rekursiv mit ihrer Struktur übernommen.
    art: „file“, „link“ (symbolischer Link – wird als Link kopiert, auch auf Ordner oder ins Leere)
    oder „dir“ (leerer Ordner – Ordner mit Inhalt entstehen beim Kopieren ohnehin)."""
    dest = Path(dest)
    for s in paths:
        sp = Path(s).absolute()
        if sp.is_dir():
            for root, dirs, files in os.walk(sp):
                rp = Path(root)
                links = [d for d in dirs if (rp / d).is_symlink()]   # os.walk folgt ihnen nicht
                dirs[:] = sorted(d for d in dirs if d not in links)
                names = sorted(f for f in files + links if not is_part(f))
                if not dirs and not names:
                    yield rp, dest / sp.name / rp.relative_to(sp), "dir"
                for f in names:
                    fp = rp / f
                    if fp.is_symlink():
                        yield fp, dest / sp.name / fp.relative_to(sp), "link"
                    elif fp.is_file():
                        yield fp, dest / sp.name / fp.relative_to(sp), "file"
        elif sp.is_file():
            yield sp, dest / sp.name, "file"
        elif sp.is_symlink():                                       # Link ins Leere
            yield sp, dest / sp.name, "link"


class Scanner:
    """Liest hinzugefügte Pfade im Hintergrund ein und trägt sie schubweise in die DB ein.

    Jeder Schub geht als ("rows", …) an die GUI, *bevor* er committet wird – so kennt die GUI
    eine Zeile immer schon, wenn ein Worker sie übernimmt und Statusmeldungen dazu schickt."""

    BATCH, EVERY = 500, 0.3

    def __init__(self):
        self.req = queue.Queue()
        self.out = queue.Queue()
        self.gen = 0
        self.idle = threading.Event()
        self.idle.set()
        threading.Thread(target=self._run, daemon=True).start()

    def add(self, rid, paths, target, mode):
        self.req.put((self.gen, rid, paths, target, mode))

    def abort(self, timeout=5.0):
        """Laufendes und wartendes Einlesen verwerfen. True, wenn der Thread rechtzeitig stand."""
        self.gen += 1
        return self.idle.wait(timeout)

    def _run(self):
        c = db_connect()
        while True:
            gen, rid, paths, target, mode = self.req.get()
            self.idle.clear()
            stats = dict(new=0, skipped=0, dup=0, total=0, seen=0, error=None)
            try:
                if gen == self.gen:
                    self._scan(c, gen, rid, paths, target, mode, stats)
            except Exception as e:
                stats["error"] = getattr(e, "strerror", None) or str(e)
            finally:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                self.out.put((gen, rid, "end", stats))
                self.idle.set()

    def _scan(self, c, gen, rid, paths, target, mode, stats):
        """Dateien erst im Speicher sammeln, dann schubweise in einer kurzen Transaktion eintragen –
        so ist die DB nur Millisekunden gesperrt und Worker/GUI kommen sofort dran."""
        found, now, last = [], time.time(), time.monotonic()

        def flush():
            nonlocal found, last
            rows = []
            c.execute("BEGIN")
            for sp, dp, st, status, off, kind in found:
                size = st.st_size if kind == "file" else 0
                cur = c.execute("INSERT OR IGNORE INTO jobs(src,dst,size,mtime,offset,status,added,root,kind) "
                                "VALUES(?,?,?,?,?,?,?,?,?)",
                                (sp, dp, size, st.st_mtime, off if kind == "file" else 0, status, now,
                                 str(target), kind))
                if cur.rowcount:
                    rows.append(dict(id=cur.lastrowid, src=sp, dst=dp, size=size, kind=kind,
                                     offset=off if kind == "file" else 0, status=status, error=None))
                    stats["new"] += 1
                    if status == "pending":
                        stats["total"] += size
                    else:
                        stats["skipped"] += 1
                else:
                    stats["dup"] += 1
            self.out.put((gen, rid, "rows", (rows, dict(stats))))
            c.execute("COMMIT")
            found, last = [], time.monotonic()

        for sp, dp, kind in collect(paths, target):
            if gen != self.gen:
                return                           # verworfen: noch nicht Eingetragenes fallen lassen
            stats["seen"] += 1
            try:
                st = sp.lstat() if kind == "link" else sp.stat()
            except OSError:
                continue
            status, off = "pending", 0
            try:
                ds = dp.lstat()
            except OSError:
                ds = None
            if ds is not None and (kind == "dir" or keep_existing(mode, st, ds)):
                status, off = "skipped", st.st_size
            found.append((str(sp), str(dp), st, status, off, kind))
            if len(found) >= self.BATCH or time.monotonic() - last >= self.EVERY:
                flush()
        flush()
