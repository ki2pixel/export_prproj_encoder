#!/usr/bin/env bash
# run_app.sh — Lance l'application Flask (soumission + encodeur prproj2ffmpeg + moniteur).
#
# Usage :
#   ./run_app.sh                # premier plan (Ctrl+C pour arrêter)
#   ./run_app.sh --background   # arrière-plan (nohup) + attente de /api/health + navigateur
#   ./run_app.sh --no-browser   # ne pas ouvrir le navigateur
#
# Si le serveur répond déjà, le script se contente d'ouvrir le navigateur (double-clic sans effet de bord).
# En cas d'échec de démarrage en arrière-plan, une notification de bureau est affichée.

set -uo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR" || exit 1
ICON="$APP_DIR/static/icon.svg"

BACKGROUND=false
OPEN_BROWSER=true
for arg in "$@"; do
  case "$arg" in
    --background|-b) BACKGROUND=true ;;
    --no-browser)    OPEN_BROWSER=false ;;
    -h|--help)
      sed -n '2,10p' "$0"
      exit 0 ;;
    *)
      echo "Option inconnue : $arg" >&2
      exit 2 ;;
  esac
done

if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "AVERTISSEMENT: aucun fichier .env trouvé — copie de .env.example"
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
fi

PYEXE="$APP_DIR/.venv/bin/python"
if [[ ! -x "$PYEXE" ]]; then
  echo "ENV: pas de virtualenv détecté (./.venv) — utilisation de python3"
  PYEXE="$(command -v python3)"
fi

# Port lu depuis .env (défaut 5000)
PORT="$(sed -n 's/^[[:space:]]*FLASK_PORT[[:space:]]*=[[:space:]]*\([0-9]\{1,\}\).*/\1/p' "$APP_DIR/.env" | tail -n1)"
PORT="${PORT:-5000}"

check_health() {
  "$PYEXE" - "$PORT" <<'PYEOF'
import sys, urllib.request
port = sys.argv[1]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)
PYEOF
}

open_browser() {
  if [[ "$OPEN_BROWSER" == true ]] && command -v xdg-open >/dev/null; then
    xdg-open "http://localhost:$PORT" >/dev/null 2>&1 &
  fi
}

notify() {
  local title="$1" message="$2" urgency="${3:-normal}"
  if command -v notify-send >/dev/null; then
    notify-send -a "Export MP4" -u "$urgency" -i "$ICON" "$title" "$message" >/dev/null 2>&1 || true
  fi
}

# --- L'application tourne-t-elle déjà ? (double-clic / relance) ---
if check_health; then
  if [[ "$OPEN_BROWSER" == true ]]; then
    echo "L'application répond déjà sur http://localhost:$PORT — ouverture du navigateur."
  else
    echo "L'application répond déjà sur http://localhost:$PORT."
  fi
  open_browser
  exit 0
fi

find_busy_pid() {
  local pid=""
  if command -v ss >/dev/null; then
    pid="$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)"
  elif command -v lsof >/dev/null; then
    pid="$(lsof -i ":$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1)"
  fi
  echo "$pid"
}

BUSY_PID="$(find_busy_pid)"
if [[ -n "$BUSY_PID" ]]; then
  echo "ÉCHEC : Le port $PORT est déjà utilisé par un autre processus (PID $BUSY_PID)." >&2
  echo "Modifiez FLASK_PORT dans .env ou arrêtez le programme en conflit." >&2
  notify "Export MP4 : port $PORT occupé" \
         "Le port $PORT est déjà utilisé par un autre programme (PID $BUSY_PID). Modifiez FLASK_PORT dans .env." critical
  exit 1
fi

if [[ "$BACKGROUND" == true ]]; then
  LOG_FILE="$APP_DIR/logs_watchfolder/run_app.log"
  mkdir -p "$(dirname "$LOG_FILE")"
  nohup "$PYEXE" "$APP_DIR/app.py" </dev/null >> "$LOG_FILE" 2>&1 &
  APP_PID=$!
  disown "$APP_PID" 2>/dev/null || true
  echo "Application démarrée en arrière-plan (PID $APP_PID) — journal : $LOG_FILE"

  for _ in $(seq 1 30); do
    if check_health; then
      echo "Serveur prêt sur http://localhost:$PORT"
      open_browser
      exit 0
    fi
    if ! kill -0 "$APP_PID" 2>/dev/null; then
      echo "ÉCHEC: le serveur s'est arrêté de manière inattendue (PID $APP_PID). Voir $LOG_FILE" >&2
      notify "Export MP4 : arrêt inattendu" \
             "Le serveur s'est arrêté inopinément. Consultez $LOG_FILE" critical
      exit 1
    fi
    sleep 1
  done

  echo "ÉCHEC: le serveur n'a pas répondu sur /api/health après 30 s. Voir $LOG_FILE" >&2
  notify "Export MP4 : démarrage impossible" \
         "Le serveur n'a pas répondu sur /api/health après 30 s. Consultez $LOG_FILE" critical
  exit 1
fi

open_browser
exec "$PYEXE" "$APP_DIR/app.py"
