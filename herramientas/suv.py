#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pydicom>=3.0,<4", "numpy>=1.26", "nibabel>=5"]
# ///
"""
suv.py 0.2.3
============
Cálculo de SUV de peso corporal (SUVbw) para una serie PET DICOM siguiendo el manual IBSI-SUV
(OncoRay, "Standardizing SUV Computation"), cuadro por cuadro, con las reglas de unidades
(BQML, GML y tipos de SUV, CM2ML, CNTS de Philips), corrección de decaimiento (ADMIN, START,
NONE), determinación de la hora de inicio del escaneo (etiquetas privadas de Siemens y GE,
adquisición igual a serie, retrocálculo por vendedor), hora de administración (con fecha, sin
fecha, cruce de medianoche, radionúclidos de vida larga) y objetos Enhanced PET.

Body-weight SUV for a PET DICOM series following the IBSI-SUV manual, frame by frame, with a
validation mode against the OncoRay digital reference objects. Prints counts, strategies and
numbers; never identity values.

Uso / Usage
  uv run suv.py calcular SERIE_PET [--mascara mask.nii.gz] [--nifti salida.nii.gz] [--json r.json]
  uv run suv.py comparar SERIE_PET_LIMPIA [--privado CARPETA_PRIVADA] [--json r.json]
        compara vóxel a vóxel el SUV de cada corte limpio con el de su original, emparejados por el
        índice privado; exige igualdad exacta (compares every voxel of each clean slice with its original,
        matched through the private index; exact equality expected)
  uv run suv.py validar  CARPETA_DRO            # carpeta DRO del repositorio oncoray/suv_computation

Código de salida / exit code: 0 ok, 1 no se pudo calcular o la validación falló, 2 error de uso.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import math
import os
import re
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
import numpy as np  # noqa: E402
import pydicom  # noqa: E402

pydicom.config.convert_wrong_length_to_UN = True

VERSION = "0.2.4"
PET_CLASICO = "1.2.840.10008.5.1.4.1.1.128"
PET_ENHANCED = "1.2.840.10008.5.1.4.1.1.130"
UMBRAL_MBQ = 1e4          # dosis < 1e4 → almacenada en MBq
VIDA_MEDIA_CORTA = 41400  # s; por encima se exige la fecha de administración
TIPOS_GML = {"", "BW", "LBM", "LBMJAMES128", "LBMJANMA", "IBW"}


class NoCalculable(Exception):
    """SUVbw no se puede calcular con los atributos presentes."""


# --------------------------------------------------------------------------------------
# fechas, horas y vendedores / dates, times, vendors
# --------------------------------------------------------------------------------------

def parse_dt(valor) -> dt.datetime | None:
    s = str(valor or "").strip()
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2}(?:\.\d{1,6})?)?", s)
    if not m or not s:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    h, mi = int(m.group(4) or 0), int(m.group(5) or 0)
    sec = float(m.group(6) or 0)
    try:
        return dt.datetime(y, mo, d, h, mi, int(sec), int(round((sec - int(sec)) * 1e6)))
    except ValueError:
        return None


def parse_da_tm(fecha, hora) -> dt.datetime | None:
    f, t = str(fecha or "").strip(), str(hora or "").strip().replace(":", "")
    if not f or not t:
        return None
    m = re.match(r"^(\d{2})(\d{2})(\d{2}(?:\.\d{1,6})?)?$", t)
    if not m:
        return None
    base = parse_dt(f[:8])
    if base is None:
        return None
    sec = float(m.group(3) or 0)
    return base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=int(sec),
                        microsecond=int(round((sec - int(sec)) * 1e6)))


def parse_tm(hora) -> dt.timedelta | None:
    t = str(hora or "").strip().replace(":", "")
    m = re.match(r"^(\d{2})(\d{2})(\d{2}(?:\.\d{1,6})?)?$", t)
    if not m:
        return None
    return dt.timedelta(hours=int(m.group(1)), minutes=int(m.group(2)), seconds=float(m.group(3) or 0))


def vendedor(ds) -> str:
    m = str(ds.get("Manufacturer", "")).upper()
    if "SIEMENS" in m:
        return "SIEMENS"
    if "PHILIPS" in m:
        return "PHILIPS"
    if re.search(r"(^|[^A-Z])GE([^A-Z]|$)|GEMS", m):
        return "GE"
    return "OTRO"


def positivo(valor) -> float | None:
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def t_ave(lmbda: float, T: float) -> float:
    """Tiempo de actividad media dentro de un cuadro de duración T (s)."""
    return (1.0 / lmbda) * math.log((lmbda * T) / (1.0 - math.exp(-lmbda * T)))


def buscar(item, keyword):
    """Primer elemento con esa palabra clave dentro de un ítem (secuencias incluidas)."""
    if item is None:
        return None
    if keyword in item:
        return item.get(keyword)
    for e in item:
        if str(e.VR) == "SQ":
            for sub in e.value:
                v = buscar(sub, keyword)
                if v is not None:
                    return v
    return None


# --------------------------------------------------------------------------------------
# factores de normalización / normalization factors (W kg, H cm)
# --------------------------------------------------------------------------------------

def factor_tipo(tipo: str, W: float, H_cm: float, sexo: str) -> float:
    def lbm(s):
        return 1.10 * W - 120 * (W / H_cm) ** 2 if s == "M" else 1.07 * W - 148 * (W / H_cm) ** 2
    def lbm128(s):
        return 1.10 * W - 128 * (W / H_cm) ** 2 if s == "M" else 1.07 * W - 148 * (W / H_cm) ** 2
    def janma(s):
        bmi = W / (H_cm / 100) ** 2
        return 9270 * W / (6680 + 216 * bmi) if s == "M" else 9270 * W / (8780 + 244 * bmi)
    def ibw(s):
        return 48.0 + 1.06 * (H_cm - 152) if s == "M" else 45.5 + 0.91 * (H_cm - 152)
    f = {"LBM": lbm, "LBMJAMES128": lbm128, "LBMJANMA": janma, "IBW": ibw}[tipo]
    if sexo == "O":
        return (f("M") + f("F")) / 2
    return f(sexo)


# --------------------------------------------------------------------------------------
# hora de administración y de referencia / administration and reference datetimes
# --------------------------------------------------------------------------------------

def hora_administracion(rf, t_ref: dt.datetime, vida_media: float, avisos: list[str]) -> dt.datetime:
    inicio_dt = parse_dt(buscar(rf, "RadiopharmaceuticalStartDateTime"))
    inicio_tm = parse_tm(buscar(rf, "RadiopharmaceuticalStartTime"))
    if inicio_dt is not None:
        offset = (t_ref - inicio_dt).total_seconds()
        if -3600 <= offset < 2 * vida_media:
            return inicio_dt
        if vida_media >= VIDA_MEDIA_CORTA:
            raise NoCalculable("fechas de administración y referencia incoherentes con un radionúclido de vida larga")
        avisos.append("fecha de administración no fiable; se usa la fecha de referencia con la hora de administración")
        hora = dt.timedelta(hours=inicio_dt.hour, minutes=inicio_dt.minute, seconds=inicio_dt.second, microseconds=inicio_dt.microsecond)
    elif inicio_tm is not None:
        if vida_media >= VIDA_MEDIA_CORTA:
            raise NoCalculable("solo hay hora de administración y el radionúclido es de vida larga")
        avisos.append("sin fecha de administración; se asume la fecha de referencia")
        hora = inicio_tm
    else:
        raise NoCalculable("falta la hora de administración")
    adm = dt.datetime.combine(t_ref.date(), dt.time()) + hora
    if (t_ref - adm).total_seconds() < -3600:
        adm -= dt.timedelta(days=1)
        avisos.append("tiempo de captación que cruza la medianoche")
    return adm


def hora_referencia_clasico(ds, vend: str, lmbda: float, avisos: list[str]) -> tuple[dt.datetime, str]:
    """Hora a la que están corregidos los valores (START) o hora de medición (NONE)."""
    correccion = str(ds.get("DecayCorrection", "")).upper()
    t_acq = parse_da_tm(ds.get("AcquisitionDate"), ds.get("AcquisitionTime"))
    frt = ds.get("FrameReferenceTime", None)
    afd = positivo(ds.get("ActualFrameDuration", None))
    if correccion == "START":
        if vend == "SIEMENS":
            priv = parse_dt(ds.get((0x0071, 0x1022), None).value if (0x0071, 0x1022) in ds else None)
            if priv:
                return priv, "privada Siemens (0071,1022)"
        if vend == "GE":
            priv = parse_dt(ds.get((0x0009, 0x100D), None).value if (0x0009, 0x100D) in ds else None)
            if priv:
                return priv, "privada GE (0009,100D)"
        t_ser = parse_da_tm(ds.get("SeriesDate"), ds.get("SeriesTime"))
        if t_acq is not None and t_ser is not None and t_acq == t_ser:
            return t_acq, "adquisición igual a serie"
        if t_acq is None:
            raise NoCalculable("falta fecha y hora de adquisición")
        if frt is None or float(frt) < 0:
            raise NoCalculable("falta Frame Reference Time")
        delta = dt.timedelta(milliseconds=float(frt))
        if vend == "GE":
            return t_acq - delta, "retrocálculo GE (adquisición menos FRT)"
        if afd is None:
            raise NoCalculable("falta Actual Frame Duration para el retrocálculo")
        if vend == "OTRO":
            avisos.append("fabricante no reconocido; se aplica la regla general de retrocálculo")
        return t_acq + dt.timedelta(seconds=t_ave(lmbda, afd / 1000.0)) - delta, "retrocálculo general (adquisición + Tave menos FRT)"
    if correccion == "NONE":
        if t_acq is None or afd is None:
            raise NoCalculable("NONE requiere adquisición y Actual Frame Duration")
        return t_acq + dt.timedelta(seconds=t_ave(lmbda, afd / 1000.0)), "medición (adquisición + Tave), sin corrección de decaimiento"
    raise NoCalculable(f"Decay Correction {correccion or 'ausente'} no admitida")


# --------------------------------------------------------------------------------------
# dosis y conversión / dose path
# --------------------------------------------------------------------------------------

def factor_dosis(ds, rf, t_ref_fn, avisos: list[str]) -> tuple[float, str, float | None]:
    """Devuelve (factor g/Bq·ml tal que SUVbw = Bq/ml · factor, estrategia, intervalo s)."""
    W = positivo(ds.get("PatientWeight", None))
    if W is None:
        raise NoCalculable("falta el peso del paciente")
    if W >= 1000:
        W /= 1000.0
        avisos.append("peso en gramos, convertido a kg")
    dosis = positivo(buscar(rf, "RadionuclideTotalDose"))
    if dosis is None:
        raise NoCalculable("falta la dosis administrada o no es positiva")
    if dosis < UMBRAL_MBQ:
        dosis *= 1e6
        avisos.append("dosis en MBq, convertida a Bq")
    correccion = str(buscar(ds, "DecayCorrection") or "").upper()
    if correccion == "ADMIN":
        return W * 1000.0 / dosis, "ADMIN, sin corrección de dosis", None
    vida_media = positivo(buscar(rf, "RadionuclideHalfLife"))
    if vida_media is None:
        raise NoCalculable("falta la vida media del radionúclido")
    lmbda = math.log(2) / vida_media
    t_ref, estrategia = t_ref_fn(lmbda)
    t_adm = hora_administracion(rf, t_ref, vida_media, avisos)
    intervalo = (t_ref - t_adm).total_seconds()
    dosis_corr = dosis * math.exp(-lmbda * intervalo)
    return W * 1000.0 / dosis_corr, estrategia, intervalo


def cuadro_clasico(ds) -> tuple[np.ndarray, dict]:
    """SUVbw de un objeto PET Image Storage (un cuadro)."""
    avisos: list[str] = []
    if "RescaleSlope" not in ds or "RescaleIntercept" not in ds:
        raise NoCalculable("faltan Rescale Slope o Rescale Intercept")
    m, b = float(ds.RescaleSlope), float(ds.RescaleIntercept)
    U = ds.pixel_array.astype(np.float64) * m + b
    unidades = str(ds.get("Units", "")).upper()
    rf = ds.RadiopharmaceuticalInformationSequence[0] if "RadiopharmaceuticalInformationSequence" in ds and len(ds.RadiopharmaceuticalInformationSequence) else None
    vend = vendedor(ds)
    info = {"unidades": unidades, "fabricante": vend, "correccion": str(ds.get("DecayCorrection", "")), "avisos": avisos}

    if unidades == "GML":
        tipo = str(ds.get("SUVType", "")).upper()
        if tipo in ("", "BW"):
            info["estrategia"] = "GML = SUVbw directo"
            return U, info
        if tipo not in TIPOS_GML:
            raise NoCalculable(f"SUV Type {tipo} no admitido")
        W, H = positivo(ds.get("PatientWeight", None)), positivo(ds.get("PatientSize", None))
        sexo = str(ds.get("PatientSex", "")).upper()
        if W is None or H is None or sexo not in ("M", "F", "O"):
            raise NoCalculable("GML con otro tipo de SUV requiere peso, talla y sexo válidos")
        f = factor_tipo(tipo, W, H * 100.0, sexo)
        info["estrategia"] = f"GML tipo {tipo}, convertido a SUVbw"
        return U * W / f, info
    if unidades == "CM2ML":
        W, H = positivo(ds.get("PatientWeight", None)), positivo(ds.get("PatientSize", None))
        if W is None or H is None:
            raise NoCalculable("CM2ML requiere peso y talla válidos")
        bsa = 0.007184 * (H * 100.0) ** 0.725 * W ** 0.425
        info["estrategia"] = "CM2ML (BSA), convertido a SUVbw"
        return U * (W * 1000.0) / (bsa * 1e4), info
    if unidades == "CNTS":
        if vend != "PHILIPS":
            raise NoCalculable("CNTS solo se admite con factores Philips")
        f_act = positivo(ds.get((0x7053, 0x1009), None).value if (0x7053, 0x1009) in ds else None)
        f_suv = positivo(ds.get((0x7053, 0x1000), None).value if (0x7053, 0x1000) in ds else None)
        if f_act:
            U = U * f_act
            unidades = "BQML"
            info["estrategia"] = "CNTS por factor de concentración de actividad Philips"
        elif f_suv and str(ds.get("SUVType", "")).upper() in ("", "BW"):
            info["estrategia"] = "CNTS por factor SUV Philips"
            return U * f_suv, info
        else:
            raise NoCalculable("CNTS sin factores Philips utilizables")
    if unidades != "BQML":
        raise NoCalculable(f"unidades {unidades or 'ausentes'} no admitidas")
    if rf is None:
        raise NoCalculable("falta Radiopharmaceutical Information Sequence")
    factor, estrategia, intervalo = factor_dosis(ds, rf, lambda l: hora_referencia_clasico(ds, vend, l, avisos), avisos)
    info["estrategia"] = info.get("estrategia", "BQML") + "; " + estrategia
    info["factor"], info["intervalo_s"] = factor, intervalo
    return U * factor, info


def cuadros_enhanced(ds) -> list[tuple[np.ndarray, dict]]:
    """SUVbw de un objeto Enhanced PET (multicuadro), cuadro por cuadro."""
    compartido = ds.SharedFunctionalGroupsSequence[0] if "SharedFunctionalGroupsSequence" in ds else None
    por_cuadro = list(ds.PerFrameFunctionalGroupsSequence) if "PerFrameFunctionalGroupsSequence" in ds else []
    pix = ds.pixel_array.astype(np.float64)
    if pix.ndim == 2:
        pix = pix[None]
    rf = ds.RadiopharmaceuticalInformationSequence[0] if "RadiopharmaceuticalInformationSequence" in ds and len(ds.RadiopharmaceuticalInformationSequence) else None
    vend = vendedor(ds)
    salida = []
    for i in range(pix.shape[0]):
        item = por_cuadro[i] if i < len(por_cuadro) else None
        avisos: list[str] = []

        def val(kw):
            # por cuadro → grupos compartidos → nivel superior del objeto / per-frame → shared → top level
            v = buscar(item, kw)
            if v is None:
                v = buscar(compartido, kw)
            if v is None and kw in ds:
                v = ds.get(kw)
            return v

        # mapeo a valores reales / real world value mapping
        rwv = val("RealWorldValueMappingSequence")
        elegido = None
        for cand in (rwv or []):
            code = str(buscar(cand, "CodeValue") or buscar(cand, "LongCodeValue") or "").strip()
            if code:
                elegido = (cand, code) if elegido is None or ("SUV" in code and "SUV" not in elegido[1]) else elegido
        if elegido is None:
            raise NoCalculable("falta Real World Value Mapping con unidades")
        cand, code = elegido
        m = float(buscar(cand, "RealWorldValueSlope") or 1.0)
        b = float(buscar(cand, "RealWorldValueIntercept") or 0.0)
        U = pix[i] * m + b
        info = {"unidades": code, "fabricante": vend, "avisos": avisos}
        if code.lower().startswith("g/ml"):
            tipo = re.sub(r"[^A-Z0-9]", "", code.split("{")[-1].upper().replace("SUV", ""))
            if tipo in ("", "BW"):
                info["estrategia"] = "g/ml{SUVbw} directo"
                salida.append((U, info))
                continue
            W, H = positivo(ds.get("PatientWeight", None)), positivo(ds.get("PatientSize", None))
            sexo = str(ds.get("PatientSex", "")).upper()
            if tipo not in TIPOS_GML or W is None or H is None or sexo not in ("M", "F", "O"):
                raise NoCalculable(f"tipo de SUV {tipo} no convertible")
            salida.append((U * W / factor_tipo(tipo, W, H * 100.0, sexo), info | {"estrategia": f"g/ml tipo {tipo}"}))
            continue
        if code.lower() not in ("bq/ml", "bqml"):
            raise NoCalculable(f"unidades {code} no admitidas")
        if rf is None:
            raise NoCalculable("falta Radiopharmaceutical Information Sequence")
        decay_corr = str(val("DecayCorrected") or "").upper()

        def t_ref_fn(lmbda):
            if decay_corr == "YES":
                t = parse_dt(val("DecayCorrectionDateTime"))
                if t is None:
                    raise NoCalculable("falta Decay Correction DateTime")
                return t, "Enhanced: Decay Correction DateTime"
            t = parse_dt(val("FrameReferenceDateTime"))
            if t is None:
                raise NoCalculable("falta Frame Reference DateTime")
            return t, "Enhanced: Frame Reference DateTime (sin corrección)"

        factor, estrategia, intervalo = factor_dosis(ds, rf, t_ref_fn, avisos)
        info["estrategia"], info["factor"], info["intervalo_s"] = "Bq/ml; " + estrategia, factor, intervalo
        salida.append((U * factor, info))
    return salida


# --------------------------------------------------------------------------------------
# serie completa / whole series
# --------------------------------------------------------------------------------------

def leer_serie(carpeta: Path) -> list[tuple[float, np.ndarray, dict]]:
    """Devuelve [(z, suv_2d, info)] ordenados por z ascendente."""
    cuadros = []
    for ruta in sorted(p for p in carpeta.rglob("*") if p.is_file() and not p.is_symlink()):
        try:
            ds = pydicom.dcmread(ruta, force=True)
            sop = str(ds.get("SOPClassUID", ""))
        except Exception:
            continue
        if sop == PET_CLASICO:
            suv, info = cuadro_clasico(ds)
            z = float(ds.ImagePositionPatient[2]) if "ImagePositionPatient" in ds else float(len(cuadros))
            cuadros.append((z, suv, info))
        elif sop == PET_ENHANCED:
            for i, (suv, info) in enumerate(cuadros_enhanced(ds)):
                cuadros.append((float(i), suv, info))
    cuadros.sort(key=lambda c: c[0])
    return cuadros


def volumen(cuadros) -> np.ndarray:
    return np.stack([c[1] for c in cuadros], axis=0)   # (z, filas, columnas)


def cargar_mascara(ruta: Path, forma_zyx: tuple[int, int, int]) -> np.ndarray:
    import nibabel as nib
    m = nib.load(str(ruta))
    datos = np.asarray(m.dataobj) > 0                  # NIfTI (i, j, k) = (columna, fila, corte)
    datos = np.transpose(datos, (2, 1, 0))             # → (corte, fila, columna)
    if datos.shape != forma_zyx:
        raise NoCalculable(f"la máscara {datos.shape} no coincide con el volumen {forma_zyx}")
    return datos


def resumen(cuadros, mascara: np.ndarray | None) -> dict:
    vol = volumen(cuadros)
    estrategias = sorted({c[2].get("estrategia", "") for c in cuadros})
    avisos = sorted({a for c in cuadros for a in c[2].get("avisos", [])})
    factores = [c[2]["factor"] for c in cuadros if "factor" in c[2]]
    intervalos = [c[2]["intervalo_s"] for c in cuadros if c[2].get("intervalo_s") is not None]
    r = {"cuadros": len(cuadros), "forma": list(vol.shape), "unidades": sorted({c[2]["unidades"] for c in cuadros}),
         "fabricante": sorted({c[2]["fabricante"] for c in cuadros}), "estrategias": estrategias, "avisos": avisos,
         "factor_min": min(factores) if factores else None, "factor_max": max(factores) if factores else None,
         "intervalo_s_min": min(intervalos) if intervalos else None, "intervalo_s_max": max(intervalos) if intervalos else None,
         "suv_max_volumen": float(vol.max()), "suv_p99_volumen": float(np.percentile(vol, 99))}
    if mascara is not None:
        v = vol[mascara]
        r["mascara"] = {"voxeles": int(mascara.sum()), "suv_min": float(v.min()), "suv_mediana": float(np.median(v)), "suv_max": float(v.max()), "suv_media": float(v.mean())}
    return r


# --------------------------------------------------------------------------------------
# comandos / commands
# --------------------------------------------------------------------------------------

def cmd_calcular(args) -> int:
    carpeta = Path(args.serie).expanduser()
    if not carpeta.is_dir():
        print("No existe la carpeta (folder not found)")
        return 2
    try:
        cuadros = leer_serie(carpeta)
        if not cuadros:
            print("No hay imágenes PET en la carpeta (no PET images)")
            return 1
        mascara = cargar_mascara(Path(args.mascara).expanduser(), tuple(volumen(cuadros).shape)) if args.mascara else None
        r = resumen(cuadros, mascara)
    except NoCalculable as e:
        print(f"SUVbw no calculable: {e}")
        return 1
    print(f"suv {VERSION}: {r['cuadros']} cuadros, volumen {r['forma']}, unidades {r['unidades']}, fabricante {r['fabricante']}")
    print(f"  estrategia: {'; '.join(r['estrategias'])}")
    if r["factor_min"] is not None:
        print(f"  factor SUV (g/Bq·ml): {r['factor_min']:.6g} .. {r['factor_max']:.6g}; intervalo administración-referencia: {r['intervalo_s_min']:.0f} .. {r['intervalo_s_max']:.0f} s")
    print(f"  SUV máximo del volumen: {r['suv_max_volumen']:.3f}; percentil 99: {r['suv_p99_volumen']:.3f}")
    if "mascara" in r:
        m = r["mascara"]
        print(f"  dentro de la máscara ({m['voxeles']} vóxeles): mín {m['suv_min']:.3f}, mediana {m['suv_mediana']:.3f}, máx {m['suv_max']:.3f}, media {m['suv_media']:.3f}")
    for a in r["avisos"]:
        print(f"  aviso: {a}")
    if args.nifti:
        import nibabel as nib
        vol = volumen(cuadros)
        primera = next(iter(sorted(p for p in carpeta.rglob("*") if p.is_file())), None)
        ds = pydicom.dcmread(primera, force=True)
        sp = [float(x) for x in ds.PixelSpacing] if "PixelSpacing" in ds else [1.0, 1.0]
        dz = (cuadros[1][0] - cuadros[0][0]) if len(cuadros) > 1 else float(ds.get("SliceThickness", 1.0) or 1.0)
        ipp = [float(x) for x in ds.ImagePositionPatient] if "ImagePositionPatient" in ds else [0.0, 0.0, 0.0]
        afin = np.array([[-sp[1], 0, 0, -ipp[0]], [0, -sp[0], 0, -ipp[1]], [0, 0, dz, cuadros[0][0]], [0, 0, 0, 1]])
        nib.save(nib.Nifti1Image(np.transpose(vol, (2, 1, 0)).astype(np.float32), afin), str(Path(args.nifti).expanduser()))
        print(f"  volumen SUV guardado en NIfTI: {args.nifti}")
    if args.json:
        Path(args.json).expanduser().write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def cmd_validar(args) -> int:
    raiz = Path(args.dro).expanduser()
    lista = raiz / "DRO_list.csv"
    if not lista.exists():
        print("No se encontró DRO_list.csv (expected in the DRO folder)")
        return 2
    filas = list(csv.DictReader(open(lista, encoding="utf-8")))
    ok = fallos = 0
    for f in filas:
        ident, carpeta = f["ID"], raiz / "data" / f["ID"]
        esperado_error = f["SUVmin_expected"].strip().upper() == "ERROR"
        try:
            cuadros = leer_serie(carpeta / "PT")
            if not cuadros:
                raise NoCalculable("sin cuadros")
            mascara = cargar_mascara(carpeta / "mask" / "DRO_mask.nii.gz", tuple(volumen(cuadros).shape))
            r = resumen(cuadros, mascara)["mascara"]
            if esperado_error:
                print(f"  {ident:14s} FALLA: se esperaba error y se calculó mín {r['suv_min']:.3f} med {r['suv_mediana']:.3f} máx {r['suv_max']:.3f}")
                fallos += 1
                continue
            esperados = [float(f["SUVmin_expected"]), float(f["SUVmed_expected"]), float(f["SUVmax_expected"])]
            medidos = [r["suv_min"], r["suv_mediana"], r["suv_max"]]
            bien = all(abs(a - b) <= max(0.01, 0.01 * b) for a, b in zip(medidos, esperados))
            print(f"  {ident:14s} {'OK   ' if bien else 'FALLA'} mín {medidos[0]:.3f} med {medidos[1]:.3f} máx {medidos[2]:.3f}  (esperado {esperados[0]:.2f} {esperados[1]:.2f} {esperados[2]:.2f})")
            ok += bien
            fallos += not bien
        except NoCalculable as e:
            if esperado_error:
                print(f"  {ident:14s} OK    error esperado: {e}")
                ok += 1
            else:
                print(f"  {ident:14s} FALLA: no calculable: {e}")
                fallos += 1
        except Exception as e:
            print(f"  {ident:14s} FALLA: {type(e).__name__}: {e}")
            fallos += 1
    print(f"\nValidación IBSI-SUV: {ok} correctos, {fallos} fallos de {len(filas)} objetos de referencia")
    return 0 if fallos == 0 else 1


def cargar_indice(carpeta_privada: Path, limpios: Path) -> dict[str, str]:
    """limpio (absoluto) → original (absoluto), resolviendo raíces relativas y reubicaciones."""
    out: dict[str, str] = {}
    try:
        reubicadas = json.loads((carpeta_privada / "raices.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        reubicadas = {}
    base = limpios.parent
    try:
        lineas = (carpeta_privada / "indice_origen.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for linea in lineas:
        try:
            d = json.loads(linea)
        except ValueError:
            continue
        limpio = str((limpios / d["limpio_rel"]).resolve()) if d.get("limpio_rel") else str(Path(d.get("limpio", "")).resolve())
        if "original_rel" in d:
            raiz = d.get("raiz", "")
            r = Path(reubicadas.get(raiz) or raiz)
            r = r if r.is_absolute() else base / r
            out[limpio] = str(r / d["original_rel"])
        else:
            out[limpio] = d.get("original", "")
    return out


def cmd_comparar(args) -> int:
    """Compara vóxel a vóxel el SUV de una serie PET limpia con su original. Primero establece que la
    correspondencia es completa en ambos sentidos (serie original, índice privado, carpeta limpia) y que el
    orden físico es coherente; solo después compara valores."""
    serie = Path(args.serie).expanduser().resolve()
    if not serie.is_dir():
        print("No existe la carpeta (folder not found)")
        return 2
    limpios = next((p for p in serie.parents if p.name == "limpios"), None)
    if limpios is None and not args.privado:
        print("La serie debe estar dentro de limpios/ o hay que indicar --privado (series must sit under limpios/ or pass --privado)")
        return 2
    privado = Path(args.privado).expanduser() if args.privado else limpios.parent / "limpios_PRIVADO"
    indice = cargar_indice(privado, limpios or serie.parent.parent.parent)
    problemas: list[str] = []

    # 1. inventario limpio → originales por el índice
    limpios_en_carpeta = sorted(p.resolve() for p in serie.iterdir() if p.is_file() and p.suffix.lower() == ".dcm")
    if getattr(args, "solo_lista", None):
        # Conjunto exacto: la comparación se limita a estos archivos limpios de la serie. Todo lo demás es igual de estricto.
        try:
            base = (limpios or serie.parent.parent.parent).resolve()
            conjunto = {(base / Path(*str(r).split("/"))).resolve() for r in json.loads(Path(args.solo_lista).read_text(encoding="utf-8"))["archivos"]}
        except (OSError, ValueError, KeyError, TypeError):
            print("No se pudo leer la lista del conjunto a comparar (cannot read the file list)")
            return 2
        fuera = [c for c in limpios_en_carpeta if c not in conjunto]
        limpios_en_carpeta = [c for c in limpios_en_carpeta if c in conjunto]
        print(f"conjunto exacto: {len(limpios_en_carpeta)} archivo(s) de esta serie; {len(fuera)} fuera del conjunto, sin examinar")
        if not limpios_en_carpeta:
            print("Ningún archivo de la lista pertenece a esta serie (no listed file in this series)")
            return 2
    limpio_a_original: dict[Path, Path] = {}
    sin_indice = 0
    for c in limpios_en_carpeta:
        o = indice.get(str(c))
        if not o:
            sin_indice += 1
            continue
        limpio_a_original[c] = Path(o)
    if sin_indice:
        problemas.append(f"{sin_indice} archivo(s) limpio(s) sin entrada en el índice privado")
    original_a_limpios: dict[Path, list[Path]] = {}
    for c, o in limpio_a_original.items():
        original_a_limpios.setdefault(o, []).append(c)
    duplicados = sum(1 for cs in original_a_limpios.values() if len(cs) > 1)
    if duplicados:
        problemas.append(f"{duplicados} original(es) con más de un archivo limpio")
    originales_ausentes = sum(1 for o in original_a_limpios if not o.is_file())
    if originales_ausentes:
        problemas.append(f"{originales_ausentes} original(es) no localizable(s); reimporta o reubica la raíz")

    # 2. serie original completa: todos los archivos de las carpetas de origen con el mismo SeriesInstanceUID
    uids_serie: set[str] = set()
    for o in list(original_a_limpios):
        if o.is_file():
            try:
                uids_serie.add(str(pydicom.dcmread(o, force=True, stop_before_pixels=True).get("SeriesInstanceUID", "")))
            except Exception:
                problemas.append(f"original ilegible: {o.name}")
    if len(uids_serie) > 1:
        problemas.append(f"los originales emparejados pertenecen a {len(uids_serie)} series distintas")
    serie_original: list[Path] = []
    if len(uids_serie) == 1:
        uid = next(iter(uids_serie))
        for carpeta in sorted({o.parent for o in original_a_limpios if o.is_file()}):
            for f in sorted(p for p in carpeta.iterdir() if p.is_file()):
                try:
                    ds = pydicom.dcmread(f, force=True, stop_before_pixels=True)
                except Exception:
                    continue
                if str(ds.get("SeriesInstanceUID", "")) == uid and str(ds.get("SOPClassUID", "")) in (PET_CLASICO, PET_ENHANCED):
                    serie_original.append(f.resolve())
        inversa = {str(Path(o).resolve()): c for c, o in indice.items()}
        faltantes = [o for o in serie_original if o not in original_a_limpios and not Path(inversa.get(str(o), "")).is_file()]
        mal_mapeados = [o for o in serie_original if o not in original_a_limpios and Path(inversa.get(str(o), "")).is_file()
                        and Path(inversa[str(o)]).resolve().parent != serie]
        if faltantes:
            problemas.append(f"{len(faltantes)} corte(s) de la serie original sin archivo limpio (faltantes)")
        if mal_mapeados:
            problemas.append(f"{len(mal_mapeados)} corte(s) de la serie original cuyo archivo limpio está en otra carpeta")
        sobrantes = [o for o in original_a_limpios if o not in set(serie_original)]
        if sobrantes:
            problemas.append(f"{len(sobrantes)} archivo(s) limpio(s) cuyo original no pertenece a esta serie")
    pares = [(cs[0], o) for o, cs in original_a_limpios.items() if o.is_file() and len(cs) == 1]

    # 3. orden físico, geometría de cada par y números de instancia (lista aparte de la correspondencia)
    problemas_orden: list[str] = []
    exactos = geometria_distinta = no_finitos = instancias_inconsistentes = geometria_incompleta = 0
    max_dif, voxeles_distintos, voxeles_total = 0.0, 0, 0
    factores: set[tuple[float, float]] = set()
    intervalos: set[tuple[float, float]] = set()
    detalles: list[str] = []
    zs: list[tuple[float, int]] = []
    suvmax_o = suvmax_c = 0.0
    todos_o, todos_c = [], []
    geo = lambda d: (tuple(float(x) for x in d.get("ImagePositionPatient", [])), tuple(float(x) for x in d.get("ImageOrientationPatient", [])),
                     tuple(float(x) for x in d.get("PixelSpacing", [])), int(d.get("Rows", 0)), int(d.get("Columns", 0)))
    def geometria_completa(d) -> bool:
        return ("ImagePositionPatient" in d and len(d.ImagePositionPatient) == 3 and "ImageOrientationPatient" in d
                and len(d.ImageOrientationPatient) == 6 and "PixelSpacing" in d and len(d.PixelSpacing) == 2)

    for limpio, original in pares:
        ds_o = pydicom.dcmread(original, force=True)
        ds_c = pydicom.dcmread(limpio, force=True)
        if not (geometria_completa(ds_o) and geometria_completa(ds_c)):
            geometria_incompleta += 1          # cada par debe traer posición, orientación y espaciado en ambos lados
            detalles.append(f"{limpio.name}: geometría incompleta en el original o en el limpio")
            continue
        if geo(ds_o) != geo(ds_c):
            geometria_distinta += 1
            continue
        if str(ds_o.get("InstanceNumber", "")) != str(ds_c.get("InstanceNumber", "")):
            instancias_inconsistentes += 1
            detalles.append(f"{limpio.name}: número de instancia distinto del original")
        if "ImagePositionPatient" in ds_o and "ImageOrientationPatient" in ds_o:
            o = [float(x) for x in ds_o.ImageOrientationPatient]
            n = [o[1] * o[5] - o[2] * o[4], o[2] * o[3] - o[0] * o[5], o[0] * o[4] - o[1] * o[3]]
            z = sum(a * b for a, b in zip([float(x) for x in ds_o.ImagePositionPatient], n))
            try:
                zs.append((z, int(ds_o.InstanceNumber)))
            except (TypeError, ValueError):
                zs.append((z, 0))
        try:
            suv_o, info_o = cuadro_clasico(ds_o)
            suv_c, info_c = cuadro_clasico(ds_c)
        except NoCalculable as e:
            detalles.append(f"{limpio.name}: no calculable: {e}")
            problemas.append(f"{limpio.name}: SUV no calculable")
            continue
        if suv_o.shape != suv_c.shape:
            geometria_distinta += 1
            continue
        if not (np.isfinite(suv_o).all() and np.isfinite(suv_c).all()):
            no_finitos += 1
        dif = np.abs(suv_o - suv_c)
        voxeles_distintos += int((dif != 0).sum())
        voxeles_total += int(dif.size)
        max_dif = max(max_dif, float(dif.max()))
        exacto = bool(np.array_equal(suv_o, suv_c))
        exactos += exacto
        nan = float("nan")
        factores.add((info_o.get("factor") if info_o.get("factor") is not None else nan, info_c.get("factor") if info_c.get("factor") is not None else nan))
        intervalos.add((info_o.get("intervalo_s") if info_o.get("intervalo_s") is not None else nan, info_c.get("intervalo_s") if info_c.get("intervalo_s") is not None else nan))
        suvmax_o, suvmax_c = max(suvmax_o, float(suv_o.max())), max(suvmax_c, float(suv_c.max()))
        todos_o.append(np.percentile(suv_o, 99)); todos_c.append(np.percentile(suv_c, 99))
        if not exacto:
            detalles.append(f"{limpio.name}: {int((dif != 0).sum())} vóxeles distintos, diferencia máxima {float(dif.max()):.3g}")
    zs_ord = sorted(zs)
    posiciones_duplicadas = sum(1 for a, b in zip(zs_ord, zs_ord[1:]) if abs(b[0] - a[0]) < 1e-3)
    orden_fisico_ok = bool(zs) and len(zs) == len(pares) and posiciones_duplicadas == 0
    instancias_vs_z = [i for _, i in zs_ord]
    orden_instancias_ok = instancias_vs_z == sorted(instancias_vs_z) or instancias_vs_z == sorted(instancias_vs_z, reverse=True)
    if geometria_incompleta:
        problemas_orden.append(f"geometría incompleta en {geometria_incompleta} par(es): posición, orientación o espaciado ausentes")
    if pares and not zs:
        problemas_orden.append("geometría ausente: sin ImagePositionPatient u orientación en los cortes")
    if zs and not orden_instancias_ok:
        problemas_orden.append("los números de instancia no siguen el orden físico de los cortes")
    if posiciones_duplicadas:
        problemas_orden.append(f"{posiciones_duplicadas} posición(es) de corte duplicada(s) en la serie")
    if instancias_inconsistentes:
        problemas_orden.append(f"{instancias_inconsistentes} número(s) de instancia distinto(s) entre original y limpio")
    if geometria_distinta:
        problemas_orden.append(f"{geometria_distinta} corte(s) con geometría distinta")

    factores_iguales = all((math.isnan(a) and math.isnan(b)) or (not math.isnan(a) and not math.isnan(b) and a == b) for a, b in factores)
    intervalos_iguales = all((math.isnan(a) and math.isnan(b)) or (not math.isnan(a) and not math.isnan(b) and a == b) for a, b in intervalos)
    completo = not problemas and len(serie_original) == len(pares) == len(limpios_en_carpeta) and len(serie_original) > 0
    pixeles_identicos = bool(pares) and exactos == len(pares) and no_finitos == 0 and factores_iguales and intervalos_iguales
    orden_ok = not problemas_orden and orden_fisico_ok and orden_instancias_ok
    veredicto = "IDENTICO" if (completo and orden_ok and pixeles_identicos) else "DIFERENCIAS"
    r = {"version": VERSION, "serie_limpia": serie.name, "cortes_serie_original": len(serie_original), "archivos_limpios": len(limpios_en_carpeta),
         "pares_comparados": len(pares), "correspondencia_completa": completo, "problemas_de_correspondencia": problemas,
         "orden_y_geometria_ok": orden_ok, "problemas_de_orden_y_geometria": problemas_orden, "geometria_incompleta": geometria_incompleta,
         "pixeles_y_suv_identicos": pixeles_identicos,
         "orden_fisico_ok": orden_fisico_ok, "orden_instancias_coherente": orden_instancias_ok, "instancias_inconsistentes": instancias_inconsistentes,
         "cortes_exactos": exactos, "voxeles_comparados": voxeles_total, "voxeles_distintos": voxeles_distintos,
         "diferencia_maxima_suv": max_dif, "cortes_con_valores_no_finitos": no_finitos, "factores_identicos": factores_iguales,
         "intervalos_identicos": intervalos_iguales, "factor_original": sorted({f"{a:.6g}" for a, _ in factores}),
         "factor_limpio": sorted({f"{b:.6g}" for _, b in factores}),
         "intervalo_s_original": sorted({round(a, 1) for a, _ in intervalos if not math.isnan(a)}),
         "intervalo_s_limpio": sorted({round(b, 1) for _, b in intervalos if not math.isnan(b)}),
         "intervalo_aplicable": any(not math.isnan(a) for a, _ in intervalos),
         "suvmax_original": suvmax_o, "suvmax_limpio": suvmax_c,
         "p99_mediana_original": float(np.median(todos_o)) if todos_o else None, "p99_mediana_limpio": float(np.median(todos_c)) if todos_c else None,
         "detalles": detalles[:50], "veredicto": veredicto,
         "nota": "igualdad exacta con correspondencia completa demuestra que la limpieza conserva el SUV; no prueba la calibración del escáner"}
    print(f"suv comparar {VERSION}: serie original {len(serie_original)} cortes, carpeta limpia {len(limpios_en_carpeta)} archivos, {len(pares)} pares comparados; "
          f"correspondencia completa: {'sí' if completo else 'NO'}")
    for q in problemas:
        print(f"  - correspondencia: {q}")
    for q in problemas_orden:
        print(f"  - orden/geometría: {q}")
    print(f"  orden físico sin duplicados: {'sí' if orden_fisico_ok else 'NO'}; números de instancia coherentes con el orden físico: {'sí' if orden_instancias_ok else 'NO'}; instancias distintas original/limpio: {instancias_inconsistentes}")
    print(f"  cortes exactos: {exactos}/{len(pares)}; vóxeles distintos: {voxeles_distintos} de {voxeles_total}; diferencia máxima de SUV: {max_dif:.3g}")
    print(f"  factores idénticos: {factores_iguales} ({', '.join(r['factor_original'])}); intervalos idénticos: {intervalos_iguales} ({', '.join(str(x) for x in r['intervalo_s_original'])} s)")
    print(f"  SUVmax original {suvmax_o:.3f} / limpio {suvmax_c:.3f}")
    for d in detalles[:10]:
        print(f"  - {d}")
    print(f"  resultados por separado: correspondencia completa {'sí' if completo else 'NO'}; orden y geometría {'sí' if orden_ok else 'NO'}; "
          f"píxeles y SUV idénticos en los pares {'sí' if pixeles_identicos else 'NO'}")
    print(f"Veredicto: {veredicto}. {r['nota']}")
    destino = Path(args.json).expanduser() if args.json else (limpios.parent / "limpios_REPORTES" / f"comparacion_suv_{serie.name}_{dt.datetime.now():%Y%m%d_%H%M%S}.json" if limpios else None)
    if destino:
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  reporte guardado: {destino.name}")
    return 0 if veredicto == "IDENTICO" else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="SUVbw según IBSI-SUV (SUVbw per the IBSI-SUV manual)")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calcular")
    c.add_argument("serie")
    c.add_argument("--mascara")
    c.add_argument("--nifti")
    c.add_argument("--json")
    c.set_defaults(func=cmd_calcular)
    v = sub.add_parser("validar")
    v.add_argument("dro")
    v.set_defaults(func=cmd_validar)
    k = sub.add_parser("comparar")
    k.add_argument("serie", help="serie PET limpia (clean PET series folder under limpios/)")
    k.add_argument("--privado", help="carpeta privada con el índice (private folder with the index)")
    k.add_argument("--json")
    k.add_argument("--solo-lista", help="JSON con 'archivos': rutas limpias (relativas a limpios/, con '/') a las que se limita la comparación")
    k.set_defaults(func=cmd_comparar)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
