"""Programmstart: Einzelinstanz, Übergabe von Pfaden an ein laufendes Fenster."""
import fcntl
import os
import sys

from .qt import QtCore, QtNetwork, QtWidgets
from .db import DB_PATH, migrate_old_db
from .window import APP, MainWindow

SOCKET_NAME = f"pcopy-gui-{os.getuid()}"


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setDesktopFileName("pcopy-gui")
    paths = [os.path.abspath(p) for p in sys.argv[1:] if os.path.exists(p)]

    # Läuft schon ein Fenster? Dann Pfade dorthin schicken und beenden.
    sock = QtNetwork.QLocalSocket()
    sock.connectToServer(SOCKET_NAME)
    if sock.waitForConnected(300):
        sock.write("\n".join(paths).encode() or b"\n")
        sock.flush()
        sock.waitForBytesWritten(1000)
        sock.disconnectFromServer()
        return 0

    lockf = open(str(DB_PATH) + ".lock", "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        QtWidgets.QMessageBox.critical(None, "pcopy", "pcopy läuft bereits.")
        return 1

    try:
        migrated = migrate_old_db()
    except Exception as e:
        migrated = None
        QtWidgets.QMessageBox.warning(None, "pcopy", f"Alte Liste konnte nicht übernommen werden:\n{e}")

    win = MainWindow()
    if migrated:
        win.status.showMessage(f"Liste vom alten Speicherort übernommen ({migrated} Einträge) – "
                               f"jetzt in {DB_PATH}", 15000)
    server = QtNetwork.QLocalServer()
    QtNetwork.QLocalServer.removeServer(SOCKET_NAME)
    server.listen(SOCKET_NAME)

    def incoming():
        conn = server.nextPendingConnection()
        buf = bytearray()

        def read():
            buf.extend(bytes(conn.readAll()))

        def done():
            read()
            win.showNormal(); win.raise_(); win.activateWindow()
            got = [p for p in bytes(buf).decode().splitlines() if p]
            if got:
                win.add_paths(got)
            conn.deleteLater()

        conn.readyRead.connect(read)
        conn.disconnected.connect(done)

    server.newConnection.connect(incoming)
    win.show()
    if paths:
        QtCore.QTimer.singleShot(200, lambda: win.add_paths(paths))
    win._server, win._lock = server, lockf
    rc = app.exec()
    server.close()
    fcntl.flock(lockf, fcntl.LOCK_UN)
    if not win.clean_exit:
        # Ein Worker hängt im Kernel – nicht auf ihn warten, sofort beenden.
        sys.stdout.flush(); sys.stderr.flush()
        os._exit(rc)
    return rc
