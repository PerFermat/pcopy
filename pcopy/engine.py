"""Kopier-Engine: Worker-Threads, Tempo-Begrenzung, wiederaufnehmbares Kopieren.

Große Dateien werden einzeln mit fsync gesichert (und alle SYNC_EVERY s ihr Stand). Kleine
Dateien (< SMALL) bekommen erst den Status „written“; ein Sync-Thread sichert sie gesammelt per
syncfs und setzt sie dann gemeinsam auf „done“. Was beim Programmstart noch „written“ ist, war
nicht sicher auf der Platte und wird neu kopiert."""
import ctypes
import ctypes.util
import os
import queue
import shutil
import threading
import time
from collections import deque

from .db import db_connect

CHUNK = 1024 * 1024
SYNC_EVERY = 2.0          # s: Stand großer Dateien sichern / kleine Dateien gesammelt syncen
SMALL = 8 * 1024 * 1024   # kleiner als das: kein fsync pro Datei, sondern gesammelt
SYNC_FILES = 200          # spätestens nach so vielen kleinen Dateien syncen

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


class Active:
    def __init__(self, jid, src, dst, size):
        self.id, self.src, self.dst, self.size = jid, src, dst, size
        self.copied = self.base = 0
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
        self.n = 4
        self.written = []         # (job-id, ziel) kleiner Dateien, die noch gesynct werden müssen
        self.written_since = None
        self.wlock = threading.Lock()
        self.sync_lock = threading.Lock()
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
        f = threading.Thread(target=self.flush, daemon=True)
        f.start()
        f.join(max(2.0, end - time.monotonic()))
        return not any(t.is_alive() for t in threads) and not f.is_alive()

    # ---------------------------------------------------------------- gesammeltes Sichern
    def unsynced(self):
        with self.wlock:
            return len(self.written)

    def _syncer(self):
        c = db_connect()
        while True:
            time.sleep(0.25)
            with self.wlock:
                n, since = len(self.written), self.written_since
            if n and (n >= SYNC_FILES or time.monotonic() - since >= SYNC_EVERY or not self.active):
                self.flush(c)

    def flush(self, c=None):
        """Alle „written“-Dateien per syncfs sichern und gemeinsam auf „done“ setzen."""
        with self.sync_lock:
            with self.wlock:
                batch, self.written, self.written_since = self.written, [], None
            if not batch:
                return
            c = c or db_connect()
            ids = [(jid,) for jid, _ in batch]
            try:
                fs = {}
                for _, dst in batch:
                    d = os.path.dirname(dst)
                    try:
                        fs.setdefault(os.stat(d).st_dev, d)
                    except OSError:
                        pass
                for d in fs.values():
                    syncfs(d)
            except OSError as e:
                # nicht sicher geschrieben -> neu kopieren
                c.executemany("UPDATE jobs SET status='pending', offset=0 WHERE id=? AND status='written'", ids)
                for (jid,) in ids:
                    self.events.put((jid, {"status": "pending", "offset": 0,
                                           "error": f"Sichern fehlgeschlagen: {e.strerror or e}"}))
                return
            c.execute("BEGIN")
            c.executemany("UPDATE jobs SET status='done' WHERE id=? AND status='written'", ids)
            c.execute("COMMIT")
            for (jid,) in ids:
                self.events.put((jid, {"status": "done"}))

    def session_bytes(self):
        with self.flock:
            return self.finished_bytes + sum(a.copied - a.base for a in list(self.active.values()))

    def _claim(self, c):
        with self.claim_lock:
            row = c.execute("SELECT id,src,dst,size,mtime,offset FROM jobs "
                            "WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if row:
                c.execute("UPDATE jobs SET status='running' WHERE id=?", (row[0],))
            return row

    def _worker(self, nr, stop):
        c = db_connect()
        while not stop.is_set() and nr < self.n:
            job = self._claim(c)
            if not job:
                stop.wait(0.5)
                continue
            jid, src, dst, size, mtime, offset = job
            act = Active(jid, src, dst, size)
            self.active[jid] = act
            self.events.put((jid, {"status": "running"}))
            try:
                result = self._copy(c, act, mtime, offset, stop)
                if result:
                    self.events.put((jid, {"status": result, "offset": act.size, "size": act.size, "error": None,
                                           "speed": act.avg}))
                else:
                    self.events.put((jid, {"status": "pending", "offset": act.copied, "size": act.size}))
            except Exception as e:
                msg = getattr(e, "strerror", None) or str(e)
                c.execute("UPDATE jobs SET status='failed', error=?, offset=? WHERE id=?", (msg, act.copied, jid))
                self.events.put((jid, {"status": "failed", "error": msg, "offset": act.copied}))
            finally:
                with self.flock:
                    self.finished_bytes += act.copied - act.base
                    self.active.pop(jid, None)

    def _copy(self, c, act, mtime, offset, stop):
        st = os.stat(act.src)
        if st.st_size != act.size or abs(st.st_mtime - mtime) > 1e-3:
            offset, act.size = 0, st.st_size        # Quelle geändert -> neu
            c.execute("UPDATE jobs SET size=?, mtime=?, offset=0 WHERE id=?", (st.st_size, st.st_mtime, act.id))
        small = act.size < SMALL
        os.makedirs(os.path.dirname(act.dst), exist_ok=True)
        if offset > 0 and (not os.path.exists(act.dst) or os.path.getsize(act.dst) < offset):
            offset = 0
        act.copied = act.base = offset
        with open(act.src, "rb") as fs, open(act.dst, "r+b" if offset else "wb") as fd:
            fd.truncate(offset)
            fs.seek(offset)
            fd.seek(offset)
            t0 = last = time.monotonic()
            act.samples.append((last, offset))   # Startpunkt: Tempo schon nach dem ersten Block
            while True:
                if stop.is_set():
                    fd.flush(); os.fsync(fd.fileno())
                    c.execute("UPDATE jobs SET status='pending', offset=? WHERE id=?", (act.copied, act.id))
                    return None
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
                fd.write(buf)
                act.copied += len(buf)
                now = time.monotonic()
                act.samples.append((now, act.copied))
                if now - last >= SYNC_EVERY:
                    fd.flush(); os.fsync(fd.fileno())
                    c.execute("UPDATE jobs SET offset=? WHERE id=?", (act.copied, act.id))
                    last = now
            fd.flush()
            if not small:
                os.fsync(fd.fileno())
        shutil.copystat(act.src, act.dst)
        took = time.monotonic() - t0                 # große Dateien: inkl. abschließendem fsync
        moved = act.copied - act.base                # bei Fortsetzung: nur der Teil dieser Sitzung
        act.avg = moved / took if moved and took > 0 else None
        status = "written" if small else "done"
        c.execute("UPDATE jobs SET status=?, offset=size, error=NULL, speed=? WHERE id=?", (status, act.avg, act.id))
        if small:
            with self.wlock:
                self.written.append((act.id, act.dst))
                if self.written_since is None:
                    self.written_since = time.monotonic()
        return status
