#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["fastapi>=0.115,<1", "uvicorn>=0.30,<1", "pydicom>=3.0,<4", "numpy>=1.26", "pillow>=10", "nibabel>=5"]
# ///
"""
lanzador.py
===========
Punto de entrada único de la aplicación empaquetada ("PET-CT local").

  PET-CT local                               → abre la aplicación (app.py)
  PET-CT local herramienta anonimizar ...    → ejecuta una herramienta con sus argumentos
  PET-CT local herramienta demo CARPETA      → crea el caso sintético de demostración

Cuando el programa va congelado con PyInstaller, sys.executable es el propio ejecutable; app.py
invoca entonces "PET-CT local herramienta X ..." en vez de "python X.py ...". Desde el código
fuente, `uv run lanzador.py` hace lo mismo con los .py de esta carpeta.

Single entry point for the packaged app: the app by default, tools as subcommands. Works both
from source and frozen with PyInstaller.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except (AttributeError, ValueError):
    pass
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)   # la consola muestra UTF-8; sin esto los acentos salen como "Ã³"
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass

HERRAMIENTAS = ("anonimizar", "emparejar", "cotejar", "suv", "demo")
AQUI = Path(__file__).resolve().parent

# Importaciones estáticas para que PyInstaller empaquete las herramientas como módulos.
import anonimizar  # noqa: E402,F401
import cotejar  # noqa: E402,F401
import demo  # noqa: E402,F401
import emparejar  # noqa: E402,F401
import suv  # noqa: E402,F401
import app  # noqa: E402,F401


def cargar(nombre: str):
    """Módulo de una herramienta: ya importado arriba; desde el código fuente se recarga desde esta carpeta."""
    if getattr(sys, "frozen", False):
        return importlib.import_module(nombre)
    spec = importlib.util.spec_from_file_location(nombre, AQUI / f"{nombre}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["herramienta"]:
        if len(argv) < 2 or argv[1] not in HERRAMIENTAS:
            print("Herramientas disponibles (available tools): " + ", ".join(HERRAMIENTAS))
            return 2
        mod = cargar(argv[1])
        return int(mod.main(argv[2:]) or 0)
    if argv[:1] in (["--version"], ["version"]):
        print(f"PET-CT local {cargar('app').VERSION}")
        return 0
    sys.argv = [sys.argv[0]] + argv
    cargar("app").main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
