"""Prüfung vor dem Start: passt die Warteschlange auf die Ziel-Laufwerke?

  • freier Platz je Ziel-Dateisystem
  • FAT32: Dateien ab 4 GiB sind dort unmöglich
  • FAT32/exFAT: Zeichen, die in Datei- und Ordnernamen nicht erlaubt sind
"""
import os
import re
from html import escape

from .db import guess_root
from .util import hsize

FAT = {"vfat", "msdos", "fat"}
NO_SPECIAL = FAT | {"exfat"}
BAD_CHARS = set('<>:"\\|?*') | {chr(i) for i in range(32)}
FAT_MAX = 4 * 1024 ** 3                 # FAT32: höchstens 4 GiB − 1 Byte pro Datei


def _mounts():
    """Einhängepunkt -> Dateisystemtyp."""
    m = {}
    try:
        with open("/proc/self/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    point = re.sub(r"\\([0-7]{3})", lambda x: chr(int(x.group(1), 8)), parts[1])  # \040 = Leerzeichen
                    m[point] = parts[2]
    except OSError:
        pass
    return m


def _existing(path):
    p = os.path.abspath(path)
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


class Target:
    def __init__(self, root, mounts):
        p = os.path.realpath(_existing(root))
        self.mount = max((m for m in mounts if p == m or p.startswith(m.rstrip("/") + "/")), key=len, default="/")
        self.fstype = mounts.get(self.mount, "?")
        st = os.statvfs(p)
        self.free = st.f_bavail * st.f_frsize
        self.dev = os.stat(p).st_dev
        self.need = 0


def check(db):
    """Rückgabe: (meldungen, betroffen) – betroffen: {job-id: grund} für Dateien, die auf ihrem
    Ziel nicht angelegt werden können."""
    mounts = _mounts()
    targets, by_dev, affected = {}, {}, {}
    too_big, bad_name = [], []
    links = []
    rows = db.execute("SELECT id,src,dst,root,size,offset,kind FROM jobs WHERE status='pending'").fetchall()
    for jid, src, dst, root, size, offset, kind in rows:
        root = root or guess_root(src, dst)
        t = targets.get(root)
        if t is None:
            try:
                t = Target(root, mounts)
            except OSError:
                continue
            t = by_dev.setdefault(t.dev, t)
            targets[root] = t
        if kind == "link" and t.fstype in NO_SPECIAL:
            affected[jid] = f"{t.fstype} kann keine symbolischen Links speichern ({t.mount})"
            links.append(os.path.basename(dst))
            continue
        if t.fstype in FAT and size >= FAT_MAX:
            affected[jid] = f"Größer als 4 GB – passt nicht auf FAT32 ({t.mount})"
            too_big.append(os.path.basename(dst))
            continue
        if t.fstype in NO_SPECIAL:
            rel = os.path.relpath(dst, root)
            if any(ch in BAD_CHARS for ch in rel.replace(os.sep, "")):
                affected[jid] = f"Name enthält Zeichen, die {t.fstype} nicht erlaubt (< > : \" \\ | ? *)"
                bad_name.append(rel)
                continue
        t.need += (size or 0) - (offset or 0)

    msgs = []
    for t in by_dev.values():
        if t.need > t.free:
            msgs.append(f"<b>Zu wenig Platz</b> auf {t.mount}: benötigt ca. {hsize(t.need)}, frei {hsize(t.free)}."
                        " (Dateien, die ersetzt werden, sind nicht abgezogen.)")
    if too_big:
        msgs.append(f"<b>{len(too_big)} Dateien sind größer als 4 GB</b> und passen nicht auf FAT32: "
                    + _examples(too_big))
    if links:
        msgs.append(f"<b>{len(links)} symbolische Links</b> lassen sich auf FAT32/exFAT nicht anlegen: "
                    + _examples(links))
    if bad_name:
        msgs.append(f"<b>{len(bad_name)} Datei- oder Ordnernamen</b> enthalten Zeichen, die das Ziel nicht "
                    "erlaubt (&lt; &gt; : \" \\ | ? *): " + _examples(bad_name))
    return msgs, affected


def _examples(names, n=4):
    shown = ", ".join(escape(x) for x in names[:n])
    return shown + (f" … (+{len(names) - n})" if len(names) > n else "")
