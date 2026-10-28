#!/usr/bin/env bash
# stop_app.sh — Arrête proprement l'application (serveur Flask, encodeur, moniteur).
#
# Usage :
#   ./stop_app.sh            # arrête l'instance qui écoute sur le port configuré (FLASK_PORT)
#   ./stop_app.sh --all      # arrête en plus toute autre instance de l'application (multi-ports)
#   ./stop_app.sh --force    # envoie SIGKILL si l'arrêt propre n'aboutit pas
#   FLASK_PORT=5050 ./stop_app.sh   # cible un port précis
#
# L'arrêt propre (SIGTERM) laisse l'application terminer : un encodage en cours est abandonné
# (statut EXPORT_ABORTED), un fichier partiel éventuel est purgé au prochain démarrage.

set -uo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PYEXE="$APP_DIR/.venv/bin/python"
[[ -x "$PYEXE" ]] || PYEXE="$(command -v python3)"

FORCE=false
ALL=false
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=true ;;
    --all)   ALL=true ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Option inconnue : $arg" >&2; exit 2 ;;
  esac
done

if [[ -n "${FLASK_PORT:-}" ]]; then
  PORT="$FLASK_PORT"
else
  PORT="$(sed -n 's/^[[:space:]]*FLASK_PORT[[:space:]]*=[[:space:]]*\([0-9]\{1,\}\).*/\1/p' "$APP_DIR/.env" 2>/dev/null | tail -n1)"
fi
PORT="${PORT:-5000}"

# Vrai si le PID correspond bien à cette application (chemin absolu dans la ligne de commande,
# ou lancement avec un chemin relatif depuis le dossier du projet : vérification du répertoire courant)
is_our_app() {
  local pid="$1" cmdline cwd
  cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"
  [[ -n "$cmdline" ]] || return 1
  case "$cmdline" in
    *"$APP_DIR/app.py"*) return 0 ;;
    *app.py*)
      cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null)"
      [[ "$cwd" == "$APP_DIR" ]] && return 0
      ;;
  esac
  return 1
}

# PID de l'instance qui écoute sur le port (en refusant tout autre programme sur ce port)
find_pid_by_port() {
  local pid
  pid="$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)"
  [[ -n "$pid" ]] || return 1
  if is_our_app "$pid"; then
    echo "$pid"
    return 0
  fi
  echo "Le port $PORT est utilisé par un autre programme (PID $pid) : aucun arrêt effectué." >&2
  return 1
}

# Toutes les instances de l'application (tous ports), pour --all
find_all_pids() {
  local pid
  for pid in $(pgrep -f "app\.py" 2>/dev/null); do
    is_our_app "$pid" && echo "$pid"
  done
}

PIDS="$(find_pid_by_port || true)"
if [[ -z "$PIDS" ]]; then
  if [[ "$ALL" == true ]]; then
    PIDS="$(find_all_pids || true)"
  fi
  if [[ -z "$PIDS" ]]; then
    echo "Aucune instance de l'application sur le port $PORT (utilisez --all pour chercher sur tous les ports)."
    exit 0
  fi
fi

if [[ "$ALL" == true ]]; then
  for pid in $(find_all_pids || true); do
    case " $PIDS " in
      *" $pid "*) ;;
      *) PIDS="$PIDS $pid" ;;
    esac
  done
fi

echo "Arrêt de l'application (PID : $(echo "$PIDS" | tr '\n' ' '))..."
while read -r pid; do
  [[ -n "$pid" ]] && kill -TERM "$pid" 2>/dev/null || true
done <<< "$PIDS"

for _ in $(seq 1 20); do
  RUNNING=false
  while read -r pid; do
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && RUNNING=true
  done <<< "$PIDS"
  if [[ "$RUNNING" == false ]]; then
    echo "Application arrêtée proprement (port $PORT libéré)."
    exit 0
  fi
  sleep 1
done

if [[ "$FORCE" == true ]]; then
  echo "L'arrêt propre n'a pas abouti : envoi de SIGKILL..."
  while read -r pid; do
    [[ -n "$pid" ]] && kill -KILL "$pid" 2>/dev/null || true
  done <<< "$PIDS"
  sleep 1
  echo "Application arrêtée de force."
  exit 0
fi

echo "AVERTISSEMENT : l'application répond encore après 20 s (abandon d'un encodage en cours)." >&2
echo "Relancez « $0 --force » pour forcer l'arrêt." >&2
exit 1
