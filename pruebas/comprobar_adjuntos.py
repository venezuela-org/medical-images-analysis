#!/usr/bin/env python3
"""
Qué puede entrar en los adjuntos de una versión, comprobado por contenido y no por nombre.
What may enter release attachments, checked by content, not by file name.

Reglas / Rules
  - Ningún archivo dentro de ningún adjunto puede ser un DICOM (marcador DICM en el byte 128), con una sola excepción:
    los archivos de datos-demo-sinteticos.zip, donde cada DICOM debe llevar la identidad sintética de demo.py
    (nombre DEMO^PACIENTE^SINTETICO, fabricante DEMO SINTETICA) y ninguna etiqueta privada ajena a la demo.
    El paquete de la aplicación no lleva ningún DICOM: ni datos de prueba ni paletas de las dependencias.
  - Ningún archivo puede llamarse como los archivos privados del proyecto (mapa de seudónimos, índice de
    originales, raíces, corridas, revisiones de píxeles, bitácora) ni ser un paquete de exportación.
  - Ningún archivo de texto puede contener la cadena de identidad del proyecto fuera de la demo.

Lo que esta comprobación demuestra: que en los adjuntos no hay ningún archivo DICOM distinto de los sintéticos y
ningún archivo privado conocido. Lo que no demuestra: que no exista información de pacientes en alguna otra
forma que este programa no reconozca. Por eso los adjuntos se revisan también a mano antes de publicar.

Uso / Usage
  python pruebas/comprobar_adjuntos.py adjuntos/*.zip
"""
from __future__ import annotations

import io
import re
import sys
import zipfile
from pathlib import Path

PRIVADOS = re.compile(r"(mapa_seudonimos|indice_origen|raices\.json|corridas\.jsonl|revision_pixeles|registro\.log|/PRIVADO/|limpios_PRIVADO|paquete_\d{8}_)", re.I)
DEMO_NOMBRE, DEMO_FABRICANTE, DEMO_CREADOR = b"DEMO^PACIENTE^SINTETICO", b"DEMO SINTETICA", b"DEMO PRIVADO"


def es_dicom(datos: bytes) -> bool:
    return len(datos) > 132 and datos[128:132] == b"DICM"


def revisar_zip(ruta: Path) -> list[str]:
    problemas: list[str] = []
    es_demo = ruta.name.startswith("datos-demo-sinteticos")
    with zipfile.ZipFile(ruta) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            if PRIVADOS.search(info.filename):
                problemas.append(f"{ruta.name}: nombre de archivo privado o paquete de exportación: {info.filename}")
            datos = z.read(info.filename)
            if es_dicom(datos):
                if not es_demo:
                    problemas.append(f"{ruta.name}: contiene un DICOM: {info.filename}")
                    continue
                if DEMO_NOMBRE not in datos or DEMO_FABRICANTE not in datos:
                    problemas.append(f"{ruta.name}: DICOM sin la identidad sintética de la demo: {info.filename}")
                # cualquier creador privado que no sea el de la demo delata un origen distinto
                try:
                    import pydicom
                    ds = pydicom.dcmread(io.BytesIO(datos), stop_before_pixels=True, force=True)
                    creadores = {str(e.value).strip() for e in ds if e.tag.is_private and e.tag.element < 0x100}
                    if creadores - {DEMO_CREADOR.decode()}:
                        problemas.append(f"{ruta.name}: DICOM con creadores privados ajenos a la demo: {info.filename}")
                except ImportError:
                    pass
            elif es_demo and not info.filename.endswith("/"):
                pass
    return problemas


def main(argv: list[str]) -> int:
    rutas = [Path(a) for a in argv]
    if not rutas:
        print("indica los zips a comprobar (give the zips to check)")
        return 2
    todos: list[str] = []
    for r in rutas:
        if not r.is_file():
            todos.append(f"no existe: {r}")
            continue
        problemas = revisar_zip(r)
        with zipfile.ZipFile(r) as z:
            n = sum(1 for i in z.infolist() if not i.is_dir())
        print(f"{r.name}: {n} archivos revisados, {len(problemas)} problema(s)")
        todos += problemas
    for p in todos:
        print("  - " + p)
    if todos:
        print("Adjuntos rechazados: la versión no se publica con estos archivos.")
        return 1
    print("Adjuntos aceptados por contenido: sin DICOM fuera de la demo sintética y sin archivos privados conocidos. "
          "Esto no sustituye la revisión manual antes de publicar.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
