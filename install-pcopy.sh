#!/bin/bash
# Richtet pcopy-gui für den aktuellen Benutzer ein (KDE/Dolphin).
# Es wird nichts kopiert: Startmenü, Schreibtisch-Symbol, Dolphin-Kontextmenü und der Befehl
# „pcopy-gui“ zeigen auf diesen Ordner. Änderungen am Programm wirken also sofort.
# Die Job-Liste (pcopy.db) liegt ebenfalls hier.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

python3 -c "import PyQt6" 2>/dev/null || python3 -c "import PySide6" 2>/dev/null || {
    echo "Installiere PyQt6 …"; sudo apt install -y python3-pyqt6; }

chmod +x "$DIR/pcopy-gui.py"

# Desktop-Datei im Programmordner
cat > "$DIR/pcopy.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=pcopy
GenericName=Dateien kopieren
Comment=Viele Dateien parallel und wiederaufnehmbar kopieren
Exec="$DIR/pcopy-gui.py" %F
Icon=$DIR/pcopy.svg
Terminal=false
Categories=Utility;FileTools;
StartupWMClass=pcopy-gui
EOF
chmod +x "$DIR/pcopy.desktop"

# Startmenü (Name passt zu app.setDesktopFileName → richtiges Symbol in der Taskleiste)
mkdir -p ~/.local/share/applications
cp "$DIR/pcopy.desktop" ~/.local/share/applications/pcopy-gui.desktop

# Schreibtisch
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESK" ]; then
    cp "$DIR/pcopy.desktop" "$DESK/pcopy.desktop"
    chmod +x "$DESK/pcopy.desktop"
    gio set "$DESK/pcopy.desktop" metadata::trusted true 2>/dev/null || true
fi

# Dolphin-Kontextmenü: Rechtsklick -> "Mit pcopy kopieren"
mkdir -p ~/.local/share/kio/servicemenus
cat > ~/.local/share/kio/servicemenus/pcopy.desktop <<EOF
[Desktop Entry]
Type=Service
MimeType=all/all;
Actions=pcopy
X-KDE-Priority=TopLevel

[Desktop Action pcopy]
Name=Mit pcopy kopieren
Icon=$DIR/pcopy.svg
Exec="$DIR/pcopy-gui.py" %F
EOF
chmod +x ~/.local/share/kio/servicemenus/pcopy.desktop

# Befehl „pcopy-gui“ im Terminal (ersetzt eine früher dorthin kopierte Programmversion)
mkdir -p ~/.local/bin
ln -sfn "$DIR/pcopy-gui.py" ~/.local/bin/pcopy-gui

update-desktop-database ~/.local/share/applications 2>/dev/null || true
echo "Fertig. Start über das Symbol auf dem Schreibtisch, das Startmenü (pcopy) oder"
echo "Rechtsklick in Dolphin → „Mit pcopy kopieren“. Job-Liste: $DIR/pcopy.db"
