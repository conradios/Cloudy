#!/bin/bash
# Cloudy para macOS: doble clic para abrir. La primera vez instala lo necesario.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "No encontré Python 3. Instalalo desde https://www.python.org/downloads/ y volvé a abrir este archivo."
  read -r -p "Enter para cerrar"; exit 1
fi
if [ ! -x ".venv/bin/python" ]; then
  echo "Creando entorno de Python (solo la primera vez)..."
  python3 -m venv .venv || exit 1
fi
if [ ! -f ".venv/instalado-v3.txt" ]; then
  echo "Instalando dependencias (solo la primera vez, puede tardar unos minutos)..."
  .venv/bin/python -m pip install --upgrade pip && .venv/bin/python -m pip install -r requirements.txt || { read -r -p "Hubo un error. Enter para cerrar"; exit 1; }
  echo ok > .venv/instalado-v3.txt
fi
PYTHONPATH="$(pwd)/src" .venv/bin/python -m iphone_vault
