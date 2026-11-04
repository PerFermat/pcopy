"""Job-Datenbank (SQLite). Liegt immer neben dem Programm."""
import os
import sqlite3
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent          # Ordner mit pcopy-gui.py
DB_PATH = Path(os.environ.get("PCOPY_DB", APP_DIR / "pcopy.db"))
OLD_DB = Path.home() / ".local/share/pcopy/jobs.db"       # Ort bis Nov. 2026


def db_connect(path=DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path), timeout=60, isolation_level=None, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    # NORMAL statt FULL: kein fsync der DB pro Commit. Bei Stromausfall gehen höchstens die
    # letzten Statuswechsel verloren – dann wird eine Datei eben nochmal kopiert.
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("""CREATE TABLE IF NOT EXISTS jobs(
        id INTEGER PRIMARY KEY, src TEXT NOT NULL, dst TEXT NOT NULL,
        size INTEGER, mtime REAL, offset INTEGER DEFAULT 0,
        status TEXT DEFAULT 'pending', error TEXT, added REAL,
        UNIQUE(src, dst))""")
    cols = [r[1] for r in c.execute("PRAGMA table_info(jobs)")]
    if "root" not in cols:
        c.execute("ALTER TABLE jobs ADD COLUMN root TEXT")      # Zielordner beim Hinzufügen
    if "speed" not in cols:
        c.execute("ALTER TABLE jobs ADD COLUMN speed REAL")     # Ø-Tempo (Bytes/s) einer fertigen Datei
    return c


def guess_root(src, dst):
    """Zielordner für ältere Einträge ohne gespeicherten root: dst ohne den Teil, den es mit
    src gemeinsam hat (Ordnername + Unterpfad). Für neue Einträge wird root gespeichert."""
    s, d = Path(src).parts, Path(dst).parts
    k = 0
    while k < min(len(s), len(d)) - 1 and s[-1 - k] == d[-1 - k]:
        k += 1
    return str(Path(*d[:len(d) - max(k, 1)]))


def migrate_old_db():
    """Liste vom alten Ort (~/.local/share/pcopy) einmalig übernehmen, falls es hier noch keine gibt.
    Die alte Datei bleibt unverändert liegen. Rückgabe: Anzahl übernommener Einträge oder None."""
    if DB_PATH.exists() or not OLD_DB.exists() or "PCOPY_DB" in os.environ:
        return None
    src = sqlite3.connect(str(OLD_DB))
    tmp = DB_PATH.with_name(DB_PATH.name + ".tmp")
    dst = sqlite3.connect(str(tmp))
    try:
        src.backup(dst)                      # konsistente Kopie inkl. WAL-Inhalt
        n = dst.execute("SELECT count(*) FROM jobs").fetchone()[0]
    finally:
        dst.close()
        src.close()
    tmp.rename(DB_PATH)
    return n
