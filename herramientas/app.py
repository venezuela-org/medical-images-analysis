#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastapi>=0.115,<1",
#   "uvicorn>=0.30,<1",
#   "pydicom>=3.0,<4",
#   "numpy>=1.26",
#   "pillow>=10",
# ]
# ///
"""
app.py 0.7.0: aplicación local PET/CT (anonimizar, revisar, enviar).
Local PET/CT app (anonymize, review, send). Corre solo en 127.0.0.1. Nada sale a internet.

Uso / Usage
  uv run app.py                              # abre el navegador con la dirección y el token
  uv run app.py --trabajo ~/datos-pet        # carpeta de trabajo distinta
  uv run app.py --puerto 8765 --sin-navegador

Carpeta de trabajo / Work folder (por defecto ~/datos-pet)
  limpios/     copia anonimizada por seudónimo, estudio y serie     (compartible)
  paquetes/    zips verificados, con manifiesto                     (compartible)
  PRIVADO/     mapa de seudónimos y secreto de UID                  (nunca sale)
  registro.log bitácora privada: rutas y seudónimos, nunca valores  (nunca sale)

Requiere anonimizar.py (misma versión) en la misma carpeta que app.py.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys

# Consola en UTF-8 también en Windows / UTF-8 console output on Windows too
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except (AttributeError, ValueError):
    pass
import tempfile
import threading
import warnings
import webbrowser
import zipfile
from pathlib import Path

warnings.simplefilter("ignore")
import numpy as np  # noqa: E402
import pydicom  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import APIRouter, Body, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response  # noqa: E402
from PIL import Image  # noqa: E402

pydicom.config.convert_wrong_length_to_UN = True

import importlib  # noqa: E402
import importlib.util  # noqa: E402

CONGELADO = bool(getattr(sys, "frozen", False))   # empaquetado con PyInstaller / frozen build


def _cargar_anonimizar():
    if CONGELADO:
        return importlib.import_module("anonimizar")
    ruta = Path(__file__).resolve().parent / "anonimizar.py"
    spec = importlib.util.spec_from_file_location("anonimizar", ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def comando_herramienta(nombre: str) -> list[str]:
    """Cómo invocar una herramienta en un subproceso: el ejecutable congelado con 'herramienta X', o python X.py."""
    if CONGELADO:
        return [sys.executable, "herramienta", nombre]
    return [sys.executable, str(AQUI / f"{nombre}.py")]

ANON = _cargar_anonimizar()

VERSION = "0.9.3"
AQUI = Path(__file__).resolve().parent
ANONIMIZAR = AQUI / "anonimizar.py"
SYNC_MARCAS = ("Library/Mobile Documents", "Library/CloudStorage", "OneDrive", "Dropbox", "Google Drive", "My Drive", "iCloud")
VENTANAS = {"tejido": (40, 400), "pulmon": (-600, 1500), "hueso": (300, 1500), "cerebro": (40, 80)}
PACIENTE_RE = re.compile(r"^[A-Z0-9]{2,12}-\d{4}$")

TOKEN = secrets.token_urlsafe(18)
TRABAJO = Path.home() / "datos-pet"
TRABAJOS: dict[str, dict] = {}
INDICE: dict[str, dict] = {}
REGISTRO: list[str] = []
CANDADO = threading.Lock()


# --------------------------------------------------------------------------------------
# utilidades / helpers
# --------------------------------------------------------------------------------------

def registrar(texto: str) -> None:
    linea = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {texto}"
    REGISTRO.append(linea)
    try:
        with open(TRABAJO / "registro.log", "a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except OSError:
        pass


def en_sincronizada(ruta: Path) -> str | None:
    s = str(ruta.resolve())
    return next((m for m in SYNC_MARCAS if m in s), None)


TITULOS_DIALOGO = {"origen": "Carpeta con los DICOM originales", "destino": "Carpeta donde guardar el zip"}


def dialogo_carpeta(para: str = "origen") -> str | None:
    """Selector nativo. El título sale de una lista fija de dos textos; nunca se interpola texto de la petición."""
    titulo = TITULOS_DIALOGO["destino" if para == "destino" else "origen"]
    try:
        if sys.platform == "darwin":
            guion = ('POSIX path of (choose folder with prompt "Carpeta con los DICOM originales")' if titulo == TITULOS_DIALOGO["origen"]
                     else 'POSIX path of (choose folder with prompt "Carpeta donde guardar el zip")')
            r = subprocess.run(["osascript", "-e", guion], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
            return (r.stdout.strip() or None) if r.returncode == 0 else None
        if sys.platform.startswith("win"):
            # La ruta elegida puede llevar acentos (C:\\Users\\José\\...): PowerShell la escribe en UTF-8 y aquí se lee como UTF-8
            descripcion = ("$d.Description = 'Carpeta con los DICOM originales'; " if titulo == TITULOS_DIALOGO["origen"]
                           else "$d.Description = 'Carpeta donde guardar el zip'; ")
            ps = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Add-Type -AssemblyName System.Windows.Forms; "
                  "$d = New-Object System.Windows.Forms.FolderBrowserDialog; " + descripcion +
                  "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
            return r.stdout.strip() or None
        argumento = "--title=Carpeta con los DICOM originales" if titulo == TITULOS_DIALOGO["origen"] else "--title=Carpeta donde guardar el zip"
        r = subprocess.run(["zenity", "--file-selection", "--directory", argumento],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        return r.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def correr_en_segundo_plano(nombre: str, comando: list[str]) -> str:
    tid = secrets.token_hex(4)
    TRABAJOS[tid] = {"nombre": nombre, "estado": "corriendo", "lineas": [], "codigo": None}

    def hilo() -> None:
        try:
            p = subprocess.Popen(comando, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)  # las herramientas escriben UTF-8; sin esto Windows decodifica en cp1252
            assert p.stdout is not None
            for linea in p.stdout:
                TRABAJOS[tid]["lineas"].append(linea.rstrip())
            codigo = p.wait()
        except OSError as e:
            TRABAJOS[tid]["lineas"].append(f"error: {type(e).__name__}")
            codigo = 2
        TRABAJOS[tid]["codigo"] = codigo
        TRABAJOS[tid]["estado"] = "ok" if codigo == 0 else ("con avisos" if codigo == 1 else "error")
        registrar(f"{nombre}: terminó con código {codigo}")
        CANDADO.release()

    if not CANDADO.acquire(blocking=False):
        raise HTTPException(409, "ya hay un trabajo en curso / a job is already running")
    threading.Thread(target=hilo, daemon=True).start()
    registrar(f"{nombre}: iniciado")
    return tid


def ejecutar(comando: list[str]) -> tuple[int, str]:
    r = subprocess.run(comando, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout + r.stderr)


def carpeta_reportes() -> Path:
    return TRABAJO / "limpios_REPORTES"


def carpeta_privada() -> Path:
    """La misma carpeta que usa la línea de comandos para limpios/: limpios_PRIVADO. Si solo existe la antigua PRIVADO, se respeta."""
    nueva, antigua = TRABAJO / "limpios_PRIVADO", TRABAJO / "PRIVADO"
    if not (nueva / "mapa_seudonimos.json").exists() and (antigua / "mapa_seudonimos.json").exists():
        return antigua
    return nueva


def prefijo_actual() -> str:
    pol = TRABAJO / "limpios" / ".politica.json"
    try:
        return json.loads(pol.read_text(encoding="utf-8")).get("prefijo", "ANON")
    except (OSError, ValueError):
        return "ANON"


def sha256_archivo(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def carpeta_paciente(pac: str) -> Path:
    """Solo seudónimos del inventario, contenidos en limpios/, sin enlaces simbólicos."""
    if not PACIENTE_RE.match(pac):
        raise HTTPException(400, "identificador de paciente no válido / invalid patient id")
    limpios = (TRABAJO / "limpios").resolve()
    ruta = limpios / pac
    if ruta.is_symlink() or not ruta.is_dir() or ruta.resolve().parent != limpios:
        raise HTTPException(400, "carpeta de paciente no válida / invalid patient folder")
    return ruta


def archivos_regulares(carpeta: Path):
    for raiz, dirs, nombres in os.walk(carpeta, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(raiz, d)))
        for n in sorted(nombres):
            p = Path(raiz) / n
            if not p.is_symlink() and p.is_file():
                yield p


# --------------------------------------------------------------------------------------
# índice / index
# --------------------------------------------------------------------------------------

# --------------------------------------------------------------------------------------
# orden de los cortes de una serie: un solo criterio para el visor y para la exportación
# --------------------------------------------------------------------------------------
# 1. InstanceNumber, cuando todos los archivos de la serie lo tienen, es un entero válido y no se repite.
# 2. Si no, posición y orientación, solo cuando describen una pila coherente: todos los archivos con
#    ImagePositionPatient e ImageOrientationPatient, una sola orientación, posiciones distintas a lo largo de la
#    normal del plano y ninguna señal de varias fases temporales. Orden creciente a lo largo de la normal.
# 3. Si no, un desempate determinista (número de instancia si lo hay, posición si la hay, nombre interno) y un
#    aviso: ese orden NO es un orden anatómico verificado.
# El resultado es un índice de presentación. No se escribe en ningún atributo DICOM.

CRITERIO_NUMERO, CRITERIO_POSICION, CRITERIO_DESEMPATE = "InstanceNumber", "posición y orientación", "desempate determinista"
ETIQUETAS_ORDEN = ["InstanceNumber", "ImagePositionPatient", "ImageOrientationPatient", "TemporalPositionIdentifier",
                   "NumberOfTemporalPositions", "NumberOfTimeSlices", "Modality", "Rows", "Columns", "SeriesDescription",
                   "ProtocolName", "PixelSpacing", "SliceThickness", "PatientIdentityRemoved", "DeidentificationMethod",
                   "PatientSex", "PatientAge"]


def _entero_valido(v) -> int | None:
    try:
        t = str(v).strip()
        return int(t) if t != "" and v is not None else None
    except (TypeError, ValueError):
        return None


def datos_de_orden(ds) -> dict:
    """Lo que hace falta de una cabecera para ordenar su archivo dentro de la serie."""
    def flotantes(nombre: str, n: int):
        try:
            v = [float(x) for x in ds.get(nombre, [])]
            return v if len(v) == n and all(np.isfinite(v)) else None
        except (TypeError, ValueError):
            return None
    def mayor_que_uno(nombre: str) -> bool:
        n = _entero_valido(ds.get(nombre, None))
        return n is not None and n > 1
    return {"numero": _entero_valido(ds.get("InstanceNumber", None)), "ipp": flotantes("ImagePositionPatient", 3),
            "iop": flotantes("ImageOrientationPatient", 6), "fase": _entero_valido(ds.get("TemporalPositionIdentifier", None)),
            "varias_fases": mayor_que_uno("NumberOfTemporalPositions") or mayor_que_uno("NumberOfTimeSlices")}


def _proyecciones(items: list[dict]) -> list[float] | None:
    """Posición de cada corte a lo largo de la normal, si los archivos forman una pila coherente; si no, None."""
    if len(items) < 2 or any(i["ipp"] is None or i["iop"] is None for i in items):
        return None
    if any(i["varias_fases"] for i in items) or len({i["fase"] for i in items if i["fase"] is not None}) > 1:
        return None                                   # varias fases temporales: la posición sola no ordena
    iop0 = np.array(items[0]["iop"])
    if any(np.abs(np.array(i["iop"]) - iop0).max() > 1e-3 for i in items):
        return None                                   # más de una orientación
    normal = np.cross(iop0[:3], iop0[3:])
    if not np.isfinite(normal).all() or np.linalg.norm(normal) < 1e-6:
        return None
    proy = [float(np.dot(np.array(i["ipp"]), normal)) for i in items]
    orden = sorted(proy)
    if any(b - a < 1e-3 for a, b in zip(orden, orden[1:])):
        return None                                   # posiciones repetidas: no es una pila única
    return proy


def ordenar_serie(archivos: list[Path], cabeceras: dict | None = None) -> dict:
    """Ordena los archivos de UNA serie. Devuelve {'orden': [Path...], 'criterio': str, 'aviso': str | None}.
    El resultado no depende del orden de entrada."""
    items = []
    for a in sorted(archivos, key=lambda x: x.name):
        d = (cabeceras or {}).get(str(a))
        if d is None:
            try:
                d = datos_de_orden(pydicom.dcmread(a, stop_before_pixels=True, specific_tags=ETIQUETAS_ORDEN))
            except Exception:
                d = {"numero": None, "ipp": None, "iop": None, "fase": None, "varias_fases": False}
        items.append({**d, "ruta": a})
    numeros = [i["numero"] for i in items]
    if items and all(n is not None for n in numeros) and len(set(numeros)) == len(numeros):
        return {"orden": [i["ruta"] for i in sorted(items, key=lambda i: i["numero"])], "criterio": CRITERIO_NUMERO, "aviso": None}
    motivo = "InstanceNumber ausente en algún archivo" if any(n is None for n in numeros) else "InstanceNumber repetido"
    proy = _proyecciones(items)
    if proy is not None:
        pares = sorted(zip(proy, range(len(items))))
        return {"orden": [items[k]["ruta"] for _, k in pares], "criterio": CRITERIO_POSICION,
                "aviso": f"{motivo}; cortes ordenados por posición a lo largo de la normal del plano"}
    def clave(i: dict):
        return (i["numero"] is None, i["numero"] or 0, i["fase"] is None, i["fase"] or 0, i["ruta"].name)
    return {"orden": [i["ruta"] for i in sorted(items, key=clave)], "criterio": CRITERIO_DESEMPATE,
            "aviso": f"{motivo} y la posición no describe una pila única; el orden de los cortes es un desempate determinista, no un orden anatómico verificado"}


def mapa_exportacion(rels: list[str]) -> tuple[dict[str, str], dict[str, dict]]:
    """De rutas internas (relativas a limpios/, con '/') a rutas exportadas, serie por serie:
    PAC/estudio/serie/corte_000001_<nombre interno>.dcm. La numeración es independiente en cada serie y se calcula
    solo sobre los archivos recibidos. Devuelve también el criterio y el aviso de cada serie."""
    limpios = (TRABAJO / "limpios").resolve()
    por_serie: dict[str, list[str]] = {}
    for rel in rels:
        por_serie.setdefault(rel.rsplit("/", 1)[0], []).append(rel)
    mapa, info = {}, {}
    for serie in sorted(por_serie):
        o = ordenar_serie([limpios / r for r in por_serie[serie]])
        ancho = max(6, len(str(len(o["orden"]))))
        for k, a in enumerate(o["orden"], 1):
            mapa[f"{serie}/{a.name}"] = f"{serie}/corte_{k:0{ancho}d}_{a.stem}.dcm"
        info[serie] = {"criterio": o["criterio"], "aviso": o["aviso"], "archivos": len(o["orden"])}
    return mapa, info


# --------------------------------------------------------------------------------------
# demografía por estudio: identificador anónimo, sexo registrado y edad en la fecha del estudio
# --------------------------------------------------------------------------------------
# La limpieza calcula la edad antes de anonimizar las fechas y deja, por cada archivo limpio, lo que su original decía
# de la edad y el sexo (limpios_PRIVADO/demografia.json: valores derivados, ninguna fecha). Aquí se resume por estudio,
# siempre sobre un conjunto exacto de archivos: toda la serie guardada para Estudios, los archivos de la corrida para
# el modo rápido, los archivos del paquete para el manifiesto.

def registro_demografia() -> dict:
    try:
        return json.loads((carpeta_privada() / "demografia.json").read_text(encoding="utf-8")).get("archivos", {})
    except (OSError, ValueError, AttributeError):
        return {}


def demografia_de(rels: list[str], de_los_limpios: dict[str, dict] | None = None, registro: dict | None = None) -> list[dict]:
    """Un resumen por estudio (paciente/estudio) de los archivos recibidos, y solo de ellos. Para los archivos sin
    registro (importados con una versión anterior) se usa lo que conserva el propio archivo limpio: el sexo y, si
    existe, la edad informada por el equipo; la edad calculada no se puede obtener sin volver a importar."""
    registro = registro_demografia() if registro is None else registro
    limpios = (TRABAJO / "limpios").resolve()
    por_estudio: dict[str, list[dict]] = {}
    for rel in rels:
        obs = registro.get(rel) or (de_los_limpios or {}).get(rel)
        if obs is None:
            try:
                obs = ANON.observacion_de_archivo_limpio(pydicom.dcmread(limpios / rel, stop_before_pixels=True, specific_tags=["PatientSex", "PatientAge"]))
            except Exception:
                obs = {"e": ["n", "archivo ilegible"], "i": None, "s": ""}
        por_estudio.setdefault("/".join(rel.split("/")[:2]), []).append(obs)
    out = []
    for clave in sorted(por_estudio):
        paciente, estudio = clave.split("/")
        resumen = ANON.resumen_demografia(por_estudio[clave])
        out.append({"paciente": paciente, "estudio": estudio, "etiqueta": ANON.etiqueta_demografia(paciente, resumen), **resumen})
    return out


def indexar() -> None:
    INDICE.clear()
    limpios = TRABAJO / "limpios"
    if not limpios.is_dir():
        return
    de_los_limpios: dict[str, dict] = {}          # ruta limpia → sexo y edad informada que conserva el propio archivo
    rels_por_estudio: dict[str, list[str]] = {}
    limpios_abs = limpios.resolve()
    for carpeta_serie in sorted(p for p in limpios.glob("*/*/*") if p.is_dir() and not p.is_symlink()):
        if not PACIENTE_RE.match(carpeta_serie.parts[-3]):
            continue
        archivos = sorted(p for p in carpeta_serie.iterdir() if p.suffix.lower() == ".dcm" and not p.is_symlink())
        if not archivos:
            continue
        cabeceras, legibles, meta = {}, [], {}
        for a in archivos:
            try:
                ds = pydicom.dcmread(a, stop_before_pixels=True, specific_tags=ETIQUETAS_ORDEN)
            except Exception:
                continue
            cabeceras[str(a)] = datos_de_orden(ds)
            legibles.append(a)
            rel_a = a.resolve().relative_to(limpios_abs).as_posix()
            de_los_limpios[rel_a] = ANON.observacion_de_archivo_limpio(ds)
            rels_por_estudio.setdefault("/".join(rel_a.split("/")[:2]), []).append(rel_a)
            if not meta:
                meta = {"modalidad": str(ds.get("Modality", "")), "filas": int(ds.get("Rows", 0) or 0),
                        "descripcion": str(ds.get("SeriesDescription", "")), "protocolo": str(ds.get("ProtocolName", "")),
                        "columnas": int(ds.get("Columns", 0) or 0),
                        "espaciado": [float(x) for x in ds.get("PixelSpacing", [])] if "PixelSpacing" in ds else None,
                        "grosor": float(ds.SliceThickness) if "SliceThickness" in ds and str(ds.SliceThickness) else None,
                        "identidad_removida": str(ds.get("PatientIdentityRemoved", "")),
                        "politica": str(ds.get("DeidentificationMethod", "")).split(";")[0]}
        if not legibles:
            continue
        o = ordenar_serie(legibles, cabeceras)                       # el mismo orden que usará la exportación
        cortes = [(k, str(a)) for k, a in enumerate(o["orden"], 1)]
        meta["orden_criterio"], meta["orden_aviso"] = o["criterio"], o["aviso"]
        rel = carpeta_serie.relative_to(limpios)
        sid = hashlib.sha1(str(rel).encode()).hexdigest()[:12]
        INDICE[sid] = {"id": sid, "paciente": rel.parts[0], "estudio": rel.parts[1], "serie": rel.parts[2],
                       "n": len(cortes), "cortes": cortes, **meta}
    # demografía de cada estudio guardado, sobre todos sus archivos; cada serie lleva la de su estudio
    registro = registro_demografia()
    por_estudio = {f"{d['paciente']}/{d['estudio']}": d for rels in rels_por_estudio.values() for d in demografia_de(rels, de_los_limpios, registro)}
    for s_ in INDICE.values():
        d = por_estudio.get(f"{s_['paciente']}/{s_['estudio']}")
        s_["demografia"], s_["etiqueta"] = d, (d or {}).get("etiqueta", s_["paciente"])
    registrar(f"índice: {len(INDICE)} series")


def render_corte(ruta: str, ventana: str) -> bytes:
    """PNG de un corte leído de disco. Para mostrar bytes ya verificados, usar render_bytes() con esos mismos bytes."""
    return render_bytes(Path(ruta).read_bytes(), ventana)


def render_bytes(datos: bytes, ventana: str) -> bytes:
    """PNG de un corte a partir de los bytes exactos de su archivo DICOM. No abre ninguna ruta."""
    ds = pydicom.dcmread(io.BytesIO(datos))
    arr = ds.pixel_array.astype(np.float32)
    arr = arr * float(ds.get("RescaleSlope", 1) or 1) + float(ds.get("RescaleIntercept", 0) or 0)
    if ventana == "auto" or str(ds.get("Modality", "")) == "PT":
        lo, hi = np.percentile(arr, 0.5), np.percentile(arr, 99.5)
    else:
        c, w = VENTANAS.get(ventana, VENTANAS["tejido"])
        lo, hi = c - w / 2, c + w / 2
    if hi <= lo:
        hi = lo + 1
    img = np.clip((arr - lo) / (hi - lo), 0, 1) * 255
    if str(ds.get("PhotometricInterpretation", "")) == "MONOCHROME1":
        img = 255 - img
    buf = io.BytesIO()
    Image.fromarray(img.astype(np.uint8)).save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------------------

def auth(request: Request) -> None:
    host = request.headers.get("host", "").split(":")[0]
    if host not in ("127.0.0.1", "localhost"):
        raise HTTPException(403, "solo acceso local / local access only")
    token = request.headers.get("x-token") or request.query_params.get("t")
    if not token or not secrets.compare_digest(token, TOKEN):
        raise HTTPException(401, "token inválido / invalid token")


api = APIRouter(prefix="/api")


@api.get("/estado")
def estado(request: Request):
    auth(request)
    rep = carpeta_reportes() / "ultimo_reporte.json"
    reporte = {}
    try:
        r = json.loads(rep.read_text(encoding="utf-8"))
        reporte = {k: r.get(k) for k in ("fecha_ejecucion", "archivos_escritos", "duplicados_omitidos", "pacientes",
                                         "cuarentena", "cuantificacion_pet", "problemas_restantes", "politica")}
        reporte["cuarentena"] = len(reporte.get("cuarentena") or [])
    except (OSError, ValueError):
        pass
    return {"version": VERSION, "trabajo": str(TRABAJO), "sincronizada": en_sincronizada(TRABAJO),
            "series": len(INDICE), "pacientes": len({s["paciente"] for s in INDICE.values()}),
            "anonimizar_presente": CONGELADO or ANONIMIZAR.exists(), "prefijo": prefijo_actual(), "ultimo_reporte": reporte,
            "congelado": CONGELADO}


@api.post("/elegir-carpeta")
def elegir_carpeta(request: Request, cuerpo: dict = Body(default={})):
    auth(request)
    ruta = dialogo_carpeta("destino" if str((cuerpo or {}).get("para", "")) == "destino" else "origen")
    if not ruta:
        return {"ruta": None, "mensaje": "No se pudo abrir el selector; escribe la ruta a mano"}
    return {"ruta": ruta, "sincronizada": en_sincronizada(Path(ruta))}


@api.post("/anonimizar")
def anonimizar(request: Request, cuerpo: dict):
    auth(request)
    origen = Path(str(cuerpo.get("origen", ""))).expanduser()
    if not CONGELADO and not ANONIMIZAR.exists():
        raise HTTPException(500, "falta anonimizar.py junto a app.py")
    validar_origen(origen)
    prefijo = str(cuerpo.get("prefijo", "")).strip() or prefijo_actual()
    if not re.fullmatch(r"[A-Z0-9]{2,12}", prefijo):
        raise HTTPException(400, "prefijo: de 2 a 12 letras mayúsculas o dígitos")
    comando = comando_herramienta("anonimizar") + ["limpiar", str(origen), str(TRABAJO / "limpios"),
               "--mapa", str(carpeta_privada() / "mapa_seudonimos.json"), "--prefijo", prefijo]
    if cuerpo.get("conservar_descripciones"):
        comando.append("--conservar-descripciones")
    return {"trabajo": correr_en_segundo_plano("anonimizar", comando)}


@api.post("/demo")
def crear_demo(request: Request):
    """Crea un caso PET/CT sintético en la carpeta de trabajo para probar todo el flujo sin datos de pacientes."""
    auth(request)
    destino = TRABAJO / "demo-sintetico" / f"caso_{dt.datetime.now():%Y%m%d_%H%M%S}"
    codigo, salida = ejecutar(comando_herramienta("demo") + [str(destino)])
    if codigo != 0:
        raise HTTPException(500, salida.strip()[-300:])
    registrar(f"demo: caso sintético creado en {destino.name}")
    return {"ruta": str(destino), "salida": salida.strip()}


@api.get("/trabajo/{tid}")
def trabajo(request: Request, tid: str):
    auth(request)
    t = TRABAJOS.get(tid)
    if not t:
        raise HTTPException(404, "trabajo desconocido")
    if t["estado"] != "corriendo" and t["nombre"] == "anonimizar" and not t.get("indexado"):
        indexar()
        t["indexado"] = True
        rep = carpeta_reportes() / "ultimo_reporte.md"
        t["reporte"] = rep.read_text(encoding="utf-8") if rep.exists() else ""
    return {k: v for k, v in t.items() if k not in ("indexado", "archivos", "exportacion", "series_corrida")}


@api.get("/estudios")
def estudios(request: Request, refrescar: int = 0):
    auth(request)
    if refrescar or not INDICE:
        indexar()
    return [{k: v for k, v in s.items() if k != "cortes"} for s in INDICE.values()]


@api.get("/serie/{sid}/corte/{i}")
def corte(request: Request, sid: str, i: int, ventana: str = "tejido"):
    auth(request)
    s = INDICE.get(sid)
    if not s:
        raise HTTPException(404, "serie desconocida")
    i = max(0, min(i, s["n"] - 1))
    try:
        png = render_corte(s["cortes"][i][1], ventana)
    except Exception as e:
        raise HTTPException(500, f"no se pudo renderizar el corte: {type(e).__name__}")
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


def verificar_pacientes(pacientes: list[str]) -> tuple[dict, dict]:
    prefijo = prefijo_actual()
    resultados, fallos = {}, {}
    for pac in pacientes:
        carpeta = carpeta_paciente(pac)
        r = ANON.verificar_carpeta(carpeta, prefijo)
        salida = f"archivos revisados: {r['archivos_revisados']}, limpios: {r['limpios']}, con problemas: {r['con_problemas']}\n"
        salida += "".join(f"  {ruta}\n" + "".join(f"    - {p}\n" for p in ps) for ruta, ps in list(r["fallos"].items())[:20])
        codigo = 1 if r["fallos"] else (0 if r["archivos_revisados"] else 2)
        resultados[pac] = {"codigo": codigo, "salida": salida}
        if codigo != 0:
            fallos[pac] = salida
    return resultados, fallos


@api.post("/verificar")
def verificar(request: Request, cuerpo: dict):
    auth(request)
    pacientes = [str(p) for p in cuerpo.get("pacientes", [])]
    if not pacientes:
        raise HTTPException(400, "elige al menos un paciente / pick at least one patient")
    resultados, _ = verificar_pacientes(pacientes)
    registrar("verificar: " + ", ".join(f"{p}={r['codigo']}" for p, r in resultados.items()))
    return resultados


def _escribir_json(ruta: Path, datos: dict) -> None:
    """Escritura de un registro JSON en disco; un único punto para todas las evidencias (manifiestos y revisiones)."""
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")


def _eliminar(ruta: Path) -> bool:
    """Borra un archivo; devuelve False si sigue existiendo."""
    try:
        Path(ruta).unlink(missing_ok=True)
    except OSError:
        pass
    return not Path(ruta).exists()


SUFIJO_PREPARADO = ".preparada"   # evidencia escrita antes de publicar; sin confirmar no acredita ninguna publicación


class PublicacionFallida(Exception):
    """El zip no se publicó. `restos` son los archivos intermedios que no se pudieron retirar (rutas completas)."""

    def __init__(self, codigo: int, mensaje: str, restos: list[str] | None = None):
        super().__init__(mensaje)
        self.codigo, self.mensaje, self.restos = codigo, mensaje, list(restos or [])


def texto_restos(restos: list[str]) -> str:
    if not restos:
        return ""
    return (" No se pudieron retirar estos archivos intermedios; bórralos a mano: " + ", ".join(restos) +
            f". Los que terminan en {SUFIJO_PREPARADO} son evidencia preparada de una publicación que no ocurrió, no un registro de revisión.")


class PaqueteRechazado(Exception):
    """El paquete no se creó; lleva el código HTTP y el cuerpo que explican por qué."""

    def __init__(self, codigo: int, cuerpo: dict):
        super().__init__(str(cuerpo))
        self.codigo, self.cuerpo = codigo, cuerpo


def crear_paquete(pacientes: list[str], destino_dir: Path | None = None, limpieza_en_curso_permitida: bool = False,
                  solo: set[str] | None = None, revision_corrida: dict | None = None,
                  hashes_esperados: dict[str, str] | None = None, antes_de_publicar=None,
                  exportacion: dict[str, str] | None = None, orden_series: dict[str, dict] | None = None,
                  demografia: list[dict] | None = None) -> dict:
    """Verifica, hashea y empaqueta a partir de los mismos bytes; escribe el zip en destino_dir (por defecto, paquetes/).
    Con `solo` (rutas relativas a limpios/, con '/') entra exactamente ese conjunto de archivos y ninguno más.
    Con `revision_corrida` el manifiesto lleva el estado de revisión de esa corrida, no el del paciente completo.
    Con `hashes_esperados` cada archivo se compara, sobre los mismos bytes que se van a empaquetar, con el hash
    congelado; cualquier diferencia rechaza el paquete ANTES de escribir nada en ninguna parte.

    Nombres: los archivos limpios conservan su nombre interno; en el zip y en su manifiesto cada uno aparece con su
    ruta exportada, `PAC/estudio/serie/corte_000001_<nombre interno>.dcm`, según `exportacion` (ruta interna → ruta
    exportada). Si no se pasa, se calcula aquí con mapa_exportacion() sobre los archivos que entran. Los bytes no cambian.

    Orden de efectos, con la publicación como punto de compromiso:
      1. leer, verificar y hashear cada archivo (nada escrito);
      2. zip temporal en la carpeta de trabajo local, releído y comprobado;
      3. evidencias locales: el manifiesto externo junto al temporal y, si se pasa, `antes_de_publicar(info)`
         (por ejemplo, el registro de la revisión). Si cualquiera falla, nada se publica;
      4. PUBLICACIÓN: el zip aparece con su nombre final en el destino (renombrado atómico);
      5. limpieza posterior: lo que falle aquí son avisos en el resultado, nunca un error, y la ruta publicada se conserva."""
    carpetas = {pac: carpeta_paciente(pac) for pac in pacientes}   # valida cada identificador antes de tocar nada
    if not limpieza_en_curso_permitida and (TRABAJO / "limpios" / ".corriendo").exists():
        raise PaqueteRechazado(409, {"detail": "hay una limpieza en curso; espera a que termine / a cleaning run is in progress"})

    # 1. cada archivo se lee UNA vez: esos bytes se verifican, se hashean y se escriben al zip
    #    each file is read ONCE: those same bytes are verified, hashed and written to the zip
    prefijo = prefijo_actual()
    limpios = (TRABAJO / "limpios").resolve()
    entradas: list[tuple[str, bytes, str]] = []
    fallos: dict[str, list[str]] = {}
    for pac, carpeta in carpetas.items():
        for raiz, dirs, nombres in os.walk(carpeta, followlinks=False):
            for d in list(dirs):
                if os.path.islink(os.path.join(raiz, d)):
                    fallos[f"{pac}/{d}"] = ["enlace simbólico / symlink"]
                    dirs.remove(d)
            for n in sorted(nombres):
                a = Path(raiz) / n
                rel = a.relative_to(limpios).as_posix()   # el zip y el manifiesto usan '/', también en Windows
                if solo is not None and rel not in solo:
                    continue                              # fuera del conjunto exacto de esta corrida
                if a.is_symlink():
                    fallos[rel] = ["enlace simbólico / symlink"]
                    continue
                if a.suffix.lower() != ".dcm":
                    fallos[rel] = ["archivo inesperado, solo se empaquetan DICOM limpios / unexpected file"]
                    continue
                datos = a.read_bytes()
                problemas, _ = ANON.verificar_bytes(datos, prefijo)
                if problemas:
                    fallos[rel] = problemas
                    continue
                h = hashlib.sha256(datos).hexdigest()
                if hashes_esperados is not None and hashes_esperados.get(rel) != h:
                    fallos[rel] = ["el archivo cambió desde la corrida que se revisó / changed since the reviewed run"]
                    continue
                entradas.append((rel, datos, h))
    if fallos:
        registrar(f"empaquetar: rechazado, {len(fallos)} archivo(s) con problemas en {', '.join(pacientes)}")
        raise PaqueteRechazado(409, {"error": "la compuerta falló; nada se empaquetó", "fallos": fallos})
    if not entradas:
        raise PaqueteRechazado(409, {"detail": "no hay archivos DICOM limpios en la selección / nothing to package"})
    if solo is not None and {rel for rel, _, _ in entradas} != set(solo):
        faltan = sorted(set(solo) - {rel for rel, _, _ in entradas})
        raise PaqueteRechazado(409, {"error": "faltan archivos del conjunto de la corrida; nada se empaquetó", "fallos": {f: ["ausente"] for f in faltan[:50]}})

    # nombres exportados: correspondencia explícita, comprobada antes de escribir nada
    internos = [rel for rel, _, _ in entradas]
    if exportacion is None:
        exportacion, orden_series = mapa_exportacion(internos)
    else:
        exportacion = dict(exportacion)                    # correspondencia fijada por quien llama (una corrida del modo rápido)
        orden_series = dict(orden_series) if orden_series else None
    exportados = [exportacion.get(rel) for rel in internos]
    if set(exportacion) != set(internos) or None in exportados or len(set(exportados)) != len(exportados) \
            or any(not e.endswith(".dcm") or e.split("/")[:-1] != rel.split("/")[:-1] for rel, e in zip(internos, exportados)):
        raise PaqueteRechazado(409, {"detail": "la correspondencia de nombres exportados no coincide con los archivos del paquete; nada se empaquetó"})
    if orden_series is None:
        orden_series = {}
        for rel in internos:
            orden_series.setdefault(rel.rsplit("/", 1)[0], None)
    entradas = sorted(((exportacion[rel], datos, h, rel) for rel, datos, h in entradas), key=lambda e: e[0])

    # demografía por estudio: solo de los pacientes y estudios presentes en el paquete. Si viene fijada por una corrida,
    # se usa tal cual; si no, se calcula ahora sobre los archivos que entran.
    demografia = copy.deepcopy(demografia) if demografia is not None else demografia_de(internos)
    if {(d["paciente"], d["estudio"]) for d in demografia} != {tuple(rel.split("/")[:2]) for rel in internos}:
        raise PaqueteRechazado(409, {"detail": "la demografía no corresponde a los estudios del paquete; nada se empaquetó"})

    try:
        politica = json.loads((limpios / ".politica.json").read_text(encoding="utf-8")).get("politica", "")
    except (OSError, ValueError):
        politica = ""
    if revision_corrida is not None:
        # modo rápido: el estado de revisión es el de ESTA corrida y ESTOS archivos, nunca el del paciente completo
        pendientes = [] if revision_corrida.get("estado") == "sin_hallazgos" else list(pacientes)
        bloque_revision = {"alcance": "los archivos de este paquete (corrida del modo rápido)", **revision_corrida}
        aviso_manifiesto = ("revisión de píxeles sin hallazgos en los archivos de este paquete" if not pendientes
                            else "paquete creado localmente; la revisión de píxeles de los archivos de este paquete está pendiente")
    else:
        revisiones = {pac: leer_revision(pac) for pac in pacientes}
        pendientes = [pac for pac, r in revisiones.items() if not r.get("lista_para_compartir")]
        bloque_revision = {pac: {k: r.get(k) for k in ("estado", "vigente", "fecha", "hallazgos", "detalle")} for pac, r in revisiones.items()}
        aviso_manifiesto = ("paquete creado localmente; la revisión de píxeles está pendiente para los pacientes listados" if pendientes
                            else "revisión de píxeles vigente y sin hallazgos en todos los pacientes")
    manifiesto = {"version_app": VERSION, "politica": politica, "prefijo": prefijo,
                  "creado": dt.datetime.now().isoformat(timespec="seconds"), "pacientes": pacientes,
                  "revision_pixeles": bloque_revision,
                  "revision_pixeles_pendiente": pendientes,
                  "aviso": aviso_manifiesto,
                  "archivos": [{"ruta": exp, "sha256": h, "bytes": len(datos)} for exp, datos, h, _ in entradas],
                  "nombres": "corte_NNNNNN es un índice de presentación por serie; no modifica InstanceNumber ni ningún otro atributo DICOM",
                  "demografia": {"nota": "Por estudio: identificador anónimo, sexo registrado en el DICOM (no se infiere) y edad en la fecha del "
                                         "estudio, calculada antes de anonimizar las fechas. No se exportan la fecha de nacimiento ni la fecha "
                                         "original del estudio. 90 años o más se agrupan, como en PatientAge.",
                                 "estudios": demografia},
                  "orden_de_cortes": {serie: ({"criterio": o["criterio"], "aviso": o["aviso"]} if o else {"criterio": "fijado en la corrida"})
                                      for serie, o in sorted(orden_series.items())},
                  "verificacion": "cada archivo verificado a partir de los mismos bytes que se empaquetaron"}

    # 2. zip temporal en la carpeta de trabajo local, desde esos bytes; relectura y comparación de hashes
    destino_dir = Path(destino_dir) if destino_dir else TRABAJO / "paquetes"
    local_dir = TRABAJO / "paquetes"
    local_dir.mkdir(parents=True, exist_ok=True)
    nombre = f"paquete_{dt.datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(3)}.zip"
    final = destino_dir / nombre
    manifiesto_local = local_dir / (nombre[:-4] + ".manifiesto.json")
    manifiesto_preparado = manifiesto_local.with_name(manifiesto_local.name + SUFIJO_PREPARADO)
    fd, tmp = tempfile.mkstemp(dir=local_dir, prefix=".tmp_", suffix=".zip")
    os.close(fd)
    tmp = Path(tmp)
    parcial = None
    es_local = destino_dir.resolve() == local_dir.resolve()
    etapa = "la comprobación del paquete"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for exp, datos, _, _ in entradas:
                z.writestr(exp, datos)
            z.writestr("manifiesto.json", json.dumps(manifiesto, ensure_ascii=False, indent=2))
        with zipfile.ZipFile(tmp) as z:
            nombres = z.namelist()
            if sorted(nombres) != sorted([exp for exp, _, _, _ in entradas] + ["manifiesto.json"]):
                raise RuntimeError("entradas del zip")
            for exp, _, h, _ in entradas:
                if hashlib.sha256(z.read(exp)).hexdigest() != h:
                    raise RuntimeError(exp)
        huella = sha256_archivo(tmp)

        # 3. evidencias locales ANTES de publicar: manifiesto externo y lo que pida quien llama
        etapa = "el guardado del manifiesto local"
        _escribir_json(manifiesto_preparado, {**manifiesto, "zip": {"nombre": nombre, "destino": str(destino_dir), "sha256": huella},
                                              "correspondencia_interna": {exp: rel for exp, _, _, rel in entradas}})
        if antes_de_publicar is not None:
            etapa = "el guardado del registro previo a la publicación"
            antes_de_publicar({"nombre": nombre, "ruta": str(final), "sha256": huella, "manifiesto_local": str(manifiesto_local),
                               "hashes": {rel: h for _, _, h, rel in entradas}})

        # 4. PUBLICACIÓN (punto de compromiso): el zip aparece con su nombre final de una sola vez
        etapa = "la copia al destino"
        if es_local:
            os.replace(tmp, final)
        else:
            destino_dir.mkdir(parents=True, exist_ok=True)
            parcial = destino_dir / (".parcial_" + nombre)
            shutil.copyfile(tmp, parcial)
            if sha256_archivo(parcial) != huella:
                raise RuntimeError("copia al destino")
            os.replace(parcial, final)
    except Exception as e:
        # nada se publicó: se retira todo lo intermedio, y lo que no se pueda retirar se devuelve con su ruta
        restos = [str(resto) for resto in (tmp, parcial, manifiesto_preparado) if resto is not None and not _eliminar(resto)]
        registrar(f"empaquetar: falló {etapa} ({type(e).__name__}); nada publicado" + (f"; sin retirar: {', '.join(restos)}" if restos else ""))
        raise PaqueteRechazado(500, {"detail": f"falló {etapa}; el zip no se publicó / package not published", "restos": restos})

    # 5. después de publicar ya no hay errores, solo avisos: la ruta publicada se conserva pase lo que pase
    avisos = []
    try:
        os.replace(manifiesto_preparado, manifiesto_local)      # la evidencia preparada pasa a confirmada
    except OSError:
        manifiesto_local = manifiesto_preparado
        avisos.append(f"el manifiesto local quedó con su nombre provisional aunque el zip está publicado: {manifiesto_preparado}")
    if not es_local and not _eliminar(tmp):
        avisos.append(f"no se pudo eliminar el zip temporal de la carpeta de trabajo: {tmp}")
    registrar(f"empaquetar: {nombre} con {len(entradas)} archivos de {', '.join(pacientes)}; revisión de píxeles pendiente: {', '.join(pendientes) or 'ninguno'}"
              + (f"; avisos: {len(avisos)}" if avisos else ""))
    return {"ruta": str(final), "archivos": len(entradas), "pacientes": pacientes, "revision_pixeles_pendiente": pendientes,
            "hashes": {rel: h for _, _, h, rel in entradas}, "sha256": huella, "manifiesto_local": str(manifiesto_local), "avisos": avisos,
            "demografia": demografia, "exportacion": dict(exportacion), "orden_sin_verificar": sorted(serie for serie, o in orden_series.items() if o and o["criterio"] == CRITERIO_DESEMPATE)}


@api.post("/empaquetar")
def empaquetar(request: Request, cuerpo: dict):
    auth(request)
    pacientes = sorted({str(p) for p in cuerpo.get("pacientes", [])})
    if not pacientes:
        raise HTTPException(400, "elige al menos un paciente / pick at least one patient")
    try:
        r = crear_paquete(pacientes)
        for clave in ("hashes", "sha256", "exportacion", "demografia"):
            r.pop(clave, None)
        return r
    except PaqueteRechazado as e:
        if "detail" in e.cuerpo:
            restos = e.cuerpo.get("restos", [])
            return JSONResponse(status_code=e.codigo, content={"detail": e.cuerpo["detail"] + texto_restos(restos), "restos": restos})
        return JSONResponse(status_code=e.codigo, content=e.cuerpo)


# --------------------------------------------------------------------------------------
# modo rápido: una carpeta, de principio a fin / express mode
# --------------------------------------------------------------------------------------
# Reglas:
#  - El zip contiene exactamente las salidas limpias que la limpieza de ESTA corrida produjo o verificó (nuevas,
#    actualizadas y duplicados verificados), según la lista que escribe anonimizar.py. No se usa el índice histórico
#    por carpeta: lo que alguna vez salió de esa carpeta y ya no está en ella no entra.
#  - El zip de una corrida nace SIEMPRE con la revisión de píxeles pendiente, exista o no una revisión anterior
#    del paciente, y se queda en la carpeta de trabajo local. Solo tras registrar la revisión de esa corrida se
#    escribe en el destino elegido.
#  - La revisión se aplica al conjunto congelado de la corrida (rutas y hashes): si algo cambió, se rechaza.

PASOS_RAPIDO = [("limpieza", "Limpieza"), ("compuerta", "Compuerta de metadatos"), ("cotejo", "Cotejo con el original"),
                ("emparejamiento", "Emparejamiento CT + PET"), ("suv", "SUV original / limpio"), ("paquete", "Paquete")]


def validar_origen(origen: Path) -> None:
    if not origen.is_dir():
        raise HTTPException(400, "la carpeta de originales no existe / originals folder not found")
    marca = en_sincronizada(TRABAJO)
    if marca:
        raise HTTPException(400, f"la carpeta de trabajo está dentro de una carpeta sincronizada ({marca}); elige otra")
    o = origen.resolve()
    for prohibida in (TRABAJO / "limpios", carpeta_privada(), carpeta_reportes(), TRABAJO / "paquetes"):
        pr = prohibida.resolve()
        if o == pr or o.is_relative_to(pr) or pr.is_relative_to(o):
            raise HTTPException(400, "la carpeta de originales se solapa con la copia limpia, la privada, los reportes o los paquetes; elige otra")


def validar_destino(destino: Path, origen: Path) -> Path:
    """Destino del zip ya revisado: cualquier carpeta existente salvo las internas de la aplicación y la de originales."""
    por_defecto = (TRABAJO / "paquetes").resolve()
    d = destino.resolve()
    if d == por_defecto:
        d.mkdir(parents=True, exist_ok=True)
        return d
    if not d.is_dir():
        raise HTTPException(400, "la carpeta de destino no existe; elígela con el selector o créala antes")
    for prohibida, nombre in ((TRABAJO / "limpios", "la copia limpia"), (carpeta_privada(), "la carpeta privada"),
                              (carpeta_reportes(), "los reportes"), (origen.resolve(), "la carpeta de originales")):
        pr = Path(prohibida).resolve()
        if d == pr or d.is_relative_to(pr):
            raise HTTPException(400, f"el destino no puede estar dentro de {nombre}; elige otra carpeta")
    return d


def clasificar_suv(codigo: int, informe: dict, salida: str) -> tuple[str, str]:
    """(estado, etiqueta): distingue una discrepancia real de 'no se puede calcular' y de un problema de orden."""
    if codigo == 0 and informe.get("veredicto") == "IDENTICO":
        return "ok", "IDENTICO"
    if informe.get("voxeles_distintos", 0) or informe.get("factores_identicos") is False or informe.get("intervalos_identicos") is False:
        return "error", f"DISCREPANCIA: {informe.get('voxeles_distintos', 0)} vóxeles distintos entre original y limpio"
    textos = " ".join(informe.get("detalles", []) + informe.get("problemas_de_correspondencia", [])) + salida
    if "no calculable" in textos or "SUVbw no calculable" in textos:
        motivo = next((d.split("no calculable:")[-1].strip() for d in informe.get("detalles", []) if "no calculable" in d), "faltan atributos")
        return "aviso", f"no se puede calcular el SUV ({motivo}); no es una discrepancia"
    if informe.get("correspondencia_completa") is False:
        return "aviso", "correspondencia incompleta entre original y limpio: " + "; ".join(informe.get("problemas_de_correspondencia", []))[:120]
    if informe.get("orden_y_geometria_ok") is False:
        return "aviso", "orden o geometría incoherentes en la serie: " + "; ".join(informe.get("problemas_de_orden_y_geometria", []))[:120]
    return "aviso", "sin veredicto"


def _rapido(tid: str, origen: Path, destino: Path, prefijo: str) -> None:
    t = TRABAJOS[tid]
    pasos = {c: {"clave": c, "titulo": titulo, "estado": "pendiente", "resumen": "", "salida": ""} for c, titulo in PASOS_RAPIDO}
    t["pasos"] = [pasos[c] for c, _ in PASOS_RAPIDO]
    limpios = (TRABAJO / "limpios").resolve()

    def paso(clave: str, estado: str, resumen: str = "", salida: str = "") -> None:
        pasos[clave].update(estado=estado, resumen=resumen, salida=salida[-6000:])

    restos_temporales: list[str] = []

    def terminar(estado: str, mensaje: str) -> None:
        for c, _ in PASOS_RAPIDO:
            if pasos[c]["estado"] in ("pendiente", "corriendo"):
                pasos[c]["estado"] = "omitido"
        if restos_temporales:
            mensaje += " No se pudieron eliminar estas listas temporales de la carpeta privada; bórralas a mano: " + ", ".join(restos_temporales) + "."
            if estado == "ok":
                estado = "con avisos"
        t["estado"], t["mensaje"] = estado, mensaje
        registrar(f"rápido: {estado}; {mensaje}")

    try:
        # 1. limpieza
        paso("limpieza", "corriendo")
        carpeta_privada().mkdir(parents=True, exist_ok=True)
        lista = carpeta_privada() / f".lista_rapido_{tid}.json"
        try:
            codigo, salida = ejecutar(comando_herramienta("anonimizar") + ["limpiar", str(origen), str(TRABAJO / "limpios"),
                                      "--mapa", str(carpeta_privada() / "mapa_seudonimos.json"), "--prefijo", prefijo,
                                      "--lista-salida", str(lista)])
            try:
                datos_lista = json.loads(lista.read_text(encoding="utf-8"))
                rels = sorted(set(datos_lista["archivos"]))
                entradas_aceptadas = int(datos_lista.get("entradas_aceptadas", len(rels)))
            except (OSError, ValueError, KeyError):
                rels = None
        finally:
            if not _eliminar(lista):
                restos_temporales.append(str(lista))
        listo = next((l for l in salida.splitlines() if l.startswith("Listo")), "").replace(" (done)", "")
        if codigo not in (0, 1) or rels is None:
            paso("limpieza", "error", "la limpieza no terminó", salida)
            return terminar("error", "La limpieza falló; no se creó ningún zip.")
        paso("limpieza", "ok" if codigo == 0 else "aviso", listo or "limpieza terminada", salida)
        indexar()
        archivos = [limpios / r_ for r_ in rels]
        if not rels or not all(a.is_file() for a in archivos):
            return terminar("error", "Ningún archivo de esa carpeta quedó limpio (¿son CT, PET o MR?); no se creó ningún zip.")
        pacientes = sorted({r_.split("/")[0] for r_ in rels})
        t["pacientes"] = pacientes
        otros = {pac: len(hashes_paciente(pac)) - sum(1 for r_ in rels if r_.startswith(pac + "/")) for pac in pacientes}
        t["otros_archivos_del_paciente"] = {pac: n for pac, n in otros.items() if n > 0}

        # 2. compuerta: exactamente los archivos de esta corrida
        paso("compuerta", "corriendo")
        fallos = {}
        for a, rel in zip(archivos, rels):
            problemas, _ = ANON.verificar_bytes(a.read_bytes(), prefijo)
            if problemas:
                fallos[rel] = problemas
        if fallos:
            paso("compuerta", "error", f"{len(fallos)} archivo(s) con problemas en los metadatos",
                 "\n".join(f"{r_}: {'; '.join(ps)}" for r_, ps in list(fallos.items())[:30]))
            return terminar("error", "La compuerta de metadatos falló; no se creó ningún zip.")
        paso("compuerta", "ok", f"metadatos en regla en los {len(rels)} archivos de esta carpeta")

        # 3. cotejo, sobre el conjunto exacto de esta corrida: cada original presente debe llegar a uno de estos archivos,
        #    cada uno de estos archivos debe venir de un original presente y coincidir byte a byte. Los archivos guardados
        #    del paciente que no son de esta corrida (por ejemplo, cortes retirados de la carpeta) no se examinan aquí.
        paso("cotejo", "corriendo")
        lista_corrida = carpeta_privada() / f".lista_corrida_{tid}.json"     # la usan el cotejo, el emparejamiento y el SUV
        _escribir_json(lista_corrida, {"archivos": rels})
        codigo, salida = ejecutar(comando_herramienta("cotejar") + [str(origen), str(TRABAJO / "limpios"),
                                  "--mapa", str(carpeta_privada() / "mapa_seudonimos.json"), "--solo-lista", str(lista_corrida)])
        veredicto = next((l for l in salida.splitlines() if l.startswith("Veredicto")), "")
        iguales = next((l.strip() for l in salida.splitlines() if "coinciden byte a byte" in l), "")
        if codigo != 0:
            paso("cotejo", "error", veredicto or "el cotejo no terminó", salida)
            return terminar("error", "La copia limpia no coincide con la limpieza recalculada; no se creó ningún zip.")
        n_cotejados = int(m.group(1)) if (m := re.search(r"coinciden byte a byte:\s*(\d+)", salida)) else -1
        if n_cotejados != entradas_aceptadas:
            paso("cotejo", "error", f"el cotejo cubrió {n_cotejados} archivo(s) y la limpieza de esta corrida aceptó {entradas_aceptadas}", salida)
            return terminar("error", "El cotejo y la limpieza no cubren el mismo conjunto de archivos; no se creó ningún zip.")
        paso("cotejo", "ok", f"{veredicto}; {iguales}".strip("; "), salida)

        # 4. emparejamiento por estudio de esta corrida (informa; no bloquea)
        paso("emparejamiento", "corriendo")
        estudios_dir = sorted({a.parents[1] for a in archivos})
        textos, estados = [], []
        for d in estudios_dir:
            codigo, salida = ejecutar(comando_herramienta("emparejar") + [str(d), "--solo-lista", str(lista_corrida)])
            linea = next((l.strip() for l in salida.splitlines() if " + PET " in l), "")
            nombre = f"{d.parent.name}/{d.name}"
            if codigo == 0:
                textos.append(f"{nombre}: {linea or 'CANDIDATO COMPATIBLE'}"); estados.append("ok")
            elif codigo == 1:
                textos.append(f"{nombre}: PAR INCONSISTENTE: {linea or 'CT y PET no corresponden'}"); estados.append("aviso")
            else:
                textos.append(f"{nombre}: no evaluable (falta CT o PET, o información insuficiente)"); estados.append("omitido")
        peor = "aviso" if "aviso" in estados else ("ok" if "ok" in estados else "omitido")
        paso("emparejamiento", peor, "; ".join(textos)[:300], "\n".join(textos))

        # 5. SUV original / limpio por serie PET de esta corrida
        paso("suv", "corriendo")
        series_dir = {str(a.parent.resolve()) for a in archivos}
        series_pt = [s_ for s_ in INDICE.values() if s_["modalidad"] == "PT" and str(Path(s_["cortes"][0][1]).parent.resolve()) in series_dir]
        if not series_pt:
            paso("suv", "omitido", "no hay series PET en esta carpeta")
        else:
            textos, estados = [], []
            for s_ in series_pt:
                fd, tmpj = tempfile.mkstemp(suffix=".json")
                os.close(fd)
                try:
                    codigo, salida = ejecutar(comando_herramienta("suv") + ["comparar", str(Path(s_["cortes"][0][1]).parent),
                                              "--privado", str(carpeta_privada()), "--json", tmpj, "--solo-lista", str(lista_corrida)])
                    try:
                        informe = json.loads(Path(tmpj).read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        informe = {}
                finally:
                    Path(tmpj).unlink(missing_ok=True)
                estado_s, etiqueta = clasificar_suv(codigo, informe, salida)
                textos.append(f"{s_['paciente']} {s_['serie']}: {etiqueta}"); estados.append(estado_s)
            if "error" in estados:
                paso("suv", "error", "; ".join(textos)[:300], "\n".join(textos))
                return terminar("error", "El SUV calculado sobre la copia limpia difiere del original; no se creó ningún zip.")
            paso("suv", "aviso" if "aviso" in estados else "ok", "; ".join(textos)[:300], "\n".join(textos))

        # 6. paquete: exactamente estos archivos, con la revisión pendiente, en la carpeta de trabajo local.
        #    La correspondencia de nombres exportados se calcula UNA vez, sobre los archivos de esta corrida, y se congela:
        #    el zip pendiente y el revisado llevan los mismos nombres aunque después se importe más de este paciente.
        paso("paquete", "corriendo")
        exportacion, orden_series = mapa_exportacion(rels)
        t["exportacion"], t["orden_de_cortes"] = exportacion, orden_series
        t["series_corrida"] = series_congeladas(exportacion, orden_series)
        # La demografía de la corrida se calcula una vez, sobre sus archivos exactos, y se congela: el zip pendiente, el
        # revisado y el visor de la corrida muestran lo mismo aunque después se importe más de este paciente.
        t["demografia"] = demografia_de(rels)
        try:
            r = crear_paquete(pacientes, TRABAJO / "paquetes", solo=set(rels), exportacion=exportacion, orden_series=orden_series,
                              demografia=t["demografia"], revision_corrida={"estado": "pendiente", "detalle": "revisión de píxeles de esta corrida sin registrar"})
        except PaqueteRechazado as e:
            paso("paquete", "error", "el paquete no se creó", json.dumps(e.cuerpo, ensure_ascii=False)[:3000])
            return terminar("error", "El paquete no se creó." + texto_restos(e.cuerpo.get("restos", [])))
        t["zip"], t["archivos"] = r["ruta"], r["hashes"]
        t["revision_pixeles_pendiente"] = r["revision_pixeles_pendiente"]
        t["destino_sincronizado"] = en_sincronizada(destino)
        extra = "".join(f" {pac} tiene además {n} archivo(s) de importaciones anteriores que no entran en este zip." for pac, n in t["otros_archivos_del_paciente"].items())
        sin_verificar = sorted(serie for serie, o in orden_series.items() if o["criterio"] == CRITERIO_DESEMPATE)
        por_posicion = sorted(serie for serie, o in orden_series.items() if o["criterio"] == CRITERIO_POSICION)
        nota_orden = (f" Orden de cortes NO verificado en {len(sin_verificar)} serie(s): {', '.join(x.split('/')[-1] for x in sin_verificar)}." if sin_verificar else "") + \
                     (f" {len(por_posicion)} serie(s) ordenadas por posición, sin InstanceNumber utilizable." if por_posicion else "")
        paso("paquete", "aviso" if sin_verificar else "ok",
             f"{r['archivos']} archivos de esta carpeta ({', '.join(pacientes)}), en la carpeta de trabajo, numerados por serie.{extra}{nota_orden}",
             r["ruta"] + "\n" + "\n".join(f"{serie}: {o['criterio']}" + (f" ({o['aviso']})" if o["aviso"] else "") for serie, o in sorted(orden_series.items())))
        hay_avisos = any(p_["estado"] == "aviso" for p_ in t["pasos"])
        terminar("con avisos" if hay_avisos else "ok",
                 "Zip creado en la carpeta de trabajo con la revisión de píxeles pendiente. Pasará al destino elegido cuando registres la revisión.")
    except Exception as e:   # nada debe dejar el trabajo colgado
        terminar("error", f"Error inesperado ({type(e).__name__}); no se creó ningún zip.")
    finally:
        sobrante = carpeta_privada() / f".lista_corrida_{tid}.json"
        if not _eliminar(sobrante):
            t["mensaje"] = (t.get("mensaje") or "") + f" No se pudo eliminar una lista temporal de la carpeta privada; bórrala a mano: {sobrante}."
            if t.get("estado") == "ok":
                t["estado"] = "con avisos"
        CANDADO.release()


def series_congeladas(exportacion: dict[str, str], orden_series: dict[str, dict]) -> list[dict]:
    """Las series de una corrida con sus archivos en el orden del paquete: el de sus nombres exportados."""
    por_serie: dict[str, list[tuple[str, str]]] = {}
    for interno, exportado in exportacion.items():
        por_serie.setdefault(exportado.rsplit("/", 1)[0], []).append((exportado, interno))
    out = []
    for serie in sorted(por_serie):
        pares = sorted(por_serie[serie])
        partes = serie.rsplit("/", 1)[1].split("_")
        o = orden_series.get(serie) or {}
        out.append({"serie": serie, "modalidad": partes[2] if len(partes) >= 4 else "", "n": len(pares),
                    "criterio": o.get("criterio"), "aviso": o.get("aviso"),
                    "archivos": [interno for _, interno in pares], "nombres": [exportado.rsplit("/", 1)[1] for exportado, _ in pares]})
    return out


def corrida_con_zip(tid: str) -> dict:
    t = TRABAJOS.get(tid)
    if not t or t.get("nombre") != "rapido" or t.get("estado") == "corriendo" or not t.get("zip"):
        raise HTTPException(404, "no hay una corrida del modo rápido con paquete para ese identificador")
    return t


def png_de_corrida(t: dict, indice: int, k: int, ventana: str) -> bytes:
    """El corte k (desde 1) de la serie `indice` de la corrida, en el orden congelado del paquete. Solo se muestra si el
    archivo sigue siendo, byte a byte, el que entró en el paquete."""
    series = t.get("series_corrida") or []
    if not 0 <= indice < len(series) or not 1 <= k <= series[indice]["n"]:
        raise HTTPException(404, "corte fuera de rango")
    rel = series[indice]["archivos"][k - 1]
    ruta = (TRABAJO / "limpios").resolve() / rel
    # Una sola lectura: el hash se calcula sobre estos bytes y la imagen se dibuja desde estos mismos bytes. La ruta no
    # se vuelve a abrir, así que un cambio en disco después de esta línea no puede llegar a la pantalla.
    try:
        datos = ruta.read_bytes()
    except OSError:
        raise HTTPException(409, "este archivo cambió desde la corrida o ya no está; vuelve a ejecutar el modo rápido")
    if hashlib.sha256(datos).hexdigest() != t["archivos"].get(rel):
        raise HTTPException(409, "este archivo cambió desde la corrida; vuelve a ejecutar el modo rápido")
    return render_bytes(datos, ventana if ventana in VENTANAS or ventana == "auto" else "tejido")


@api.get("/rapido/{tid}/series")
def rapido_series(request: Request, tid: str):
    """Las series de la corrida y los nombres exportados de sus archivos, en el orden del paquete."""
    auth(request)
    t = corrida_con_zip(tid)
    por_estudio = {f"{d['paciente']}/{d['estudio']}": d for d in t.get("demografia", [])}
    return [{"indice": i, **{k: v for k, v in s_.items() if k != "archivos"}, "demografia": por_estudio.get(s_["serie"].rsplit("/", 1)[0])}
            for i, s_ in enumerate(t["series_corrida"])]


@api.get("/rapido/{tid}/corte/{indice}/{k}")
def rapido_corte(request: Request, tid: str, indice: int, k: int, ventana: str = "tejido"):
    auth(request)
    return Response(png_de_corrida(corrida_con_zip(tid), indice, k, ventana), media_type="image/png", headers={"Cache-Control": "no-store"})


@api.post("/rapido")
def rapido(request: Request, cuerpo: dict):
    """Una carpeta, de principio a fin: limpiar, verificar, cotejar, emparejar, comparar SUV y empaquetar."""
    auth(request)
    origen = Path(str(cuerpo.get("origen", ""))).expanduser()
    validar_origen(origen)
    destino_txt = str(cuerpo.get("destino", "")).strip()
    destino = validar_destino(Path(destino_txt).expanduser() if destino_txt else TRABAJO / "paquetes", origen)
    prefijo = prefijo_actual()
    if not CANDADO.acquire(blocking=False):
        raise HTTPException(409, "ya hay un trabajo en curso / a job is already running")
    tid = secrets.token_hex(4)
    TRABAJOS[tid] = {"nombre": "rapido", "estado": "corriendo", "lineas": [], "codigo": None, "pasos": [], "pacientes": [],
                     "origen": str(origen), "destino": str(destino), "zip": None, "mensaje": "", "archivos": {}, "revisado": False,
                     "exportacion": {}, "orden_de_cortes": {}, "series_corrida": [], "demografia": []}
    threading.Thread(target=_rapido, args=(tid, origen, destino, prefijo), daemon=True).start()
    registrar("rápido: iniciado")
    return {"trabajo": tid, "destino": str(destino)}


def aplicar_revision_rapido(t: dict, revisor: str) -> dict:
    """Registra la revisión sin hallazgos del conjunto congelado de una corrida y lleva el zip revisado al destino.
    Orden: candado; comprobación previa por hash; empaquetado que vuelve a comparar los hashes congelados con los
    bytes que empaqueta, antes de escribir nada; solo un zip ya comprobado se copia al destino."""
    if not t or t.get("nombre") != "rapido" or t.get("estado") == "corriendo" or not t.get("zip") or not t.get("archivos"):
        raise HTTPException(400, "no hay un zip del modo rápido al que aplicar la revisión")
    if t.get("revisado"):
        raise HTTPException(409, "la revisión de esta corrida ya está registrada")
    error = validar_registro_revision("sin_hallazgos", [], revisor)
    if error:
        raise HTTPException(400, error)
    if not CANDADO.acquire(blocking=False):
        raise HTTPException(409, "hay otro trabajo en curso; espera a que termine antes de registrar la revisión")
    try:
        limpios = (TRABAJO / "limpios").resolve()
        cambiados = [rel for rel, h in t["archivos"].items()
                     if not (limpios / rel).is_file() or sha256_archivo(limpios / rel) != h]
        if cambiados:
            registrar(f"rápido: revisión rechazada, {len(cambiados)} archivo(s) cambiaron desde la corrida")
            raise HTTPException(409, f"{len(cambiados)} archivo(s) de esta corrida cambiaron o ya no están; vuelve a ejecutar el modo rápido antes de registrar la revisión")
        destino = validar_destino(Path(t["destino"]), Path(t["origen"]))
        fecha = dt.datetime.now().isoformat(timespec="seconds")
        revision = {"estado": "sin_hallazgos", "revisor": revisor, "fecha": fecha, "hallazgos": [],
                    "detalle": "revisión registrada sobre el conjunto exacto de archivos de esta corrida"}
        carpeta_reportes().mkdir(parents=True, exist_ok=True)
        registro_revision = carpeta_reportes() / f"revision_rapido_{dt.datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(2)}.json"
        registro_preparado = registro_revision.with_name(registro_revision.name + SUFIJO_PREPARADO)

        def evidencia(info: dict) -> None:
            # Se escribe ANTES de publicar: si no se puede guardar el registro de la revisión, el zip no sale.
            _escribir_json(registro_preparado, {**revision, "app": VERSION, "pacientes": t["pacientes"], "archivos": t["archivos"],
                                                "zip": {"nombre": info["nombre"], "destino": str(destino), "sha256": info["sha256"]},
                                                "nota": f"Se escribe antes de publicar el zip. Con el sufijo {SUFIJO_PREPARADO} no acredita ninguna publicación; al confirmarse la publicación el archivo pierde ese sufijo."})

        try:
            r = crear_paquete(t["pacientes"], destino, solo=set(t["archivos"]), revision_corrida=revision,
                              hashes_esperados=dict(t["archivos"]), antes_de_publicar=evidencia,
                              exportacion=dict(t["exportacion"]), orden_series=dict(t["orden_de_cortes"]),   # los mismos nombres que el zip pendiente
                              demografia=t["demografia"])                                                     # y la misma demografía
        except PaqueteRechazado as e:
            # la publicación no ocurrió: se retira la evidencia preparada, y lo que no se pueda retirar se dice con su ruta
            restos = list(e.cuerpo.get("restos", []))
            if not _eliminar(registro_preparado):
                restos.append(str(registro_preparado))
            if restos:
                registrar(f"rápido: publicación fallida con restos sin retirar: {', '.join(restos)}")
            if e.cuerpo.get("fallos"):
                registrar("rápido: revisión rechazada al empaquetar; un archivo cambió entre la comprobación y el empaquetado")
                raise PublicacionFallida(409, f"{len(e.cuerpo['fallos'])} archivo(s) de esta corrida cambiaron o ya no están; nada se escribió en el destino. Vuelve a ejecutar el modo rápido." + texto_restos(restos), restos)
            raise PublicacionFallida(e.codigo, (e.cuerpo.get("detail") or e.cuerpo.get("error") or "el paquete no se pudo rehacer") + "." + texto_restos(restos), restos)

        # ---- a partir de aquí el zip revisado está publicado: todo lo que falle es un aviso, con la ruta a la vista ----
        t["zip_pendiente_anterior"] = t["zip"]
        anterior = Path(t["zip"])
        t["zip"], t["revision_pixeles_pendiente"], t["revisado"] = r["ruta"], r["revision_pixeles_pendiente"], True
        avisos_posteriores = list(r.get("avisos", []))
        registro_confirmado = True
        try:
            os.replace(registro_preparado, registro_revision)   # la evidencia preparada pasa a registro confirmado
        except OSError:
            registro_confirmado, registro_revision = False, registro_preparado
            avisos_posteriores.append(f"el registro de la revisión quedó con su nombre provisional aunque el zip está publicado: {registro_preparado}")
        cubiertos = []
        for pac in t["pacientes"]:
            try:   # la revisión del paciente completo solo se actualiza si la corrida cubre todos sus archivos guardados
                if set(hashes_paciente(pac)) == {rel.split("/", 1)[1] for rel in t["archivos"] if rel.startswith(pac + "/")}:
                    guardar_revision(pac, "sin_hallazgos", [], revisor, "registrada desde el modo rápido; cubre todos los archivos del paciente")
                    cubiertos.append(pac)
            except Exception as e:
                avisos_posteriores.append(f"no se pudo actualizar la revisión de {pac} en Estudios ({type(e).__name__}); el zip publicado y su registro no cambian")
        no_eliminados = [str(v) for v in (anterior, anterior.with_name(anterior.name[:-4] + ".manifiesto.json"))
                         if v.resolve() != Path(r["ruta"]).resolve() and not _eliminar(v)]
        hay_avisos = any(p_["estado"] == "aviso" for p_ in t["pasos"])
        t["mensaje"] = "Zip revisado y guardado en el destino." + (" Hay avisos en los pasos de arriba." if hay_avisos else "")
        registrar(f"rápido: revisión registrada por {revisor}; zip en el destino; pacientes con revisión completa: {', '.join(cubiertos) or 'ninguno'}"
                  + (f"; avisos posteriores: {len(avisos_posteriores) + len(no_eliminados)}" if avisos_posteriores or no_eliminados else ""))
        return {"zip": r["ruta"], "revision_pixeles_pendiente": r["revision_pixeles_pendiente"], "mensaje": t["mensaje"],
                "avisos": hay_avisos, "pendiente_anterior_no_eliminado": no_eliminados, "avisos_posteriores": avisos_posteriores,
                "registro_revision": str(registro_revision), "registro_confirmado": registro_confirmado,
                "revision_de_paciente_actualizada": cubiertos,
                "destino_sincronizado": en_sincronizada(destino)}
    finally:
        CANDADO.release()


@api.post("/rapido/revision")
def rapido_revision(request: Request, cuerpo: dict):
    """Tras recorrer todos los cortes: registra la revisión de la corrida y escribe el zip revisado en el destino."""
    auth(request)
    try:
        return aplicar_revision_rapido(TRABAJOS.get(str(cuerpo.get("trabajo", ""))), str(cuerpo.get("revisor", "")).strip()[:120])
    except PublicacionFallida as e:
        return JSONResponse(status_code=e.codigo, content={"detail": e.mensaje, "restos": e.restos})


def raices_reubicadas() -> dict[str, str]:
    """Raíces de originales que el usuario reubicó: {raíz guardada → carpeta actual}. Privado, se queda en la máquina."""
    try:
        return json.loads((carpeta_privada() / "raices.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def resolver_raiz(raiz: str) -> Path:
    """Una raíz guardada relativa se resuelve contra la carpeta de trabajo; una reubicación la sustituye."""
    reubicada = raices_reubicadas().get(raiz)
    if reubicada:
        return Path(reubicada)
    p = Path(raiz)
    return p if p.is_absolute() else (TRABAJO / p)


def indice_origen() -> dict[str, dict]:
    """limpio → {original, raiz (guardada), raiz_actual}, de los índices privados (se quedan en esta máquina)."""
    out: dict[str, dict] = {}
    limpios = (TRABAJO / "limpios").resolve()
    for carpeta in (TRABAJO / "PRIVADO", TRABAJO / "limpios_PRIVADO"):
        try:
            for linea in (carpeta / "indice_origen.jsonl").read_text(encoding="utf-8").splitlines():
                try:
                    d = json.loads(linea)
                    clave = str((limpios / d["limpio_rel"]).resolve()) if d.get("limpio_rel") else str(Path(d["limpio"]).resolve())
                    raiz = d.get("raiz", "")
                    raiz_actual = resolver_raiz(raiz) if raiz else None
                    if "original_rel" in d and raiz_actual is not None:
                        original = str(raiz_actual / d["original_rel"])
                    else:
                        original = d.get("original", "")
                    out[clave] = {"original": original, "raiz": raiz, "raiz_actual": str(raiz_actual) if raiz_actual else ""}
                except (ValueError, KeyError):
                    continue
        except OSError:
            continue
    return out


def elementos(ds, prefijo: str = "") -> dict[str, tuple[str, str, bool]]:
    """{ruta: (VR, valor como texto, es_privada)} de todo el dataset, secuencias incluidas. Solo para comparar; nunca se devuelven valores."""
    out: dict[str, tuple[str, str, bool]] = {}
    for e in ds:
        kw = e.keyword or f"({e.tag.group:04X},{e.tag.element:04X})"
        ruta = f"{prefijo}{kw}"
        if str(e.VR) == "SQ":
            for i, item in enumerate(e.value):
                out.update(elementos(item, f"{ruta}[{i}]."))
            out[ruta] = ("SQ", f"{len(e.value)} items", e.tag.is_private)
        else:
            try:
                v = str(e.value) if e.keyword != "PixelData" else "(píxeles)"
            except Exception:
                v = "?"
            out[ruta] = (str(e.VR), v, e.tag.is_private)
    return out


def clasificar(kw: str, vr: str, privada: bool) -> str:
    base = kw.rsplit(".", 1)[-1]
    if privada:
        return "privada conservada con fecha desplazada" if vr == "DT" else "privada conservada"
    if base in ANON.REEMPLAZAR:
        return "seudónimo"
    if base in ANON.VACIAR:
        return "vaciado"
    if base == "CodeMeaning":
        return "significado de código canonizado"
    if base == "PatientAge":
        return "edad agrupada"
    if base == "StudyID":
        return "identificador de estudio derivado"
    if vr in ("DA", "DT"):
        return "fecha desplazada"
    if vr == "UI":
        return "UID reescrito"
    return "modificado"


@api.get("/serie/{sid}/cambios/{i}")
def cambios(request: Request, sid: str, i: int):
    """Qué cambió entre el original y este archivo limpio: acciones y nombres de etiqueta, nunca valores."""
    auth(request)
    s = INDICE.get(sid)
    if not s:
        raise HTTPException(404, "serie desconocida")
    i = max(0, min(i, s["n"] - 1))
    limpio = Path(s["cortes"][i][1]).resolve()
    entrada = indice_origen().get(str(limpio))
    original = entrada["original"] if entrada else None
    if not entrada:
        return {"disponible": False, "motivo": "este archivo no figura en el índice privado; vuelve a importar su carpeta original para reconstruirlo"}
    if not original or not Path(original).is_file():
        return {"disponible": False, "raiz": entrada.get("raiz", ""), "raiz_actual": entrada.get("raiz_actual", ""),
                "motivo": "los originales no están donde se importaron; indica dónde están ahora"}
    try:
        o = pydicom.dcmread(original, force=True)
        c = pydicom.dcmread(limpio, force=True)
    except Exception:
        return {"disponible": False, "motivo": "no se pudo leer alguno de los dos archivos"}
    eo, ec = elementos(o), elementos(c)
    acciones: dict[str, list[str]] = {}
    for ruta, (vr, v, priv) in eo.items():
        if ruta == "PixelData":
            continue
        if ruta not in ec:
            acciones.setdefault("eliminado" if not priv else "privada eliminada", []).append(ruta)
        elif ec[ruta][1] != v:
            acciones.setdefault(clasificar(ruta, vr, priv), []).append(ruta)
    for ruta in ec:
        if ruta not in eo and ruta != "PixelData":
            acciones.setdefault("añadido", []).append(ruta)
    px_o = hashlib.sha256(bytes(o.PixelData)).hexdigest() if "PixelData" in o else ""
    px_c = hashlib.sha256(bytes(c.PixelData)).hexdigest() if "PixelData" in c else ""
    sin_cambio = sum(1 for r in eo if r in ec and ec[r][1] == eo[r][1] and r != "PixelData")
    destacados = {
        "nombre": str(c.get("PatientName", "")), "id": str(c.get("PatientID", "")),
        "fecha_nacimiento": "vacía" if not str(c.get("PatientBirthDate", "")).strip() else "con valor",
        "pixeles_identicos": bool(px_o) and px_o == px_c,
        "politica": str(c.get("DeidentificationMethod", "")).split(";")[0],
    }
    resumen = {k: len(v) for k, v in acciones.items()}
    registrar(f"cambios: {s['paciente']} corte {i}: {sum(resumen.values())} acciones")
    return {"disponible": True, "destacados": destacados, "resumen": resumen,
            "detalle": {k: sorted(v)[:400] for k, v in acciones.items()}, "sin_cambio": sin_cambio}


@api.get("/serie/{sid}/corte_original/{i}")
def corte_original(request: Request, sid: str, i: int, ventana: str = "tejido"):
    """La imagen original (solo píxeles, misma ventana) para ponerla junto a la limpia."""
    auth(request)
    s = INDICE.get(sid)
    if not s:
        raise HTTPException(404, "serie desconocida")
    i = max(0, min(i, s["n"] - 1))
    entrada = indice_origen().get(str(Path(s["cortes"][i][1]).resolve()))
    original = entrada["original"] if entrada else None
    if not original or not Path(original).is_file():
        raise HTTPException(404, "original no disponible")
    try:
        png = render_corte(original, ventana)
    except Exception as e:
        raise HTTPException(500, f"no se pudo renderizar: {type(e).__name__}")
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


def raices_del_paciente(pac: str) -> list[str]:
    """Carpetas de entrada actuales con las que se limpió este paciente, según el índice privado."""
    carpeta = str((TRABAJO / "limpios" / pac).resolve())
    return sorted({e["raiz_actual"] for l, e in indice_origen().items() if l.startswith(carpeta + os.sep) and e.get("raiz_actual")})


@api.post("/raiz")
def reubicar_raiz(request: Request, cuerpo: dict):
    """El usuario indica dónde están ahora los originales de una raíz guardada (selector nativo o ruta explícita)."""
    auth(request)
    anterior = str(cuerpo.get("anterior", ""))
    if not anterior:
        raise HTTPException(400, "falta la raíz anterior")
    nueva = str(cuerpo.get("nueva", "")).strip() or (dialogo_carpeta() or "")
    if not nueva or not Path(nueva).is_dir():
        return {"ok": False, "mensaje": "no se eligió una carpeta válida"}
    priv = carpeta_privada()
    priv.mkdir(parents=True, exist_ok=True)
    reubicadas = raices_reubicadas()
    reubicadas[anterior] = str(Path(nueva).resolve())
    (priv / "raices.json").write_text(json.dumps(reubicadas, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(priv / "raices.json", 0o600)
    except OSError:
        pass
    registrar("raíz de originales reubicada")
    return {"ok": True, "nueva": str(Path(nueva).resolve())}


@api.post("/comprobar")
def comprobar(request: Request, cuerpo: dict):
    """Botones de flujo: emparejar, cotejar o suv sobre un paciente o una serie. Devuelve la salida de texto del script."""
    auth(request)
    que = str(cuerpo.get("que", ""))
    pac = str(cuerpo.get("paciente", ""))
    carpeta = carpeta_paciente(pac)
    if que == "emparejar":
        codigo, salida = ejecutar(comando_herramienta("emparejar") + [str(carpeta)])
    elif que == "cotejar":
        raices = raices_del_paciente(pac)
        if not raices:
            return {"codigo": 2, "salida": "no hay carpeta original registrada para este paciente; vuelve a importar la carpeta original para reconstruir el índice"}
        salida, codigo = "", 0
        for raiz in raices:
            c, s = ejecutar(comando_herramienta("cotejar") + [raiz, str(TRABAJO / "limpios"),
                             "--mapa", str(carpeta_privada() / "mapa_seudonimos.json")])
            salida += s
            codigo = max(codigo, c)
    elif que in ("suv", "comparar_suv"):
        sid = str(cuerpo.get("serie", ""))
        s = INDICE.get(sid)
        if not s or s["paciente"] != pac:
            raise HTTPException(400, "serie desconocida")
        carpeta_serie = str(Path(s["cortes"][0][1]).parent)
        if que == "suv":
            codigo, salida = ejecutar(comando_herramienta("suv") + ["calcular", carpeta_serie])
        else:
            codigo, salida = ejecutar(comando_herramienta("suv") + ["comparar", carpeta_serie, "--privado", str(carpeta_privada())])
    else:
        raise HTTPException(400, "comprobación desconocida")
    registrar(f"comprobar {que}: {pac} → código {codigo}")
    return {"codigo": codigo, "salida": salida}


def hashes_paciente(pac: str) -> dict[str, str]:
    carpeta = carpeta_paciente(pac)
    return {p.relative_to(carpeta).as_posix(): sha256_archivo(p) for p in archivos_regulares(carpeta) if p.suffix.lower() == ".dcm"}


HALLAZGOS_VALIDOS = ("texto_grabado", "rostro", "otro")


def validar_registro_revision(estado: str, hallazgos: list, revisor: str) -> str | None:
    """Regla del registro: 'sin_hallazgos' exige lista vacía y revisor; 'con_hallazgos' exige al menos un hallazgo válido y revisor."""
    if estado not in ("sin_hallazgos", "con_hallazgos"):
        return "estado debe ser sin_hallazgos o con_hallazgos"
    if not str(revisor or "").strip():
        return "indica quién revisa"
    invalidos = [h for h in hallazgos if h not in HALLAZGOS_VALIDOS]
    if invalidos:
        return "tipo de hallazgo desconocido"
    if estado == "sin_hallazgos" and hallazgos:
        return "hay hallazgos marcados: el resultado no puede ser 'sin hallazgos'"
    if estado == "con_hallazgos" and not hallazgos:
        return "indica al menos un tipo de hallazgo"
    return None


def evaluar_registro_revision(r: dict, actuales: dict[str, str]) -> dict:
    """Estado efectivo de un registro frente a los archivos actuales. Cualquier hallazgo registrado cuenta como pendiente,
    diga lo que diga la etiqueta; un registro contradictorio también."""
    vigente = actuales == r.get("archivos", {})
    r2 = {k: v for k, v in r.items() if k != "archivos"}
    r2["archivos_revisados"] = len(r.get("archivos", {}))
    r2["vigente"] = vigente
    hallazgos = list(r.get("hallazgos") or [])
    if not vigente:
        r2["estado"], r2["detalle"] = "pendiente", "invalidada: los archivos cambiaron desde la revisión"
    elif validar_registro_revision(str(r.get("estado", "")), hallazgos, str(r.get("revisor", ""))):
        r2["estado"], r2["detalle"] = "pendiente", "registro contradictorio o incompleto; vuelve a registrar la revisión"
    elif hallazgos:
        r2["estado"], r2["detalle"] = "con_hallazgos", "con hallazgos sin resolver: requiere tratamiento antes de compartir"
    else:
        r2["detalle"] = "vigente"
    r2["lista_para_compartir"] = vigente and r2["estado"] == "sin_hallazgos"
    return r2


def leer_revision(pac: str) -> dict:
    """Estado de la revisión de píxeles de un paciente, comprobando que los archivos revisados no cambiaron."""
    ruta = carpeta_reportes() / f"revision_pixeles_{pac}.json"
    if not ruta.exists():
        return {"estado": "pendiente", "vigente": False, "lista_para_compartir": False, "detalle": "sin revisión registrada"}
    try:
        r = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"estado": "pendiente", "vigente": False, "lista_para_compartir": False, "detalle": "registro ilegible"}
    return evaluar_registro_revision(r, hashes_paciente(pac))


def guardar_revision(pac: str, estado: str, hallazgos: list[str], revisor: str, notas: str = "") -> dict:
    """Escribe el registro de revisión de píxeles de un paciente, ligado a los hashes de sus archivos."""
    try:
        politica = json.loads((TRABAJO / "limpios" / ".politica.json").read_text(encoding="utf-8")).get("politica", "")
    except (OSError, ValueError):
        politica = ""
    registro = {"paciente": pac, "estado": estado, "hallazgos": hallazgos, "notas": notas, "revisor": revisor,
                "fecha": dt.datetime.now().isoformat(timespec="seconds"), "app": VERSION, "politica": politica,
                "archivos": hashes_paciente(pac)}
    carpeta_reportes().mkdir(parents=True, exist_ok=True)
    (carpeta_reportes() / f"revision_pixeles_{pac}.json").write_text(json.dumps(registro, ensure_ascii=False, indent=2), encoding="utf-8")
    registrar(f"revisión de píxeles: {pac} → {estado} ({len(registro['archivos'])} archivos)")
    return leer_revision(pac)


@api.get("/revision/{pac}")
def revision(request: Request, pac: str):
    auth(request)
    carpeta_paciente(pac)
    return leer_revision(pac)


@api.post("/revision")
def registrar_revision(request: Request, cuerpo: dict):
    """Registra la revisión de píxeles de un paciente contra los hashes exactos de sus archivos."""
    auth(request)
    pac = str(cuerpo.get("paciente", ""))
    carpeta_paciente(pac)
    estado = str(cuerpo.get("estado", ""))
    hallazgos = [str(h) for h in cuerpo.get("hallazgos", [])]
    revisor = str(cuerpo.get("revisor", "")).strip()[:120]
    error = validar_registro_revision(estado, hallazgos, revisor)
    if error:
        raise HTTPException(400, error)
    return guardar_revision(pac, estado, hallazgos, revisor, str(cuerpo.get("notas", ""))[:2000])


@api.get("/registro")
def registro(request: Request):
    auth(request)
    return REGISTRO[-300:]


# --------------------------------------------------------------------------------------
# interfaz / UI (HTML sin datos; los datos entran solo por textContent desde app.js)
# --------------------------------------------------------------------------------------

HTML = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PET/CT local</title><link rel="stylesheet" href="/static/app.css"></head><body>
<header class="marca">
  <svg class="icono" viewBox="0 0 26 26" aria-hidden="true"><rect x="2" y="2" width="15" height="15" rx="3" fill="none" stroke="currentColor" stroke-width="1.8"/><rect x="9" y="9" width="15" height="15" rx="3" fill="var(--acento)" fill-opacity=".18" stroke="var(--acento)" stroke-width="1.8"/><circle cx="16.5" cy="16.5" r="2.2" fill="var(--acento)"/></svg>
  <span class="nombre">PET/CT local</span>
  <span class="lema">anonimizar · revisar · enviar · todo en 127.0.0.1</span>
  <span class="version" id="version"></span>
</header>
<nav class="pestanas"><button data-tab="rapido" title="Una carpeta, de principio a fin, con un solo botón"><span class="paso">»</span>Rápido</button><button data-tab="entrada" class="activa"><span class="paso">1</span>Entrada</button><button data-tab="estudios"><span class="paso">2</span>Estudios</button><button data-tab="enviar"><span class="paso">3</span>Enviar</button><button data-tab="registro"><span class="paso">·</span>Registro</button></nav>
<main>
<section id="rapido" class="pestana">
  <h2>Anonimizar una carpeta, de principio a fin</h2>
  <p class="suave">Un solo botón: limpia la carpeta, comprueba los metadatos, coteja con el original, empareja CT y PET, compara el SUV y deja un zip donde tú digas. Mirar las imágenes sigue siendo tarea de una persona: el zip nace con la revisión de píxeles pendiente y se queda en la carpeta de trabajo; cuando registres la revisión de esta corrida, pasa al destino que elijas.</p>
  <div class="fila"><input type="text" id="rap-origen" placeholder="Carpeta del estudio, en el disco local"><button id="btn-rap-origen">Elegir carpeta…</button></div>
  <div class="fila"><input type="text" id="rap-destino" placeholder="Dónde guardar el zip ya revisado"><button id="btn-rap-destino">Elegir destino…</button></div>
  <div class="fila"><button id="btn-rapido" class="primario">Anonimizar carpeta</button><span id="rap-reloj" class="terciario"></span></div>
  <div id="rap-estado"></div>
  <div id="rap-pasos" class="pasos"></div>
  <div id="rap-revision" class="oculto">
    <div class="fila"><input type="text" id="rap-revisor" placeholder="quien revisa" class="corto"><button id="btn-rap-revision">Revisé todos los cortes: sin hallazgos</button></div>
    <p class="terciario">Recorre aquí abajo todos los cortes de todas las series antes de registrar. La revisión vale para los archivos exactos de esta corrida: si alguno cambia, hay que repetirla. Al registrar, el zip revisado se escribe en el destino y el pendiente se elimina de la carpeta de trabajo.</p>
  </div>
  <div id="rap-visor" class="oculto">
    <h3>Los archivos de este paquete, en su orden</h3>
    <p class="terciario">Son exactamente los archivos del zip de esta corrida, con la numeración de sus nombres. Importaciones posteriores no cambian esta lista. La pestaña Estudios muestra todo lo guardado del paciente, que puede ser más.</p>
    <div class="fila" id="rap-series"></div>
    <p id="rap-etiqueta-estudio" class="etiqueta-estudio"></p><div id="rap-aviso-demografia"></div>
    <div class="visor-corrida"><div class="lienzo"><img id="rap-corte" alt="corte de la corrida"></div>
      <div><input type="range" id="rap-deslizador" min="1" max="1" value="1">
        <div class="fila"><label>Ventana <select id="rap-ventana"><option value="tejido">tejido blando</option><option value="pulmon">pulmón</option><option value="hueso">hueso</option><option value="cerebro">cerebro</option><option value="auto">auto (PET, solo vista)</option></select></label></div>
        <p id="rap-etiqueta" class="mono"></p>
        <div id="rap-orden"></div></div></div>
  </div>
  <details id="rap-detalles" class="oculto"><summary>Salida de cada paso</summary><pre id="rap-salida" class="salida"></pre></details>
</section>
<section id="entrada" class="pestana activa">
  <h2>Carpeta con los DICOM originales</h2>
  <p class="suave">Solo se leen desde donde están. La copia limpia se escribe en la carpeta de trabajo. Los objetos que no son CT, PET, MR o segmentación van a cuarentena y no se copian.</p>
  <div class="fila"><input type="text" id="origen" placeholder="/Users/tu/estudios-originales"><button id="btn-origen">Elegir carpeta…</button><button id="btn-demo" title="Crea un caso CT + PET inventado, sin datos de pacientes, para probar el flujo completo">Crear caso de demostración</button></div>
  <div id="aviso-origen"></div>
  <p class="terciario">Carpeta de trabajo: <code id="trabajo"></code> <span id="aviso-trabajo"></span></p>
  <div class="fila"><label>Prefijo <input type="text" id="prefijo" class="corto"></label>
    <label><input type="checkbox" id="conservar"> conservar descripciones de serie y protocolo (revisadas contra nombre e ID)</label>
    <button id="btn-anonimizar" class="primario">Anonimizar</button></div>
  <div id="estado-anon"></div>
  <pre id="salida-anon" class="salida oculto"></pre>
  <details id="det-reporte" class="oculto"><summary>Reporte de la limpieza</summary><pre id="reporte-anon" class="salida"></pre></details>
</section>
<section id="estudios" class="pestana">
  <div class="fila"><h2 class="sin-margen">Estudios limpios</h2><button id="btn-refrescar">Refrescar</button><span id="resumen-estudios" class="terciario"></span></div>
  <div class="tabla-marco"><table id="tabla-series" class="datos"><thead><tr><th>Paciente</th><th>Sexo · edad en el estudio</th><th>Estudio</th><th>Serie</th><th>Mod.</th><th>Descripción</th><th class="num">Cortes</th><th>Matriz</th><th>Política</th></tr></thead><tbody></tbody></table></div>
  <div class="visor oculto" id="visor">
    <div><div class="lienzo"><img id="corte" alt="corte"></div><input type="range" id="deslizador" min="0" max="0" value="0">
      <div class="fila"><label>Ventana <select id="ventana"><option value="tejido">tejido blando</option><option value="pulmon">pulmón</option><option value="hueso">hueso</option><option value="cerebro">cerebro</option><option value="auto">auto (PET, solo vista)</option></select></label><span id="pos" class="terciario"></span></div>
      <p class="terciario"><span id="nota-orden">Cortes ordenados por InstanceNumber.</span> Vista previa sin escala cuantitativa; el SUV se calcula en las comprobaciones, no aquí. Al exportar, los archivos se numeran con este mismo orden.</p>
      <div id="aviso-orden"></div></div>
    <div><p id="etiqueta-estudio" class="etiqueta-estudio"></p><div id="aviso-demografia"></div><div class="meta" id="meta"></div>
      <h3 style="margin-top:16px">Comprobaciones</h3>
      <div class="fila" id="botones-comprobar"><button data-que="emparejar">Emparejar CT + PET</button><button data-que="cotejar">Cotejar con el original</button><button data-que="suv">SUV de esta serie</button><button data-que="comparar_suv">Comparar SUV original / limpio</button></div>
      <p class="terciario">Alcance: emparejar y cotejar actúan sobre el paciente en pantalla; SUV y la comparación, sobre esta serie.</p>
      <pre id="salida-comprobar" class="salida oculto"></pre>
      <div class="revision" id="revision">
        <h3>Revisión de píxeles de este paciente</h3>
        <div id="rev-estado"></div>
        <div class="fila"><label><input type="checkbox" id="rev-texto"> texto grabado</label><label><input type="checkbox" id="rev-rostro"> rostro identificable</label><label><input type="checkbox" id="rev-otro"> otro identificador visible</label></div>
        <div class="fila"><input type="text" id="rev-revisor" placeholder="quien revisa" class="corto"><input type="text" id="rev-notas" placeholder="notas (opcional)"></div>
        <div class="fila"><button id="rev-sin">Registrar: revisada, sin hallazgos</button><button id="rev-con">Registrar: con hallazgos</button></div>
        <p class="terciario">La revisión queda ligada a los hashes exactos de los archivos del paciente; si cambian, pasa a pendiente. Revisar toda la serie, no solo algunos cortes.</p>
      </div></div>
  </div>
  <div class="antes-despues oculto" id="antes-despues">
    <h2>Antes y después de este corte</h2>
    <div id="ad-estado"></div>
    <div class="par"><div><p class="etq">Original</p><div class="lienzo"><img id="img-original" alt="original"></div></div><div><p class="etq">Limpio</p><div class="lienzo"><img id="img-limpio" alt="limpio"></div></div></div>
    <div class="rejilla" id="destacados"></div>
    <details><summary>Todas las etiquetas por acción</summary><div class="meta" id="detalle-cambios"></div></details>
  </div>
</section>
<section id="enviar" class="pestana">
  <h2>Paquete para el colega</h2>
  <p class="suave">Elige pacientes uno por uno. Cada archivo se lee una sola vez: esos mismos bytes se verifican contra la política, se hashean en el manifiesto y se escriben al zip, que se relee y comprueba antes de publicarse. Solo entran DICOM limpios. El zip se crea localmente; la revisión de píxeles pendiente se muestra aquí y queda escrita en el manifiesto.</p>
  <div id="lista-pacientes"></div>
  <div class="fila"><button id="btn-verificar">Verificar</button><button id="btn-empaquetar" class="primario">Verificar y empaquetar</button></div>
  <div id="estado-enviar"></div>
  <pre id="salida-enviar" class="salida oculto"></pre>
</section>
<section id="registro" class="pestana"><h2>Registro</h2><p class="terciario">Bitácora privada; se queda en la carpeta de trabajo. Rutas y seudónimos, nunca valores.</p><pre id="texto-registro" class="salida"></pre></section>
</main>
<script src="/static/app.js"></script></body></html>"""

def _leer_estilo() -> str:
    """Hoja de estilo compartida (tokens y componentes); junto a app.py o en el paquete congelado."""
    candidatos = [AQUI / "estilo.css"]
    if CONGELADO:
        candidatos.insert(0, Path(getattr(sys, "_MEIPASS", AQUI)) / "estilo.css")
    for c in candidatos:
        try:
            return c.read_text(encoding="utf-8")
        except OSError:
            continue
    return "body{font:14px/1.5 system-ui;margin:0;padding:16px}"


CSS = _leer_estilo()

JS = r"""'use strict';
const T = new URLSearchParams(location.search).get('t');
const $ = s => document.querySelector(s);
const texto = (sel, v) => { const el = $(sel); el.textContent = v == null ? '' : String(v); return el; };
const api = async (ruta, cuerpo) => {
  const r = await fetch('/api/' + ruta, cuerpo === undefined ? {headers: {'X-Token': T}} :
    {method: 'POST', headers: {'X-Token': T, 'Content-Type': 'application/json'}, body: JSON.stringify(cuerpo)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(j.detail || j.error || r.statusText); e.datos = j; throw e; }
  return j;
};
const mensaje = (sel, tipo, txt) => { const el = $(sel); el.textContent = ''; if (!txt) return el; const d = document.createElement('div'); d.className = 'mensaje ' + tipo; d.textContent = txt; el.appendChild(d); return el; };
const insignia = (tipo, txt) => { const s = document.createElement('span'); s.className = 'insignia ' + tipo; s.textContent = txt; return s; };
const celda = (tr, v, cls) => { const td = document.createElement('td'); td.textContent = v == null ? '' : String(v); if (cls) td.className = cls; tr.appendChild(td); };
document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button, main section').forEach(x => x.classList.remove('activa'));
  b.classList.add('activa'); $('#' + b.dataset.tab).classList.add('activa');
  if (b.dataset.tab === 'estudios') cargarEstudios(); if (b.dataset.tab === 'enviar') cargarPacientes(); if (b.dataset.tab === 'registro') cargarRegistro();
});
async function estado() {
  try {
    const e = await api('estado');
    texto('#trabajo', e.trabajo); $('#prefijo').value = e.prefijo || 'ANON'; if (!$('#rap-destino').value) $('#rap-destino').value = e.trabajo.replace(/[\\/]+$/, '') + (e.trabajo.includes('\\') ? '\\paquetes' : '/paquetes'); texto('#version', 'v' + e.version + (e.congelado ? ' · paquete' : ' · código fuente'));
    if (e.sincronizada) { const s = $('#aviso-trabajo'); s.textContent = ''; s.appendChild(insignia('error', 'carpeta sincronizada con la nube: ' + e.sincronizada + '; usa otra con --trabajo')); }
    if (!e.anonimizar_presente) mensaje('#estado-anon', 'error', 'Falta anonimizar.py junto a app.py');
  } catch (err) { mensaje('#estado-anon', 'error', 'Abre la dirección completa que imprimió la terminal, con el token.'); }
}
let RAPIDO = null;
const ETIQUETA_PASO = {pendiente: ['neutro', '· pendiente'], corriendo: ['info', '… en curso'], ok: ['ok', '✓ correcto'], aviso: ['aviso', '! aviso'], error: ['error', '× falló'], omitido: ['neutro', '– omitido']};
function pintarPasos(t) {
  const cont = $('#rap-pasos'); cont.textContent = '';
  for (const paso of (t.pasos || [])) {
    const fila = document.createElement('div'); fila.className = 'paso-fila';
    const nombre = document.createElement('span'); nombre.className = 'paso-nombre'; nombre.textContent = paso.titulo;
    const [tipo, txt] = ETIQUETA_PASO[paso.estado] || ETIQUETA_PASO.pendiente;
    const resumen = document.createElement('span'); resumen.className = 'paso-resumen'; resumen.textContent = paso.resumen || '';
    fila.appendChild(nombre); fila.appendChild(insignia(tipo, txt)); fila.appendChild(resumen); cont.appendChild(fila);
  }
  const salida = (t.pasos || []).filter(x => x.salida).map(x => '== ' + x.titulo + ' ==\n' + x.salida).join('\n\n');
  $('#rap-detalles').classList.toggle('oculto', !salida); texto('#rap-salida', salida);
}
function pintarResultadoRapido(t) {
  if (t.estado === 'error') { mensaje('#rap-estado', 'error', t.mensaje || 'No se creó ningún zip.'); $('#rap-revision').classList.add('oculto'); return; }
  const nube = t.destino_sincronizado ? ' Ese destino está sincronizado con la nube (' + t.destino_sincronizado + '): el zip solo llegará allí ya revisado.' : '';
  if (!t.revisado) mensaje('#rap-estado', 'aviso', (t.mensaje || '') + ' Zip pendiente: ' + t.zip + '. Destino tras la revisión: ' + t.destino + '.' + nube);
  else mensaje('#rap-estado', t.estado === 'con avisos' ? 'aviso' : 'ok', (t.mensaje || '') + ' Zip: ' + t.zip);
  $('#rap-revision').classList.toggle('oculto', !(t.zip && !t.revisado));
}
$('#btn-rap-origen').onclick = async () => { const r = await api('elegir-carpeta', {para: 'origen'}); if (r.ruta) $('#rap-origen').value = r.ruta; else mensaje('#rap-estado', 'aviso', r.mensaje); };
$('#btn-rap-destino').onclick = async () => { const r = await api('elegir-carpeta', {para: 'destino'}); if (r.ruta) $('#rap-destino').value = r.ruta; else mensaje('#rap-estado', 'aviso', r.mensaje); };
$('#btn-rapido').onclick = async () => {
  const origen = $('#rap-origen').value.trim();
  if (!origen) { mensaje('#rap-estado', 'aviso', 'Elige o escribe la carpeta del estudio.'); return; }
  $('#btn-rapido').disabled = true; $('#rap-revision').classList.add('oculto'); $('#rap-visor').classList.add('oculto'); $('#rap-pasos').textContent = ''; $('#rap-detalles').classList.add('oculto');
  const inicio = Date.now(); const reloj = () => { const s = Math.round((Date.now() - inicio) / 1000); return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0'); };
  mensaje('#rap-estado', 'neutro', 'en curso · 00:00');
  try {
    const r = await api('rapido', {origen, destino: $('#rap-destino').value.trim()});
    RAPIDO = r.trabajo;
    const esperar = async () => {
      const t = await api('trabajo/' + r.trabajo); pintarPasos(t);
      if (t.estado === 'corriendo') { mensaje('#rap-estado', 'neutro', 'en curso · ' + reloj()); return setTimeout(esperar, 800); }
      texto('#rap-reloj', 'terminó en ' + reloj()); pintarResultadoRapido(t); $('#btn-rapido').disabled = false;
      if (t.zip) cargarVisorCorrida(r.trabajo);
      ACTUAL = null; $('#visor').classList.add('oculto'); SERIES = [];
    };
    esperar();
  } catch (err) { mensaje('#rap-estado', 'error', err.message); $('#btn-rapido').disabled = false; }
};
$('#btn-rap-revision').onclick = async () => {
  const revisor = $('#rap-revisor').value.trim(), trabajo = RAPIDO;
  if (!revisor) { mensaje('#rap-estado', 'aviso', 'Indica quién revisa antes de registrar.'); return; }
  $('#btn-rap-revision').disabled = true;
  try {
    const r = await api('rapido/revision', {trabajo, revisor});
    if (trabajo !== RAPIDO) return;
    const pend = r.revision_pixeles_pendiente || [], resto = r.pendiente_anterior_no_eliminado || [], post = r.avisos_posteriores || [];
    const tipo = (pend.length || r.avisos || resto.length || post.length) ? 'aviso' : 'ok';
    mensaje('#rap-estado', tipo, r.mensaje + ' Zip: ' + r.zip + (pend.length ? ' Revisión de píxeles aún pendiente para: ' + pend.join(', ') + '.' : '') +
      (resto.length ? ' No se pudo eliminar el zip pendiente de la carpeta de trabajo: ' + resto.join(', ') + '; bórralo a mano.' : '') +
      (post.length ? ' El zip está publicado; quedaron avisos: ' + post.join('; ') + '.' : '') +
      (r.destino_sincronizado ? ' El destino está sincronizado con la nube (' + r.destino_sincronizado + ').' : ''));
    $('#rap-revision').classList.toggle('oculto', pend.length === 0);
  } catch (err) { if (trabajo === RAPIDO) mensaje('#rap-estado', 'error', err.message); }
  finally { $('#btn-rap-revision').disabled = false; }
};
let CORRIDA = {trabajo: null, series: [], indice: 0};
function pintarCorteCorrida() {
  const s = CORRIDA.series[CORRIDA.indice]; if (!s) return;
  const k = Math.min(Math.max(parseInt($('#rap-deslizador').value, 10) || 1, 1), s.n);
  $('#rap-corte').src = '/api/rapido/' + encodeURIComponent(CORRIDA.trabajo) + '/corte/' + CORRIDA.indice + '/' + k + '?ventana=' + encodeURIComponent($('#rap-ventana').value) + '&t=' + encodeURIComponent(T);
  texto('#rap-etiqueta', 'corte ' + k + ' de ' + s.n + ' · ' + s.nombres[k - 1]);
}
function elegirSerieCorrida(i) {
  CORRIDA.indice = i; const s = CORRIDA.series[i];
  document.querySelectorAll('#rap-series button').forEach((b, j) => b.classList.toggle('primario', j === i));
  const d = $('#rap-deslizador'); d.min = 1; d.max = s.n; d.value = 1;
  $('#rap-ventana').value = s.modalidad === 'PT' ? 'auto' : 'tejido';
  pintarDemografia('#rap-etiqueta-estudio', '#rap-aviso-demografia', s.demografia);
  mensaje('#rap-orden', s.criterio === 'desempate determinista' ? 'aviso' : 'neutro', 'Cortes ordenados por ' + (s.criterio || 'InstanceNumber') + (s.aviso ? ': ' + s.aviso : '') + '.');
  pintarCorteCorrida();
}
async function cargarVisorCorrida(trabajo) {
  try {
    const series = await api('rapido/' + trabajo + '/series');
    if (trabajo !== RAPIDO) return;
    CORRIDA = {trabajo, series, indice: 0};
    const cont = $('#rap-series'); cont.textContent = '';
    series.forEach((s, i) => { const b = document.createElement('button'); b.textContent = (s.modalidad || 'serie') + ' · ' + s.n + ' cortes · ' + s.serie.split('/').pop(); b.onclick = () => elegirSerieCorrida(i); cont.appendChild(b); });
    $('#rap-visor').classList.toggle('oculto', series.length === 0);
    if (series.length) elegirSerieCorrida(0);
  } catch (err) { $('#rap-visor').classList.add('oculto'); }
}
$('#rap-deslizador').oninput = pintarCorteCorrida;
$('#rap-ventana').onchange = pintarCorteCorrida;
$('#rap-corte').onerror = () => texto('#rap-etiqueta', 'Este archivo cambió desde la corrida o no se puede mostrar; vuelve a ejecutar el modo rápido.');
$('#btn-origen').onclick = async () => {
  const r = await api('elegir-carpeta', {});
  if (r.ruta) { $('#origen').value = r.ruta; mensaje('#aviso-origen', r.sincronizada ? 'aviso' : 'neutro', r.sincronizada ? 'Está dentro de una carpeta sincronizada (' + r.sincronizada + '). Se puede leer, pero conviene sacarla de la nube.' : 'Carpeta elegida.'); }
  else mensaje('#aviso-origen', 'aviso', r.mensaje);
};
$('#btn-demo').onclick = async () => {
  $('#btn-demo').disabled = true; mensaje('#aviso-origen', 'neutro', 'creando caso sintético…');
  try { const r = await api('demo', {}); $('#origen').value = r.ruta; mensaje('#aviso-origen', 'ok', 'Caso sintético listo (identidad ficticia, sin datos reales). Pulsa Anonimizar para recorrer el flujo completo.'); }
  catch (err) { mensaje('#aviso-origen', 'error', err.message); } finally { $('#btn-demo').disabled = false; }
};
$('#btn-anonimizar').onclick = async () => {
  const origen = $('#origen').value.trim();
  if (!origen) { mensaje('#aviso-origen', 'aviso', 'Elige o escribe la carpeta de originales.'); return; }
  $('#btn-anonimizar').disabled = true; $('#salida-anon').classList.remove('oculto'); texto('#salida-anon', ''); $('#det-reporte').classList.add('oculto');
  const inicio = Date.now(); const reloj = () => { const s = Math.round((Date.now() - inicio) / 1000); return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0'); };
  mensaje('#estado-anon', 'neutro', 'limpieza en curso · 00:00');
  try {
    const {trabajo} = await api('anonimizar', {origen, prefijo: $('#prefijo').value.trim(), conservar_descripciones: $('#conservar').checked});
    const esperar = async () => {
      const t = await api('trabajo/' + trabajo); texto('#salida-anon', t.lineas.join('\n'));
      if (t.estado === 'corriendo') { const prog = [...t.lineas].reverse().find(l => /archivos limpios/.test(l)); mensaje('#estado-anon', 'neutro', 'limpieza en curso · ' + reloj() + (prog ? ' · ' + prog.trim().split(' (')[0] : '')); return setTimeout(esperar, 800); }
      const listo = t.lineas.find(l => l.startsWith('Listo')) || '';
      if (t.estado === 'ok') mensaje('#estado-anon', 'ok', 'Limpieza terminada en ' + reloj() + '. ' + listo.replace(/\s*\(done\)\s*$/, '') + '. La verificación posterior pasó en las etiquetas revisadas; los píxeles no se revisan.');
      else if (t.estado === 'con avisos') mensaje('#estado-anon', 'aviso', 'Terminó con cuarentena o avisos en ' + reloj() + '. ' + listo.replace(/\s*\(done\)\s*$/, '') + '. Revisa el reporte antes de enviar.');
      else mensaje('#estado-anon', 'error', 'Terminó con error. Revisa la salida.');
      if (t.reporte) { $('#det-reporte').classList.remove('oculto'); texto('#reporte-anon', t.reporte); }
      $('#btn-anonimizar').disabled = false; ACTUAL = null; $('#visor').classList.add('oculto'); SERIES = []; cargarEstudios(1);
    };
    esperar();
  } catch (err) { mensaje('#estado-anon', 'error', err.message); $('#btn-anonimizar').disabled = false; }
};
let SERIES = [], ACTUAL = null, ADSEQ = 0;
function demografiaCorta(d) {
  if (!d) return '';
  const sexo = d.sexo.estado === 'registrado' ? d.sexo.valor : (d.sexo.estado === 'conflicto' ? 'sexo en conflicto' : 'sexo no registrado');
  const e = d.edad_en_el_estudio;
  const edad = e.estado === 'calculada' ? e.texto : (e.estado === 'informada' ? e.texto + ' (informada)' : 'edad no disponible');
  return sexo + ' · ' + edad;
}
function avisosDemografia(d) {
  if (!d) return [];
  const e = d.edad_en_el_estudio, out = [];
  if (e.estado === 'no_disponible') out.push('Edad no disponible: ' + e.motivo);
  return out.concat(e.avisos || [], d.sexo.avisos || []);
}
function pintarDemografia(selEtiqueta, selAvisos, d) {
  texto(selEtiqueta, d ? d.etiqueta : '');
  const avisos = avisosDemografia(d);
  mensaje(selAvisos, d && (d.sexo.estado === 'conflicto' || avisos.length) ? 'aviso' : 'neutro', avisos.length ? avisos.join('. ') + '.' : '');
}
async function cargarEstudios(refrescar) {
  SERIES = await api('estudios' + (refrescar ? '?refrescar=1' : ''));
  const tb = $('#tabla-series tbody'); tb.textContent = '';
  texto('#resumen-estudios', SERIES.length + ' series, ' + new Set(SERIES.map(s => s.paciente)).size + ' paciente(s)');
  for (const s of SERIES) {
    const tr = document.createElement('tr'); tr.className = 'fila-serie';
    celda(tr, s.paciente); celda(tr, demografiaCorta(s.demografia)); celda(tr, s.estudio); celda(tr, s.serie); celda(tr, s.modalidad); celda(tr, s.descripcion); celda(tr, s.n, 'num');
    celda(tr, s.filas + '×' + s.columnas); const tdp = document.createElement('td'); tdp.appendChild(insignia(s.politica ? 'info' : 'error', s.politica || 'sin política')); tr.appendChild(tdp);
    tr.dataset.id = s.id; tr.onclick = () => abrir(s); tb.appendChild(tr);
  }
  if (ACTUAL) { const fila = tb.querySelector('tr[data-id="' + ACTUAL.id + '"]'); if (fila) fila.classList.add('seleccionada'); else { ACTUAL = null; $('#visor').classList.add('oculto'); } }
}
$('#btn-refrescar').onclick = () => cargarEstudios(1);
function abrir(s) {
  pintarDemografia('#etiqueta-estudio', '#aviso-demografia', s.demografia);
  texto('#nota-orden', 'Cortes ordenados por ' + (s.orden_criterio || 'InstanceNumber') + '.');
  mensaje('#aviso-orden', s.orden_criterio === 'desempate determinista' ? 'aviso' : 'neutro', s.orden_aviso ? 'Orden de los cortes: ' + s.orden_aviso + '.' : '');
  ACTUAL = s; $('#visor').classList.remove('oculto');
  document.querySelectorAll('#tabla-series tr.fila-serie').forEach(tr => tr.classList.toggle('seleccionada', tr.dataset.id === s.id));
  limpiarFormularioRevision(); cargarRevision(s.paciente); const d = $('#deslizador'); d.max = s.n - 1; d.value = Math.floor(s.n / 2);
  $('#antes-despues').classList.add('oculto'); $('#img-original').removeAttribute('src'); $('#img-limpio').removeAttribute('src'); $('#destacados').textContent = ''; $('#detalle-cambios').textContent = ''; texto('#ad-estado', '');
  $('#salida-comprobar').classList.add('oculto'); texto('#salida-comprobar', '');
  $('#ventana').value = s.modalidad === 'PT' ? 'auto' : 'tejido';
  document.querySelectorAll('#botones-comprobar button[data-que=suv], #botones-comprobar button[data-que=comparar_suv]').forEach(b => { b.disabled = s.modalidad !== 'PT'; b.title = s.modalidad === 'PT' ? '' : 'Solo para series PET: elige la serie PT de este paciente'; });
  const m = $('#meta'); m.textContent = '';
  for (const [k, v] of [['Paciente', s.paciente], ['Estudio', s.estudio], ['Serie', s.serie], ['Modalidad', s.modalidad], ['Descripción', s.descripcion], ['Protocolo', s.protocolo],
       ['Matriz', s.filas + '×' + s.columnas], ['Espaciado (mm)', s.espaciado ? s.espaciado.join(' × ') : ''], ['Grosor (mm)', s.grosor ?? ''], ['Cortes', s.n], ['Política', s.politica]]) {
    const div = document.createElement('div'); const b = document.createElement('b'); b.textContent = k; div.appendChild(b); const val = document.createElement('span'); val.textContent = (v == null || v === '') ? '—' : String(v); div.appendChild(val); m.appendChild(div);
  }
  mostrarCorte();
}
function mostrarCorte() {
  if (!ACTUAL) return; const i = Number($('#deslizador').value); texto('#pos', 'corte ' + (i + 1) + ' de ' + ACTUAL.n);
  $('#corte').src = '/api/serie/' + encodeURIComponent(ACTUAL.id) + '/corte/' + i + '?ventana=' + encodeURIComponent($('#ventana').value) + '&t=' + encodeURIComponent(T);
  clearTimeout(mostrarCorte.t); mostrarCorte.t = setTimeout(() => antesDespues(i), 350);
}
async function antesDespues(i) {
  const seq = ++ADSEQ, serieId = ACTUAL.id, ventana = $('#ventana').value;
  const panel = $('#antes-despues'); panel.classList.remove('oculto');
  const v = encodeURIComponent(ventana), sid = encodeURIComponent(serieId);
  $('#img-limpio').src = '/api/serie/' + sid + '/corte/' + i + '?ventana=' + v + '&t=' + encodeURIComponent(T);
  try {
    const r = await api('serie/' + sid + '/cambios/' + i);
    if (seq !== ADSEQ || !ACTUAL || ACTUAL.id !== serieId || Number($('#deslizador').value) !== i || $('#ventana').value !== ventana) return;
    if (!r.disponible) {
      mensaje('#ad-estado', 'aviso', r.motivo); $('#img-original').removeAttribute('src'); $('#destacados').textContent = ''; $('#detalle-cambios').textContent = '';
      if (r.raiz) { const b = document.createElement('button'); b.textContent = 'Indicar dónde están ahora los originales…'; b.onclick = async () => { const x = await api('raiz', {anterior: r.raiz}); if (x.ok) antesDespues(i); else mensaje('#ad-estado', 'error', x.mensaje); }; $('#ad-estado').appendChild(b); }
      return;
    }
    $('#img-original').src = '/api/serie/' + sid + '/corte_original/' + i + '?ventana=' + v + '&t=' + encodeURIComponent(T);
    mensaje('#ad-estado', 'info', 'Misma ventana en ambos lados. Los valores originales nunca se muestran; aquí solo aparece qué se hizo con cada etiqueta. Es una comprobación de metadatos y de bytes de píxeles; la revisión de píxeles es aparte.');
    const d = r.destacados, s = r.resumen, n = k => s[k] || 0;
    const tarjetas = [['Nombre del paciente', 'oculto → ' + d.nombre], ['ID del paciente', 'oculto → ' + d.id], ['Fecha de nacimiento', 'oculta → ' + d.fecha_nacimiento],
      ['Píxeles (bytes de PixelData)', d.pixeles_identicos ? 'idénticos al original, SHA-256' : 'DISTINTOS al original'], ['Fechas', n('fecha desplazada') + ' desplazadas, horas intactas'],
      ['UID', n('UID reescrito') + ' reescritos'], ['Etiquetas eliminadas', (n('eliminado') + n('privada eliminada')) + ' (' + n('privada eliminada') + ' privadas)'],
      ['Sin cambio', r.sin_cambio + ' etiquetas técnicas'], ['Política', d.politica]];
    const cont = $('#destacados'); cont.textContent = '';
    for (const [k, val] of tarjetas) { const div = document.createElement('div'); div.className = 'tarjeta'; const b = document.createElement('b'); b.textContent = k; div.appendChild(b); div.appendChild(document.createTextNode(val)); cont.appendChild(div); }
    const det = $('#detalle-cambios'); det.textContent = '';
    for (const [accion, lista] of Object.entries(r.detalle)) { const div = document.createElement('div'); div.style.display = 'block'; const b = document.createElement('b'); b.textContent = accion + ' (' + lista.length + '): '; div.appendChild(b); div.appendChild(document.createTextNode(lista.join(', '))); det.appendChild(div); }
  } catch (err) { mensaje('#ad-estado', 'error', err.message); }
}
$('#deslizador').oninput = mostrarCorte; $('#ventana').onchange = mostrarCorte;
async function cargarPacientes() {
  if (!SERIES.length) SERIES = await api('estudios');
  const pacs = [...new Set(SERIES.map(s => s.paciente))]; const cont = $('#lista-pacientes'); cont.textContent = '';
  if (!pacs.length) { texto('#lista-pacientes', 'Aún no hay estudios limpios.'); return; }
  for (const p of pacs) {
    const lab = document.createElement('label'); const cb = document.createElement('input'); cb.type = 'checkbox'; cb.value = p;
    lab.appendChild(cb); lab.appendChild(document.createTextNode(' ' + p + ' (' + SERIES.filter(s => s.paciente === p).length + ' series) '));
    const est = insignia('neutro', 'consultando revisión…'); est.dataset.paciente = p; lab.appendChild(est); cont.appendChild(lab);
    api('revision/' + encodeURIComponent(p)).then(r => { const el = cont.querySelector('.insignia[data-paciente="' + p.replace(/"/g, '') + '"]'); if (!el) return; const ok = !!r.lista_para_compartir; el.textContent = ok ? '✓ revisión de píxeles vigente, sin hallazgos' : '! revisión de píxeles pendiente'; el.className = 'insignia ' + (ok ? 'ok' : 'aviso'); }).catch(() => { const el = cont.querySelector('.insignia[data-paciente="' + p.replace(/"/g, '') + '"]'); if (el) el.textContent = 'estado de revisión no disponible'; });
  }
}
const elegidos = () => [...document.querySelectorAll('#lista-pacientes input:checked')].map(i => i.value);
$('#btn-verificar').onclick = async () => {
  const o = $('#salida-enviar'); o.classList.remove('oculto');
  try { const r = await api('verificar', {pacientes: elegidos()}); const malos = Object.values(r).filter(v => v.codigo !== 0).length;
    mensaje('#estado-enviar', malos ? 'error' : 'ok', malos ? 'Compuerta: ' + malos + ' paciente(s) con problemas. Esto es la verificación de metadatos; la revisión de píxeles es aparte.' : 'Compuerta: metadatos en regla en todos los pacientes elegidos. Esto no es la revisión de píxeles ni significa listo para compartir.');
    texto('#salida-enviar', Object.entries(r).map(([p, v]) => p + ': ' + (v.codigo === 0 ? 'LIMPIO' : 'PROBLEMAS') + '\n' + v.salida).join('\n')); }
  catch (err) { mensaje('#estado-enviar', 'error', err.message); }
};
$('#btn-empaquetar').onclick = async () => {
  const o = $('#salida-enviar'); o.classList.remove('oculto'); mensaje('#estado-enviar', 'neutro', 'verificando y empaquetando…'); texto('#salida-enviar', '');
  try { const r = await api('empaquetar', {pacientes: elegidos()}); const pend = r.revision_pixeles_pendiente || [];
    const sinOrden = r.orden_sin_verificar || [];
    const notaOrden = sinOrden.length ? ' Orden de cortes no verificado en ' + sinOrden.length + ' serie(s): la numeración de esos archivos es un desempate, no un orden anatómico.' : '';
    mensaje('#estado-enviar', (pend.length || sinOrden.length) ? 'aviso' : 'ok', (pend.length ? 'Paquete creado localmente, con revisión de píxeles pendiente para ' + pend.join(', ') + '. Existe, pero no está listo para compartirse.' : 'Paquete creado localmente con revisión de píxeles vigente y sin hallazgos en todos los pacientes.') + notaOrden);
    texto('#salida-enviar', r.ruta + '\n' + r.archivos + ' archivos de ' + r.pacientes.join(', ') + '\nIncluye manifiesto.json con el hash de cada archivo y el estado de la revisión. El mapa privado no entra.'); }
  catch (err) { mensaje('#estado-enviar', 'error', 'Rechazado: ' + err.message); texto('#salida-enviar', err.datos && err.datos.fallos ? Object.entries(err.datos.fallos).map(([p, s]) => p + ': ' + (Array.isArray(s) ? s.join('; ') : s)).join('\n') : ''); }
};
document.querySelectorAll('#botones-comprobar button').forEach(b => b.onclick = async () => {
  if (!ACTUAL) return; const o = $('#salida-comprobar'); o.classList.remove('oculto'); texto('#salida-comprobar', 'corriendo ' + b.dataset.que + '…');
  try { const r = await api('comprobar', {que: b.dataset.que, paciente: ACTUAL.paciente, serie: ACTUAL.id}); texto('#salida-comprobar', r.salida); }
  catch (err) { texto('#salida-comprobar', err.message); }
});
function pintarRevision(r) {
  if (r.lista_para_compartir) mensaje('#rev-estado', 'ok', 'Revisión de píxeles: sin hallazgos, vigente. Registrada el ' + r.fecha + (r.revisor ? ' por ' + r.revisor : '') + ' sobre ' + r.archivos_revisados + ' archivos. La compuerta de metadatos se vuelve a comprobar al empaquetar.');
  else if (r.estado === 'con_hallazgos' && r.vigente) mensaje('#rev-estado', 'error', 'Revisión de píxeles: con hallazgos (' + (r.hallazgos || []).join(', ') + ') el ' + r.fecha + '. Requiere tratamiento antes de compartir.');
  else mensaje('#rev-estado', 'aviso', 'Revisión de píxeles pendiente: ' + (r.detalle || ''));
}
let REVSEQ = 0;
function limpiarFormularioRevision() { REVSEQ++; ['#rev-texto', '#rev-rostro', '#rev-otro'].forEach(s => { $(s).checked = false; }); $('#rev-notas').value = ''; mensaje('#rev-estado', 'neutro', 'cargando…'); $('#rev-sin').disabled = $('#rev-con').disabled = true; }
function hallazgosMarcados() { return [['#rev-texto', 'texto_grabado'], ['#rev-rostro', 'rostro'], ['#rev-otro', 'otro']].filter(([s]) => $(s).checked).map(([, v]) => v); }
function actualizarBotonesRevision() { if (REV_GUARDANDO) return; const h = hallazgosMarcados().length, rev = $('#rev-revisor').value.trim().length > 0; $('#rev-sin').disabled = h > 0 || !rev; $('#rev-con').disabled = h === 0 || !rev; }
['#rev-texto', '#rev-rostro', '#rev-otro'].forEach(s => { $(s).onchange = actualizarBotonesRevision; }); $('#rev-revisor').oninput = actualizarBotonesRevision;
async function cargarRevision(pac) {
  const seq = ++REVSEQ;
  const vigente = () => seq === REVSEQ && ACTUAL && ACTUAL.paciente === pac;
  try { const r = await api('revision/' + encodeURIComponent(pac)); if (vigente()) pintarRevision(r); }
  catch (err) { if (vigente()) mensaje('#rev-estado', 'error', err.message); }
  finally { if (vigente()) actualizarBotonesRevision(); }
}
let REV_GUARDANDO = false;
async function registrarRevision(estado) {
  if (!ACTUAL || REV_GUARDANDO) return;
  const pac = ACTUAL.paciente, seq = ++REVSEQ;
  const hallazgos = hallazgosMarcados(), revisor = $('#rev-revisor').value.trim();
  if (!revisor) { mensaje('#rev-estado', 'aviso', 'indica quién revisa'); return; }
  if (estado === 'sin_hallazgos' && hallazgos.length) { mensaje('#rev-estado', 'aviso', 'hay hallazgos marcados: el resultado no puede ser sin hallazgos'); return; }
  REV_GUARDANDO = true; $('#rev-sin').disabled = $('#rev-con').disabled = true; mensaje('#rev-estado', 'neutro', 'guardando…');
  const vigente = () => seq === REVSEQ && ACTUAL && ACTUAL.paciente === pac;   // misma petición, misma selección
  try { const r = await api('revision', {paciente: pac, estado, hallazgos, revisor, notas: $('#rev-notas').value.trim()}); if (vigente()) pintarRevision(r); }
  catch (err) { if (vigente()) mensaje('#rev-estado', 'error', err.message); }
  finally { REV_GUARDANDO = false; if (vigente()) actualizarBotonesRevision(); }
}
$('#rev-sin').onclick = () => registrarRevision('sin_hallazgos'); $('#rev-con').onclick = () => registrarRevision('con_hallazgos');
async function cargarRegistro() { texto('#texto-registro', (await api('registro')).join('\n')); }
estado();
"""


app = FastAPI(title="PET/CT local", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(api)


@app.middleware("http")
async def cabeceras(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/", response_class=HTMLResponse)
def inicio(request: Request):
    auth(request)
    return HTML


@app.get("/static/app.css")
def css():
    # código sin datos: público para que el navegador pueda cargarlo; la página y la API exigen el token
    return PlainTextResponse(CSS, media_type="text/css; charset=utf-8")


@app.get("/static/app.js")
def js():
    return PlainTextResponse(JS, media_type="text/javascript; charset=utf-8")


def main() -> None:
    global TRABAJO
    p = argparse.ArgumentParser(description="Aplicación local PET/CT (local PET/CT app)")
    p.add_argument("--trabajo", help="carpeta de trabajo, por defecto ~/datos-pet (work folder)")
    p.add_argument("--puerto", type=int, default=8765)
    p.add_argument("--sin-navegador", action="store_true")
    p.add_argument("--permitir-sincronizada", action="store_true", help="arrancar aunque la carpeta de trabajo esté sincronizada (no recomendado)")
    args = p.parse_args()
    if args.trabajo:
        TRABAJO = Path(args.trabajo).expanduser().resolve()
    marca = en_sincronizada(TRABAJO)
    if marca and not args.permitir_sincronizada:
        print(f"La carpeta de trabajo {TRABAJO} está dentro de una carpeta sincronizada ({marca}). Usa --trabajo con otra ruta. "
              f"(Work folder is cloud-synced; choose another.)")
        sys.exit(2)
    TRABAJO.mkdir(parents=True, exist_ok=True)
    os.chmod(TRABAJO, 0o700)
    priv = carpeta_privada()
    priv.mkdir(exist_ok=True)
    os.chmod(priv, 0o700)
    url = f"http://127.0.0.1:{args.puerto}/?t={TOKEN}"
    print(f"PET/CT local {VERSION}")
    print(f"Carpeta de trabajo (work folder): {TRABAJO}")
    # La dirección se imprime cuando el índice está hecho: antes de eso el servidor todavía no escucha, y abrirla da
    # "conexión rechazada". Con miles de archivos el índice tarda unos segundos.
    print("Preparando el índice de estudios; con muchos archivos tarda unos segundos... (indexing studies)", flush=True)
    indexar()
    print(f"Listo. Abre en el navegador (open in your browser): {url}", flush=True)
    registrar(f"app {VERSION} iniciada en 127.0.0.1:{args.puerto}")
    if not args.sin_navegador:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.puerto, log_level="warning")


if __name__ == "__main__":
    main()
