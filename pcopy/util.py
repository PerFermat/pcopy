"""Formatierung von Größen und Zeiten."""


def hsize(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}".replace(".", ",")
        n /= 1024


def htime(s):
    if not s or s == float("inf"):
        return "–"
    s = int(s + 0.999)                     # aufrunden: 0,4 s Rest ist „0:01“, nicht „0:00“
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
