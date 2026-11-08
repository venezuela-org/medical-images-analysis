#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pywebview>=5.3",
#   "fastapi>=0.115,<1",
#   "uvicorn>=0.30,<1",
#   "pydicom>=3.0,<4",
#   "numpy>=1.26",
#   "pillow>=10",
# ]
# ///
"""
ventana.py 0.1.0 (experimental)
===============================
La misma aplicación local, en una ventana propia en vez de una pestaña del navegador.
Arranca el servidor de app.py en un hilo, escuchando solo en 127.0.0.1 con su token, y abre
una ventana nativa (WebKit en macOS, WebView2 en Windows) apuntando a esa dirección.
Al cerrar la ventana se apaga el servidor.

Same local app in its own window instead of a browser tab. Experimental: not yet run on a
Mac or a Windows machine by its author; app.py and the launchers are the tested path.

Uso / Usage
  uv run ventana.py [--trabajo RUTA] [--puerto 8765]
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

import uvicorn  # noqa: E402
import webview  # noqa: E402


def cargar_app():
    ruta = Path(__file__).resolve().parent / "app.py"
    spec = importlib.util.spec_from_file_location("app_local", ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def main() -> int:
    p = argparse.ArgumentParser(description="PET/CT local en una ventana propia (native window)")
    p.add_argument("--trabajo", help="carpeta de trabajo, por defecto ~/datos-pet")
    p.add_argument("--puerto", type=int, default=8765)
    args = p.parse_args()
    app = cargar_app()
    if args.trabajo:
        app.TRABAJO = Path(args.trabajo).expanduser().resolve()
    marca = app.en_sincronizada(app.TRABAJO)
    if marca:
        print(f"La carpeta de trabajo está dentro de una carpeta sincronizada ({marca}); usa --trabajo con otra ruta.")
        return 2
    app.TRABAJO.mkdir(parents=True, exist_ok=True)
    priv = app.carpeta_privada()
    priv.mkdir(exist_ok=True)
    try:
        os.chmod(app.TRABAJO, 0o700)
        os.chmod(priv, 0o700)
    except OSError:
        pass
    app.indexar()
    app.registrar(f"ventana {app.VERSION} iniciada en 127.0.0.1:{args.puerto}")
    url = f"http://127.0.0.1:{args.puerto}/?t={app.TOKEN}"

    servidor = uvicorn.Server(uvicorn.Config(app.app, host="127.0.0.1", port=args.puerto, log_level="warning"))
    threading.Thread(target=servidor.run, daemon=True).start()
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{args.puerto}/static/app.js", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        print("El servidor local no respondió; prueba con uv run app.py")
        return 1

    webview.create_window("PET/CT local", url, width=1200, height=860, min_size=(900, 640))
    webview.start()          # bloquea hasta que se cierra la ventana / blocks until the window closes
    servidor.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
