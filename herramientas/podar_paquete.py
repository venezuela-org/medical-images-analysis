#!/usr/bin/env python3
"""
Poda del paquete de escritorio tras PyInstaller: elimina los datos de prueba y las paletas de terceros y exige que no
quede ningún archivo DICOM dentro del paquete. pydicom se importa sin sus archivos de ejemplo (sus funciones de
ejemplo devuelven None sin intentar descargas), y la aplicación no usa paletas de color DICOM.

Prune the PyInstaller output: drop third-party test data and palettes; the application package must contain no DICOM
file at all. Any DICOM fails the build.

Uso / Usage:  python podar_paquete.py "dist/PET-CT local"
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path


def es_dicom(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            f.seek(128)
            return f.read(4) == b"DICM"
    except OSError:
        return False


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    raiz = Path(argv[0]).resolve()
    interno = raiz / "_internal"
    if not interno.is_dir():
        print(f"no existe {interno}")
        return 2
    for d in list(interno.rglob("*")):
        if d.is_dir() and d.name in ("tests", "test", "testing", "test_files", "charset_files", "palettes"):
            shutil.rmtree(d, ignore_errors=True)
    sobrantes = [str(p.relative_to(raiz)) for p in interno.rglob("*") if p.is_file() and p.stat().st_size > 132 and es_dicom(p)]
    print(f"poda: {len(sobrantes)} archivo(s) DICOM dentro del paquete (deben ser 0)")
    for s in sobrantes:
        print("  -", s)
    return 1 if sobrantes else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
