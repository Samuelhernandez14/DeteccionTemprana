#!/usr/bin/env bash
# Lanzador para cron / systemd. Activa el entorno virtual si existe y ejecuta todo.
#   ./ejecutar.sh            -> simulado (guarda los correos como .eml)
#   ./ejecutar.sh --enviar   -> envía los correos de verdad
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
for v in venv .venv; do
  [ -f "$DIR/$v/bin/activate" ] && . "$DIR/$v/bin/activate" && break
done
exec python3 "$DIR/ejecutar.py" "$@"
