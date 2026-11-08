#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydicom>=3.0,<4"]
# ///
"""
cotejar.py 0.2.2
================
Cotejo exacto entre los originales de un caso y su copia limpia, usando el mapa privado:

  1. Cada archivo original se vuelve a limpiar en memoria con el mismo mapa (seudónimo,
     desplazamiento de fechas, secreto de UID). El resultado se serializa y se compara byte a byte
     con el archivo limpio que debe existir en la ruta derivada de los UID reescritos.
  2. Todo original que no tenga su archivo limpio debe figurar en la cuarentena del reporte de la
     corrida, con su motivo. Los archivos que no son DICOM deben coincidir con los omitidos del reporte.
  3. En las carpetas de serie de este caso no puede existir ningún archivo limpio sin un original
     que lo explique. Otros casos limpiados con el mismo mapa viven en sus propias carpetas y no cuentan.

Así cada original queda en exactamente una categoría: coincide, en cuarentena con registro,
omitido con registro, o problema. Un cambio de un solo byte en la copia limpia (píxeles,
interpretación fotométrica, una fecha, bytes sobrantes al final) es una diferencia.

Exact comparison between the originals of a case and the clean copy, using the private map:
every original is re-cleaned in memory and compared byte for byte with the clean file at its
derived path; every missing clean file must appear in the run's quarantine record; no clean
file may exist without an original. Prints counts and pseudonyms only. Needs the private map,
so it runs on the machine that holds it, and anonimizar.py (same version) next to it.

Uso / Usage
  uv run cotejar.py "Casos S/P4" ~/datos-pet/limpios
        [--mapa ~/datos-pet/limpios_PRIVADO/mapa_seudonimos.json]
        [--reporte ~/datos-pet/limpios_REPORTES/ultimo_reporte.json] [--json cotejo.json]

La carpeta original debe ser la misma que se pasó a `limpiar` (las cuarentenas se registran por
hash de la ruta relativa). The original folder must be the one given to `limpiar`.

Código de salida / exit code: 0 coincide, 1 diferencias, 2 error de uso.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import logging
import sys

# Consola en UTF-8 también en Windows / UTF-8 console output on Windows too
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except (AttributeError, ValueError):
    pass
import warnings
from collections import Counter
from pathlib import Path

warnings.simplefilter("ignore")
logging.getLogger("pydicom").setLevel(logging.CRITICAL)
import pydicom  # noqa: E402

pydicom.config.convert_wrong_length_to_UN = True
VERSION = "0.2.2"


def cargar_anonimizar():
    if getattr(sys, "frozen", False):
        return importlib.import_module("anonimizar")
    ruta = Path(__file__).resolve().parent / "anonimizar.py"
    if not ruta.exists():
        print("Falta anonimizar.py junto a cotejar.py (anonimizar.py must sit next to cotejar.py)")
        sys.exit(2)
    spec = importlib.util.spec_from_file_location("anonimizar", ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Cotejo exacto original contra copia limpia (exact comparison)")
    p.add_argument("original")
    p.add_argument("limpio", help="carpeta raíz de la copia limpia (clean root, e.g. ~/datos-pet/limpios)")
    p.add_argument("--mapa")
    p.add_argument("--reporte")
    p.add_argument("--json")
    args = p.parse_args(argv)
    anon = cargar_anonimizar()

    orig, limpio = Path(args.original).expanduser(), Path(args.limpio).expanduser()
    if not orig.is_dir() or not limpio.is_dir():
        print("Falta alguna carpeta (folder not found)")
        return 2
    ruta_mapa = Path(args.mapa).expanduser() if args.mapa else limpio.parent / f"{limpio.name}_PRIVADO" / "mapa_seudonimos.json"
    ruta_rep = Path(args.reporte).expanduser() if args.reporte else limpio.parent / f"{limpio.name}_REPORTES" / "ultimo_reporte.json"
    if not ruta_mapa.exists():
        print("No se encontró el mapa privado; indícalo con --mapa (private map not found)")
        return 2
    mapa = json.loads(ruta_mapa.read_text(encoding="utf-8"))
    secreto, pacientes_mapa = mapa["secreto_uid"], mapa["pacientes"]
    reporte = json.loads(ruta_rep.read_text(encoding="utf-8")) if ruta_rep.exists() else {}
    if not reporte:
        print("Aviso: sin reporte de corrida; ninguna cuarentena podrá darse por registrada (no run report found)")
    if reporte.get("version") not in (None, anon.VERSION):
        print(f"Aviso: el reporte es de anonimizar {reporte.get('version')} y esta copia usa {anon.VERSION}; el cotejo puede marcar diferencias legítimas")
    conservar = bool(reporte.get("conservar_descripciones", False))
    cuarentena = {c["archivo"]: c["motivo"] for c in reporte.get("cuarentena", [])}
    omitidos_reporte = sum(reporte.get("archivos_omitidos", {}).values())

    conteo = Counter()
    problemas: list[str] = []
    esperados: set[Path] = set()
    seudonimos: set[str] = set()
    motivos_cuarentena = Counter()

    for ruta in anon.iter_archivos(orig):
        rel = str(ruta.relative_to(orig))
        clave_c = anon.hash_corto(rel, 8)
        if ruta.name.upper() == "DICOMDIR":
            conteo["omitidos (DICOMDIR)"] += 1
            continue
        try:
            ds = pydicom.dcmread(ruta, force=True)
            meta_ok = getattr(ds, "file_meta", None) is not None and "MediaStorageSOPClassUID" in ds.file_meta
            if not (meta_ok or "SOPClassUID" in ds):
                conteo["omitidos (no DICOM)"] += 1
                continue
            sop = str(ds.get("SOPClassUID", "")) or str(ds.file_meta.get("MediaStorageSOPClassUID", ""))
            aceptado = (sop in anon.SOP_PERMITIDAS and str(ds.get("Modality", "")) in anon.MODALIDADES_PERMITIDAS
                        and str(ds.get("BurnedInAnnotation", "")).upper() != "YES")
            if aceptado:
                _ = ds.PixelData
        except Exception:
            aceptado = False
        if not aceptado:
            if clave_c in cuarentena:
                conteo["en cuarentena con registro"] += 1
                motivos_cuarentena[cuarentena[clave_c]] += 1
            else:
                conteo["PROBLEMA: rechazable sin registro de cuarentena"] += 1
                problemas.append(f"original no aceptable sin registro de cuarentena / rejectable original without quarantine record ({clave_c})")
            continue

        clave, _ = anon.MapaPrivado.clave(ds)
        pac = pacientes_mapa.get(clave)
        if pac is None:
            if clave_c in cuarentena:
                conteo["en cuarentena con registro"] += 1
                motivos_cuarentena[cuarentena[clave_c]] += 1
            else:
                conteo["PROBLEMA: sin seudónimo en el mapa"] += 1
                problemas.append(f"original sin seudónimo en el mapa / original without pseudonym ({clave_c})")
            continue
        seudonimos.add(pac["seudonimo"])
        try:
            anon.limpiar_dataset(ds, pac, secreto, Counter(), [], conservar)
            esperado = anon.ruta_destino(ds, pac["seudonimo"], limpio)
            bytes_esperados = anon.canonico(ds)
        except Exception as e:
            if clave_c in cuarentena:
                conteo["en cuarentena con registro"] += 1
                motivos_cuarentena[cuarentena[clave_c]] += 1
            else:
                problemas.append(f"no se pudo recalcular la limpieza ({type(e).__name__}) y no hay registro de cuarentena ({clave_c})")
                conteo["PROBLEMA: sin recalcular ni registro"] += 1
            continue
        esperados.add(esperado)
        if not esperado.exists():
            if clave_c in cuarentena:
                conteo["en cuarentena con registro"] += 1
                motivos_cuarentena[cuarentena[clave_c]] += 1
            else:
                conteo["PROBLEMA: archivo limpio ausente sin registro"] += 1
                problemas.append(f"falta el archivo limpio de un original aceptado y no hay registro de cuarentena / clean file missing ({clave_c})")
            continue
        actuales = esperado.read_bytes()
        if actuales == bytes_esperados:
            conteo["coinciden byte a byte"] += 1
        elif anon.solo_cambia_version(esperado, bytes_esperados):
            conteo["coinciden salvo la versión de la herramienta (reimportar actualiza)"] += 1
        else:
            conteo["PROBLEMA: archivo limpio distinto"] += 1
            problemas.append(f"el archivo limpio difiere de la limpieza recalculada / clean file differs ({clave_c})")

    # Solo las carpetas de serie a las que este caso debía llegar; otros casos del mismo mapa no son sobrantes
    carpetas_caso = {e.parent for e in esperados}
    existentes = {p for c in carpetas_caso if c.is_dir() for p in anon.iter_archivos(c) if p.suffix.lower() == ".dcm"}
    sobrantes = existentes - esperados
    if sobrantes:
        conteo["PROBLEMA: archivos limpios sin original"] = len(sobrantes)
        problemas.append(f"{len(sobrantes)} archivo(s) limpio(s) sin original que los explique / clean files without an original")
    omitidos_aqui = conteo["omitidos (no DICOM)"] + conteo["omitidos (DICOMDIR)"]
    if reporte and omitidos_aqui != omitidos_reporte:
        problemas.append(f"omitidos: {omitidos_aqui} aquí frente a {omitidos_reporte} en el reporte / skipped counts differ")

    print(f"cotejar {VERSION} (política {anon.POLITICA}); seudónimos del caso: {', '.join(sorted(seudonimos)) or '-'}")
    for k, v in sorted(conteo.items()):
        print(f"  {k}: {v}")
    for m, n in motivos_cuarentena.items():
        print(f"    cuarentena, {m}: {n}")
    for q in problemas:
        print(f"  - {q}")
    veredicto = "COINCIDE" if not problemas else "DIFERENCIAS"
    print(f"Veredicto: {veredicto}")
    if args.json:
        Path(args.json).write_text(json.dumps({"version": VERSION, "seudonimos": sorted(seudonimos), "conteo": dict(conteo),
                                               "cuarentena": dict(motivos_cuarentena), "problemas": problemas,
                                               "veredicto": veredicto}, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if not problemas else 1


if __name__ == "__main__":
    sys.exit(main())
