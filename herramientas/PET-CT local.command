#!/bin/bash
# PET/CT local: doble clic para abrir la aplicación (macOS)
# Double-click to open the app. Instala uv la primera vez si hace falta.
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "Instalando uv (una sola vez)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
DATOS="${PETCT_DATOS:-$HOME/datos-pet}"
echo "Carpeta de datos: $DATOS  (cámbiala con la variable PETCT_DATOS)"
echo "Se abrirá el navegador. Cierra esta ventana para apagar la aplicación."
uv run app.py --trabajo "$DATOS"
