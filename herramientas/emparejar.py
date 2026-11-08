#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydicom>=3.0,<4"]
# ///
"""
emparejar.py 0.2.0
==================
Revisa un caso (una carpeta con sus series, originales o limpias) y clasifica cada posible
par CT + PET en uno de tres veredictos:

  candidato compatible      todas las imágenes validadas, identificadores completos y coincidentes,
                            geometría completa, sin duplicados, solape tridimensional real
  inconsistente             alguna imagen contradice a su serie, o CT y PET no se corresponden
  información insuficiente  faltan identificadores o posiciones en alguna imagen

Un resultado verde es un candidato para la validación de volumen; no es permiso para compartir
ni prueba de que el examen esté completo.

It classifies each possible CT + PET pair as "compatible candidate", "inconsistent" or
"insufficient information". Every image is validated against its series; comparisons use the
full identifiers internally; the printout shows only salted short labels, generated fresh on
every run, plus counts and geometry. No folder names and no acquisition times are printed.

Uso / Usage
  uv run emparejar.py "Casos S/P4"
  uv run emparejar.py ~/datos-pet/limpios/ANON-0003 --json par.json

Código de salida / exit code: 0 candidato compatible, 1 inconsistente, 3 información insuficiente, 2 error de uso.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import statistics
import sys

# Consola en UTF-8 también en Windows / UTF-8 console output on Windows too
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except (AttributeError, ValueError):
    pass
import warnings
from pathlib import Path

warnings.simplefilter("ignore")
logging.getLogger("pydicom").setLevel(logging.CRITICAL)
import pydicom  # noqa: E402

pydicom.config.convert_wrong_length_to_UN = True

VERSION = "0.2.0"
SAL = secrets.token_hex(8)          # etiquetas con sal nueva en cada corrida / fresh salt per run
TOL_ORIENTACION = 1e-3
TOL_DUPLICADO_MM = 1e-3
CLASES_IMAGEN = {"1.2.840.10008.5.1.4.1.1.2", "1.2.840.10008.5.1.4.1.1.2.1", "1.2.840.10008.5.1.4.1.1.128",
                 "1.2.840.10008.5.1.4.1.1.130", "1.2.840.10008.5.1.4.1.1.4", "1.2.840.10008.5.1.4.1.1.4.1"}


def etiqueta(texto: str) -> str:
    return hashlib.sha256(f"{SAL}:{texto}".encode()).hexdigest()[:8] if texto else "-"


def iter_archivos(carpeta: Path):
    for raiz, dirs, nombres in os.walk(carpeta, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(raiz, d)))
        for n in sorted(nombres):
            p = Path(raiz) / n
            if not p.is_symlink() and p.is_file() and n.upper() != "DICOMDIR":
                yield p


def cruz(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def leer_imagenes(carpeta: Path) -> tuple[list[dict], int]:
    """Un registro por imagen con identificadores completos (nunca se imprimen) y geometría."""
    imagenes, omitidos = [], 0
    for ruta in iter_archivos(carpeta):
        try:
            ds = pydicom.dcmread(ruta, force=True, stop_before_pixels=True)
            sop = str(ds.get("SOPClassUID", "")) or str(ds.file_meta.get("MediaStorageSOPClassUID", ""))
        except Exception:
            omitidos += 1
            continue
        if sop not in CLASES_IMAGEN:
            omitidos += 1
            continue
        pid = str(ds.get("PatientID", "")).strip()
        imagenes.append({
            "paciente": f"{str(ds.get('IssuerOfPatientID', '')).strip()}|{pid}" if pid else "",
            "estudio": str(ds.get("StudyInstanceUID", "")).strip(),
            "serie": str(ds.get("SeriesInstanceUID", "")).strip(),
            "marco": str(ds.get("FrameOfReferenceUID", "")).strip(),
            "modalidad": str(ds.get("Modality", "")),
            "filas": int(ds.get("Rows", 0) or 0), "columnas": int(ds.get("Columns", 0) or 0),
            "espaciado": [float(x) for x in ds.PixelSpacing] if "PixelSpacing" in ds and len(ds.PixelSpacing) == 2 else None,
            "grosor": float(ds.SliceThickness) if "SliceThickness" in ds and str(ds.SliceThickness) else None,
            "orientacion": [float(x) for x in ds.ImageOrientationPatient] if "ImageOrientationPatient" in ds and len(ds.ImageOrientationPatient) == 6 else None,
            "posicion": [float(x) for x in ds.ImagePositionPatient] if "ImagePositionPatient" in ds and len(ds.ImagePositionPatient) == 3 else None,
            "instancia": int(ds.InstanceNumber) if "InstanceNumber" in ds and str(ds.InstanceNumber) else None,
            "unidades": str(ds.get("Units", "")), "texto_grabado": str(ds.get("BurnedInAnnotation", "")).upper() == "YES",
        })
    return imagenes, omitidos


def caja(img: dict) -> list[list[float]] | None:
    """Esquinas del corte en coordenadas del paciente / slice corners in patient coordinates."""
    if not (img["posicion"] and img["orientacion"] and img["espaciado"] and img["filas"] and img["columnas"]):
        return None
    o = img["orientacion"]
    fila, col = o[:3], o[3:6]
    ancho = (img["columnas"] - 1) * img["espaciado"][1]
    alto = (img["filas"] - 1) * img["espaciado"][0]
    p = img["posicion"]
    return [[p[i] + a * fila[i] * ancho + b * col[i] * alto for i in range(3)] for a in (0, 1) for b in (0, 1)]


def analizar_serie(uid: str, imgs: list[dict]) -> dict:
    """Valida todas las imágenes de la serie entre sí. Estado: ok, inconsistente o insuficiente."""
    insuficiente, inconsistente, avisos = [], [], []
    ref = imgs[0]
    for campo, nombre in (("paciente", "paciente"), ("estudio", "estudio"), ("marco", "marco de referencia"),
                          ("modalidad", "modalidad")):
        vals = {i[campo] for i in imgs}
        if "" in vals:
            insuficiente.append(f"{nombre} ausente en {sum(1 for i in imgs if not i[campo])} imagen(es)")
        if len(vals - {""}) > 1:
            inconsistente.append(f"{nombre} distinto entre imágenes de la misma serie")
    if len({(i["filas"], i["columnas"]) for i in imgs}) > 1:
        inconsistente.append("matriz distinta entre imágenes")
    if any(i["espaciado"] is None for i in imgs):
        insuficiente.append("espaciado de píxel ausente")
    elif len({tuple(i["espaciado"]) for i in imgs}) > 1:
        inconsistente.append("espaciado de píxel distinto entre imágenes")
    if any(i["orientacion"] is None for i in imgs):
        insuficiente.append(f"orientación ausente en {sum(1 for i in imgs if i['orientacion'] is None)} imagen(es)")
    else:
        for i in imgs[1:]:
            if any(abs(a - b) > TOL_ORIENTACION for a, b in zip(i["orientacion"], ref["orientacion"])):
                inconsistente.append("orientación distinta entre imágenes")
                break
    sin_pos = sum(1 for i in imgs if i["posicion"] is None)
    if sin_pos:
        insuficiente.append(f"posición ausente en {sin_pos} imagen(es)")
    if any(i["texto_grabado"] for i in imgs):
        avisos.append("texto grabado en píxeles / burned-in annotation")

    z, paso, duplicados, huecos = [], None, 0, 0
    if not sin_pos and ref["orientacion"]:
        nv = cruz(ref["orientacion"][:3], ref["orientacion"][3:6])
        z = sorted(sum(a * b for a, b in zip(i["posicion"], nv)) for i in imgs)
        pasos = [b - a for a, b in zip(z, z[1:])]
        duplicados = sum(1 for p in pasos if abs(p) < TOL_DUPLICADO_MM)
        reales = [p for p in pasos if abs(p) >= TOL_DUPLICADO_MM]
        paso = statistics.median(reales) if reales else None
        huecos = sum(1 for p in reales if paso and p > 1.5 * paso)
        if duplicados:
            inconsistente.append(f"{duplicados} posición(es) de corte duplicada(s)")
        if huecos:
            avisos.append(f"{huecos} hueco(s) entre cortes")
    inst = sorted({i["instancia"] for i in imgs if i["instancia"] is not None})
    if inst and inst[-1] - inst[0] + 1 != len(inst):
        avisos.append(f"{inst[-1] - inst[0] + 1 - len(inst)} número(s) de instancia faltante(s)")

    cajas = [c for c in (caja(i) for i in imgs) if c]
    extremos = None
    if cajas and not sin_pos and len(cajas) == len(imgs):
        pts = [p for c in cajas for p in c]
        extremos = [[round(min(p[k] for p in pts), 1), round(max(p[k] for p in pts), 1)] for k in range(3)]

    estado = "insuficiente" if insuficiente else ("inconsistente" if inconsistente else "ok")
    return {"etiqueta": etiqueta(uid), "modalidad": ref["modalidad"], "imagenes": len(imgs),
            "paciente": etiqueta(ref["paciente"]), "estudio": etiqueta(ref["estudio"]), "marco": etiqueta(ref["marco"]),
            "_paciente": ref["paciente"], "_estudio": ref["estudio"], "_marco": ref["marco"], "_orientacion": ref["orientacion"],
            "_z": z, "matriz": f"{ref['filas']}x{ref['columnas']}", "espaciado_mm": ref["espaciado"], "grosor_mm": ref["grosor"],
            "paso_mm": round(paso, 4) if paso else None, "extremos_mm": extremos, "unidades": ref["unidades"],
            "estado": estado, "insuficiente": insuficiente, "inconsistente": inconsistente, "avisos": avisos}


def emparejar(ct: dict, pt: dict) -> dict:
    razones, insuficiente, comprobaciones = [], [], {}
    cobertura = solape = None
    for s in (ct, pt):
        if s["estado"] == "insuficiente":
            insuficiente.append(f"serie {s['etiqueta']} ({s['modalidad']}): " + "; ".join(s["insuficiente"]))
        elif s["estado"] == "inconsistente":
            razones.append(f"serie {s['etiqueta']} ({s['modalidad']}): " + "; ".join(s["inconsistente"]))
    if not insuficiente:
        comprobaciones["mismo_paciente"] = ct["_paciente"] == pt["_paciente"]
        comprobaciones["mismo_estudio"] = ct["_estudio"] == pt["_estudio"]
        comprobaciones["mismo_marco_de_referencia"] = ct["_marco"] == pt["_marco"]
        comprobaciones["misma_orientacion"] = all(abs(a - b) <= TOL_ORIENTACION for a, b in zip(ct["_orientacion"], pt["_orientacion"]))
        if ct["_z"] and pt["_z"] and pt["paso_mm"]:
            tol = 0.5 * pt["paso_mm"]
            coinciden = sum(1 for z in pt["_z"] if any(abs(z - c) <= tol for c in ct["_z"]))
            comprobaciones["cada_corte_pet_tiene_ct"] = coinciden == len(pt["_z"])
            cobertura = {"cortes_pet": len(pt["_z"]), "con_ct_coincidente": coinciden, "tolerancia_mm": round(tol, 3)}
        else:
            comprobaciones["cada_corte_pet_tiene_ct"] = False
        if ct["extremos_mm"] and pt["extremos_mm"]:
            solape = [round(min(ct["extremos_mm"][k][1], pt["extremos_mm"][k][1]) - max(ct["extremos_mm"][k][0], pt["extremos_mm"][k][0]), 1)
                      for k in range(3)]
            comprobaciones["solape_tridimensional"] = all(s > 0 for s in solape)
            tol_plano = max(pt["espaciado_mm"]) if pt["espaciado_mm"] else 0.0
            comprobaciones["pet_dentro_del_ct_en_3d"] = all(
                pt["extremos_mm"][k][0] >= ct["extremos_mm"][k][0] - tol_plano and pt["extremos_mm"][k][1] <= ct["extremos_mm"][k][1] + tol_plano
                for k in range(3))
        else:
            comprobaciones["solape_tridimensional"] = False
        razones += [k for k, v in comprobaciones.items() if not v]
    veredicto = ("información insuficiente" if insuficiente else "inconsistente" if razones else "candidato compatible")
    return {"ct": ct["etiqueta"], "pet": pt["etiqueta"], "veredicto": veredicto, "comprobaciones": comprobaciones,
            "razones": razones, "insuficiente": insuficiente, "cobertura": cobertura, "solape_mm": solape}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Clasificación de pares CT + PET de un caso (pairing check)")
    p.add_argument("caso")
    p.add_argument("--json")
    args = p.parse_args(argv)
    carpeta = Path(args.caso).expanduser()
    if not carpeta.is_dir():
        print("No existe la carpeta (folder not found)")
        return 2
    imagenes, omitidos = leer_imagenes(carpeta)
    if not imagenes:
        print("No se encontraron imágenes CT, PET o MR (no CT, PET or MR images found)")
        return 2
    grupos: dict[str, list[dict]] = {}
    for n, i in enumerate(imagenes):
        grupos.setdefault(i["serie"] or f"sin-serie:{n}", []).append(i)
    series = [analizar_serie(uid, imgs) for uid, imgs in grupos.items()]

    print(f"emparejar {VERSION}: {len(imagenes)} imágenes en {len(series)} serie(s), {omitidos} archivo(s) no imagen omitidos")
    print("Etiquetas con sal aleatoria de esta corrida, no comparables entre corridas (salted labels, per run).")
    for s in series:
        print(f"  serie {s['etiqueta']}  {s['modalidad']:>3}  {s['imagenes']:>4} imágenes  {s['matriz']:>8}  espaciado {s['espaciado_mm']}  "
              f"paso z {s['paso_mm']} mm  extremos {s['extremos_mm']}  paciente {s['paciente']}  estudio {s['estudio']}  marco {s['marco']}  estado: {s['estado']}")
        for r in s["insuficiente"] + s["inconsistente"] + s["avisos"]:
            print(f"      - {r}")
    cts = [s for s in series if s["modalidad"] == "CT"]
    pts = [s for s in series if s["modalidad"] == "PT"]
    pares = [emparejar(c, t) for c in cts for t in pts]
    print()
    if not pares:
        print("No hay CT y PET en la carpeta; nada que emparejar (no CT and PET present)")
    for x in pares:
        print(f"CT {x['ct']} + PET {x['pet']}: {x['veredicto'].upper()}")
        for r in x["razones"] + x["insuficiente"]:
            print(f"      - {r}")
        if x["cobertura"]:
            print(f"      cortes PET con CT en la misma posición: {x['cobertura']['con_ct_coincidente']}/{x['cobertura']['cortes_pet']} (tolerancia {x['cobertura']['tolerancia_mm']} mm)")
        if x["solape_mm"]:
            print(f"      solape CT-PET en x, y, z: {x['solape_mm']} mm")
    print("\nUn candidato compatible es apto para la validación de volumen; no es permiso para compartir el caso.")
    if args.json:
        limpio = [{k: v for k, v in s.items() if not k.startswith("_")} for s in series]
        Path(args.json).write_text(json.dumps({"version": VERSION, "series": limpio, "pares": pares}, ensure_ascii=False, indent=2), encoding="utf-8")
    if any(x["veredicto"] == "candidato compatible" for x in pares):
        return 0
    if pares and all(x["veredicto"] == "información insuficiente" for x in pares):
        return 3
    return 1


if __name__ == "__main__":
    sys.exit(main())
