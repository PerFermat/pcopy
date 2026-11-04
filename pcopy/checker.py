"""Nachträgliches Prüfen fertiger Dateien (Rechtsklick → „Prüfen“).

Läuft in einem eigenen Thread nacheinander, unabhängig vom Kopieren. Das Ziel wird am Seiten-Cache
vorbei von der Platte gelesen; verglichen wird mit der beim Kopieren gespeicherten SHA-256 oder –
falls es keine gibt – mit der SHA-256 der Quelle."""
import hashlib
import os
import queue
import threading

from .db import db_connect
from .devices import drop_cache, lower_priority
from .engine import Active

CHUNK = 1024 * 1024


class Checker:
    def __init__(self, engine):
        self.engine = engine
        self.q = queue.Queue()
        self.current = None           # Active-Objekt der gerade geprüften Datei (für die Anzeige)
        self.gen = 0                  # erhöht bei Abbruch: wartende Aufträge verwerfen
        self.cancel = threading.Event()
        self.ok = self.bad = 0        # Ergebnis der laufenden Prüfrunde (für die Statusmeldung)
        threading.Thread(target=self._run, daemon=True).start()

    def add(self, rows):
        """rows: Zeilen-Dicts aus dem Tabellenmodell (status done/skipped)."""
        for r in rows:
            self.q.put((self.gen, r["id"], r["src"], r["dst"], r["size"], r.get("hash"), r["status"],
                        r.get("kind", "file")))
            self.engine.events.put((r["id"], {"status": "check_wait"}))

    def stop(self):
        """Laufende und wartende Prüfungen abbrechen; die Einträge behalten ihren alten Status."""
        self.gen += 1
        self.cancel.set()

    def busy(self):
        return self.current is not None or not self.q.empty()

    def _hash(self, path, act, base, uncached):
        h = hashlib.sha256()
        never = threading.Event()
        th = self.engine.throttle
        with open(path, "rb") as f:
            if uncached:
                drop_cache(f.fileno())                # vom Datenträger lesen, nicht aus dem Cache
            done = 0
            while True:
                if self.cancel.is_set():
                    return None
                size = th.chunk()
                th.wait(size, never)
                buf = f.read(size)
                th.refund(size - len(buf))
                if not buf:
                    if self.engine.gentle:
                        drop_cache(f.fileno())
                    return h.hexdigest()
                h.update(buf)
                done += len(buf)
                act.verified = base + done * act.scale

    def _run(self):
        lower_priority()
        c = db_connect()
        ev = self.engine.events
        while True:
            gen, jid, src, dst, size, digest, old, kind = self.q.get()
            if gen != self.gen:
                ev.put((jid, {"status": old}))
                continue
            self.cancel.clear()
            act = Active(jid, src, dst, size)
            act.verified = 0
            act.scale = 1.0 if digest else 0.5   # ohne Prüfsumme: erst Quelle, dann Ziel lesen
            self.current = act
            ev.put((jid, {"status": "checking"}))
            try:
                if kind in ("dir", "link"):
                    if kind == "dir":
                        ok, msg = os.path.isdir(dst), "Prüfung: Ordner fehlt"
                    else:
                        ok = os.path.islink(dst) and os.readlink(dst) == os.readlink(src)
                        msg = "Prüfung: Link fehlt oder zeigt woandershin"
                    if ok:
                        self.ok += 1
                        ev.put((jid, {"status": old, "checked": True, "error": None}))
                    else:
                        self.bad += 1
                        c.execute("UPDATE jobs SET status='failed', error=? WHERE id=?", (msg, jid))
                        ev.put((jid, {"status": "failed", "error": msg}))
                    continue
                if not os.path.exists(dst):
                    ok, msg = False, "Prüfung: Zieldatei fehlt"
                else:
                    want = digest or self._hash(src, act, 0, uncached=False)
                    got = want and self._hash(dst, act, size * (1 - act.scale), uncached=True)
                    if want is None or got is None:          # abgebrochen
                        ev.put((jid, {"status": old}))
                        continue
                    ok, msg = got == want, "Prüfung fehlgeschlagen: Ziel weicht von der Quelle ab"
                    digest = want
                if ok:
                    self.ok += 1
                    c.execute("UPDATE jobs SET hash=?, error=NULL WHERE id=?", (digest, jid))
                    ev.put((jid, {"status": old, "hash": digest, "error": None}))
                else:
                    self.bad += 1
                    c.execute("UPDATE jobs SET status='failed', offset=0, error=? WHERE id=?", (msg, jid))
                    ev.put((jid, {"status": "failed", "offset": 0, "error": msg}))
            except Exception as e:                       # der Prüf-Thread darf nie ausfallen
                self.bad += 1
                msg = f"Prüfung nicht möglich: {getattr(e, 'strerror', None) or e}"
                c.execute("UPDATE jobs SET error=? WHERE id=?", (msg, jid))
                ev.put((jid, {"status": old, "error": msg}))
            finally:
                self.current = None
