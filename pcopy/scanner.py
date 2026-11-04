"""Einlesen hinzugefügter Dateien/Ordner im Hintergrund."""
import os
import queue
import stat
import threading
import time
from fnmatch import fnmatchcase
from pathlib import Path

from .db import db_connect
from .engine import is_part

EXISTING = [("skip", "überspringen"), ("overwrite", "überschreiben"), ("newer", "überschreiben, wenn neuer")]
ACTIONS = [("copy", "Kopieren"), ("move", "Verschieben"), ("mirror", "Spiegeln")]


def keep_existing(mode, st, ds):
    """True, wenn die vorhandene Zieldatei (ds) laut Einstellung bleiben soll.
    Toleranz 2 s, weil FAT-Datenträger Zeiten nur auf 2 s genau speichern."""
    if mode == "overwrite":
        return False
    if mode == "newer":
        return st.st_mtime <= ds.st_mtime + 2
    return True


def parse_patterns(text):
    """Ausschlussmuster: eine pro Zeile, # leitet Kommentare ein."""
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip() and not ln.strip().startswith("#")]


def excluded(rel, name, pats):
    """Muster ohne „/“ gelten für den Namen (Datei oder Ordner, überall), mit „/“ für den Pfad
    innerhalb des hinzugefügten Ordners, z. B. „Fotos/.thumbnails“ oder „*/cache/*“."""
    for p in pats:
        if "/" in p:
            if fnmatchcase(rel, p.strip("/")):
                return True
        elif fnmatchcase(name, p):
            return True
    return False


def collect(paths, dest, pats=(), counter=None):
    """(quelle, ziel, art)-Tripel; Ordner werden rekursiv mit ihrer Struktur übernommen.
    art: „file“, „link“ (symbolischer Link – wird als Link kopiert, auch auf Ordner oder ins Leere)
    oder „dir“ (leerer Ordner – Ordner mit Inhalt entstehen beim Kopieren ohnehin).
    Ausgeschlossenes (pats) wird übersprungen und in counter[0] gezählt."""
    dest = Path(dest)
    counter = counter if counter is not None else [0]

    def keep(rp, sp, name):
        rel = (rp / name).relative_to(sp).as_posix()
        if pats and excluded(rel, name, pats):
            counter[0] += 1
            return False
        return True

    for s in paths:
        sp = Path(s).absolute()
        if sp.is_dir():
            for root, dirs, files in os.walk(sp):
                rp = Path(root)
                links = [d for d in dirs if (rp / d).is_symlink()]   # os.walk folgt ihnen nicht
                dirs[:] = sorted(d for d in dirs if d not in links and keep(rp, sp, d))
                names = sorted(f for f in files + links if not is_part(f) and keep(rp, sp, f))
                if not dirs and not names:
                    yield rp, dest / sp.name / rp.relative_to(sp), "dir"
                for f in names:
                    fp = rp / f
                    if fp.is_symlink():
                        yield fp, dest / sp.name / fp.relative_to(sp), "link"
                    elif fp.is_file():
                        yield fp, dest / sp.name / fp.relative_to(sp), "file"
        elif sp.is_file():
            if not (pats and excluded(sp.name, sp.name, pats)):
                yield sp, dest / sp.name, "file"
            else:
                counter[0] += 1
        elif sp.is_symlink():                                       # Link ins Leere
            yield sp, dest / sp.name, "link"


def extras(src_dir, dst_dir, pats=()):
    """Spiegeln: was unter dst_dir liegt, aber in src_dir fehlt -> (quelle-gedacht, ziel).
    Ganze überzählige Ordner werden als ein Eintrag geliefert. Ausgeschlossenes und pcopys eigene
    Hilfsdateien bleiben unangetastet."""
    def walk(sd, dd, rel):
        try:
            entries = sorted(os.scandir(dd), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            r = f"{rel}/{e.name}" if rel else e.name
            if is_part(e.name) or (pats and excluded(r, e.name, pats)):
                continue
            s = os.path.join(sd, e.name)
            if not os.path.lexists(s):
                yield s, e.path
            elif e.is_dir(follow_symlinks=False) and os.path.isdir(s) and not os.path.islink(s):
                yield from walk(s, e.path, r)
    if os.path.isdir(dst_dir):
        yield from walk(str(src_dir), str(dst_dir), "")


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

    def add(self, rid, paths, target, mode, action="copy", pats=()):
        self.req.put((self.gen, rid, paths, target, (mode, action, tuple(pats))))

    def abort(self, timeout=5.0):
        """Laufendes und wartendes Einlesen verwerfen. True, wenn der Thread rechtzeitig stand."""
        self.gen += 1
        return self.idle.wait(timeout)

    def _run(self):
        c = db_connect()
        while True:
            gen, rid, paths, target, mode = self.req.get()
            self.idle.clear()
            stats = dict(new=0, skipped=0, dup=0, total=0, seen=0, error=None, excluded=0, delete=0)
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

    def _scan(self, c, gen, rid, paths, target, opts, stats):
        """Dateien erst im Speicher sammeln, dann schubweise in einer kurzen Transaktion eintragen –
        so ist die DB nur Millisekunden gesperrt und Worker/GUI kommen sofort dran."""
        mode, action, pats = opts
        found, now, last = [], time.time(), time.monotonic()
        stored = "move" if action == "move" else "copy"

        def flush():
            nonlocal found, last
            rows = []
            c.execute("BEGIN")
            for sp, dp, st, status, off, kind in found:
                size = st.st_size if kind == "file" or (kind == "delete" and not stat.S_ISDIR(st.st_mode)) else 0
                act = "copy" if kind == "delete" else stored
                cur = c.execute("INSERT OR IGNORE INTO jobs(src,dst,size,mtime,offset,status,added,root,kind,action) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (sp, dp, size, st.st_mtime, off if kind == "file" else 0, status, now,
                                 str(target), kind, act))
                if cur.rowcount:
                    rows.append(dict(id=cur.lastrowid, src=sp, dst=dp, size=size, kind=kind, action=act, prio=0,
                                     offset=off if kind == "file" else 0, status=status, error=None))
                    stats["new"] += 1
                    if kind == "delete":
                        stats["delete"] += 1
                    elif status == "pending":
                        stats["total"] += size
                    else:
                        stats["skipped"] += 1
                else:
                    stats["dup"] += 1
            self.out.put((gen, rid, "rows", (rows, dict(stats))))
            c.execute("COMMIT")
            found, last = [], time.monotonic()

        counter = [0]
        for sp, dp, kind in collect(paths, target, pats, counter):
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
        stats["excluded"] = counter[0]
        if action == "mirror":
            # Spiegeln: was im Ziel liegt, in der Quelle aber nicht mehr, kommt als „wird gelöscht“ dazu
            for s in paths:
                sp = Path(s).absolute()
                if not sp.is_dir():
                    continue
                for ghost, dp in extras(sp, Path(target) / sp.name, pats):
                    if gen != self.gen:
                        return
                    try:
                        st = os.lstat(dp)
                    except OSError:
                        continue
                    found.append((ghost, dp, st, "pending", 0, "delete"))
                    if len(found) >= self.BATCH:
                        flush()
        flush()
