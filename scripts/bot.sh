#!/usr/bin/env bash
# Startet/stoppt den Decocity Ausschreibungsbot im Hintergrund.
#   ./scripts/bot.sh start|stop|restart|status|logs
set -euo pipefail
cd "$(dirname "$0")/.."

PID_FILE=data/bot.pid
LOG_FILE=data/bot.log
mkdir -p data

is_running() { [[ -f $PID_FILE ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

case "${1:-}" in
  start)
    if is_running; then echo "Läuft bereits (PID $(cat $PID_FILE))."; exit 0; fi
    [[ -f .env ]] || { echo "Fehlt: .env  →  cp example.env .env und ausfüllen"; exit 1; }
    nohup uv run ausschreibungsbot >>"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
    sleep 3
    if is_running; then echo "Gestartet (PID $(cat $PID_FILE)). Logs: ./scripts/bot.sh logs"
    else echo "Start fehlgeschlagen:"; tail -n 30 "$LOG_FILE"; exit 1; fi
    ;;
  stop)
    if is_running; then
      kill "$(cat $PID_FILE)"; sleep 2
      is_running && kill -9 "$(cat $PID_FILE)" || true
      echo "Gestoppt."
    else echo "Läuft nicht."; fi
    rm -f "$PID_FILE"
    ;;
  restart) "$0" stop; "$0" start ;;
  status) if is_running; then echo "Läuft (PID $(cat $PID_FILE))."; else echo "Läuft nicht."; fi ;;
  logs) tail -n 100 -f "$LOG_FILE" ;;
  *) echo "Nutzung: $0 start|stop|restart|status|logs"; exit 1 ;;
esac
