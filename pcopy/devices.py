"""Laufwerke erkennen (drehende Festplatte oder nicht) und Threads rücksichtsvoll einstellen."""
import ctypes
import ctypes.util
import os
import platform
import threading

# ioprio_set: Klasse IDLE – die Platte bekommt pcopy nur, wenn sonst niemand sie braucht
# (mq-deadline/BFQ; mq-deadline lässt dank „aging“ trotzdem nichts ewig warten)
_IOPRIO_SET = {"x86_64": 251, "aarch64": 30, "i386": 289, "i686": 289, "armv7l": 314, "ppc64le": 273}
IOPRIO_CLASS_IDLE, IOPRIO_WHO_PROCESS = 3, 1

try:
    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
except OSError:
    _libc = None


def lower_priority():
    """Aufrufenden Thread auf niedrige Platten- und CPU-Priorität setzen (nur Linux)."""
    tid = threading.get_native_id()
    nr = _IOPRIO_SET.get(platform.machine())
    if _libc is not None and nr is not None:
        _libc.syscall(nr, IOPRIO_WHO_PROCESS, tid, IOPRIO_CLASS_IDLE << 13)
    try:
        os.setpriority(os.PRIO_PROCESS, tid, max(os.getpriority(os.PRIO_PROCESS, tid), 10))
    except (OSError, AttributeError):
        pass


def drop_cache(fd, offset=0, length=0):
    """Gelesene/gesicherte Seiten aus dem Seiten-Cache werfen, damit große Kopien den
    Speicher der anderen Programme nicht verdrängen."""
    try:
        os.posix_fadvise(fd, offset, length, os.POSIX_FADV_DONTNEED)
    except (AttributeError, OSError):
        pass


class Devices:
    """Welches Blockgerät steckt hinter einem Pfad, und dreht es sich?"""

    def __init__(self):
        self.lock = threading.Lock()
        self.by_dir = {}          # Ordner -> st_dev
        self.info = {}            # st_dev -> (drehend?, name)

    def dev(self, path):
        d = os.path.dirname(path)
        with self.lock:
            v = self.by_dir.get(d)
        if v is None:
            p = d
            while not os.path.exists(p) and os.path.dirname(p) != p:
                p = os.path.dirname(p)                 # Ziel-Ordner gibt es evtl. noch nicht
            try:
                v = os.stat(p).st_dev
            except OSError:
                v = -1
            with self.lock:
                self.by_dir[d] = v
        return v

    def describe(self, dev):
        """(drehend?, Name) – Netz-, FUSE- und RAM-Dateisysteme gelten als nicht drehend."""
        with self.lock:
            if dev in self.info:
                return self.info[dev]
        rot, name = False, None
        if dev >= 0 and os.major(dev):
            sysp = os.path.realpath(f"/sys/dev/block/{os.major(dev)}:{os.minor(dev)}")
            if os.path.exists(os.path.join(sysp, "partition")):
                sysp = os.path.dirname(sysp)          # Partition -> ganzes Laufwerk
            name = os.path.basename(sysp)
            try:
                with open(os.path.join(sysp, "queue", "rotational")) as f:
                    rot = f.read().strip() == "1"
            except OSError:
                pass
            try:
                with open(os.path.join(sysp, "device", "model")) as f:
                    name = f"{name} ({f.read().strip()})"
            except OSError:
                pass
        with self.lock:
            self.info[dev] = (rot, name)
        return rot, name
