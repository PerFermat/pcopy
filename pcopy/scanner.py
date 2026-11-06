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

EXISTING = [("skip", "überspringen"), ("overwrite", "überschreiben"), ("newer", "überschreiben, wenn neuer"),
            ("rename", "beide behalten (umbenennen)"), ("ask", "nachfragen")]
ACTIONS = [("copy", "Kopieren"), ("move", "Verschieben"), ("mirror", "Spiegeln")]


def same_file(st, ds):
    """Gleiche Größe und Änderungszeit – dann gilt die Zieldatei als dieselbe.
    Toleranz 2 s, weil FAT-Datenträger Zeiten nur auf 2 s genau speichern."""
    return st.st_size == ds.st_size and abs(st.st_mtime - ds.st_mtime) <= 2


def resolve(mode, st, ds):
    """Was mit einer Datei geschieht, deren Ziel (ds) es schon gibt: „skip“, „copy“ (überschreiben),
    „rename“ (beide behalten) oder „ask“ (vor dem Start nachfragen). Bei „rename“ und „ask“ wird
    eine offensichtlich gleiche Datei übersprungen – sonst gäbe jeder neue Lauf weitere Kopien."""
    if mode == "overwrite":
        return "copy"
    if mode == "newer":
        return "copy" if st.st_mtime > ds.st_mtime + 2 else "skip"
    if mode in ("rename", "ask"):
        return "skip" if same_file(st, ds) else mode
    return "skip"


def free_name(dst, taken):
    """Freier Name „NAME (2).ENDUNG“, „NAME (3).ENDUNG“ … im selben Ordner.
    taken(pfad): True, wenn der Name schon von einem anderen Eintrag der Liste belegt ist."""
    d, name = os.path.split(dst)
    stem, ext = os.path.splitext(name)
    n = 2
    while True:
        cand = os.path.join(d, f"{stem} ({n}){ext}")
        if not os.path.lexists(cand) and not taken(cand):
            return cand
        n += 1


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
            stats = dict(new=0, skipped=0, dup=0, total=0, seen=0, error=None, excluded=0, delete=0, renamed=0,
                         conflict=0)
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
                    elif status == "conflict":
                        stats["conflict"] += 1
                    elif status == "pending":
                        stats["total"] += size
                    else:
                        stats["skipped"] += 1
                else:
                    stats["dup"] += 1
            self.out.put((gen, rid, "rows", (rows, dict(stats))))
            c.execute("COMMIT")
            found, last = [], time.monotonic()

        reserved = set()                         # neue Namen aus „beide behalten“ in diesem Durchlauf
        planned = set()                          # alle Ziele dieses Durchlaufs (noch nicht alle in der DB)

        def taken(path):
            return path in planned or c.execute("SELECT 1 FROM jobs WHERE dst=? LIMIT 1", (path,)).fetchone()

        counter = [0]
        for sp, dp, kind in collect(paths, target, pats, counter):
            if gen != self.gen:
                return                           # verworfen: noch nicht Eingetragenes fallen lassen
            stats["seen"] += 1
            try:
                st = sp.lstat() if kind == "link" else sp.stat()
            except OSError:
                continue
            status, off, dst = "pending", 0, str(dp)
            try:
                ds = dp.lstat()
            except OSError:
                ds = None
            todo = "copy" if ds is None else "skip" if kind == "dir" else resolve(mode, st, ds)
            if todo != "skip" and dst in reserved:
                todo = "rename"                  # Name ist schon für eine umbenannte Datei vergeben
            if todo in ("rename", "ask") and c.execute(
                    "SELECT 1 FROM jobs WHERE src=? AND root=? LIMIT 1", (str(sp), str(target))).fetchone():
                stats["dup"] += 1                # schon (umbenannt) in der Liste
                continue
            if todo == "skip":
                status, off = "skipped", st.st_size
            elif todo == "ask":
                status = "conflict"
            elif todo == "rename":
                dst = free_name(dst, taken)
                reserved.add(dst)
                stats["renamed"] += 1
            planned.add(dst)
            found.append((str(sp), dst, st, status, off, kind))
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
