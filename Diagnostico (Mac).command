#!/bin/bash
cd "$(dirname "$0")" || exit 1
[ -x ".venv/bin/python" ] || { echo "Primero abrí «Iniciar Cloudy (Mac).command» una vez."; read -r; exit 1; }
echo "Conectá el iPhone por cable y desbloquealo..."
PYTHONPATH="$(pwd)/src" .venv/bin/python -m iphone_vault diagnose --out "$(pwd)/diagnostico_cloudy.json"
read -r -p "Listo. Enter para cerrar"
