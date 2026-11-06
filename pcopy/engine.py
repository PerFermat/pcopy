"""Kopier-Engine: Worker-Threads, Tempo-Begrenzung, wiederaufnehmbares Kopieren.

Ablauf pro Datei:
  1. Kopieren in eine Hilfsdatei `.NAME.pcopy-part` im Zielordner (wiederaufnehmbar).
  2. Sichern: große Dateien einzeln per fsync (und alle SYNC_EVERY s ihr Stand); kleine Dateien
     (< SMALL) bekommen den Status „written“ und werden vom Sync-Thread gesammelt per syncfs
     gesichert.
  3. Prüfen (falls eingeschaltet): Hilfsdatei aus dem Seiten-Cache werfen, von der Platte
     zurücklesen und die SHA-256 mit der beim Kopieren berechneten Prüfsumme der Quelle vergleichen.
  Harte Links der Quelle werden im Ziel wieder als harte Links auf die erste Kopie angelegt; auf
  btrfs/XFS wird innerhalb eines Dateisystems per Reflink kopiert (gemeinsame Blöcke, sofort fertig),
  und Lücken dünn besetzter Dateien bleiben Lücken.
  4. Erst dann per os.replace auf den echten Namen umbenennen → unter dem echten Namen liegt
     nie eine halbe oder fehlerhafte Datei, eine ersetzte Datei bleibt bis zuletzt erhalten.
Was beim Programmstart noch „written“ ist, war nicht sicher auf der Platte und wird neu kopiert."""
import ctypes
import ctypes.util
import errno
import fcntl
import hashlib
import os
import queue
import shutil
import subprocess
import threading
import time
from collections import deque

from .db import db_connect, guess_root
from .devices import Devices, drop_cache, lower_priority

CHUNK = 1024 * 1024
SYNC_EVERY = 2.0          # s: Stand großer Dateien sichern / kleine Dateien gesammelt syncen
SMALL = 8 * 1024 * 1024   # kleiner als das: kein fsync pro Datei, sondern gesammelt
SYNC_FILES = 200          # spätestens nach so vielen kleinen Dateien syncen
# Höchstens so viel ungesichert im Seiten-Cache, sonst warten die Worker. Ohne diese Bremse
# „kopiert“ ein langsames Ziel (USB-Stick) gigabyteweise in den Speicher und hängt dann minutenlang
# beim Sichern – ohne Fortschritt in der Anzeige.
MAX_UNSYNCED_FILES = 2 * SYNC_FILES
MAX_UNSYNCED_BYTES = 64 * 1024 * 1024
PART_SUFFIX = ".pcopy-part"
FICLONE = 0x40049409      # ioctl: Datei als Reflink klonen (btrfs, XFS, bcachefs …)
HOLE = 64 * 1024          # Nullblöcke dieser Größe werden in dünn besetzten Dateien übersprungen
ZERO = bytes(HOLE)
RETRY_DELAYS = (10, 30, 60)   # s bis zum 2., 3., 4. Versuch bei vorübergehenden Fehlern
# vorübergehend (z. B. USB-Reset, Netzlaufwerk hängt) – dauerhaft wären etwa ENOENT, EACCES, ENOSPC
TRANSIENT = {errno.EIO, errno.ENXIO, errno.ENODEV, errno.ETIMEDOUT, errno.EAGAIN, errno.EBUSY,
             errno.ESTALE, errno.ENOTCONN, errno.EINTR, errno.ECONNRESET, errno.EHOSTDOWN}

try:
    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    _libc.syncfs.argtypes = [ctypes.c_int]
except (OSError, AttributeError):
    _libc = None


def syncfs(path):
    """Alle gepufferten Daten des Dateisystems sichern, auf dem `path` liegt."""
    if _libc is None:
        os.sync()
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if _libc.syncfs(fd) != 0:
            e = ctypes.get_errno()
            raise OSError(e, os.strerror(e), path)
    finally:
        os.close(fd)


def fsync_dir(path):
    """Verzeichniseintrag (z. B. nach dem Umbenennen) sichern."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    except OSError:
        pass                      # manche Dateisysteme (z. B. FUSE) können das nicht – dann eben nicht
    finally:
        os.close(fd)


def copy_owner(st, path, follow=True):
    """Eigentümer und Gruppe übernehmen. Einen fremden Eigentümer darf nur root setzen, die Gruppe
    auch ein Mitglied – was nicht geht (oder FAT/NTFS gar nicht kennt), bleibt still wie es ist."""
    uid = st.st_uid if os.geteuid() == 0 else -1
    try:
        os.chown(path, uid, st.st_gid, follow_symlinks=follow)
    except OSError:
        pass


def write_sparse(fd, buf):
    """Nullblöcke nicht schreiben, sondern überspringen – das Ziel bekommt dort eine Lücke."""
    mv = memoryview(buf)
    for i in range(0, len(buf), HOLE):
        blk = mv[i:i + HOLE]
        if blk == ZERO[:len(blk)]:
            fd.seek(len(blk), os.SEEK_CUR)
        else:
            fd.write(blk)


def part_path(dst, jid):
    """Hilfsdatei, in die kopiert wird, bis die Datei fertig (und geprüft) ist."""
    d, name = os.path.split(dst)
    part = f".{name}{PART_SUFFIX}"
    if len(part.encode()) > 250:              # Namen dürfen meist höchstens 255 Bytes lang sein
        part = f".pcopy-{jid}{PART_SUFFIX}"
    return os.path.join(d, part)


def is_part(name):
    return name.startswith(".") and name.endswith(PART_SUFFIX)


class VerifyError(Exception):
    pass


class Active:
    def __init__(self, jid, src, dst, size):
        self.id, self.src, self.dst, self.size = jid, src, dst, size
        self.copied = self.base = 0
        self.verified = None      # Bytes beim Prüfen (None = es wird gerade nicht geprüft)
        self.avg = None           # Ø-Tempo nach Abschluss (Bytes/s)
        self.started = time.monotonic()
        self.samples = deque(maxlen=60)

    def speed(self):
        if len(self.samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0


class Throttle:
    """Gemeinsame Tempo-Begrenzung aller Worker (Bytes/s, 0 = unbegrenzt)."""

    def __init__(self, rate=0):
        self.lock = threading.Lock()
        self.rate = rate
        self.next = time.monotonic()

    def set_rate(self, rate):
        with self.lock:
            self.rate = rate
            self.next = time.monotonic()

    def chunk(self):
        r = self.rate
        return CHUNK if r <= 0 else max(64 * 1024, min(CHUNK, r // 4))

    def wait(self, n, stop):
        with self.lock:
            if self.rate <= 0:
                return
            now = time.monotonic()
            self.next = max(self.next, now)
            delay = self.next - now
            self.next += n / self.rate
        if delay > 0:
            stop.wait(delay)

    def refund(self, n):
        """Vorab verbuchte, aber nicht gelesene Bytes zurückgeben (Dateirest, Dateiende)."""
        with self.lock:
            if self.rate > 0 and n > 0:
                self.next -= n / self.rate


class Engine:
    """Worker-Threads kopieren Jobs aus der DB; Statuswechsel gehen als Events an die GUI.

    Auf Worker wird nie unbegrenzt gewartet: Hängt einer im Kernel (z. B. USB-Platte antwortet
    nicht), bleibt er als „Altlast“ liegen; jeder Start bekommt ein eigenes Stop-Event."""

    def __init__(self):
        self.stop = threading.Event()
        self.stop.set()
        self.claim_lock = threading.Lock()
        self.flock = threading.Lock()
        self.active = {}          # job-id -> Active (auch von hängenden Alt-Workern)
        self.workers = {}
        self.stale = []
        self.finished_bytes = 0
        self.events = queue.Queue()
        self.throttle = Throttle()
        self.verify = True        # nach dem Kopieren von der Platte zurücklesen und vergleichen
        self.hdd_serial = True    # je drehender Festplatte (Quelle oder Ziel) nur eine Kopie gleichzeitig
        self.gentle = True        # niedrige Platten-/CPU-Priorität, Seiten-Cache schonen
        self.devices = Devices()
        self.dev_busy = {}        # st_dev -> laufende Kopien auf dieser Festplatte
        self.n = 4
        self.written = []         # kleine Dateien, die noch gesynct/geprüft/umbenannt werden müssen
        self.written_since = None
        self.written_bytes = 0    # Größe der Dateien in `written`
        self.syncing = 0          # Dateien, die flush() gerade sichert und prüft
        self.syncing_bytes = 0
        self.wlock = threading.Lock()
        self.sync_lock = threading.Lock()
        self.touched = {}         # Zielordner -> Quellordner, deren Zeitstempel am Ende gesetzt werden
        self.rm_dirs = set()      # Quellordner verschobener Dateien – am Ende entfernen, wenn leer
        self.tlock = threading.Lock()
        self.stamping = False
        self.no_clone = set()     # (Quell-, Ziel-Gerät), auf denen Reflink nicht geht
        threading.Thread(target=self._syncer, daemon=True).start()

    def alive(self):
        return any(t.is_alive() for t in self.workers.values())

    def is_running(self):
        return not self.stop.is_set() and self.alive()

    def start(self, n):
        if self.is_running():
            return
        self.stale = [t for t in self.stale + list(self.workers.values()) if t.is_alive()]
        self.workers = {}
        self.n = n
        self.stop = threading.Event()
        held = list(self.active)                 # Jobs, an denen ein Alt-Worker noch hängt
        c = db_connect()
        c.execute("UPDATE jobs SET status='pending' WHERE status='running' "
                  f"AND id NOT IN ({','.join('?' * len(held))})", held)
        self._spawn()

    def set_parallel(self, n):
        self.n = n
        if self.is_running():
            self._spawn()

    def _spawn(self):
        for nr in range(self.n):
            t = self.workers.get(nr)
            if t is None or not t.is_alive():
                t = threading.Thread(target=self._worker, args=(nr, self.stop), daemon=True)
                self.workers[nr] = t
                t.start()

    def pause(self):
        self.stop.set()

    def shutdown(self, timeout=5.0):
        """Anhalten und höchstens `timeout` s warten. True, wenn alle Worker beendet sind."""
        self.stop.set()
        end = time.monotonic() + timeout
        threads = self.stale + list(self.workers.values())
        for t in threads:
            t.join(max(0.0, end - time.monotonic()))
        # noch nicht gesicherte kleine Dateien sichern; hängt das Ziel, bleiben sie „written“
        # und werden beim nächsten Start neu kopiert
        f = threading.Thread(target=lambda: (self.flush(), self._stamp_dirs(db_connect())), daemon=True)
        f.start()
        f.join(max(2.0, end - time.monotonic()))
        return not any(t.is_alive() for t in threads) and not f.is_alive()

    # ---------------------------------------------------------------- Prüfen
    def _verify(self, path, digest, stop=None, act=None):
        """Datei von der Platte (nicht aus dem Cache) lesen und mit `digest` vergleichen.
        Rückgabe True/False; None, wenn per `stop` abgebrochen."""
        h = hashlib.sha256()
        never = threading.Event()
        with open(path, "rb") as f:
            try:
                os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)   # Seiten sind gesynct
            except (AttributeError, OSError):
                pass
            done = 0
            while True:
                if stop is not None and stop.is_set():
                    return None
                size = self.throttle.chunk()
                self.throttle.wait(size, stop or never)
                buf = f.read(size)
                self.throttle.refund(size - len(buf))
                if not buf:
                    break
                h.update(buf)
                done += len(buf)
                if act is not None:
                    act.verified = done
            if self.gentle:
                drop_cache(f.fileno())
        return h.hexdigest() == digest

    # ---------------------------------------------------------------- gesammeltes Sichern
    def unsynced(self):
        """Kleine Dateien, die kopiert, aber noch nicht gesichert/geprüft/umbenannt sind."""
        with self.wlock:
            return len(self.written) + self.syncing

    def _backlog_full(self):
        with self.wlock:
            return (len(self.written) + self.syncing >= MAX_UNSYNCED_FILES or
                    self.written_bytes + self.syncing_bytes >= MAX_UNSYNCED_BYTES)

    def _add_written(self, entry, size):
        with self.wlock:
            self.written.append(entry)
            self.written_bytes += size
            if self.written_since is None:
                self.written_since = time.monotonic()

    def _syncer(self):
        lower_priority()
        c = db_connect()
        while True:
            time.sleep(0.25)
            with self.wlock:
                n, since = len(self.written), self.written_since
            if n and (n >= SYNC_FILES or time.monotonic() - since >= SYNC_EVERY or not self.active):
                self.flush(c)

    def _sync_dirs(self, paths):
        fs = {}
        for p in paths:
            d = os.path.dirname(p)
            try:
                fs.setdefault(os.stat(d).st_dev, d)
            except OSError:
                pass
        for d in fs.values():
            syncfs(d)

    def flush(self, c=None):
        """Kleine Dateien gesammelt sichern, prüfen, umbenennen und auf „done“ setzen."""
        with self.sync_lock:
            with self.wlock:
                batch, self.written, self.written_since = self.written, [], None
                self.syncing, self.syncing_bytes = len(batch), self.written_bytes
                self.written_bytes = 0
            if not batch:
                return
            try:
                self._flush(batch, c or db_connect())
            finally:
                with self.wlock:
                    self.syncing = self.syncing_bytes = 0

    def _flush(self, batch, c):
        ok, bad = [], []                     # bad: (jid, part, fehlertext, wiederholen?)
        try:
            self._sync_dirs(p for _, p, _, _, _ in batch)        # Daten sicher auf der Platte
            for jid, part, dst, digest, _ in batch:
                try:
                    if self.gentle and digest is None:
                        with open(part, "rb") as f:
                            drop_cache(f.fileno())
                    if digest is not None and not self._verify(part, digest):
                        bad.append((jid, part, "Prüfung fehlgeschlagen: Ziel weicht von der Quelle ab", True))
                        continue
                    os.replace(part, dst)
                    ok.append(jid)
                except OSError as e:
                    bad.append((jid, part, e.strerror or str(e), e.errno in TRANSIENT))
            if ok:
                done = set(ok)
                self._sync_dirs(dst for jid, _, dst, _, _ in batch if jid in done)    # Umbenennungen
        except OSError as e:
            # nicht sicher geschrieben -> neu kopieren (bei vorübergehenden Fehlern später erneut)
            for jid, _, _, _, _ in batch:
                self._fail(c, jid, f"Sichern fehlgeschlagen: {e.strerror or e}",
                           retry=e.errno in TRANSIENT, restart=True)
            return
        c.execute("BEGIN")
        c.executemany("UPDATE jobs SET status='done', tries=0, retry_at=NULL WHERE id=? AND status='written'",
                      [(j,) for j in ok])
        c.execute("COMMIT")
        for jid in ok:
            self.events.put((jid, {"status": "done", "tries": 0, "retry_at": None}))
        ok_set = set(ok)
        moved = {jid: src for jid, _, _, _, src in batch if src and jid in ok_set}
        for jid, src in moved.items():                                     # Verschieben: Quelle weg
            act = Active(jid, src, "", 0)
            self._remove_source(c, act)
        for jid, part, msg, retry in bad:
            try:
                os.remove(part)
            except OSError:
                pass
            self._fail(c, jid, msg, retry=retry, restart=True)

    def session_bytes(self):
        with self.flock:
            return self.finished_bytes + sum(a.copied - a.base for a in list(self.active.values()))

    def _hdds(self, src, dst):
        """Drehende Festplatten, die diese Kopie belegt (Quelle und/oder Ziel)."""
        if not self.hdd_serial:
            return set()
        devs = {self.devices.dev(src), self.devices.dev(dst)}
        return {d for d in devs if self.devices.describe(d)[0]}

    def _claim(self, c):
        """Nächste wartende Datei übernehmen – aber keine, deren Festplatte schon beschäftigt ist:
        mehrere Kopien gleichzeitig lassen den Lesekopf springen und machen alles langsamer."""
        with self.claim_lock:
            rows = c.execute("SELECT id,src,dst,size,mtime,offset,kind,root,action,link_src FROM jobs "
                             "WHERE status='pending' "
                             "AND (retry_at IS NULL OR retry_at <= ?) ORDER BY prio DESC, id LIMIT 64",
                             (time.time(),)).fetchall()
            for row in rows:
                hdds = self._hdds(row[1], row[2])
                if any(self.dev_busy.get(d, 0) >= 1 for d in hdds):
                    continue
                for d in hdds:
                    self.dev_busy[d] = self.dev_busy.get(d, 0) + 1
                c.execute("UPDATE jobs SET status='running' WHERE id=?", (row[0],))
                return row + (hdds,)
            return None

    def _release(self, hdds):
        with self.claim_lock:
            for d in hdds:
                self.dev_busy[d] = max(0, self.dev_busy.get(d, 0) - 1)

    def _worker(self, nr, stop):
        if self.gentle:
            lower_priority()
        c = db_connect()
        while not stop.is_set() and nr < self.n:
            if self._backlog_full():
                stop.wait(0.1)                   # erst muss das Ziel das Geschriebene sichern
                continue
            job = self._claim(c)
            if not job:
                if nr == 0:
                    self._stamp_dirs(c)
                stop.wait(0.5)
                continue
            jid, src, dst, size, mtime, offset, kind, root, action, link_src, hdds = job
            move = action == "move"
            link = (link_src, root) if link_src and root else None
            root = root or guess_root(src, dst)
            act = Active(jid, src, dst, size)
            self.active[jid] = act
            self.events.put((jid, {"status": "running", "link_wait": False}))
            try:
                if kind == "dir":
                    result, digest = self._mkdir(c, act, move)
                elif kind == "link":
                    result, digest = self._link(c, act, move)
                elif kind == "delete":
                    result, digest = self._delete(c, act, root)
                else:
                    result, digest = self._copy(c, act, mtime, offset, stop, move, link)
                if result == "wait":
                    # harter Link: die erste Datei wird noch kopiert – gleich noch einmal versuchen
                    at = time.time() + 1
                    c.execute("UPDATE jobs SET status='pending', retry_at=? WHERE id=?", (at, jid))
                    self.events.put((jid, {"status": "pending", "retry_at": at, "link_wait": True}))
                elif result:
                    self._touch(src, dst, root, kind, move)
                    self.events.put((jid, {"status": result, "offset": act.size, "size": act.size, "error": None,
                                           "speed": act.avg, "hash": digest, "retry_at": None,
                                           **({"tries": 0} if result == "done" else {})}))
                    if result == "written":
                        self._add_written(act.written, act.size)
                else:
                    self.events.put((jid, {"status": "pending", "offset": act.copied, "size": act.size}))
            except VerifyError as e:
                self._fail(c, jid, str(e), retry=True, restart=True)
            except OSError as e:
                # Offset bleibt auf dem zuletzt *gesicherten* Stand – nur der ist sicher auf der Platte
                self._fail(c, jid, e.strerror or str(e), retry=e.errno in TRANSIENT)
            except Exception as e:
                self._fail(c, jid, str(e), retry=False)
            finally:
                self._release(hdds)
                with self.flock:
                    self.finished_bytes += act.copied - act.base
                    self.active.pop(jid, None)

    # ---------------------------------------------------------------- Ordner und Links
    def _touch(self, src, dst, root, kind, move=False):
        """Ordner unterhalb des Zielordners merken, deren Zeitstempel am Ende übernommen werden –
        beim Verschieben auch die Quellordner, die danach leer sein könnten."""
        root = os.path.normpath(root)
        sd, dd = (src, dst) if kind == "dir" else (os.path.dirname(src), os.path.dirname(dst))
        with self.tlock:
            while os.path.normpath(dd) != root and dd.startswith(root + os.sep):
                if move:
                    self.rm_dirs.add(sd)
                elif dd in self.touched:
                    break                          # Rest der Kette ist schon gemerkt
                self.touched.setdefault(dd, sd)
                sd, dd = os.path.dirname(sd), os.path.dirname(dd)

    def dirs_pending(self):
        return self.stamping or bool(self.touched) or bool(self.rm_dirs)

    def _stamp_dirs(self, c):
        """Ist alles kopiert und gesichert, Zeitstempel und Rechte der Ordner übernehmen – von innen
        nach außen, denn jede neue Datei hätte den Zeitstempel ihres Ordners wieder verändert."""
        if not (self.touched or self.rm_dirs) or self.active or self.unsynced() or \
                c.execute("SELECT 1 FROM jobs WHERE status IN ('pending','running','written') LIMIT 1").fetchone():
            return
        self.stamping = True
        try:
            with self.tlock:
                pairs, self.touched = self.touched, {}
                gone, self.rm_dirs = self.rm_dirs, set()
            for dd in sorted(pairs, key=lambda p: p.count(os.sep), reverse=True):
                try:
                    copy_owner(os.stat(pairs[dd]), dd)
                    shutil.copystat(pairs[dd], dd)
                except OSError:
                    pass                           # z. B. FAT: keine Rechte – Zeiten sind dann trotzdem gesetzt
            for sd in sorted(gone, key=lambda p: p.count(os.sep), reverse=True):
                try:
                    os.rmdir(sd)                   # nur leere Ordner – sonst bleibt er einfach stehen
                except OSError:
                    pass
        finally:
            self.stamping = False

    def _remove_source(self, c, act):
        """Verschieben: Quelle löschen, nachdem die Kopie gesichert (und geprüft) ist."""
        try:
            if os.path.isdir(act.src) and not os.path.islink(act.src):
                os.rmdir(act.src)
            else:
                os.remove(act.src)
        except OSError as e:
            msg = f"Kopiert, aber Quelle nicht gelöscht: {e.strerror or e}"
            c.execute("UPDATE jobs SET error=? WHERE id=?", (msg, act.id))
            self.events.put((act.id, {"error": msg}))

    def _delete(self, c, act, root):
        """Spiegeln: Eintrag im Ziel, den es in der Quelle nicht mehr gibt, in den Papierkorb."""
        dst = os.path.normpath(act.dst)
        if not dst.startswith(os.path.normpath(root) + os.sep):
            raise RuntimeError("Sicherheitsstopp: Ziel liegt nicht im Zielordner – nichts gelöscht")
        if os.path.lexists(act.src):
            raise RuntimeError("Nicht gelöscht: gibt es in der Quelle inzwischen wieder")
        if os.path.lexists(dst):
            gio = shutil.which("gio")
            if gio is None:
                raise RuntimeError("Nicht gelöscht: Papierkorb nicht verfügbar (Programm „gio“ fehlt)")
            r = subprocess.run([gio, "trash", "--", dst], capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                raise RuntimeError(f"Nicht in den Papierkorb verschoben: {(r.stderr or '').strip() or r.returncode}")
        c.execute("UPDATE jobs SET status='done', error=NULL, tries=0, retry_at=NULL WHERE id=?", (act.id,))
        return "done", None

    def _mkdir(self, c, act, move=False):
        """Leerer Ordner."""
        os.makedirs(act.dst, exist_ok=True)
        if move:
            self._remove_source(c, act)
        c.execute("UPDATE jobs SET status='done', error=NULL, tries=0, retry_at=NULL WHERE id=?", (act.id,))
        return "done", None

    def _link(self, c, act, move=False):
        """Symbolischen Link als Link anlegen: erst unter Hilfsnamen, dann umbenennen (ersetzt atomar)."""
        target = os.readlink(act.src)
        d = os.path.dirname(act.dst)
        os.makedirs(d, exist_ok=True)
        tmp = part_path(act.dst, act.id)
        if os.path.lexists(tmp):
            os.remove(tmp)
        os.symlink(target, tmp)
        st = os.lstat(act.src)
        copy_owner(st, tmp, follow=False)
        try:
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
        except (OSError, NotImplementedError):
            pass
        if self.verify and os.readlink(tmp) != target:
            os.remove(tmp)
            raise VerifyError("Prüfung fehlgeschlagen: Linkziel weicht ab")
        os.replace(tmp, act.dst)
        fsync_dir(d)
        c.execute("UPDATE jobs SET status='done', error=NULL, hash=NULL, tries=0, retry_at=NULL WHERE id=?", (act.id,))
        if move:
            self._remove_source(c, act)
        return "done", None

    def _fail(self, c, jid, msg, retry, restart=False):
        """Fehler verbuchen. Vorübergehende Fehler: bis zu len(RETRY_DELAYS)-mal später erneut."""
        tries = (c.execute("SELECT tries FROM jobs WHERE id=?", (jid,)).fetchone() or [0])[0] or 0
        reset = ", offset=0, hash=NULL" if restart else ""
        if retry and tries < len(RETRY_DELAYS):
            delay = RETRY_DELAYS[tries]
            at = time.time() + delay
            note = f"{msg} – Versuch {tries + 2}/{len(RETRY_DELAYS) + 1} folgt"
            c.execute(f"UPDATE jobs SET status='pending', tries=?, retry_at=?, error=?{reset} WHERE id=?",
                      (tries + 1, at, note, jid))
            kw = {"status": "pending", "tries": tries + 1, "retry_at": at, "error": note}
        else:
            if tries:
                msg += f" (nach {tries + 1} Versuchen)"
            c.execute(f"UPDATE jobs SET status='failed', retry_at=NULL, error=?{reset} WHERE id=?", (msg, jid))
            kw = {"status": "failed", "retry_at": None, "error": msg}
        if restart:
            kw["offset"] = 0
        else:
            kw["offset"] = (c.execute("SELECT offset FROM jobs WHERE id=?", (jid,)).fetchone() or [0])[0]
        self.events.put((jid, kw))

    def _hardlink(self, c, act, link, move):
        """Harter Link in der Quelle: im Ziel auf die schon kopierte erste Datei verlinken.
        Rückgabe wie _copy; („wait“, None), solange die erste noch kopiert wird; None = normal kopieren
        (erste Datei übersprungen/fehlerhaft, Ziel kann keine harten Links, anderes Laufwerk …)."""
        link_src, root = link
        row = c.execute("SELECT status,dst,size,hash FROM jobs WHERE src=? AND root=? ORDER BY id LIMIT 1",
                        (link_src, root)).fetchone()
        if row is None or row[0] not in ("done", "pending", "running", "written"):
            return None
        status, first, size, digest = row
        if status != "done":
            return "wait", None
        try:
            if size != act.size or os.stat(first).st_size != act.size:
                return None                      # inzwischen geändert – dann lieber echt kopieren
            os.makedirs(os.path.dirname(act.dst), exist_ok=True)
            tmp = part_path(act.dst, act.id)
            if os.path.lexists(tmp):
                os.remove(tmp)
            os.link(first, tmp)
        except OSError:
            return None
        os.replace(tmp, act.dst)
        fsync_dir(os.path.dirname(act.dst))
        act.copied = act.size
        c.execute("UPDATE jobs SET status='done', offset=size, error=NULL, hash=?, tries=0, retry_at=NULL "
                  "WHERE id=?", (digest, act.id))
        if move:
            self._remove_source(c, act)
        return "done", digest

    def _clone(self, src, part):
        """Reflink: Ziel teilt sich die Blöcke mit der Quelle (btrfs, XFS …). True, wenn es ging."""
        key = (self.devices.dev(src), self.devices.dev(part))
        if key in self.no_clone:
            return False
        try:
            with open(src, "rb") as fs, open(part, "wb") as fd:
                fcntl.ioctl(fd.fileno(), FICLONE, fs.fileno())
                os.fsync(fd.fileno())
            return True
        except OSError as e:
            if e.errno in (errno.EOPNOTSUPP, errno.ENOTTY, errno.EXDEV, errno.EINVAL, errno.ENOSYS):
                self.no_clone.add(key)           # geht hier grundsätzlich nicht – nicht jedes Mal probieren
            return False

    def _copy(self, c, act, mtime, offset, stop, move=False, link=None):
        """Rückgabe (status, sha256) mit status „done“/„written“, oder (None, None) bei Pause."""
        if move and self.devices.dev(act.src) == self.devices.dev(act.dst) >= 0:
            # Verschieben auf demselben Laufwerk: umbenennen genügt – sofort, ohne Kopie
            os.makedirs(os.path.dirname(act.dst), exist_ok=True)
            try:
                os.replace(act.src, act.dst)
            except OSError as e:
                if e.errno != errno.EXDEV:             # z. B. Bind-Mount: dann eben kopieren
                    raise
            else:
                fsync_dir(os.path.dirname(act.dst))
                fsync_dir(os.path.dirname(act.src))
                c.execute("UPDATE jobs SET status='done', offset=size, error=NULL, hash=NULL, tries=0, "
                          "retry_at=NULL WHERE id=?", (act.id,))
                return "done", None
        st = os.stat(act.src)
        if st.st_size != act.size or abs(st.st_mtime - mtime) > 1e-3:
            offset, act.size = 0, st.st_size        # Quelle geändert -> neu
            c.execute("UPDATE jobs SET size=?, mtime=?, offset=0 WHERE id=?", (st.st_size, st.st_mtime, act.id))
        if link and offset == 0:
            r = self._hardlink(c, act, link, move)
            if r is not None:
                return r
        small = act.size < SMALL
        os.makedirs(os.path.dirname(act.dst), exist_ok=True)
        part = part_path(act.dst, act.id)
        if offset > 0 and (not os.path.exists(part) or os.path.getsize(part) < offset):
            offset = 0
        act.copied = act.base = offset
        h = hashlib.sha256() if self.verify or move else None   # Verschieben: immer prüfen

        def pause(fd):
            if sparse:
                fd.truncate()
            fd.flush(); os.fsync(fd.fileno())
            c.execute("UPDATE jobs SET status='pending', offset=? WHERE id=?", (act.copied, act.id))
            return None, None

        # Reflink (btrfs/XFS, gleiches Dateisystem): sofort fertig, die Daten sind dieselben Blöcke –
        # deshalb auch kein Zurücklesen zum Prüfen
        cloned = offset == 0 and self._clone(act.src, part)
        # dünn besetzte Quelle (VM-Image, Datenbank …): Nullblöcke als Lücken anlegen
        sparse = st.st_blocks * 512 < st.st_size
        if cloned:
            h, t0 = None, time.monotonic()
            act.copied = act.size
        else:
            with open(act.src, "rb") as fs, open(part, "r+b" if offset else "wb") as fd:
                fd.truncate(offset)
                if h is not None and offset:
                    # Fortsetzung: Prüfsumme braucht auch den schon kopierten Anfang der Quelle
                    while fs.tell() < offset:
                        if stop.is_set():
                            return pause(fd)
                        h.update(fs.read(min(CHUNK, offset - fs.tell())))
                fs.seek(offset)
                fd.seek(offset)
                gentle = self.gentle
                if gentle:
                    try:
                        os.posix_fadvise(fs.fileno(), 0, 0, os.POSIX_FADV_SEQUENTIAL)
                    except (AttributeError, OSError):
                        pass
                t0 = last = time.monotonic()
                act.samples.append((last, offset))   # Startpunkt: Tempo schon nach dem ersten Block
                while True:
                    if stop.is_set():
                        return pause(fd)
                    # nur so viel beim Tempo-Limit anmelden, wie von der Datei noch übrig ist –
                    # sonst blockiert eine 5-KB-Datei die anderen Worker für einen ganzen Block
                    size = min(self.throttle.chunk(), act.size - act.copied)
                    if size <= 0:
                        break
                    self.throttle.wait(size, stop)
                    if stop.is_set():
                        continue
                    buf = fs.read(size)
                    self.throttle.refund(size - len(buf))
                    if not buf:
                        break                            # Quelle während des Kopierens geschrumpft
                    (write_sparse(fd, buf) if sparse else fd.write(buf))
                    if h is not None:
                        h.update(buf)
                    if gentle:
                        drop_cache(fs.fileno(), act.copied, len(buf))   # Quelle: gelesen = nicht mehr nötig
                    act.copied += len(buf)
                    now = time.monotonic()
                    act.samples.append((now, act.copied))
                    if now - last >= SYNC_EVERY:
                        if sparse:
                            fd.truncate()                    # Lücke am Ende: Länge stimmt trotzdem
                        fd.flush(); os.fsync(fd.fileno())
                        if gentle:
                            drop_cache(fd.fileno())                  # Ziel: gesichert = aus dem Cache
                        c.execute("UPDATE jobs SET offset=? WHERE id=?", (act.copied, act.id))
                        last = now
                if sparse:
                    fd.truncate()
                fd.flush()
                if not small:
                    os.fsync(fd.fileno())
                    if gentle:
                        drop_cache(fd.fileno())
                    # alles sicher auf der Platte: Stand sichern, damit ein späterer Fehler (Prüfen,
                    # Umbenennen …) beim nächsten Versuch nicht wieder alles kopieren muss
                    c.execute("UPDATE jobs SET offset=? WHERE id=?", (act.copied, act.id))
        took = time.monotonic() - t0                 # große Dateien: inkl. abschließendem fsync
        moved = act.copied - act.base                # bei Fortsetzung: nur der Teil dieser Sitzung
        act.avg = moved / took if moved and took > 0 and not cloned else None
        digest = h.hexdigest() if h is not None else None
        copy_owner(st, part)                         # vor copystat: chown löscht Set-UID-Bits
        shutil.copystat(act.src, part)               # Zeiten/Rechte/ACLs gehen beim Umbenennen mit

        if small:
            # Versuchszähler bleibt, bis die Datei gesichert und geprüft ist (flush)
            c.execute("UPDATE jobs SET status='written', offset=size, error=NULL, speed=?, hash=?, retry_at=NULL "
                      "WHERE id=?",
                      (act.avg, digest, act.id))
            # in die Sicher-Liste erst nach der Meldung „written“ an die Anzeige (siehe _worker) –
            # sonst kann das „done“ von flush() vorher ankommen und wird überschrieben
            act.written = (act.id, part, act.dst, digest, act.src if move else None)
            return "written", digest

        # große Datei (Stand ist gesichert): Abbruch beim Prüfen → beim Fortsetzen nur noch prüfen
        if digest is not None:
            self.events.put((act.id, {"status": "verifying"}))
            act.verified = 0
            ok = self._verify(part, digest, stop, act)
            act.verified = None
            if ok is None:
                c.execute("UPDATE jobs SET status='pending' WHERE id=?", (act.id,))
                return None, None
            if not ok:
                os.remove(part)
                raise VerifyError("Prüfung fehlgeschlagen: Ziel weicht von der Quelle ab")
        os.replace(part, act.dst)
        fsync_dir(os.path.dirname(act.dst))
        c.execute("UPDATE jobs SET status='done', offset=size, error=NULL, speed=?, hash=?, tries=0, retry_at=NULL "
                  "WHERE id=?", (act.avg, digest, act.id))
        if move:
            self._remove_source(c, act)
        return "done", digest
