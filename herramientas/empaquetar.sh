#!/bin/bash
# Construye el paquete de escritorio "PET-CT local" con PyInstaller (una carpeta con el ejecutable y sus dependencias).
# Lo usa el flujo de publicación en Windows y macOS; también sirve localmente. Salida: dist/PET-CT local/
set -euo pipefail
cd "$(dirname "$0")"
uv run --with pyinstaller --with fastapi --with uvicorn --with pydicom --with numpy --with pillow --with nibabel \
  pyinstaller --noconfirm --clean --onedir --console --name "PET-CT local" \
  --collect-submodules pydicom \
  --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto --hidden-import uvicorn.loops.asyncio \
  --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.http.h11_impl \
  --hidden-import uvicorn.protocols.websockets.auto --hidden-import uvicorn.lifespan.on --hidden-import uvicorn.lifespan.off \
  --hidden-import anonimizar --hidden-import cotejar --hidden-import emparejar --hidden-import suv --hidden-import demo --hidden-import app \
  lanzador.py
# Poda: fuera los datos de prueba de terceros; los únicos DICOM admitidos son los ejemplos de pydicom (fijados por hash).
if uv run --with pydicom python podar_paquete.py "dist/PET-CT local"; then echo "paquete en dist/PET-CT local"; else echo "construcción rechazada por la poda"; exit 1; fi
