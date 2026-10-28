#!/usr/bin/env bash
# install_shortcut.sh — Installe (ou réinstalle) le raccourci de bureau « Export MP4 (Camille) ».
#
# Usage :
#   ./install_shortcut.sh              # installe sur le Bureau + dans le menu Applications
#   ./install_shortcut.sh --uninstall  # retire les deux raccourcis
#
# Le raccourci lance `run_app.sh --background` : démarrage en arrière-plan, attente de
# /api/health, ouverture du navigateur ; si l'application tourne déjà, seule la page est rouverte.

set -uo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="Export MP4 (Camille)"
DESKTOP_FILE_NAME="export-mp4-camille.desktop"
ICON="$APP_DIR/static/icon.svg"

# Bureau réel (français : ~/Bureau, anglais : ~/Desktop)
DESKTOP_DIR="$HOME/Bureau"
if command -v xdg-user-dir >/dev/null; then
  DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$DESKTOP_DIR")"
fi
[[ -d "$DESKTOP_DIR" ]] || DESKTOP_DIR="$HOME/Desktop"

APPLICATIONS_DIR="$HOME/.local/share/applications"
DESKTOP_TARGET="$DESKTOP_DIR/$APP_NAME.desktop"
MENU_TARGET="$APPLICATIONS_DIR/$DESKTOP_FILE_NAME"

refresh_menu() {
  command -v update-desktop-database >/dev/null && update-desktop-database "$APPLICATIONS_DIR" 2>/dev/null
  return 0
}

if [[ "${1:-}" == "--uninstall" ]]; then
  for target in "$DESKTOP_TARGET" "$MENU_TARGET"; do
    if [[ -e "$target" ]]; then
      rm -f "$target" && echo "Supprimé : $target"
    fi
  done
  refresh_menu
  echo "Raccourci retiré (l'application reste installée dans $APP_DIR)."
  exit 0
fi

for directory in "$DESKTOP_DIR" "$APPLICATIONS_DIR"; do
  [[ -d "$directory" ]] || mkdir -p "$directory"
done

cat > "$DESKTOP_TARGET" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=$APP_NAME
GenericName=Encodeur prproj2ffmpeg
Comment=Encode les projets Premiere (.prproj) des dossiers Camille avec ffmpeg/NVENC et déplace les MP4 exportés dans leur dossier projet
Exec=$APP_DIR/run_app.sh --background
Path=$APP_DIR
Icon=$ICON
Terminal=false
StartupNotify=false
Categories=AudioVideo;
Keywords=prproj;premiere;mp4;export;encodeur;camille;watchfolder;
EOF

cp "$DESKTOP_TARGET" "$MENU_TARGET"
chmod +x "$DESKTOP_TARGET" "$MENU_TARGET"

# GNOME n'autorise le lancement au double-clic qu'après avoir marqué le fichier « de confiance »
if command -v gio >/dev/null; then
  gio set "$DESKTOP_TARGET" metadata::trusted true 2>/dev/null || true
fi

refresh_menu

echo "Raccourci installé :"
echo "  Bureau      : $DESKTOP_TARGET"
echo "  Menu        : $MENU_TARGET"
echo "  Icône       : $ICON"
echo "Double-cliquez sur « $APP_NAME » pour lancer l'application"
echo "(si GNOME demande confirmation la première fois : « Autoriser le lancement »)."
