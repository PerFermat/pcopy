"""Qt-Bindung: PyQt6 bevorzugt, sonst PySide6."""
import sys

try:
    from PyQt6 import QtCore, QtGui, QtNetwork, QtWidgets
except ImportError:
    try:
        from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets
    except ImportError:
        sys.exit("Bitte installieren:  sudo apt install python3-pyqt6   (oder: pip install pyside6)")

Qt = QtCore.Qt
