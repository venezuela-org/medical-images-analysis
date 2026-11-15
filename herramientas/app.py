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
from collections import Counter
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

VERSION = "0.10.0"
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
                   "PatientSex", "PatientAge", "NumberOfFrames"]


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
    cuadros = _entero_valido(ds.get("NumberOfFrames", None))
    return {"cuadros": cuadros if cuadros and cuadros > 1 else 1,
            "numero": _entero_valido(ds.get("InstanceNumber", None)), "ipp": flotantes("ImagePositionPatient", 3),
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
                d = {"numero": None, "ipp": None, "iop": None, "fase": None, "varias_fases": False, "cuadros": 1}
        items.append({**d, "ruta": a})
    numeros = [i["numero"] for i in items]
    if items and all(n is not None for n in numeros) and len(set(numeros)) == len(numeros):
        return {"orden": [i["ruta"] for i in sorted(items, key=lambda i: i["numero"])], "criterio": CRITERIO_NUMERO, "aviso": None,
                "cuadros": {i["ruta"].name: i.get("cuadros", 1) for i in items}}
    motivo = "InstanceNumber ausente en algún archivo" if any(n is None for n in numeros) else "InstanceNumber repetido"
    proy = _proyecciones(items)
    if proy is not None:
        pares = sorted(zip(proy, range(len(items))))
        return {"orden": [items[k]["ruta"] for _, k in pares], "criterio": CRITERIO_POSICION,
                "aviso": f"{motivo}; cortes ordenados por posición a lo largo de la normal del plano",
                "cuadros": {i["ruta"].name: i.get("cuadros", 1) for i in items}}
    def clave(i: dict):
        return (i["numero"] is None, i["numero"] or 0, i["fase"] is None, i["fase"] or 0, i["ruta"].name)
    return {"orden": [i["ruta"] for i in sorted(items, key=clave)], "criterio": CRITERIO_DESEMPATE,
            "aviso": f"{motivo} y la posición no describe una pila única; el orden de los cortes es un desempate determinista, no un orden anatómico verificado",
            "cuadros": {i["ruta"].name: i.get("cuadros", 1) for i in items}}


CANDADO_ESTUDIOS = threading.Lock()


def numeros_de_estudio(estudios: dict[str, set[str]] | None = None) -> tuple[dict[str, dict[str, int]], str | None]:
    """El número de cada estudio dentro de su paciente: {paciente: {carpeta del estudio: número}}.

    Es un número de orden para las personas ("Estudio 1"), no un dato del DICOM. Se asigna una sola vez, la primera vez
    que la aplicación ve el estudio, y no cambia después: el mismo estudio lleva el mismo número en todos los paquetes
    hechos con esta carpeta de trabajo. El orden es el de llegada a la aplicación, que puede no ser el de las fechas.
    Se guarda en limpios_PRIVADO/estudios.json y no sale de la computadora; al paquete solo va el número.

    Con `estudios` ({paciente: {carpetas}}) se numeran los que falten. Si el registro existe y no se puede leer, no se
    toca y no se numera nada: se devuelve el motivo, y quien llama usa los nombres internos."""
    ruta = carpeta_privada() / "estudios.json"
    with CANDADO_ESTUDIOS:
        registro: dict[str, dict[str, int]] = {}
        if ruta.exists():
            try:
                datos = json.loads(ruta.read_text(encoding="utf-8"))
                registro = datos["pacientes"]
                if not isinstance(registro, dict) or any(
                        not isinstance(v, dict) or any(not isinstance(n, int) or isinstance(n, bool) or n < 1 for n in v.values())
                        or len(set(v.values())) != len(v) for v in registro.values()):
                    raise ValueError("estructura")
            except (OSError, ValueError, KeyError, TypeError):
                return {}, "el registro de números de estudio (estudios.json) no se puede leer"
        nuevos = False
        for pac in sorted(estudios or {}):
            del_paciente = registro.setdefault(pac, {})
            for carpeta in sorted(estudios[pac]):
                if carpeta not in del_paciente:
                    del_paciente[carpeta] = max(del_paciente.values(), default=0) + 1
                    nuevos = True
            if not del_paciente:
                registro.pop(pac)
        if nuevos:
            try:
                carpeta_privada().mkdir(parents=True, exist_ok=True)
                _escribir_json(ruta, {"version": 1, "pacientes": registro,
                                      "nota": "Número de orden de cada estudio dentro de su paciente, por orden de llegada. No sale de esta computadora."})
            except OSError as e:
                return {}, f"el registro de números de estudio no se pudo guardar ({type(e).__name__})"
        return registro, None


EDAD_EN_CARPETA = {"años": "a", "meses": "m", "semanas": "sem", "días": "d"}


def edad_para_carpeta(edad: dict) -> str | None:
    """La edad en el estudio, en la forma corta que va en el nombre de la carpeta: 37a, 8m, 3sem, 12d, 90a-o-mas.
    Solo cuando el estudio tiene una edad; una edad informada por el equipo, y no calculada, lo dice."""
    if edad.get("estado") not in ("calculada", "informada") or edad.get("valor") is None:
        return None
    corta = f"{ANON.EDAD_AGRUPADA_DESDE}a-o-mas" if edad.get("agrupada") else f"{int(edad['valor'])}{EDAD_EN_CARPETA.get(edad.get('unidad'), '')}"
    if not edad.get("agrupada") and edad.get("unidad") not in EDAD_EN_CARPETA:
        return None
    return corta + ("-informada" if edad["estado"] == "informada" else "")


def carpetas_exportadas(rels: list[str], demografia: list[dict] | None = None) -> tuple[dict[str, str], str | None]:
    """De 'PAC/estudio' (nombres internos) a la carpeta que lleva el paquete, y el motivo si no se pudo numerar.

      ANON-0002/estudio_7b7191db84  ->  ANON-0002_F/estudio_01_37a

    - El paciente lleva el sexo registrado cuando todos sus estudios del paquete registran el mismo; si falta o hay
      conflicto, lleva solo el identificador.
    - El estudio lleva su número dentro del paciente y, cuando la tiene, la edad en ese estudio. La edad es del estudio:
      el mismo paciente puede tener estudios con edades distintas.
    Son nombres de presentación del paquete. Los archivos limpios guardados, sus nombres internos y los DICOM no cambian."""
    demografia = demografia_de(rels) if demografia is None else demografia
    por_estudio = {f"{d['paciente']}/{d['estudio']}": d for d in demografia}
    claves = sorted({"/".join(rel.split("/")[:2]) for rel in rels})
    pedidos: dict[str, set[str]] = {}
    for clave in claves:
        pac, est = clave.split("/")
        pedidos.setdefault(pac, set()).add(est)
    numeros, problema = numeros_de_estudio(pedidos)
    sexos: dict[str, set] = {}
    for clave in claves:
        sexo = (por_estudio.get(clave) or {}).get("sexo") or {}
        sexos.setdefault(clave.split("/")[0], set()).add(sexo.get("valor") if sexo.get("estado") == "registrado" else None)
    out = {}
    for clave in claves:
        pac, est = clave.split("/")
        de_este = sexos[pac]
        paciente = f"{pac}_{next(iter(de_este))}" if len(de_este) == 1 and None not in de_este else pac
        numero = (numeros.get(pac) or {}).get(est)
        if numero is None:
            estudio = est                                             # sin número: el nombre interno, tal cual
        else:
            edad = edad_para_carpeta((por_estudio.get(clave) or {}).get("edad_en_el_estudio") or {})
            estudio = f"estudio_{numero:02d}" + (f"_{edad}" if edad else "")
        out[clave] = f"{paciente}/{estudio}"
    if len(set(out.values())) != len(out):                            # dos estudios no pueden compartir carpeta
        return {clave: clave for clave in claves}, "los nombres de carpeta no eran únicos"
    return out, problema


def mapa_exportacion(rels: list[str], demografia: list[dict] | None = None) -> tuple[dict[str, str], dict[str, dict]]:
    """De rutas internas (relativas a limpios/, con '/') a rutas exportadas, serie por serie:
    PAC_sexo/estudio_NN_edad/serie/corte_000001_<nombre interno>.dcm. La numeración de los cortes es independiente en
    cada serie y se calcula solo sobre los archivos recibidos; las carpetas de paciente y de estudio las da
    carpetas_exportadas(). Devuelve también el criterio y el aviso de cada serie, por su ruta interna."""
    limpios = (TRABAJO / "limpios").resolve()
    carpetas, _ = carpetas_exportadas(rels, demografia)
    por_serie: dict[str, list[str]] = {}
    for rel in rels:
        por_serie.setdefault(rel.rsplit("/", 1)[0], []).append(rel)
    mapa, info = {}, {}
    for serie in sorted(por_serie):
        o = ordenar_serie([limpios / r for r in por_serie[serie]])
        ancho = max(6, len(str(len(o["orden"]))))
        pac, est, nombre_serie = serie.split("/")
        exportada = f"{carpetas[f'{pac}/{est}']}/{nombre_serie}"
        for k, a in enumerate(o["orden"], 1):
            mapa[f"{serie}/{a.name}"] = f"{exportada}/corte_{k:0{ancho}d}_{a.stem}.dcm"
        info[serie] = {"criterio": o["criterio"], "aviso": o["aviso"], "archivos": len(o["orden"]),
                       "cuadros": [o["cuadros"].get(a.name, 1) for a in o["orden"]]}
    return mapa, info


CARPETA_PACIENTE_RE = re.compile(r"^(?P<pac>[A-Z0-9]{2,12}-\d{4})(_[FMO])?$")
CARPETA_ESTUDIO_RE = re.compile(r"^estudio_\d{2,}(_(\d{1,3}(a|m|sem|d)|\d{2,3}a-o-mas)(-informada)?)?$")


def exportacion_coherente(internos: list[str], exportados: list[str]) -> bool:
    """Lo que el empaquetador exige de una correspondencia de nombres antes de usarla, venga de donde venga:
      - cada archivo va a PACIENTE/ESTUDIO/SERIE/archivo.dcm;
      - la carpeta de la serie es la interna, sin cambios;
      - la del paciente es su identificador, solo o seguido del sexo (ANON-0002 o ANON-0002_F);
      - la del estudio es la interna o estudio_NN con la edad opcional (estudio_01, estudio_01_37a);
      - cada paciente, cada estudio y cada serie internos van siempre a la misma carpeta, y dos distintos nunca a la misma."""
    a_exportado: dict[str, str] = {}
    a_interno: dict[str, str] = {}
    for rel, exp in zip(internos, exportados):
        i, e = rel.split("/"), exp.split("/")
        if len(i) != 4 or len(e) != 4 or not exp.endswith(".dcm") or e[2] != i[2]:
            return False
        m = CARPETA_PACIENTE_RE.match(e[0])
        if not m or m.group("pac") != i[0]:
            return False
        if e[1] != i[1] and not CARPETA_ESTUDIO_RE.match(e[1]):
            return False
        for nivel in (1, 2, 3):                                        # paciente, estudio, serie
            interna, exportada = "/".join(i[:nivel]), "/".join(e[:nivel])
            if a_exportado.setdefault(interna, exportada) != exportada or a_interno.setdefault(exportada, interna) != interna:
                return False
    return True


def carpetas_de(exportacion: dict[str, str]) -> dict[str, dict[str, str]]:
    """Lo que una correspondencia ya fijada dice de las carpetas: paciente, estudio y serie internos -> exportados."""
    out: dict[str, dict[str, str]] = {"pacientes": {}, "estudios": {}, "series": {}}
    for interno, exportado in exportacion.items():
        i, e = interno.split("/"), exportado.split("/")
        out["pacientes"][i[0]] = e[0]
        out["estudios"]["/".join(i[:2])] = "/".join(e[:2])
        out["series"]["/".join(i[:3])] = "/".join(e[:3])
    return out


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
    archivos_por_estudio: dict[str, int] = {}
    for rel in rels:
        observaciones = ANON.observaciones_de_registro(registro.get(rel))        # todas las de sus originales, sin elegir
        if not observaciones:
            obs = (de_los_limpios or {}).get(rel)
            if obs is None:
                try:
                    obs = ANON.observacion_de_archivo_limpio(pydicom.dcmread(limpios / rel, stop_before_pixels=True, specific_tags=["PatientSex", "PatientAge"]))
                except Exception:
                    obs = {"e": ["n", "archivo ilegible"], "i": None, "s": ""}
            observaciones = [obs]
        clave = "/".join(rel.split("/")[:2])
        por_estudio.setdefault(clave, []).extend(observaciones)
        archivos_por_estudio[clave] = archivos_por_estudio.get(clave, 0) + 1
    out = []
    for clave in sorted(por_estudio):
        paciente, estudio = clave.split("/")
        resumen = ANON.resumen_demografia(por_estudio[clave])
        out.append({"paciente": paciente, "estudio": estudio, "etiqueta": ANON.etiqueta_demografia(paciente, resumen), **resumen,
                    "archivos": archivos_por_estudio[clave]})
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
        meta["cuadros_max"] = max(o["cuadros"].values(), default=1)
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
    # el número de cada estudio dentro de su paciente; los que llegan por primera vez se numeran aquí
    en_disco: dict[str, set[str]] = {}
    for s_ in INDICE.values():
        en_disco.setdefault(s_["paciente"], set()).add(s_["estudio"])
    numeros, _ = numeros_de_estudio(en_disco)
    for s_ in INDICE.values():
        s_["estudio_numero"] = (numeros.get(s_["paciente"]) or {}).get(s_["estudio"])
    registrar(f"índice: {len(INDICE)} series")


def render_corte(ruta: str, ventana: str) -> bytes:
    """PNG de un corte leído de disco. Para mostrar bytes ya verificados, usar render_bytes() con esos mismos bytes."""
    return render_bytes(Path(ruta).read_bytes(), ventana)


class ImagenNoMostrable(Exception):
    """Los bytes son un DICOM aceptado, pero el visor no puede dibujar esa imagen. El motivo no lleva datos del paciente."""


def _reescala(ds, cuadro: int) -> tuple[float, float]:
    """Pendiente y ordenada de la escala: del nivel superior o, en objetos Enhanced, de los grupos funcionales."""
    def de(item):
        try:
            t = item.PixelValueTransformationSequence[0]
            return float(t.get("RescaleSlope", 1) or 1), float(t.get("RescaleIntercept", 0) or 0)
        except Exception:
            return None
    if "RescaleSlope" in ds or "RescaleIntercept" in ds:
        return float(ds.get("RescaleSlope", 1) or 1), float(ds.get("RescaleIntercept", 0) or 0)
    for nombre, indice in (("PerFrameFunctionalGroupsSequence", cuadro - 1), ("SharedFunctionalGroupsSequence", 0)):
        try:
            r = de(ds[nombre][indice])
        except Exception:
            r = None
        if r:
            return r
    return 1.0, 0.0


def render_bytes(datos: bytes, ventana: str, cuadro: int = 1) -> bytes:
    """PNG de un corte a partir de los bytes exactos de su archivo DICOM. No abre ninguna ruta. En un archivo con varios
    cuadros dibuja el cuadro pedido (desde 1). Lanza ImagenNoMostrable si no puede dibujarlo."""
    ds = pydicom.dcmread(io.BytesIO(datos))
    try:
        arr = ds.pixel_array
    except Exception as e:
        raise ImagenNoMostrable(f"no se pudieron decodificar los píxeles ({type(e).__name__})")
    if int(ds.get("SamplesPerPixel", 1) or 1) != 1:
        raise ImagenNoMostrable("imagen con varias muestras por píxel (color)")
    if arr.ndim == 3:
        if not 1 <= cuadro <= arr.shape[0]:
            raise ImagenNoMostrable("cuadro fuera de rango")
        arr = arr[cuadro - 1]
    elif arr.ndim == 2:
        if cuadro != 1:
            raise ImagenNoMostrable("cuadro fuera de rango")
    else:
        raise ImagenNoMostrable(f"matriz de píxeles de {arr.ndim} dimensiones")
    arr = arr.astype(np.float32)
    pendiente, ordenada = _reescala(ds, cuadro)
    arr = arr * pendiente + ordenada
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
    try:
        Image.fromarray(img.astype(np.uint8)).save(buf, format="PNG")
    except Exception as e:
        raise ImagenNoMostrable(f"no se pudo dibujar la imagen ({type(e).__name__})")
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


def situacion_de_corrida(t: dict) -> dict:
    """Lo que consta AHORA de una corrida del modo rápido, en tres partes que no se mezclan: el procesamiento, la revisión
    de píxeles y el paquete. Se calcula en cada consulta con lo que hay en el disco (el registro de la revisión, los diarios
    de retirada, el zip); no es el recuerdo de lo que se mostró al terminar.

    No decide nada. Lee lo que ya decidieron la corrida, el registro de la revisión y las retiradas. Cuando algo no se
    puede leer, lo dice; nunca lo da por bueno."""
    pasos = t.get("pasos") or []
    cuenta = Counter(p_["estado"] for p_ in pasos)
    procesamiento = {"estado": {"corriendo": "en_curso", "ok": "terminado", "con avisos": "terminado_con_avisos", "error": "detenido"}.get(t.get("estado"), str(t.get("estado"))),
                     "paso_en_curso": next((i for i, p_ in enumerate(pasos, 1) if p_["estado"] == "corriendo"), None),
                     "paso_detenido": next((i for i, p_ in enumerate(pasos, 1) if p_["estado"] == "error"), None),
                     "correctos": cuenta["ok"], "con_aviso": cuenta["aviso"], "no_evaluados": cuenta["omitido"], "total": len(pasos)}
    base = {"procesamiento": procesamiento, "versiones": t.get("versiones") or {"aplicacion": VERSION, "politica": ANON.POLITICA},
            "comprobado": dt.datetime.now().isoformat(timespec="seconds")}
    if not t.get("zip"):
        return {**base, "revision": {"estado": "sin_paquete"}, "paquete": {"estado": "no_creado"}}

    avance = progreso_corrida(t)
    revision: dict = {"estado": "pendiente"}
    retiradas = estado_de_retiradas()
    if retiradas["bloqueo"]:                                          # no se sabe qué está retirado: nada se da por vigente
        revision = {"estado": "no_verificable", "problemas": retiradas["problemas"], "habia_registro": bool(t.get("revisado"))}
    elif t.get("revisado"):
        canonico = nombre_canonico(Path(str(t.get("registro_revision") or "")).name)
        esperada = t.get("registro_revision_sha256")
        ruta = datos = r = None
        try:
            ruta = ruta_de_registro_de_corrida(canonico) if REGISTRO_RAPIDO_RE.match(canonico) else None
            datos = ruta.read_bytes() if ruta else None
            r = json.loads(datos.decode("utf-8")) if datos is not None else None
        except (OSError, ValueError):
            pass
        # La identidad es la que se escribió ANTES de publicar, nunca la de un archivo sustituto.
        try:
            diario = retirada_que_cubre("corrida", canonico, esperada) if esperada else None
        except RetiradasNoVerificables as e:
            diario, revision = None, {"estado": "no_verificable", "problemas": e.problemas, "habia_registro": True}
        if revision["estado"] != "no_verificable":
            retirada = diario["retirada"] if diario else t.get("revision_retirada")
            valida = (isinstance(r, dict) and bool(esperada) and datos is not None
                      and hashlib.sha256(datos).hexdigest() == esperada
                      and r.get("estado") == "sin_hallazgos" and r.get("hallazgos") == []
                      and bool(r.get("revisor")) and bool(r.get("fecha"))
                      and r.get("archivos") == t.get("archivos") and r.get("pacientes") == t.get("pacientes"))
            if retirada:
                declaracion = t.get("revision_declarada") or {}
                revision = {"estado": "retirada", "registro": canonico, "retirada": retirada,
                            "revisor": declaracion.get("revisor"), "fecha": declaracion.get("fecha"),
                            "mostrado": declaracion.get("mostrado"), "declaracion": "sin_hallazgos",
                            "inventario_de_paquetes": resumen_de_inventario(diario) if diario else None}
            elif not valida:
                revision = {"estado": "registro_no_legible", "registro": canonico or None}
            else:
                revision = {"estado": "registrada", "revisor": r["revisor"], "fecha": r["fecha"],
                            "declaracion": "sin_hallazgos", "mostrado": r.get("mostrado"), "registro": canonico,
                            "registro_provisional": ruta.name != canonico}
    revision["cuadros_mostrados"], revision["cuadros_totales"] = avance["mostrados"], avance["total"]
    revision["no_mostrables"] = len(t.get("no_mostrables") or [])

    zip_ruta = Path(str(t["zip"]))
    existe, mismo = None, None
    try:
        existe = zip_ruta.is_file()
        if existe and t.get("zip_sha256"):
            mismo = sha256_archivo(zip_ruta) == t["zip_sha256"]
    except OSError:
        pass                                                        # presencia e integridad son comprobaciones distintas
    publicacion = t.get("publicacion") or {}
    pendientes = []
    for accion in publicacion.get("acciones", []):
        ruta = accion.get("ruta")
        if ruta:
            try:
                if not Path(ruta).exists():
                    continue
            except OSError:
                pass                                                # no ocultar una acción si no se puede comprobar
        pendientes.append(dict(accion))
    estado_zip = ("publicado" if t.get("revisado") else "pendiente_local") if mismo else (
        "modificado" if mismo is False else ("no_encontrado" if existe is False else "no_comprobable"))
    paquete = {"estado": estado_zip, "es_el_mismo_archivo": mismo,
               "ruta": str(zip_ruta), "existe": existe, "con_revision_registrada": bool(t.get("revisado")),
               "destino": t.get("destino"), "destino_sincronizado": t.get("destino_sincronizado") or None,
               "archivos": len(t.get("archivos") or {}), "pacientes": list(t.get("pacientes") or []),
               "acciones_pendientes": pendientes, "ultimo_intento_fallido": t.get("publicacion_fallida")}
    return {**base, "revision": revision, "paquete": paquete}


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
        t["datos"] = datos_de_limpieza()                              # las cifras y los motivos, para explicar el resultado en pantalla
    out = {k: v for k, v in t.items() if k not in ("indexado", "archivos", "exportacion", "series_corrida", "servidos", "mostrados")}
    if t.get("nombre") == "rapido" and t.get("pasos"):
        out["situacion"] = situacion_de_corrida(t)
    if t.get("nombre") == "rapido" and t.get("zip"):
        out["progreso"] = progreso_corrida(t)
        ahora = estado_de_retiradas()                                 # el estado de ahora, no el de cuando terminó la corrida
        out["retiradas_no_verificables"] = ahora["problemas"] if ahora["bloqueo"] else []
    return out


@api.get("/estudios")
def estudios(request: Request, refrescar: int = 0):
    auth(request)
    if refrescar or not INDICE:
        indexar()
    return [{k: v for k, v in s.items() if k != "cortes"} for s in INDICE.values()]


@api.get("/serie/{sid}/corte/{i}")
def corte(request: Request, sid: str, i: int, ventana: str = "tejido", cuadro: int = 1):
    auth(request)
    s = INDICE.get(sid)
    if not s:
        raise HTTPException(404, "serie desconocida")
    i = max(0, min(i, s["n"] - 1))
    ruta = s["cortes"][i][1]
    try:
        datos = Path(ruta).read_bytes()                               # una sola lectura: estos bytes se dibujan y se anotan
        png = render_bytes(datos, ventana, cuadro)
    except ImagenNoMostrable as e:
        raise HTTPException(422, f"este corte no se puede mostrar: {e}")
    except Exception as e:
        raise HTTPException(500, f"no se pudo renderizar el corte: {type(e).__name__}")
    anotar_servido_estudios(s["paciente"], ruta, cuadro, hashlib.sha256(datos).hexdigest())
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@api.post("/serie/{sid}/mostrado")
def serie_mostrado(request: Request, sid: str, cuerpo: dict):
    """El navegador avisa de que un corte de Estudios ha estado en pantalla."""
    auth(request)
    s = INDICE.get(sid)
    if not s:
        raise HTTPException(404, "serie desconocida")
    try:
        i, cuadro = int(cuerpo.get("i")), int(cuerpo.get("cuadro", 1))
    except (TypeError, ValueError):
        raise HTTPException(400, "i y cuadro deben ser números")
    if not 0 <= i < s["n"]:
        raise HTTPException(404, "corte fuera de rango")
    if not anotar_mostrado_estudios(s["paciente"], s["cortes"][i][1], cuadro):
        raise HTTPException(409, "ese corte no se ha servido al visor")
    return progreso_paciente(s["paciente"])


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
    """Escritura de un registro JSON en disco; un único punto para todas las evidencias (manifiestos, revisiones y
    retiradas). Es atómica: se escribe un temporal en la misma carpeta y se renombra, así que en disco queda el registro
    anterior entero o el nuevo entero, nunca medio."""
    ruta = Path(ruta)
    temporal = ruta.with_name(f".{ruta.name}.{secrets.token_hex(3)}.tmp")
    try:
        with open(temporal, "w", encoding="utf-8") as f:
            f.write(json.dumps(datos, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporal, ruta)
    except BaseException:
        try:
            temporal.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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


class RevisionBloqueada(Exception):
    """No se puede registrar una revisión ni publicar un paquete revisado mientras el estado de las retiradas no se
    pueda verificar. Lleva los diarios que requieren atención, con su motivo."""

    def __init__(self, problemas: list[dict], accion: str, restos: list[str] | None = None):
        self.problemas, self.accion, self.restos = list(problemas), accion, list(restos or [])
        super().__init__(self.mensaje())

    def mensaje(self) -> str:
        return (f"no se puede {self.accion}: no se puede verificar el estado de las retiradas ("
                + "; ".join(f"{p['diario']}: {p['motivo']}" for p in self.problemas)
                + "). Hasta que ese diario se pueda leer no se registra ninguna revisión ni se publica ningún paquete revisado."
                + texto_restos(self.restos))

    def cuerpo(self) -> dict:
        return {"detail": self.mensaje(), "diarios_con_problemas": self.problemas, "retiradas_no_verificables": True, "restos": self.restos}


class _RevisionDejoDeValer(Exception):
    """Uso interno del empaquetador: la revisión de algún paciente dejó de valer entre el principio del empaquetado y la
    publicación (se retiró, o su registro cambió)."""

    def __init__(self, cambios: dict[str, str]):
        super().__init__("la revisión dejó de valer")
        self.cambios = cambios


class _RetiradasDejaronDeSerVerificables(Exception):
    """Uso interno del empaquetador: el estado de las retiradas dejó de poder verificarse a mitad del empaquetado."""

    def __init__(self, problemas: list[dict]):
        super().__init__("retiradas no verificables")
        self.problemas = problemas


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

    # Regla común a todos los que empaquetan: mientras el estado de las retiradas no se pueda verificar, ningún paquete
    # puede salir como revisado. Vale también para quien trae su propia revisión de corrida (el modo rápido): un paquete
    # que la declara "sin hallazgos" se rechaza antes de leer un solo archivo. Los paquetes pendientes se pueden crear, y
    # su manifiesto dice que el estado de las retiradas no se pudo verificar.
    retiradas = estado_de_retiradas()
    declara_revision_de_corrida = revision_corrida is not None and revision_corrida.get("estado") == "sin_hallazgos"
    if retiradas["bloqueo"] and declara_revision_de_corrida:
        registrar("empaquetar: rechazado un paquete revisado; el estado de las retiradas no se puede verificar: " + texto_de_problemas(retiradas["problemas"]))
        raise PaqueteRechazado(409, {"detail": "no se puede publicar un paquete revisado: no se puede verificar el estado de las retiradas ("
                                               + texto_de_problemas(retiradas["problemas"]) + "); nada se empaquetó",
                                     "diarios_con_problemas": retiradas["problemas"], "retiradas_no_verificables": True})

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
        exportacion, orden_series = mapa_exportacion(internos, demografia)
    else:
        exportacion = dict(exportacion)                    # correspondencia fijada por quien llama (una corrida del modo rápido)
        orden_series = dict(orden_series) if orden_series else None
    exportados = [exportacion.get(rel) for rel in internos]
    if set(exportacion) != set(internos) or None in exportados or len(set(exportados)) != len(exportados) \
            or not exportacion_coherente(internos, exportados):
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
    # Qué carpeta del paquete es cada paciente, cada estudio y cada serie. Sale de la correspondencia ya fijada.
    carpetas = carpetas_de(exportacion)
    numeros, _ = numeros_de_estudio()
    for d in demografia:
        d["carpeta"] = carpetas["estudios"].get(f"{d['paciente']}/{d['estudio']}")
        d["numero"] = (numeros.get(d["paciente"]) or {}).get(d["estudio"])

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
        bloque_revision = {pac: {k: r.get(k) for k in ("estado", "vigente", "fecha", "hallazgos", "detalle", "registro_sha256")} for pac, r in revisiones.items()}
        aviso_manifiesto = ("paquete creado localmente; la revisión de píxeles está pendiente para los pacientes listados" if pendientes
                            else "revisión de píxeles vigente y sin hallazgos en todos los pacientes")
    manifiesto = {"version_app": VERSION, "politica": politica, "prefijo": prefijo,
                  "creado": dt.datetime.now().isoformat(timespec="seconds"), "pacientes": pacientes,
                  "revision_pixeles": bloque_revision,
                  "revision_pixeles_pendiente": pendientes,
                  "aviso": aviso_manifiesto,
                  "archivos": [{"ruta": exp, "sha256": h, "bytes": len(datos)} for exp, datos, h, _ in entradas],
                  "nombres": "corte_NNNNNN es un índice de presentación por serie; no modifica InstanceNumber ni ningún otro atributo DICOM",
                  "carpetas": {"nota": "Nombres de presentación de las carpetas del paquete. El paciente lleva el sexo registrado cuando todos sus "
                                       "estudios del paquete registran el mismo. El estudio lleva su número de orden dentro del paciente (por orden "
                                       "de llegada a la aplicación, no necesariamente por fecha) y, cuando la tiene, la edad en ese estudio. "
                                       "No modifican ningún atributo DICOM.",
                               "pacientes": carpetas["pacientes"], "estudios": carpetas["estudios"], "series": carpetas["series"]},
                  "retiradas": {"verificables": not retiradas["bloqueo"], "diarios_con_problemas": retiradas["problemas"],
                                "nota": ("El estado de las retiradas de revisión no se pudo verificar al crear este paquete: ninguna revisión "
                                         "se da por vigente y el paquete no está revisado." if retiradas["bloqueo"] else
                                         "El estado de las retiradas de revisión se pudo verificar al crear este paquete.")},
                  "demografia": {"nota": "Por estudio: identificador anónimo, sexo registrado en el DICOM (no se infiere) y edad en la fecha del "
                                         "estudio, calculada antes de anonimizar las fechas. No se exportan la fecha de nacimiento ni la fecha "
                                         "original del estudio. 90 años o más se agrupan, como en PatientAge.",
                                 "estudios": demografia},
                  "orden_de_cortes": {carpetas["series"].get(serie, serie): ({"criterio": o["criterio"], "aviso": o["aviso"]} if o else {"criterio": "fijado en la corrida"})
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

        # Antes de dejar ninguna evidencia: si este paquete va a salir como revisado, el estado de las retiradas tiene que
        # seguir pudiéndose verificar ahora, no solo cuando empezó el empaquetado.
        declara_revision = declara_revision_de_corrida or (revision_corrida is None and len(pendientes) < len(pacientes))

        def comprobar_que_la_revision_sigue_valiendo() -> None:
            """Lo que el paquete va a declarar tiene que seguir siendo cierto ahora, y no solo cuando empezó el empaquetado:
            el estado de las retiradas se puede verificar y, en un paquete de Enviar, la revisión de cada paciente que sale
            como revisado sigue siendo el mismo registro, sin retirar."""
            ahora = estado_de_retiradas()
            if declara_revision and ahora["bloqueo"]:
                raise _RetiradasDejaronDeSerVerificables(ahora["problemas"])
            if revision_corrida is None:
                cambios = {pac: motivo for pac in pacientes if pac not in pendientes
                           for motivo in [revision_sigue_siendo(pac, revisiones[pac].get("registro_sha256"))] if motivo}
                if cambios:
                    raise _RevisionDejoDeValer(cambios)

        etapa = "la comprobación del estado de las revisiones"
        comprobar_que_la_revision_sigue_valiendo()

        # 3. evidencias locales ANTES de publicar: manifiesto externo y lo que pida quien llama
        etapa = "el guardado del manifiesto local"
        _escribir_json(manifiesto_preparado, {**manifiesto, "zip": {"nombre": nombre, "destino": str(destino_dir), "sha256": huella},
                                              "correspondencia_interna": {exp: rel for exp, _, _, rel in entradas}})
        if antes_de_publicar is not None:
            etapa = "el guardado del registro previo a la publicación"
            antes_de_publicar({"nombre": nombre, "ruta": str(final), "sha256": huella, "manifiesto_local": str(manifiesto_local),
                               "hashes": {rel: h for _, _, h, rel in entradas}})

        # y otra vez justo antes del punto de compromiso, con las evidencias ya preparadas (si falla, se retiran)
        etapa = "la comprobación del estado de las revisiones"
        comprobar_que_la_revision_sigue_valiendo()

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
        if isinstance(e, _RevisionDejoDeValer):
            detalle = "; ".join(f"{pac}: {motivo}" for pac, motivo in sorted(e.cambios.items()))
            raise PaqueteRechazado(409, {"detail": "el paquete no se publicó: iba a salir como revisado y la revisión dejó de valer (" + detalle
                                                   + "). Vuelve a empaquetar: saldrá con la revisión pendiente",
                                         "revisiones_que_cambiaron": e.cambios, "restos": restos})
        if isinstance(e, _RetiradasDejaronDeSerVerificables):
            raise PaqueteRechazado(409, {"detail": "no se puede publicar un paquete revisado: el estado de las retiradas dejó de poder verificarse ("
                                                   + texto_de_problemas(e.problemas) + "); el zip no se publicó",
                                         "diarios_con_problemas": e.problemas, "retiradas_no_verificables": True, "restos": restos})
        raise PaqueteRechazado(500, {"detail": f"falló {etapa}; el zip no se publicó / package not published", "restos": restos})

    # 5. después de publicar ya no hay errores, solo avisos: la ruta publicada se conserva pase lo que pase
    avisos, temporales_no_eliminados = [], []
    try:
        os.replace(manifiesto_preparado, manifiesto_local)      # la evidencia preparada pasa a confirmada
    except OSError:
        manifiesto_local = manifiesto_preparado
        avisos.append(f"el manifiesto local quedó con su nombre provisional aunque el zip está publicado: {manifiesto_preparado}")
    if not es_local and not _eliminar(tmp):
        temporales_no_eliminados.append(str(tmp))
        avisos.append(f"no se pudo eliminar el zip temporal de la carpeta de trabajo: {tmp}")
    registrar(f"empaquetar: {nombre} con {len(entradas)} archivos de {', '.join(pacientes)}; revisión de píxeles pendiente: {', '.join(pendientes) or 'ninguno'}"
              + (f"; avisos: {len(avisos)}" if avisos else ""))
    return {"ruta": str(final), "archivos": len(entradas), "pacientes": pacientes, "revision_pixeles_pendiente": pendientes,
            "hashes": {rel: h for _, _, h, rel in entradas}, "sha256": huella, "manifiesto_local": str(manifiesto_local), "avisos": avisos,
            "temporales_no_eliminados": temporales_no_eliminados,
            "retiradas_no_verificables": retiradas["problemas"] if retiradas["bloqueo"] else [],
            "demografia": demografia, "exportacion": dict(exportacion), "orden_sin_verificar": sorted(serie for serie, o in orden_series.items() if o and o["criterio"] == CRITERIO_DESEMPATE)}


def empaquetar_para_enviar(pacientes: list[str]) -> dict:
    """El empaquetado de la pestaña Enviar, bajo el mismo candado que las retiradas, las corridas y las revisiones: mientras
    se empaqueta no se puede retirar una revisión, y al revés. El empaquetador vuelve a comprobarlo de todos modos."""
    if not CANDADO.acquire(blocking=False):
        raise HTTPException(409, "hay otro trabajo en curso; espera a que termine antes de empaquetar")
    try:
        return crear_paquete(pacientes)
    finally:
        CANDADO.release()


@api.post("/empaquetar")
def empaquetar(request: Request, cuerpo: dict):
    auth(request)
    pacientes = sorted({str(p) for p in cuerpo.get("pacientes", [])})
    if not pacientes:
        raise HTTPException(400, "elige al menos un paciente / pick at least one patient")
    try:
        r = empaquetar_para_enviar(pacientes)
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


def clasificar_suv_con_clase(codigo: int, informe: dict, salida: str) -> tuple[str, str, str]:
    """(estado, etiqueta, clase): distingue una discrepancia real de 'no se puede calcular' y de un problema de orden.
    La clase es el mismo veredicto como dato, para que la pantalla no tenga que leerlo en una frase:
    identico, discrepancia, no_calculable, correspondencia_incompleta, orden_incoherente o sin_veredicto."""
    if codigo == 0 and informe.get("veredicto") == "IDENTICO":
        return "ok", "IDENTICO", "identico"
    if informe.get("voxeles_distintos", 0) or informe.get("factores_identicos") is False or informe.get("intervalos_identicos") is False:
        return "error", f"DISCREPANCIA: {informe.get('voxeles_distintos', 0)} vóxeles distintos entre original y limpio", "discrepancia"
    textos = " ".join(informe.get("detalles", []) + informe.get("problemas_de_correspondencia", [])) + salida
    if "no calculable" in textos or "SUVbw no calculable" in textos:
        motivo = next((d.split("no calculable:")[-1].strip() for d in informe.get("detalles", []) if "no calculable" in d), "faltan atributos")
        return "aviso", f"no se puede calcular el SUV ({motivo}); no es una discrepancia", "no_calculable"
    if informe.get("correspondencia_completa") is False:
        return "aviso", "correspondencia incompleta entre original y limpio: " + "; ".join(informe.get("problemas_de_correspondencia", []))[:120], "correspondencia_incompleta"
    if informe.get("orden_y_geometria_ok") is False:
        return "aviso", "orden o geometría incoherentes en la serie: " + "; ".join(informe.get("problemas_de_orden_y_geometria", []))[:120], "orden_incoherente"
    return "aviso", "sin veredicto", "sin_veredicto"


def clasificar_suv(codigo: int, informe: dict, salida: str) -> tuple[str, str]:
    """(estado, etiqueta), como siempre. La clase la da clasificar_suv_con_clase()."""
    return clasificar_suv_con_clase(codigo, informe, salida)[:2]


def datos_de_limpieza(rels: list[str] | None = None) -> dict:
    """Las cifras de la limpieza que acaba de terminar, tomadas de su reporte (no de sus frases): cuántos archivos se
    leyeron, cuántos entran en esta corrida, cuántos se omitieron por no ser imágenes DICOM y cuántos DICOM quedaron en
    cuarentena, con sus motivos. Solo recuentos y motivos genéricos; ningún nombre de archivo."""
    try:
        r = json.loads((carpeta_reportes() / "ultimo_reporte.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        r = {}
    entero = lambda v: v if isinstance(v, int) and not isinstance(v, bool) else None
    omitidos = {str(k): v for k, v in (r.get("archivos_omitidos") or {}).items() if isinstance(v, int)} if isinstance(r.get("archivos_omitidos"), dict) else {}
    cuarentena = r.get("cuarentena") if isinstance(r.get("cuarentena"), list) else None
    motivos = Counter(str(c.get("motivo") or "sin motivo anotado") for c in (cuarentena or []) if isinstance(c, dict))
    aceptados = len(rels) if rels is not None else sum(entero(r.get(k)) or 0 for k in ("archivos_escritos", "duplicados_omitidos", "actualizados_version"))
    return {"leidos": entero(r.get("archivos_leidos")), "en_la_corrida": aceptados, "escritos": entero(r.get("archivos_escritos")),
            "ya_guardados": entero(r.get("duplicados_omitidos")), "actualizados": entero(r.get("actualizados_version")),
            "omitidos": omitidos, "omitidos_total": sum(omitidos.values()) if isinstance(r.get("archivos_omitidos"), dict) else None,
            "cuarentena_total": len(cuarentena) if cuarentena is not None else None, "cuarentena_motivos": dict(motivos),
            "pacientes": len({x.split("/")[0] for x in rels}) if rels is not None else entero(r.get("pacientes")),
            "conservar_descripciones": r.get("conservar_descripciones") if isinstance(r.get("conservar_descripciones"), bool) else None,
            "descripciones_redactadas": entero(r.get("descripciones_redactadas_aceptadas"))}


def _rapido(tid: str, origen: Path, destino: Path, prefijo: str) -> None:
    t = TRABAJOS[tid]
    pasos = {c: {"clave": c, "titulo": titulo, "estado": "pendiente", "resumen": "", "salida": "", "datos": {}} for c, titulo in PASOS_RAPIDO}
    t["pasos"] = [pasos[c] for c, _ in PASOS_RAPIDO]
    t["versiones"] = {"aplicacion": VERSION, "politica": ANON.POLITICA}
    limpios = (TRABAJO / "limpios").resolve()

    def paso(clave: str, estado: str, resumen: str = "", salida: str = "", datos: dict | None = None) -> None:
        """`datos` son las cifras y la clase del resultado, para la pantalla. No deciden nada: la decisión es `estado`."""
        pasos[clave].update(estado=estado, resumen=resumen, salida=salida[-6000:], datos=datos or {})

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
        paso("limpieza", "ok" if codigo == 0 else "aviso", listo or "limpieza terminada", salida, datos_de_limpieza(rels))
        indexar()
        archivos = [limpios / r_ for r_ in rels]
        if not rels or not all(a.is_file() for a in archivos):
            cifras = pasos["limpieza"]["datos"]
            ya_con_otra_opcion = sum(n for m, n in (cifras.get("cuarentena_motivos") or {}).items() if m == ANON.MOTIVO_OTRA_OPCION)
            if ya_con_otra_opcion:
                porque = (f"{ya_con_otra_opcion} imagen(es) de esa carpeta ya están guardadas, limpiadas con la casilla de conservar descripciones; "
                          "el modo rápido limpia sin ella y no sustituye la copia guardada")
            elif cifras.get("cuarentena_total"):
                porque = f"los {cifras['cuarentena_total']} DICOM de esa carpeta quedaron en cuarentena"
            else:
                porque = "ningún archivo de esa carpeta quedó limpio (¿son CT, PET o MR?)"
            paso("limpieza", "error", pasos["limpieza"]["resumen"], pasos["limpieza"]["salida"], cifras)
            return terminar("error", porque[0].upper() + porque[1:] + "; no se creó ningún zip.")
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
                 "\n".join(f"{r_}: {'; '.join(ps)}" for r_, ps in list(fallos.items())[:30]), {"comprobados": len(rels), "con_problemas": len(fallos)})
            return terminar("error", "La compuerta de metadatos falló; no se creó ningún zip.")
        paso("compuerta", "ok", f"metadatos en regla en los {len(rels)} archivos de esta carpeta", datos={"comprobados": len(rels), "con_problemas": 0})

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
            paso("cotejo", "error", veredicto or "el cotejo no terminó", salida, {"clase": "no_coincide", "esperados": entradas_aceptadas})
            return terminar("error", "La copia limpia no coincide con la limpieza recalculada; no se creó ningún zip.")
        n_cotejados = int(m.group(1)) if (m := re.search(r"coinciden byte a byte:\s*(\d+)", salida)) else -1
        if n_cotejados != entradas_aceptadas:
            paso("cotejo", "error", f"el cotejo cubrió {n_cotejados} archivo(s) y la limpieza de esta corrida aceptó {entradas_aceptadas}", salida,
                 {"clase": "conjunto_distinto", "coinciden": max(n_cotejados, 0), "esperados": entradas_aceptadas})
            return terminar("error", "El cotejo y la limpieza no cubren el mismo conjunto de archivos; no se creó ningún zip.")
        paso("cotejo", "ok", f"{veredicto}; {iguales}".strip("; "), salida, {"clase": "coincide", "coinciden": n_cotejados, "esperados": entradas_aceptadas})

        # 4. emparejamiento por estudio de esta corrida (informa; no bloquea)
        paso("emparejamiento", "corriendo")
        estudios_dir = sorted({a.parents[1] for a in archivos})
        textos, estados, clases = [], [], []
        for d in estudios_dir:
            codigo, salida = ejecutar(comando_herramienta("emparejar") + [str(d), "--solo-lista", str(lista_corrida)])
            linea = next((l.strip() for l in salida.splitlines() if " + PET " in l), "")
            nombre = f"{d.parent.name}/{d.name}"
            if codigo == 0:
                textos.append(f"{nombre}: {linea or 'CANDIDATO COMPATIBLE'}"); estados.append("ok"); clases.append("compatible")
            elif codigo == 1 and linea:                               # hay un par CT + PET y la herramienta lo da por inconsistente
                textos.append(f"{nombre}: PAR INCONSISTENTE: {linea}"); estados.append("aviso"); clases.append("inconsistente")
            else:                                                     # sin par que comparar: emparejar sale con 1 y no nombra ningún par
                textos.append(f"{nombre}: no evaluable (falta CT o PET, o información insuficiente)"); estados.append("omitido"); clases.append("no_evaluable")
        peor = "aviso" if "aviso" in estados else ("ok" if "ok" in estados else "omitido")
        paso("emparejamiento", peor, "; ".join(textos)[:300], "\n".join(textos),
             {"estudios": len(clases), "compatibles": clases.count("compatible"), "inconsistentes": clases.count("inconsistente"), "no_evaluables": clases.count("no_evaluable")})

        # 5. SUV original / limpio por serie PET de esta corrida
        paso("suv", "corriendo")
        series_dir = {str(a.parent.resolve()) for a in archivos}
        series_pt = [s_ for s_ in INDICE.values() if s_["modalidad"] == "PT" and str(Path(s_["cortes"][0][1]).parent.resolve()) in series_dir]
        if not series_pt:
            paso("suv", "omitido", "no hay series PET en esta carpeta", datos={"series": 0, "clases": {}})
        else:
            textos, estados, clases = [], [], []
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
                estado_s, etiqueta, clase = clasificar_suv_con_clase(codigo, informe, salida)
                textos.append(f"{s_['paciente']} {s_['serie']}: {etiqueta}"); estados.append(estado_s); clases.append(clase)
            datos_suv = {"series": len(clases), "clases": dict(Counter(clases))}
            if "error" in estados:
                paso("suv", "error", "; ".join(textos)[:300], "\n".join(textos), datos_suv)
                return terminar("error", "El SUV calculado sobre la copia limpia difiere del original; no se creó ningún zip.")
            paso("suv", "aviso" if "aviso" in estados else "ok", "; ".join(textos)[:300], "\n".join(textos), datos_suv)

        # 6. paquete: exactamente estos archivos, con la revisión pendiente, en la carpeta de trabajo local.
        #    La correspondencia de nombres exportados se calcula UNA vez, sobre los archivos de esta corrida, y se congela:
        #    el zip pendiente y el revisado llevan los mismos nombres aunque después se importe más de este paciente.
        paso("paquete", "corriendo")
        demografia_corrida = demografia_de(rels)
        exportacion, orden_series = mapa_exportacion(rels, demografia_corrida)
        t["exportacion"], t["orden_de_cortes"] = exportacion, orden_series
        t["series_corrida"] = series_congeladas(exportacion, orden_series)
        # La demografía de la corrida se calcula una vez, sobre sus archivos exactos, y se congela: el zip pendiente, el
        # revisado y el visor de la corrida muestran lo mismo aunque después se importe más de este paciente.
        t["demografia"] = demografia_corrida
        try:
            r = crear_paquete(pacientes, TRABAJO / "paquetes", solo=set(rels), exportacion=exportacion, orden_series=orden_series,
                              demografia=t["demografia"], revision_corrida={"estado": "pendiente", "detalle": "revisión de píxeles de esta corrida sin registrar"})
        except PaqueteRechazado as e:
            paso("paquete", "error", "el paquete no se creó", json.dumps(e.cuerpo, ensure_ascii=False)[:3000], {"clase": "no_creado"})
            return terminar("error", "El paquete no se creó." + texto_restos(e.cuerpo.get("restos", [])))
        t["zip"], t["archivos"], t["zip_sha256"] = r["ruta"], r["hashes"], r["sha256"]
        t["retiradas_no_verificables"] = r.get("retiradas_no_verificables", [])
        t["no_mostrables"] = []
        comprobar_que_se_puede_mostrar(t)
        t["revision_pixeles_pendiente"] = r["revision_pixeles_pendiente"]
        t["destino_sincronizado"] = en_sincronizada(destino)
        extra = "".join(f" {pac} tiene además {n} archivo(s) de importaciones anteriores que no entran en este zip." for pac, n in t["otros_archivos_del_paciente"].items())
        sin_verificar = sorted(serie for serie, o in orden_series.items() if o["criterio"] == CRITERIO_DESEMPATE)
        por_posicion = sorted(serie for serie, o in orden_series.items() if o["criterio"] == CRITERIO_POSICION)
        nota_orden = (f" Orden de cortes NO verificado en {len(sin_verificar)} serie(s): {', '.join(x.split('/')[-1] for x in sin_verificar)}." if sin_verificar else "") + \
                     (f" {len(por_posicion)} serie(s) ordenadas por posición, sin InstanceNumber utilizable." if por_posicion else "")
        varios_cuadros = sum(1 for s_ in t["series_corrida"] for n_ in s_["cuadros"] if n_ > 1)
        nota_cuadros = f" {varios_cuadros} archivo(s) tienen varios cuadros; el visor de la corrida permite recorrerlos." if varios_cuadros else ""
        nota_visor = (f" {len(t['no_mostrables'])} imagen(es) no se pueden mostrar en el visor ({t['no_mostrables'][0]['motivo']}): "
                      "la revisión de píxeles de esta corrida no se puede registrar aquí.") if t.get("no_mostrables") else ""
        nota_retiradas = (" No se puede verificar el estado de las retiradas (" + texto_de_problemas(t["retiradas_no_verificables"]) + "): la revisión de "
                          "esta corrida no se podrá registrar hasta que ese diario se pueda leer; el zip queda pendiente, en local.") if t["retiradas_no_verificables"] else ""
        paso("paquete", "aviso" if sin_verificar or t.get("no_mostrables") or t["retiradas_no_verificables"] else "ok",
             f"{r['archivos']} archivos de esta carpeta ({', '.join(pacientes)}), en la carpeta de trabajo, numerados por serie.{extra}{nota_orden}{nota_cuadros}{nota_visor}{nota_retiradas}",
             r["ruta"] + "\n" + "\n".join(f"{serie}: {o['criterio']}" + (f" ({o['aviso']})" if o["aviso"] else "") for serie, o in sorted(orden_series.items())),
             {"clase": "creado", "archivos": r["archivos"], "series": len(orden_series), "pacientes": len(pacientes),
              "orden_no_verificado": len(sin_verificar), "orden_por_posicion": len(por_posicion), "con_varios_cuadros": varios_cuadros,
              "no_mostrables": len(t.get("no_mostrables") or []), "de_importaciones_anteriores": sum(t["otros_archivos_del_paciente"].values())})
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
        interna = pares[0][1].rsplit("/", 1)[0]                       # la carpeta interna de la serie: por ella se guardó su criterio
        partes = interna.rsplit("/", 1)[1].split("_")
        o = orden_series.get(interna) or {}
        out.append({"serie": serie, "serie_interna": interna, "modalidad": partes[2] if len(partes) >= 4 else "", "n": len(pares),
                    "criterio": o.get("criterio"), "aviso": o.get("aviso"),
                    "archivos": [interno for _, interno in pares], "nombres": [exportado.rsplit("/", 1)[1] for exportado, _ in pares],
                    "cuadros": list(o.get("cuadros") or [1] * len(pares))})
    return out


def corrida_con_zip(tid: str) -> dict:
    t = TRABAJOS.get(tid)
    if not t or t.get("nombre") != "rapido" or t.get("estado") == "corriendo" or not t.get("zip"):
        raise HTTPException(404, "no hay una corrida del modo rápido con paquete para ese identificador")
    return t


# --------------------------------------------------------------------------------------
# avance de la revisión: cuadros MOSTRADOS en el navegador
# --------------------------------------------------------------------------------------
# Se cuenta un cuadro cuando el navegador avisa de que lo ha tenido en pantalla, y solo si antes se sirvió al visor por
# HTTP. Las comprobaciones automáticas de que una imagen se puede dibujar no pasan por ahí y no cuentan. "Mostrado" no
# significa "revisado": la revisión sigue siendo la declaración de quien firma. El recuento solo impide declarar
# "revisé todos los cortes" cuando no todos han llegado a estar en pantalla.

NOTA_MOSTRADO = "cuadros mostrados en el navegador; mostrado no significa revisado: la revisión es la declaración de quien firma"


def progreso_corrida(t: dict) -> dict:
    mostrados = t.get("mostrados") or set()
    series = [{"indice": i, "total": sum(s_["cuadros"]), "mostrados": sum(1 for m in mostrados if m[0] == i)}
              for i, s_ in enumerate(t.get("series_corrida") or [])]
    total = sum(x["total"] for x in series)
    return {"mostrados": len(mostrados), "total": total, "completo": total > 0 and len(mostrados) == total, "series": series, "nota": NOTA_MOSTRADO}


def anotar_mostrado_corrida(t: dict, indice: int, k: int, cuadro: int) -> dict:
    if (indice, k, cuadro) not in (t.get("servidos") or set()):
        raise HTTPException(409, "ese cuadro no se ha servido al visor de esta corrida")
    t.setdefault("mostrados", set()).add((indice, k, cuadro))
    return progreso_corrida(t)


# paciente → {"servidos": {(ruta, cuadro): sha256 de los bytes servidos}, "mostrados": {(ruta, cuadro): (sha256, firma)}}
MOSTRADOS_ESTUDIOS: dict[str, dict[str, dict]] = {}


def _firma_archivo(ruta: str) -> str:
    """Tamaño y fecha de modificación. Solo sirve para que el contador de la pantalla baje pronto cuando un archivo
    cambia; lo que autoriza una revisión es el sha256 de los bytes que se mostraron, no esta firma."""
    try:
        st = os.stat(ruta)
        return f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        return "ausente"


def anotar_servido_estudios(pac: str, ruta: str, cuadro: int, huella: str) -> None:
    MOSTRADOS_ESTUDIOS.setdefault(pac, {"servidos": {}, "mostrados": {}})["servidos"][(ruta, cuadro)] = huella


def anotar_mostrado_estudios(pac: str, ruta: str, cuadro: int) -> bool:
    """El navegador dice que tuvo ese corte en pantalla. Cuenta con el sha256 de los bytes que se le sirvieron."""
    estado = MOSTRADOS_ESTUDIOS.setdefault(pac, {"servidos": {}, "mostrados": {}})
    huella = estado["servidos"].get((ruta, cuadro))
    if huella is None:
        return False
    estado["mostrados"][(ruta, cuadro)] = (huella, _firma_archivo(ruta))
    return True


def progreso_paciente(pac: str, hashes: dict[str, str] | None = None) -> dict:
    """Cortes del paciente (toda la serie guardada, un cuadro por archivo) que han estado en pantalla en Estudios.
    Sin `hashes` es el contador de la pantalla: rápido, por tamaño y fecha, sobre el inventario cargado.
    Con `hashes` (los sha256 actuales de TODOS los archivos del paciente en disco) es la comprobación que autoriza:
      - un corte cuenta solo si los bytes que se mostraron son, por su sha256, los que el archivo tiene ahora;
      - el conjunto de archivos en disco tiene que ser exactamente el del inventario que se recorrió. Un archivo que
        llegó después del último inventario no se ha podido mostrar, y uno del inventario que ya no está tampoco vale."""
    mostrados = (MOSTRADOS_ESTUDIOS.get(pac) or {}).get("mostrados") or {}
    base = carpeta_paciente(pac).resolve() if hashes is not None else None
    series, total, vistos, en_inventario = [], 0, 0, set()
    for sid, s_ in INDICE.items():
        if s_["paciente"] != pac:
            continue
        n = 0
        for _, ruta in s_["cortes"]:
            marca = mostrados.get((ruta, 1))
            if hashes is None:
                n += marca is not None and marca[1] == _firma_archivo(ruta)
                continue
            try:
                rel = Path(ruta).resolve().relative_to(base).as_posix()
            except ValueError:
                continue
            en_inventario.add(rel)
            n += marca is not None and marca[0] == hashes.get(rel)
        series.append({"serie": sid, "total": s_["n"], "mostrados": n})
        total, vistos = total + s_["n"], vistos + n
    resultado = {"mostrados": vistos, "total": total, "series": series, "verificado_por_contenido": hashes is not None, "nota": NOTA_MOSTRADO}
    if hashes is None:
        return {**resultado, "completo": total > 0 and vistos == total}
    fuera = sorted(set(hashes) - en_inventario)                       # en disco, pero no en el inventario que se recorrió
    ausentes = sorted(en_inventario - set(hashes))                    # en el inventario, pero ya no en disco
    total = len(set(hashes) | en_inventario)
    return {**resultado, "total": total, "fuera_del_inventario": len(fuera), "ya_no_estan": len(ausentes),
            "completo": total > 0 and vistos == total and not fuera and not ausentes}


def anotar_no_mostrable(t: dict, indice: int, k: int, cuadro: int, motivo: str) -> None:
    s_ = t["series_corrida"][indice]
    entrada = {"serie": s_["serie"], "archivo": s_["nombres"][k - 1], "cuadro": cuadro, "motivo": motivo}
    if entrada not in t.setdefault("no_mostrables", []):
        t["no_mostrables"].append(entrada)


def png_de_corrida(t: dict, indice: int, k: int, ventana: str, cuadro: int = 1) -> bytes:
    """El corte k (desde 1) de la serie `indice` de la corrida, en el orden congelado del paquete; en archivos con varios
    cuadros, el cuadro pedido. Solo se muestra si el archivo sigue siendo, byte a byte, el que entró en el paquete."""
    series = t.get("series_corrida") or []
    if not 0 <= indice < len(series) or not 1 <= k <= series[indice]["n"]:
        raise HTTPException(404, "corte fuera de rango")
    if not 1 <= cuadro <= series[indice]["cuadros"][k - 1]:
        raise HTTPException(404, "cuadro fuera de rango")
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
    try:
        return render_bytes(datos, ventana if ventana in VENTANAS or ventana == "auto" else "tejido", cuadro)
    except ImagenNoMostrable as e:
        # Lo que no se puede ver no se puede dar por revisado: queda anotado en la corrida y bloquea su revisión.
        anotar_no_mostrable(t, indice, k, cuadro, str(e))
        raise HTTPException(422, f"este corte no se puede mostrar: {e}. La revisión de píxeles de esta corrida no se puede registrar desde aquí")


def comprobar_que_se_puede_mostrar(t: dict) -> None:
    """Antes de ofrecer la revisión: intenta dibujar el primer archivo de cada serie y el primer y el último cuadro de
    cada archivo con varios cuadros. No sustituye a mirar; detecta imágenes que el visor no sabe dibujar."""
    for indice, s_ in enumerate(t.get("series_corrida") or []):
        candidatos = {(1, 1)} | {(k, c) for k, n in enumerate(s_["cuadros"], 1) if n > 1 for c in (1, n)}
        for k, cuadro in sorted(candidatos):
            try:
                png_de_corrida(t, indice, k, "auto", cuadro)
            except HTTPException as e:
                if e.status_code != 422:
                    anotar_no_mostrable(t, indice, k, cuadro, str(e.detail))
            except Exception as e:
                anotar_no_mostrable(t, indice, k, cuadro, f"error al dibujar ({type(e).__name__})")


@api.get("/rapido/{tid}/series")
def rapido_series(request: Request, tid: str):
    """Las series de la corrida y los nombres exportados de sus archivos, en el orden del paquete."""
    auth(request)
    t = corrida_con_zip(tid)
    por_estudio = {f"{d['paciente']}/{d['estudio']}": d for d in t.get("demografia", [])}
    return [{"indice": i, **{k: v for k, v in s_.items() if k != "archivos"}, "demografia": por_estudio.get(s_.get("serie_interna", s_["serie"]).rsplit("/", 1)[0])}
            for i, s_ in enumerate(t["series_corrida"])]


@api.get("/rapido/{tid}/corte/{indice}/{k}")
def rapido_corte(request: Request, tid: str, indice: int, k: int, ventana: str = "tejido", cuadro: int = 1):
    auth(request)
    t = corrida_con_zip(tid)
    png = png_de_corrida(t, indice, k, ventana, cuadro)
    t.setdefault("servidos", set()).add((indice, k, cuadro))       # servido al visor: el navegador podrá avisar de que lo mostró
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@api.post("/rapido/{tid}/mostrado")
def rapido_mostrado(request: Request, tid: str, cuerpo: dict):
    """El navegador avisa de que un cuadro ha estado en pantalla."""
    auth(request)
    try:
        indice, k, cuadro = int(cuerpo.get("indice")), int(cuerpo.get("k")), int(cuerpo.get("cuadro", 1))
    except (TypeError, ValueError):
        raise HTTPException(400, "indice, k y cuadro deben ser números")
    return anotar_mostrado_corrida(corrida_con_zip(tid), indice, k, cuadro)


@api.get("/rapido/{tid}/progreso")
def rapido_progreso(request: Request, tid: str):
    auth(request)
    return progreso_corrida(corrida_con_zip(tid))


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
                     "exportacion": {}, "orden_de_cortes": {}, "series_corrida": [], "demografia": [], "no_mostrables": [],
                     "servidos": set(), "mostrados": set()}
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
    if t.get("no_mostrables"):
        primero = t["no_mostrables"][0]
        raise HTTPException(409, f"esta corrida tiene {len(t['no_mostrables'])} imagen(es) que el visor no puede mostrar "
                                 f"({primero['archivo']}: {primero['motivo']}); no se puede registrar una revisión de píxeles sin verlas")
    error = validar_registro_revision("sin_hallazgos", [], revisor)
    if error:
        raise HTTPException(400, error)
    avance = progreso_corrida(t)
    if not avance["completo"]:
        raise HTTPException(409, f"en el navegador se han mostrado {avance['mostrados']} de {avance['total']} cuadros de esta corrida; "
                                 "para declarar que se revisaron todos, todos tienen que haber estado en pantalla")
    # Antes de tomar el candado y de escribir ninguna evidencia: con el estado de las retiradas sin verificar, no se
    # registra ninguna revisión ni se publica nada en el destino. El zip pendiente sigue en la carpeta de trabajo.
    retiradas = estado_de_retiradas()
    if retiradas["bloqueo"]:
        registrar("rápido: revisión rechazada; el estado de las retiradas no se puede verificar: " + texto_de_problemas(retiradas["problemas"]))
        raise RevisionBloqueada(retiradas["problemas"], "registrar la revisión de esta corrida")
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
                    "detalle": "revisión declarada por quien firma, sobre el conjunto exacto de archivos de esta corrida",
                    "mostrado": {"cuadros_mostrados": avance["mostrados"], "cuadros_totales": avance["total"],
                                 "donde": "visor de la corrida, en el navegador", "nota": NOTA_MOSTRADO}}
        carpeta_reportes().mkdir(parents=True, exist_ok=True)
        registro_revision = carpeta_reportes() / f"revision_rapido_{dt.datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(2)}.json"
        registro_preparado = registro_revision.with_name(registro_revision.name + SUFIJO_PREPARADO)

        def evidencia(info: dict) -> None:
            # Se escribe ANTES de publicar: si no se puede guardar el registro de la revisión, el zip no sale.
            _escribir_json(registro_preparado, {**revision, "app": VERSION, "pacientes": t["pacientes"], "archivos": t["archivos"],
                                                "zip": {"nombre": info["nombre"], "destino": str(destino), "sha256": info["sha256"]},
                                                "nota": f"Se escribe antes de publicar el zip. Con el sufijo {SUFIJO_PREPARADO} no acredita ninguna publicación; al confirmarse la publicación el archivo pierde ese sufijo."})
            t["registro_revision_sha256"] = sha256_archivo(registro_preparado)
            t["revision_declarada"] = copy.deepcopy(revision)

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
            if e.cuerpo.get("retiradas_no_verificables"):
                raise RevisionBloqueada(e.cuerpo.get("diarios_con_problemas", []), "registrar la revisión de esta corrida", restos)
            if e.cuerpo.get("fallos"):
                registrar("rápido: revisión rechazada al empaquetar; un archivo cambió entre la comprobación y el empaquetado")
                raise PublicacionFallida(409, f"{len(e.cuerpo['fallos'])} archivo(s) de esta corrida cambiaron o ya no están; nada se escribió en el destino. Vuelve a ejecutar el modo rápido." + texto_restos(restos), restos)
            raise PublicacionFallida(e.codigo, (e.cuerpo.get("detail") or e.cuerpo.get("error") or "el paquete no se pudo rehacer") + "." + texto_restos(restos), restos)

        # ---- a partir de aquí el zip revisado está publicado: todo lo que falle es un aviso, con la ruta a la vista ----
        t["zip_pendiente_anterior"] = t["zip"]
        anterior = Path(t["zip"])
        t["zip"], t["revision_pixeles_pendiente"], t["revisado"] = r["ruta"], r["revision_pixeles_pendiente"], True
        t["zip_sha256"] = r["sha256"]
        acciones = []
        avisos_posteriores = list(r.get("avisos", []))
        registro_confirmado = True
        try:
            os.replace(registro_preparado, registro_revision)   # la evidencia preparada pasa a registro confirmado
        except OSError:
            registro_confirmado, registro_revision = False, registro_preparado
            avisos_posteriores.append(f"el registro de la revisión quedó con su nombre provisional aunque el zip está publicado: {registro_preparado}")
        t["registro_revision"] = str(registro_revision)
        for ruta in (registro_revision, Path(r["manifiesto_local"])):
            if ruta.name.endswith(SUFIJO_PREPARADO):
                acciones.append({"clase": "evidencia", "ruta": str(ruta),
                                 "texto": f"evidencia con nombre provisional; conservar y confirmar desde Registro: {ruta}"})
        for ruta in r.get("temporales_no_eliminados", []):
            acciones.append({"clase": "limpieza", "ruta": ruta, "texto": "temporal sin eliminar: " + ruta})
        cubiertos = []
        for pac in t["pacientes"]:
            try:   # la revisión del paciente completo solo se actualiza si la corrida cubre todos sus archivos guardados
                # mismos archivos y mismos bytes: lo que hay ahora del paciente es, hash por hash, lo que la corrida mostró y publicó
                actuales = hashes_paciente(pac)
                if actuales == {rel.split("/", 1)[1]: h for rel, h in t["archivos"].items() if rel.startswith(pac + "/")}:
                    guardar_revision(pac, "sin_hallazgos", [], revisor, "registrada desde el modo rápido; cubre todos los archivos del paciente",
                                     origen={"corrida": nombre_canonico(Path(registro_revision).name)}, mostrado=revision["mostrado"], archivos=actuales)
                    cubiertos.append(pac)
            except Exception as e:
                aviso = f"no se pudo actualizar la revisión de {pac} en Estudios ({type(e).__name__}); consulta Registro"
                avisos_posteriores.append(aviso)
                acciones.append({"clase": "revision_paciente", "texto": aviso})
        no_eliminados = [str(v) for v in (anterior, anterior.with_name(anterior.name[:-4] + ".manifiesto.json"))
                         if v.resolve() != Path(r["ruta"]).resolve() and not _eliminar(v)]
        acciones.extend({"clase": "limpieza", "ruta": ruta, "texto": "pendiente anterior sin eliminar: " + ruta} for ruta in no_eliminados)
        hay_avisos = any(p_["estado"] == "aviso" for p_ in t["pasos"])
        # Lo ocurrido al publicar se guarda en la corrida: la pantalla lo vuelve a leer en cada consulta, no solo una vez.
        t["publicacion"] = {"acciones": acciones, "fecha": revision["fecha"], "registro_confirmado": registro_confirmado, "avisos_posteriores": list(avisos_posteriores),
                            "pendiente_anterior_no_eliminado": list(no_eliminados), "revision_de_paciente_actualizada": list(cubiertos)}
        t.pop("publicacion_fallida", None)
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
    t = TRABAJOS.get(str(cuerpo.get("trabajo", "")))
    try:
        return aplicar_revision_rapido(t, str(cuerpo.get("revisor", "")).strip()[:120])
    except RevisionBloqueada as e:
        return JSONResponse(status_code=409, content=e.cuerpo())
    except PublicacionFallida as e:
        if t is not None:                                             # el intento fallido queda a la vista hasta que uno salga bien
            t["publicacion_fallida"] = {"fecha": dt.datetime.now().isoformat(timespec="seconds"), "detalle": e.mensaje, "restos": list(e.restos)}
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
    if r.get("estado") == "retirada":
        ret = r.get("retirada") or {}
        return {"paciente": r.get("paciente"), "estado": "retirada", "vigente": False, "lista_para_compartir": False,
                "detalle": f"revisión retirada el {ret.get('fecha', '')} por {ret.get('quien', '')}: {ret.get('motivo', '')}",
                "retirada": ret, "sustituye_a": r.get("sustituye_a"), "paquetes_afectados": r.get("paquetes_afectados", []),
                "inventario_de_paquetes": resumen_de_inventario(r)}
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
    """Estado de la revisión de píxeles de un paciente, comprobando que los archivos revisados no cambiaron. Cuando hay
    registro, el resultado lleva `registro_sha256`: la identidad exacta de ese registro, que es lo que citan los
    manifiestos de los paquetes y lo que alcanzan las retiradas."""
    ruta = carpeta_reportes() / f"revision_pixeles_{pac}.json"
    if not ruta.exists():
        return {"estado": "pendiente", "vigente": False, "lista_para_compartir": False, "detalle": "sin revisión registrada"}
    try:
        datos = ruta.read_bytes()
        r = json.loads(datos.decode("utf-8"))
    except (OSError, ValueError):
        return {"estado": "pendiente", "vigente": False, "lista_para_compartir": False, "detalle": "registro ilegible"}
    huella = hashlib.sha256(datos).hexdigest()
    if r.get("estado") == "retirada":                                 # el propio registro ya lo dice; no hace falta ningún diario
        return {**evaluar_registro_revision(r, {}), "registro_sha256": huella}
    try:
        diario = retirada_que_cubre("paciente", pac, huella)
    except RetiradasNoVerificables as e:
        return {**revision_no_verificable(pac, e.problemas), "registro_sha256": huella}
    # Una retirada comprometida manda sobre el registro, aunque el registro todavía no se haya actualizado: en cuanto la
    # retirada existe, esa revisión deja de autorizar que se comparta nada.
    if diario is not None:
        ret = diario["retirada"]
        return {"paciente": pac, "estado": "retirada", "vigente": False, "lista_para_compartir": False,
                "detalle": f"revisión retirada el {ret['fecha']} por {ret['quien']}: {ret['motivo']} (su registro está pendiente de actualizar)",
                "retirada": ret, "retirada_id": diario["id"], "registro_pendiente_de_actualizar": True,
                "paquetes_afectados": diario.get("paquetes_afectados", []), "inventario_de_paquetes": resumen_de_inventario(diario),
                "registro_sha256": huella}
    return {**evaluar_registro_revision(r, hashes_paciente(pac)), "registro_sha256": huella}


def revision_sigue_siendo(pac: str, huella_registro: str | None) -> str | None:
    """Comprobación ligera, para el instante anterior a publicar: ¿la revisión de este paciente sigue siendo el mismo
    registro y sigue sin retirar? Devuelve None si sí, o el motivo si no. No vuelve a leer las imágenes."""
    ruta = carpeta_reportes() / f"revision_pixeles_{pac}.json"
    try:
        datos = ruta.read_bytes()
    except OSError:
        return "su registro de revisión ya no se puede leer"
    actual = hashlib.sha256(datos).hexdigest()
    if actual != huella_registro:
        return "su registro de revisión cambió mientras se empaquetaba"
    try:
        if retirada_que_cubre("paciente", pac, actual) is not None:
            return "su revisión se retiró mientras se empaquetaba"
    except RetiradasNoVerificables as e:
        return "el estado de las retiradas dejó de poder verificarse (" + texto_de_problemas(e.problemas) + ")"
    return None


def archivar_registro(ruta: Path) -> dict | None:
    """Antes de sustituir un registro de revisión, guarda una copia intacta en historial_revisiones/. Devuelve su referencia
    (archivo y sha256) o None si no había registro. Ningún registro de revisión se sobrescribe sin dejar esa copia."""
    if not ruta.exists():
        return None
    datos = ruta.read_bytes()
    huella = hashlib.sha256(datos).hexdigest()
    historial = carpeta_reportes() / "historial_revisiones"
    historial.mkdir(parents=True, exist_ok=True)
    for ya in sorted(historial.glob(f"{ruta.stem}_*_{huella[:8]}.json")):      # repetir el archivado no duplica la copia
        if hashlib.sha256(ya.read_bytes()).hexdigest() == huella:
            return {"archivo": f"historial_revisiones/{ya.name}", "sha256": huella}
    copia = historial / f"{ruta.stem}_{dt.datetime.now():%Y%m%d_%H%M%S}_{huella[:8]}.json"
    copia.write_bytes(datos)
    if hashlib.sha256(copia.read_bytes()).hexdigest() != huella:
        raise OSError("la copia del registro en el historial no coincide con el original")
    return {"archivo": f"historial_revisiones/{copia.name}", "sha256": huella}


def guardar_revision(pac: str, estado: str, hallazgos: list[str], revisor: str, notas: str = "",
                     origen: dict | None = None, mostrado: dict | None = None, archivos: dict[str, str] | None = None) -> dict:
    """Escribe el registro de revisión de píxeles de un paciente, ligado a los hashes de sus archivos. El registro
    anterior, si lo hay, queda intacto en el historial. Una revisión "sin hallazgos" no se registra mientras el estado de
    las retiradas no se pueda verificar: lanza RevisionBloqueada antes de escribir nada."""
    if estado == "sin_hallazgos":
        retiradas = estado_de_retiradas()
        if retiradas["bloqueo"]:
            registrar(f"revisión de píxeles: {pac} → rechazada; el estado de las retiradas no se puede verificar: " + texto_de_problemas(retiradas["problemas"]))
            raise RevisionBloqueada(retiradas["problemas"], "registrar una revisión sin hallazgos")
    try:
        politica = json.loads((TRABAJO / "limpios" / ".politica.json").read_text(encoding="utf-8")).get("politica", "")
    except (OSError, ValueError):
        politica = ""
    registro = {"paciente": pac, "estado": estado, "hallazgos": hallazgos, "notas": notas, "revisor": revisor,
                "fecha": dt.datetime.now().isoformat(timespec="seconds"), "app": VERSION, "politica": politica,
                "archivos": dict(archivos) if archivos is not None else hashes_paciente(pac)}   # los hashes ya comprobados, si se dan
    if origen:
        registro["origen"] = origen
    if mostrado:
        registro["mostrado"] = mostrado
    carpeta_reportes().mkdir(parents=True, exist_ok=True)
    ruta = carpeta_reportes() / f"revision_pixeles_{pac}.json"
    anterior = archivar_registro(ruta)
    if anterior:
        registro["registro_anterior"] = anterior
    _escribir_json(ruta, registro)
    registrar(f"revisión de píxeles: {pac} → {estado} ({len(registro['archivos'])} archivos)")
    return leer_revision(pac)


# --------------------------------------------------------------------------------------
# retirar una revisión registrada
# --------------------------------------------------------------------------------------
# Retirar no borra nada. El registro original queda intacto (en el historial, para los de paciente; en su sitio, para
# los de una corrida) y un registro de retirada lo sustituye, con quién, cuándo y por qué. La retirada invalida el
# estado de la revisión dentro de la aplicación e identifica los paquetes revisados que dependían de ella. No puede
# cambiar los zips ya exportados: los enumera, para que no se compartan.

REGISTRO_RAPIDO_RE = re.compile(r"^revision_rapido_\d{8}_\d{6}_[0-9a-f]{4}\.json$")

# Una evidencia (registro de corrida o manifiesto local) se escribe con el sufijo .preparada antes de publicar el zip y lo
# pierde al confirmarse la publicación. Si el zip se publica y ese renombrado falla, la evidencia se queda con el sufijo:
# sigue siendo la evidencia de un paquete publicado. Por eso todo lo que busca registros de corrida o manifiestos mira
# también los provisionales, y la identidad de un registro es su nombre canónico, sin el sufijo, más su sha256.


def nombre_canonico(nombre: str) -> str:
    return nombre[:-len(SUFIJO_PREPARADO)] if nombre.endswith(SUFIJO_PREPARADO) else nombre


def ruta_de_registro_de_corrida(nombre: str) -> Path | None:
    """El archivo del registro de una corrida: el confirmado si existe; si no, el provisional."""
    canonico = nombre_canonico(nombre)
    for candidato in (carpeta_reportes() / canonico, carpeta_reportes() / (canonico + SUFIJO_PREPARADO)):
        if candidato.is_file():
            return candidato
    return None


def registros_de_corrida() -> list[tuple[str, Path]]:
    """(nombre canónico, archivo) de cada registro de corrida, confirmado o provisional; si están los dos, el confirmado."""
    carpeta = carpeta_reportes()
    if not carpeta.is_dir():
        return []
    canonicos = {nombre_canonico(p.name) for patron in ("revision_rapido_*.json", "revision_rapido_*.json" + SUFIJO_PREPARADO) for p in carpeta.glob(patron)}
    return [(c, ruta) for c in sorted(canonicos) if REGISTRO_RAPIDO_RE.match(c) for ruta in [ruta_de_registro_de_corrida(c)] if ruta is not None]


def confirmar_evidencias_publicadas() -> list[dict]:
    """Termina el renombrado de las evidencias provisionales cuyo zip está publicado: existe en su destino con el sha256
    que la evidencia anota. Las que no se puedan renombrar siguen valiendo como provisionales. No borra nada."""
    resultados = []
    candidatos = []
    if carpeta_reportes().is_dir():
        candidatos += sorted(carpeta_reportes().glob("revision_rapido_*.json" + SUFIJO_PREPARADO))
    if (TRABAJO / "paquetes").is_dir():
        candidatos += sorted((TRABAJO / "paquetes").glob("paquete_*.manifiesto.json" + SUFIJO_PREPARADO))
    for provisional in candidatos:
        final = provisional.with_name(nombre_canonico(provisional.name))
        if final.exists():
            continue
        try:
            z = json.loads(provisional.read_text(encoding="utf-8")).get("zip") or {}
            zip_ruta = Path(z.get("destino") or "") / (z.get("nombre") or "")
            publicado = bool(z.get("nombre") and z.get("sha256") and zip_ruta.is_file() and sha256_archivo(zip_ruta) == z["sha256"])
        except (OSError, ValueError, AttributeError):
            publicado = False
        if not publicado:
            continue
        try:
            os.replace(provisional, final)
            resultados.append({"evidencia": final.name, "estado": "confirmada"})
            registrar(f"evidencia confirmada tras publicar: {final.name}")
        except OSError as e:
            resultados.append({"evidencia": provisional.name, "estado": f"sigue provisional ({type(e).__name__})"})
    return resultados
NOTA_RETIRADA = ("Retirar una revisión invalida su estado en la aplicación. No modifica ni elimina los zips ya exportados: "
                 "los paquetes enumerados aquí se crearon con esa revisión y ya no deben compartirse.")

# Una retirada es un diario: limpios_REPORTES/retiradas/retirada_<fecha>_<id>.json. Escribirlo es el punto de compromiso.
#   - Dice quién, cuándo y por qué; qué registros alcanza (con su sha256) y por qué cada uno; qué paquetes dependían de
#     ellos; y qué acciones quedan por hacer sobre cada registro.
#   - Desde que existe, las revisiones que alcanza dejan de autorizar que se comparta: leer_revision() lo consulta antes de
#     mirar el registro, así que da igual que actualizar ese registro haya fallado.
#   - Las acciones (anotar la corrida, archivar el original del paciente, sustituir su registro) son idempotentes y se
#     marcan en el diario una a una. Si alguna falla, la retirada queda comprometida e incompleta, se informa de lo hecho
#     y de lo pendiente, y un reintento —o el siguiente arranque— termina lo que falta sin tocar quién la retiró ni cuándo.


class RetiradaIncompleta(Exception):
    """La retirada está comprometida, pero no todas sus acciones se completaron."""

    def __init__(self, diario: dict, completadas: list[str], pendientes: list[str], error: str):
        super().__init__(error)
        self.diario, self.completadas, self.pendientes, self.error = diario, completadas, pendientes, error

    def cuerpo(self) -> dict:
        ret = self.diario["retirada"]
        return {"detail": ("La retirada quedó registrada y esas revisiones ya no autorizan compartir nada, pero no se completaron todas las "
                           f"actualizaciones ({self.error}). Hecho: {'; '.join(self.completadas) or 'nada'}. Pendiente: {'; '.join(self.pendientes)}. "
                           "Vuelve a intentarlo para terminarla."),
                "retirada": self.diario["id"], "comprometida": True, "estado": "incompleta", "quien": ret["quien"], "fecha": ret["fecha"],
                "motivo": ret["motivo"], "completadas": self.completadas, "pendientes": self.pendientes, "error": self.error,
                "retiradas": [{"tipo": a["tipo"], "id": a["id"]} for a in self.diario["alcance"]],
                "paquetes_afectados": self.diario.get("paquetes_afectados", []), "inventario_de_paquetes": resumen_de_inventario(self.diario),
                "nota": NOTA_RETIRADA}


def carpeta_retiradas() -> Path:
    return carpeta_reportes() / "retiradas"


class RetiradasNoVerificables(Exception):
    """Hay algún diario de retirada cuyo alcance no se puede determinar: no se puede leer, no es JSON válido, su estructura
    no es válida o la carpeta de retiradas no se deja listar. No es lo mismo que "no hay ninguna retirada": mientras dure,
    no se puede afirmar que una revisión siga valiendo."""

    def __init__(self, problemas: list[dict]):
        self.problemas = problemas
        super().__init__(texto_de_problemas(problemas))


def texto_de_problemas(problemas: list[dict]) -> str:
    return "; ".join(f"{p['diario']}: {p['motivo']}" for p in problemas)


ACCIONES_DE_RETIRADA = ("anotar_corrida", "archivar_original", "sustituir_registro")


def _alcance_legible(d) -> bool:
    """¿Se puede saber con seguridad a qué registros alcanza este diario?"""
    if not isinstance(d, dict) or not isinstance(d.get("alcance"), list) or not d["alcance"]:
        return False
    return all(isinstance(a, dict) and a.get("tipo") in ("paciente", "corrida") and isinstance(a.get("id"), str) and a["id"]
               and isinstance(a.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", a["sha256"]) for a in d["alcance"])


def _defecto_de_diario(d, nombre: str) -> str | None:
    """Qué le falta a un diario para ser válido, o None si lo es."""
    if not isinstance(d, dict):
        return "no es un objeto"
    if not isinstance(d.get("id"), str) or not d["id"]:
        return "falta el identificador"
    if nombre != f"retirada_{d['id']}.json":
        return "el identificador no corresponde al nombre del archivo"
    ret = d.get("retirada")
    if not isinstance(ret, dict) or not all(isinstance(ret.get(k), str) and ret[k] for k in ("quien", "fecha", "motivo")):
        return "falta quién retiró, cuándo o por qué"
    if not _alcance_legible(d):
        return "el alcance falta o no es válido"
    acciones = d.get("acciones")
    if not isinstance(acciones, list) or not all(isinstance(a, dict) and a.get("accion") in ACCIONES_DE_RETIRADA and isinstance(a.get("id"), str)
                                                 and a.get("estado") in ("pendiente", "hecha") and isinstance(a.get("descripcion"), str) for a in acciones):
        return "la lista de acciones falta o no es válida"
    return None


def estado_de_retiradas() -> dict:
    """Lee todos los diarios de retirada sin dar nada por supuesto. Devuelve:
      diarios    los que se pueden usar: los válidos y los que tienen algún defecto pero un alcance legible
                 (estos llevan '_requiere_atencion' y no se pueden terminar hasta que se arreglen);
      problemas  cada diario, o la carpeta, que requiere atención, con el motivo y si su alcance se conoce;
      bloqueo    True si hay algo cuyo alcance no se puede determinar. Entonces no se sabe qué revisiones están
                 retiradas, y ninguna puede autorizar que se comparta nada hasta que se resuelva.
    No lanza. Que la carpeta no exista significa que no hay retiradas; cualquier otro fallo al listarla es un problema."""
    carpeta = carpeta_retiradas()
    try:
        nombres = sorted(os.listdir(carpeta))
    except FileNotFoundError:
        return {"diarios": [], "problemas": [], "bloqueo": False}
    except OSError as e:
        return {"diarios": [], "bloqueo": True,
                "problemas": [{"diario": "retiradas/", "motivo": f"no se puede listar la carpeta de retiradas ({type(e).__name__})", "alcance_conocido": False}]}
    diarios, problemas = [], []
    for nombre in nombres:
        if not (nombre.startswith("retirada_") and nombre.endswith(".json")):
            continue                                                  # temporales de una escritura y cualquier otro archivo
        try:
            datos = (carpeta / nombre).read_bytes()
        except OSError as e:
            problemas.append({"diario": nombre, "motivo": f"no se puede leer ({type(e).__name__})", "alcance_conocido": False})
            continue
        try:
            d = json.loads(datos.decode("utf-8"))
        except ValueError:                                            # incluye UnicodeDecodeError
            problemas.append({"diario": nombre, "motivo": "su contenido no es JSON válido", "alcance_conocido": False})
            continue
        defecto = _defecto_de_diario(d, nombre)
        if defecto is None:
            diarios.append(d)
        elif _alcance_legible(d):
            # se sabe a qué alcanza: sigue retirando esos registros, pero no se puede terminar ni mostrar entero
            ret = d.get("retirada") if isinstance(d.get("retirada"), dict) else {}
            diarios.append({**d, "id": d.get("id") if isinstance(d.get("id"), str) and d.get("id") else nombre[len("retirada_"):-5],
                            "retirada": {"quien": str(ret.get("quien") or "desconocido"), "fecha": str(ret.get("fecha") or "desconocida"),
                                         "motivo": str(ret.get("motivo") or "no consta")},
                            "acciones": [], "_requiere_atencion": defecto, "_archivo": nombre})
            problemas.append({"diario": nombre, "motivo": f"estructura no válida ({defecto}); su alcance se conoce y sigue retirado", "alcance_conocido": True})
        else:
            problemas.append({"diario": nombre, "motivo": f"estructura no válida ({defecto})", "alcance_conocido": False})
    return {"diarios": diarios, "problemas": problemas, "bloqueo": any(not p["alcance_conocido"] for p in problemas)}


def diarios_de_retirada() -> list[dict]:
    """Los diarios de retirada, solo si el estado de las retiradas se puede verificar entero. Si no, lanza
    RetiradasNoVerificables: quien pregunta si una revisión está retirada no puede recibir un "no" por omisión."""
    estado = estado_de_retiradas()
    if estado["bloqueo"]:
        raise RetiradasNoVerificables(estado["problemas"])
    return estado["diarios"]


def revision_no_verificable(pac: str, problemas: list[dict]) -> dict:
    return {"paciente": pac, "estado": "no_verificable", "vigente": False, "lista_para_compartir": False,
            "detalle": ("no se puede verificar si esta revisión fue retirada (" + texto_de_problemas(problemas) + "); "
                        "hasta que ese diario se pueda leer, ninguna revisión autoriza compartir"),
            "diarios_con_problemas": problemas}


def retirada_que_cubre(tipo: str, identificador: str, huella: str | None) -> dict | None:
    """El diario de retirada que alcanza exactamente ese registro (mismo tipo, mismo identificador, mismo contenido), o
    None si ninguno lo alcanza. Lanza RetiradasNoVerificables si no se puede saber."""
    for d in diarios_de_retirada():
        for a in d["alcance"]:
            if a.get("tipo") == tipo and a.get("id") == identificador and a.get("sha256") == huella:
                return d
    return None


def _ruta_diario(d: dict) -> Path:
    return carpeta_retiradas() / f"retirada_{d['id']}.json"


def _retirada_de_corrida(nombre: str) -> Path:
    return carpeta_reportes() / (nombre_canonico(nombre)[:-5] + ".retirada.json")


def _sha_de(ruta: Path) -> str | None:
    try:
        return hashlib.sha256(ruta.read_bytes()).hexdigest()
    except OSError:
        return None


def _manifiesto_valido(m) -> bool:
    """Valida los datos que permiten decidir dependencias, sin inventar campos ausentes."""
    if not isinstance(m, dict):
        return False
    pacientes = m.get("pacientes")
    pendientes = m.get("revision_pixeles_pendiente")
    revision = m.get("revision_pixeles")
    if (not isinstance(pacientes, list) or not pacientes
            or any(not isinstance(p, str) or not p for p in pacientes)
            or len(set(pacientes)) != len(pacientes)
            or not isinstance(pendientes, list)
            or any(not isinstance(p, str) or p not in pacientes for p in pendientes)
            or not isinstance(revision, dict)):
        return False
    if "zip" in m:
        z = m["zip"]
        if not isinstance(z, dict) or any(not isinstance(z.get(k), str) or not z[k]
                                         for k in ("nombre", "destino", "sha256")):
            return False
    if "alcance" in revision:
        return (isinstance(revision["alcance"], str) and bool(revision["alcance"])
                and revision.get("estado") in ("pendiente", "sin_hallazgos")
                and set(pendientes) == (set() if revision["estado"] == "sin_hallazgos" else set(pacientes)))
    if set(revision) != set(pacientes):
        return False
    for pac in pacientes:
        r = revision.get(pac)
        if not isinstance(r, dict) or not isinstance(r.get("estado"), str):
            return False
        if not isinstance(r.get("vigente"), bool) or r["estado"] not in (
                "pendiente", "sin_hallazgos", "con_hallazgos", "retirada", "no_verificable"):
            return False
        revisado = r["estado"] == "sin_hallazgos" and r["vigente"]
        if (pac not in pendientes) != revisado:
            return False  # la lista de pendientes debe concordar con el estado efectivo de cada paciente
        if revisado:
            huella = r.get("registro_sha256")
            if huella is not None:
                if not isinstance(huella, str) or re.fullmatch(r"[0-9a-f]{64}", huella) is None:
                    return False
            elif not isinstance(r.get("fecha"), str) or not r["fecha"]:
                return False  # compatibilidad con manifiestos anteriores al identificador por hash
    return True


def _leer_manifiesto_local(ruta: Path) -> tuple[dict | None, str | None]:
    """(manifiesto, None) si se puede leer y tiene la forma esperada; (None, motivo) si no. El motivo no lleva contenido."""
    try:
        datos = ruta.read_bytes()
    except OSError as e:
        return None, f"no se puede leer ({type(e).__name__})"
    try:
        m = json.loads(datos.decode("utf-8"))
    except ValueError:
        return None, "su contenido no es JSON válido"
    if not _manifiesto_valido(m) or "zip" not in m:
        return None, "su estructura no es válida"
    esperado = nombre_canonico(ruta.name).replace(".manifiesto.json", ".zip")
    if "zip" in m and m["zip"]["nombre"] != esperado:
        return None, "el nombre del zip no coincide con el nombre del manifiesto"
    return m, None


def _manifiesto_dentro_del_zip(ruta_zip: Path) -> dict | None:
    """El manifiesto que viaja dentro del propio zip: dice lo mismo que el local sobre la revisión de cada paciente."""
    try:
        with zipfile.ZipFile(ruta_zip) as z:
            m = json.loads(z.read("manifiesto.json").decode("utf-8"))
    except Exception:
        return None
    return m if _manifiesto_valido(m) else None


def inventario_de_paquetes(alcance: list[dict]) -> dict:
    """Los paquetes revisados cuya revisión es, exactamente, una de las que se retiran, y lo que no se pudo averiguar.

    Para una corrida, su propio zip. Para la revisión de un paciente, los paquetes de la pestaña Enviar que la citan (por el
    sha256 de su registro; por la fecha, en manifiestos anteriores a ese campo). De cada uno: nombre, destino, si sigue
    existiendo allí y si sigue siendo el mismo archivo.

    Un manifiesto local que no se puede leer no se salta en silencio:
      - se anota en `problemas`, con su motivo;
      - si el zip sigue en la carpeta de paquetes, se obtiene información del manifiesto del zip, sin dar por verificada su identidad;
      - si no, ese paquete va a `sin_determinar` y el inventario queda marcado como incompleto.
    Devuelve {'afectados', 'completo', 'problemas', 'sin_determinar'}. No lanza: un inventario incompleto no impide retirar."""
    corridas = [a for a in alcance if a["tipo"] == "corrida"]
    pacientes = {a["id"]: a for a in alcance if a["tipo"] == "paciente"}
    zips_de_corrida = {(a.get("zip") or {}).get("nombre"): a for a in corridas if (a.get("zip") or {}).get("nombre")}
    encontrados: dict[str, dict] = {}
    problemas: list[dict] = []
    sin_determinar: list[dict] = []
    carpeta = TRABAJO / "paquetes"
    try:
        nombres = sorted(os.listdir(carpeta))
    except FileNotFoundError:
        nombres = []
    except OSError as e:
        nombres = []
        problemas.append({"manifiesto": "paquetes/", "motivo": f"no se puede listar la carpeta de paquetes ({type(e).__name__})",
                          "zip": None, "zip_en_paquetes": False, "recuperado_del_zip": False})
        sin_determinar.append({"zip": "todos los de la carpeta de paquetes", "ruta": str(carpeta), "existe": None,
                               "motivo": f"no se puede listar la carpeta de paquetes ({type(e).__name__})"})
    # los confirmados primero; después los que quedaron con nombre provisional tras publicarse su zip
    manifiestos = [n for n in nombres if n.startswith("paquete_") and n.endswith(".manifiesto.json")] + \
                  [n for n in nombres if n.startswith("paquete_") and n.endswith(".manifiesto.json" + SUFIJO_PREPARADO)]

    # Un zip sin manifiesto también es evidencia de un paquete: no se puede omitir del inventario.
    con_manifiesto = {nombre_canonico(n).replace(".manifiesto.json", ".zip") for n in manifiestos}
    manifiestos += [n[:-4] + ".manifiesto.json" for n in nombres
                    if n.startswith("paquete_") and n.endswith(".zip") and n not in con_manifiesto]

    def cita(bloque: dict, pac: str, a: dict) -> bool:
        """¿El manifiesto cita exactamente el registro que se retira? Por su sha256; los manifiestos anteriores a ese campo
        solo llevan la fecha de la revisión, y se comparan por ella."""
        b = bloque.get(pac) or {}
        if not isinstance(b, dict):
            return False
        if b.get("registro_sha256"):
            return b["registro_sha256"] == a.get("sha256")
        return b.get("fecha") == a.get("fecha_revision")

    for archivo in manifiestos:
        zip_por_nombre = nombre_canonico(archivo).replace(".manifiesto.json", ".zip")
        m, motivo = _leer_manifiesto_local(carpeta / archivo)
        recuperado = False
        if m is None:
            zip_local = carpeta / zip_por_nombre
            m = _manifiesto_dentro_del_zip(zip_local) if zip_local.is_file() else None
            recuperado = m is not None
            problemas.append({"manifiesto": archivo, "motivo": motivo, "zip": zip_por_nombre, "zip_en_paquetes": zip_local.is_file(),
                              "recuperado_del_zip": recuperado})
            registrar(f"retirada: el manifiesto {archivo} {motivo}" + ("; su información se tomó del propio zip" if recuperado else "; no se pudo determinar si depende de la revisión"))
            if m is None:
                if zip_por_nombre not in zips_de_corrida and zip_por_nombre not in encontrados:    # el zip de una corrida se conoce por su registro
                    sin_determinar.append({"zip": zip_por_nombre, "ruta": str(zip_local), "existe": zip_local.is_file(), "motivo": f"su manifiesto local {motivo}"})
                continue
            if zip_por_nombre not in zips_de_corrida:
                sin_determinar.append({"zip": zip_por_nombre, "ruta": str(zip_local), "existe": True,
                                       "motivo": "el manifiesto del zip se pudo leer, pero su identidad original no se puede verificar sin el sha256 registrado al publicar"})
            z = {"nombre": zip_por_nombre, "destino": str(carpeta), "sha256": None}                # el sha256 anotado al publicar no se conoce
        else:
            z = m.get("zip") or {}
        nombre = z.get("nombre") or zip_por_nombre
        if nombre in encontrados:
            continue
        bloque = m.get("revision_pixeles") or {}
        if "alcance" in bloque:                                       # paquete de una corrida: depende de la revisión de esa corrida
            if nombre not in zips_de_corrida or bloque.get("estado") != "sin_hallazgos":
                continue
            afectados = sorted(m.get("pacientes", []))
        else:                                                         # paquete de Enviar: depende de la revisión de cada paciente
            afectados = sorted(pac for pac, a in pacientes.items()
                               if pac in m.get("pacientes", []) and pac not in m.get("revision_pixeles_pendiente", []) and cita(bloque, pac, a))
            if not afectados:
                continue
        encontrados[nombre] = {"zip": nombre, "destino": z.get("destino") or str(carpeta), "sha256": z.get("sha256"),
                               "creado": m.get("creado"), "pacientes": afectados, "recuperado_del_zip": recuperado}
    for nombre, a in zips_de_corrida.items():                         # el zip de la corrida, aunque su manifiesto local ya no esté
        encontrados.setdefault(nombre, {"zip": nombre, "destino": a["zip"].get("destino"), "sha256": a["zip"].get("sha256"),
                                        "creado": None, "pacientes": sorted(a.get("pacientes", [])), "recuperado_del_zip": False})
    afectados_out = []
    for e in encontrados.values():
        ruta = Path(e["destino"] or "") / e["zip"]
        existe = ruta.is_file()
        try:
            mismo = bool(existe and e["sha256"] and sha256_archivo(ruta) == e["sha256"])
        except OSError:
            mismo = False
        afectados_out.append({**e, "ruta": str(ruta), "existe": existe, "es_el_mismo_archivo": mismo if e["sha256"] else None})
    sin_determinar = [x for x in sin_determinar if not encontrados.get(x["zip"], {}).get("sha256")]
    return {"afectados": sorted(afectados_out, key=lambda e: e["zip"]), "completo": not sin_determinar,
            "problemas": problemas, "sin_determinar": sorted(sin_determinar, key=lambda e: e["zip"])}


def paquetes_que_dependen_de(alcance: list[dict]) -> list[dict]:
    """Solo la lista de afectados. Quien necesite saber si esa lista está completa debe usar inventario_de_paquetes()."""
    return inventario_de_paquetes(alcance)["afectados"]


def resumen_de_inventario(d: dict) -> dict:
    """El estado del inventario de paquetes de una retirada, tal como quedó en su diario. Los diarios anteriores a este
    campo no lo llevan: entonces no se sabe si la lista estaba completa."""
    inv = d.get("inventario_de_paquetes")
    if not isinstance(inv, dict):
        return {"completo": None, "problemas": [], "sin_determinar": []}
    if (not isinstance(inv.get("completo"), bool)
            or any(not isinstance(inv.get(k), list)
                   or any(not isinstance(e, dict) or not isinstance(e.get("motivo"), str) for e in inv[k])
                   for k in ("problemas", "sin_determinar"))):
        return {"completo": None, "problemas": [], "sin_determinar": [],
                "motivo": "el inventario guardado tiene una estructura no válida"}
    return {"completo": inv["completo"], "problemas": list(inv["problemas"]), "sin_determinar": list(inv["sin_determinar"])}


def _elemento_paciente(pac: str, incluida_por: str) -> dict | None:
    """El registro vigente de un paciente como elemento del alcance, o None si no hay una revisión que retirar."""
    ruta = carpeta_reportes() / f"revision_pixeles_{pac}.json"
    try:
        datos = ruta.read_bytes()
        r = json.loads(datos.decode("utf-8"))
    except (OSError, ValueError):
        return None
    huella = hashlib.sha256(datos).hexdigest()
    if r.get("estado") not in ("sin_hallazgos", "con_hallazgos") or retirada_que_cubre("paciente", pac, huella):
        return None
    return {"tipo": "paciente", "id": pac, "sha256": huella, "incluida_por": incluida_por, "estado_revision": r.get("estado"),
            "revisor": r.get("revisor"), "fecha_revision": r.get("fecha"), "archivos": len(r.get("archivos", {})), "origen": r.get("origen")}


def _elemento_corrida(nombre: str, incluida_por: str) -> dict | None:
    nombre = nombre_canonico(nombre)
    ruta = ruta_de_registro_de_corrida(nombre)
    if ruta is None:
        return None
    try:
        datos = ruta.read_bytes()
        r = json.loads(datos.decode("utf-8"))
    except (OSError, ValueError):
        return None
    huella = hashlib.sha256(datos).hexdigest()
    if retirada_que_cubre("corrida", nombre, huella):
        return None
    return {"tipo": "corrida", "id": nombre, "sha256": huella, "incluida_por": incluida_por, "estado_revision": r.get("estado"),
            "revisor": r.get("revisor"), "fecha_revision": r.get("fecha"), "archivos": len(r.get("archivos", {})),
            "pacientes": r.get("pacientes", []), "zip": r.get("zip")}


def alcance_de_retirada(tipo: str, identificador: str) -> list[dict]:
    """Todo lo que cae con una retirada. La declaración de una corrida es un solo acto: retirar la corrida retira las
    revisiones de paciente que escribió, y retirar una de esas revisiones retira la corrida y las demás que escribió."""
    alcance: dict[tuple, dict] = {}

    def con_corrida(nombre: str, motivo: str) -> None:
        c = _elemento_corrida(nombre, motivo)
        if c is None:
            return
        alcance[("corrida", nombre)] = c
        for pac in c["pacientes"]:
            if ("paciente", pac) in alcance or not PACIENTE_RE.match(str(pac)):
                continue
            e = _elemento_paciente(pac, f"la escribió la corrida {nombre}")
            if e and (e.get("origen") or {}).get("corrida") == nombre:
                alcance[("paciente", pac)] = e

    if tipo == "paciente":
        e = _elemento_paciente(identificador, "solicitada")
        if e is None:
            return []
        alcance[("paciente", identificador)] = e
        corrida = nombre_canonico((e.get("origen") or {}).get("corrida") or "")
        if REGISTRO_RAPIDO_RE.match(corrida):
            con_corrida(corrida, f"de ella salió la revisión de {identificador}")
    else:
        con_corrida(identificador, "solicitada")
    return list(alcance.values())


def _acciones_de(alcance: list[dict]) -> list[dict]:
    acciones = []
    for a in alcance:
        if a["tipo"] == "corrida":
            acciones.append({"accion": "anotar_corrida", "id": a["id"], "estado": "pendiente",
                             "descripcion": f"anotar la retirada junto al registro de la corrida {a['id']}"})
        else:
            acciones.append({"accion": "archivar_original", "id": a["id"], "estado": "pendiente",
                             "descripcion": f"guardar en el historial el registro original de {a['id']}"})
            acciones.append({"accion": "sustituir_registro", "id": a["id"], "estado": "pendiente",
                             "descripcion": f"sustituir el registro de {a['id']} por la retirada"})
    return acciones


def _hacer_accion(d: dict, accion: dict) -> None:
    """Ejecuta una acción de la retirada. Repetirla no hace daño: si ya está hecha, no hace nada."""
    elemento = next(a for a in d["alcance"] if a["id"] == accion["id"] and a["tipo"] == ("corrida" if accion["accion"] == "anotar_corrida" else "paciente"))
    if accion["accion"] == "anotar_corrida":
        destino = _retirada_de_corrida(elemento["id"])
        if not destino.exists():
            _escribir_json(destino, {"tipo": "retirada de la revisión de una corrida", "retirada_id": d["id"], "registro": elemento["id"],
                                     "sha256_registro": elemento["sha256"], "retirada": d["retirada"], "incluida_por": elemento["incluida_por"],
                                     "pacientes": elemento.get("pacientes", []), "archivos": elemento.get("archivos"), "revisor": elemento.get("revisor"),
                                     "fecha_revision": elemento.get("fecha_revision"), "paquetes_afectados": d.get("paquetes_afectados", []),
                                     "inventario_de_paquetes": resumen_de_inventario(d),
                                     "nota": NOTA_RETIRADA, "app": VERSION})
        return
    pac = elemento["id"]
    ruta = carpeta_reportes() / f"revision_pixeles_{pac}.json"
    actual = _sha_de(ruta)
    if actual != elemento["sha256"]:
        # ya no es el registro original: o esta misma retirada ya lo sustituyó, o hay un registro posterior que no se toca
        try:
            ya = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ya = {}
        accion["nota"] = "ya sustituido por esta retirada" if ya.get("retirada_id") == d["id"] else "el paciente tiene un registro posterior; no se toca"
        return
    referencia = archivar_registro(ruta)                              # idempotente: no duplica la copia
    if accion["accion"] == "archivar_original":
        accion["referencia"] = referencia
        return
    _escribir_json(ruta, {"paciente": pac, "estado": "retirada", "retirada": d["retirada"], "retirada_id": d["id"],
                          "incluida_por": elemento["incluida_por"],
                          "sustituye_a": {**referencia, "estado": elemento.get("estado_revision"), "revisor": elemento.get("revisor"),
                                          "fecha": elemento.get("fecha_revision"), "archivos": elemento.get("archivos"), "origen": elemento.get("origen")},
                          "paquetes_afectados": d.get("paquetes_afectados", []), "inventario_de_paquetes": resumen_de_inventario(d),
                          "nota": NOTA_RETIRADA, "app": VERSION})


def completar_retirada(d: dict) -> dict:
    """Hace las acciones pendientes de una retirada comprometida, en orden, anotando cada una en el diario. Si alguna
    falla, lanza RetiradaIncompleta con lo hecho y lo pendiente; la retirada sigue comprometida."""
    error = None
    for accion in d["acciones"]:
        if accion["estado"] == "hecha":
            continue
        try:
            _hacer_accion(d, accion)
            accion["estado"] = "hecha"
            d["estado"] = "completa" if all(a["estado"] == "hecha" for a in d["acciones"]) else "comprometida; acciones pendientes"
            _escribir_json(_ruta_diario(d), d)
        except Exception as e:                                        # cualquier fallo de escritura o de archivado
            error = f"{type(e).__name__} al {accion['descripcion']}"
            if accion["estado"] == "hecha":                           # la acción se hizo; lo que falló fue anotarla en el diario
                accion["estado"] = "pendiente"
                error = f"{type(e).__name__} al anotar en el diario: {accion['descripcion']}"
            break
    completadas = [a["descripcion"] for a in d["acciones"] if a["estado"] == "hecha"]
    pendientes = [a["descripcion"] for a in d["acciones"] if a["estado"] != "hecha"]
    if pendientes:
        d["estado"] = "comprometida; acciones pendientes"
        registrar(f"retirada {d['id']}: incompleta; pendiente: {'; '.join(pendientes)}")
        raise RetiradaIncompleta(d, completadas, pendientes, error or "acciones pendientes")
    registrar(f"retirada {d['id']}: completa ({len(d['alcance'])} registro(s), {len(d.get('paquetes_afectados', []))} paquete(s) afectado(s))")
    ret = d["retirada"]
    return {"retirada": d["id"], "estado": "completa", "comprometida": True, "quien": ret["quien"], "fecha": ret["fecha"], "motivo": ret["motivo"],
            "retiradas": [{"tipo": a["tipo"], "id": a["id"]} for a in d["alcance"]], "completadas": completadas, "pendientes": [],
            "paquetes_afectados": d.get("paquetes_afectados", []), "inventario_de_paquetes": resumen_de_inventario(d), "nota": NOTA_RETIRADA}


def _diario_de(tipo: str, identificador: str) -> dict | None:
    """El diario que ya alcanza a ese paciente o corrida en su estado actual: por el contenido de su registro o, si el
    registro ya es una retirada, por el identificador que lleva dentro."""
    if tipo == "paciente":
        ruta = carpeta_reportes() / f"revision_pixeles_{identificador}.json"
        d = retirada_que_cubre("paciente", identificador, _sha_de(ruta))
        if d is None:
            try:
                rid = json.loads(ruta.read_text(encoding="utf-8")).get("retirada_id")
            except (OSError, ValueError):
                rid = None
            d = next((x for x in diarios_de_retirada() if x["id"] == rid), None) if rid else None
        return d
    ruta = ruta_de_registro_de_corrida(identificador)
    return retirada_que_cubre("corrida", nombre_canonico(identificador), _sha_de(ruta) if ruta else None)


def retirar_revision(tipo: str, identificador: str, quien: str, motivo: str) -> dict:
    """Retira la revisión de un paciente o la de una corrida, con todo lo que depende de ella. Si ya hay una retirada
    comprometida e incompleta que la alcanza, la termina, sin cambiar quién la retiró, cuándo ni por qué."""
    quien, motivo = quien.strip()[:120], motivo.strip()[:1000]
    if tipo not in ("paciente", "corrida"):
        raise HTTPException(400, "tipo debe ser paciente o corrida")
    if tipo == "paciente":
        carpeta_paciente(identificador)
    else:
        identificador = nombre_canonico(identificador)                # el registro provisional de una corrida publicada es esa misma corrida
        if not REGISTRO_RAPIDO_RE.match(identificador):
            raise HTTPException(400, "identificador de corrida no válido")
    if not CANDADO.acquire(blocking=False):
        raise HTTPException(409, "hay otro trabajo en curso; espera a que termine antes de retirar una revisión")
    try:
        confirmar_evidencias_publicadas()                              # lo que se publicó y quedó con nombre provisional, se termina aquí si se puede
        bloqueo = estado_de_retiradas()
        if bloqueo["bloqueo"]:
            raise HTTPException(409, "no se puede verificar el estado de las retiradas (" + texto_de_problemas(bloqueo["problemas"]) + "); "
                                     "mientras tanto ninguna revisión autoriza compartir, y no se puede retirar ni terminar nada "
                                     "hasta que ese diario se pueda leer")
        existente = _diario_de(tipo, identificador)
        if existente is not None:
            if existente.get("_requiere_atencion"):
                raise HTTPException(409, f"esa revisión ya está retirada, pero su diario requiere atención ({existente['_archivo']}: "
                                         f"{existente['_requiere_atencion']}); no se puede terminar hasta que se arregle")
            if all(a["estado"] == "hecha" for a in existente["acciones"]):
                raise HTTPException(409, "esa revisión ya está retirada")
            # reintento: se termina la misma retirada. Quién la retiró, cuándo y por qué no cambian; el reintento se anota.
            existente.setdefault("reintentos", []).append({"quien": quien or None, "fecha": dt.datetime.now().isoformat(timespec="seconds")})
            return {**completar_retirada(existente), "reanudada": True}
        if not quien:
            raise HTTPException(400, "indica quién retira la revisión")
        if len(motivo) < 3:
            raise HTTPException(400, "indica por qué se retira la revisión")
        alcance = alcance_de_retirada(tipo, identificador)
        if not alcance:
            raise HTTPException(409, "no hay una revisión registrada que retirar" if tipo == "paciente"
                                else "esa corrida no tiene una revisión registrada que retirar")
        fecha = dt.datetime.now().isoformat(timespec="seconds")
        inventario = inventario_de_paquetes(alcance)                  # no impide retirar: lo que no se pudo averiguar queda dicho
        diario = {"id": f"{dt.datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(3)}", "retirada": {"quien": quien, "fecha": fecha, "motivo": motivo},
                  "solicitada": {"tipo": tipo, "id": identificador}, "alcance": alcance,
                  "paquetes_afectados": inventario["afectados"],
                  "inventario_de_paquetes": {"completo": inventario["completo"], "problemas": inventario["problemas"], "sin_determinar": inventario["sin_determinar"]},
                  "acciones": _acciones_de(alcance),
                  "estado": "comprometida; acciones pendientes", "reintentos": [], "nota": NOTA_RETIRADA, "app": VERSION}
        # ---- punto de compromiso: si el diario no se puede escribir, no se ha retirado nada ----
        try:
            carpeta_retiradas().mkdir(parents=True, exist_ok=True)
            _escribir_json(_ruta_diario(diario), diario)
        except Exception as e:
            registrar(f"retirada: no se pudo registrar ({type(e).__name__}); no se retiró nada")
            raise HTTPException(500, f"no se pudo registrar la retirada ({type(e).__name__}); no se ha retirado nada")
        registrar(f"retirada {diario['id']}: comprometida por {quien}; alcanza {', '.join(a['tipo'] + ' ' + a['id'] for a in alcance)}")
        for t in TRABAJOS.values():                                   # la corrida abierta en pantalla, si es una de estas
            if t.get("nombre") == "rapido" and nombre_canonico(Path(str(t.get("registro_revision", ""))).name) in {a["id"] for a in alcance if a["tipo"] == "corrida"}:
                t["revision_retirada"] = diario["retirada"]
        return {**completar_retirada(diario), "reanudada": False}
    finally:
        CANDADO.release()


def retiradas_incompletas() -> list[dict]:
    """Las retiradas comprometidas que se pueden terminar: diarios válidos con acciones pendientes. Los diarios con
    problemas no están aquí; estado_de_retiradas() los enumera aparte."""
    return [d for d in estado_de_retiradas()["diarios"]
            if not d.get("_requiere_atencion") and any(a.get("estado") != "hecha" for a in d.get("acciones", []))]


def reanudar_retiradas() -> list[dict]:
    """Termina, si puede, las retiradas que quedaron a medias. Se llama al arrancar y desde la pantalla de Registro. No
    cambia nada de lo que ya vale: una retirada comprometida invalida sus revisiones esté o no terminada."""
    resultados = []
    for d in retiradas_incompletas():
        if not CANDADO.acquire(blocking=False):
            break
        try:
            resultados.append(completar_retirada(d))
        except RetiradaIncompleta as e:
            resultados.append(e.cuerpo())
        finally:
            CANDADO.release()
    return resultados


def revisiones_registradas() -> dict:
    """Lo que hay registrado: la revisión de cada paciente y la de cada corrida, con su estado y los paquetes afectados;
    las retiradas que quedaron a medias; y los diarios de retirada que requieren atención."""
    carpeta = carpeta_reportes()
    estado = estado_de_retiradas()
    bloqueo = estado["bloqueo"]
    pacientes, corridas = [], []
    for ruta in sorted(carpeta.glob("revision_pixeles_*.json")) if carpeta.is_dir() else []:
        pac = ruta.stem.replace("revision_pixeles_", "")
        if not PACIENTE_RE.match(pac) or not (TRABAJO / "limpios" / pac).is_dir():
            continue
        r = leer_revision(pac)
        pacientes.append({"paciente": pac, "estado": r.get("estado"), "vigente": r.get("vigente"), "detalle": r.get("detalle"),
                          "revisor": r.get("revisor"), "fecha": r.get("fecha"), "archivos": r.get("archivos_revisados"),
                          "origen": r.get("origen"), "retirada": r.get("retirada"), "paquetes_afectados": r.get("paquetes_afectados", []),
                          "inventario_de_paquetes": r.get("inventario_de_paquetes"),
                          "se_puede_retirar": not bloqueo and (r.get("estado") in ("sin_hallazgos", "con_hallazgos")
                                                               or str(r.get("detalle", "")).startswith("invalidada"))})
    for canonico, ruta in registros_de_corrida():
        try:
            datos = ruta.read_bytes()
            r = json.loads(datos.decode("utf-8"))
        except (OSError, ValueError):
            continue
        huella = hashlib.sha256(datos).hexdigest()
        diario = None if bloqueo else next((d for d in estado["diarios"] for a in d["alcance"]
                                            if a.get("tipo") == "corrida" and a.get("id") == canonico and a.get("sha256") == huella), None)
        z = r.get("zip") or {}
        zip_ruta = Path(z.get("destino") or "") / (z.get("nombre") or "")
        corridas.append({"registro": canonico, "provisional": ruta.name != canonico,
                         "revisor": r.get("revisor"), "fecha": r.get("fecha"), "pacientes": r.get("pacientes", []),
                         "archivos": len(r.get("archivos", {})), "mostrado": r.get("mostrado"),
                         "zip": {"nombre": z.get("nombre"), "destino": z.get("destino"), "existe": zip_ruta.is_file() if z.get("nombre") else False},
                         "retirada": (diario or {}).get("retirada"), "paquetes_afectados": (diario or {}).get("paquetes_afectados", []),
                         "inventario_de_paquetes": resumen_de_inventario(diario) if diario else None,
                         "no_verificable": bloqueo, "se_puede_retirar": diario is None and not bloqueo})
    incompletas = [{"retirada": d["id"], "quien": d["retirada"]["quien"], "fecha": d["retirada"]["fecha"], "motivo": d["retirada"]["motivo"],
                    "solicitada": d.get("solicitada"), "completadas": [a["descripcion"] for a in d["acciones"] if a["estado"] == "hecha"],
                    "pendientes": [a["descripcion"] for a in d["acciones"] if a["estado"] != "hecha"]} for d in retiradas_incompletas()]
    return {"pacientes": pacientes, "corridas": corridas, "retiradas_incompletas": incompletas,
            "diarios_con_problemas": estado["problemas"], "retiradas_no_verificables": bloqueo, "nota": NOTA_RETIRADA}


@api.get("/revision/{pac}")
def revision(request: Request, pac: str):
    auth(request)
    carpeta_paciente(pac)
    if not INDICE:
        indexar()
    return {**leer_revision(pac), "progreso": progreso_paciente(pac)}


def registrar_revision_de_paciente(pac: str, estado: str, hallazgos: list[str], revisor: str, notas: str = "") -> dict:
    """Registra la revisión de píxeles de un paciente desde la pestaña Estudios, bajo el mismo candado que las
    importaciones, las corridas, los empaquetados y las retiradas: mientras se registra, nada más toca sus archivos ni sus
    registros. Para 'sin hallazgos', los archivos del paciente en disco tienen que ser exactamente los del inventario que
    se recorrió, y cada uno tiene que haberse mostrado con los bytes que tiene ahora."""
    carpeta_paciente(pac)
    error = validar_registro_revision(estado, hallazgos, revisor)
    if error:
        raise HTTPException(400, error)
    if not CANDADO.acquire(blocking=False):
        raise HTTPException(409, "hay otro trabajo en curso; espera a que termine antes de registrar la revisión")
    try:
        if estado != "sin_hallazgos":
            return guardar_revision(pac, estado, hallazgos, revisor, notas)
        retiradas = estado_de_retiradas()
        if retiradas["bloqueo"]:                                      # antes de exigir nada más: se dice qué diario requiere atención
            raise RevisionBloqueada(retiradas["problemas"], "registrar una revisión sin hallazgos")
        if not INDICE:
            indexar()
        if any(s_["paciente"] == pac and s_.get("cuadros_max", 1) > 1 for s_ in INDICE.values()):
            raise HTTPException(409, "este paciente tiene archivos con varios cuadros y la pestaña Estudios muestra uno por archivo; "
                                     "para registrar 'sin hallazgos' recorre todos los cuadros en el visor de una corrida del modo rápido")
        actuales = hashes_paciente(pac)                               # una lectura de los archivos: con ella se comprueba y se registra
        avance = progreso_paciente(pac, actuales)
        if avance["fuera_del_inventario"] or avance["ya_no_estan"]:
            indexar()                                                 # el inventario estaba desactualizado: se rehace para que se vea lo que falta
            raise HTTPException(409, "los archivos de este paciente ya no son los del inventario que se recorrió: "
                                     f"{avance['fuera_del_inventario']} archivo(s) llegaron después y {avance['ya_no_estan']} ya no están. "
                                     "El inventario se ha actualizado; pulsa Refrescar, recorre lo que falta y vuelve a registrar")
        if not avance["completo"]:
            raise HTTPException(409, f"en el navegador se han mostrado {avance['mostrados']} de {avance['total']} cortes de este paciente "
                                     "tal como están ahora; para declarar 'sin hallazgos', todos tienen que haber estado en pantalla")
        mostrado = {"cuadros_mostrados": avance["mostrados"], "cuadros_totales": avance["total"], "donde": "pestaña Estudios, en el navegador", "nota": NOTA_MOSTRADO}
        return guardar_revision(pac, estado, hallazgos, revisor, notas, mostrado=mostrado, archivos=actuales)
    finally:
        CANDADO.release()


@api.post("/revision")
def registrar_revision(request: Request, cuerpo: dict):
    """Registra la revisión de píxeles de un paciente contra los hashes exactos de sus archivos."""
    auth(request)
    try:
        return registrar_revision_de_paciente(str(cuerpo.get("paciente", "")), str(cuerpo.get("estado", "")),
                                              [str(h) for h in cuerpo.get("hallazgos", [])], str(cuerpo.get("revisor", "")).strip()[:120],
                                              str(cuerpo.get("notas", ""))[:2000])
    except RevisionBloqueada as e:
        return JSONResponse(status_code=409, content=e.cuerpo())


@api.get("/revisiones")
def revisiones(request: Request):
    auth(request)
    return revisiones_registradas()


@api.post("/revisiones/retirar")
def retirar(request: Request, cuerpo: dict):
    """Retira una revisión registrada, de un paciente o de una corrida, con quién y por qué. No borra ningún registro."""
    auth(request)
    try:
        return retirar_revision(str(cuerpo.get("tipo", "")), str(cuerpo.get("id", "")), str(cuerpo.get("quien", "")), str(cuerpo.get("motivo", "")))
    except RetiradaIncompleta as e:
        return JSONResponse(status_code=500, content=e.cuerpo())


@api.post("/revisiones/retiradas/reanudar")
def reanudar(request: Request):
    """Termina las retiradas que quedaron comprometidas e incompletas."""
    auth(request)
    confirmar_evidencias_publicadas()
    estado = estado_de_retiradas()
    return {"resultados": reanudar_retiradas(), "incompletas": len(retiradas_incompletas()),
            "diarios_con_problemas": estado["problemas"], "retiradas_no_verificables": estado["bloqueo"]}


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
  <span class="lema">anonimizar · revisar · enviar</span>
  <span class="local" title="La aplicación lee tus estudios y escribe sus resultados en esta computadora"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4.5" width="18" height="11.5" rx="1.8"/><path d="M8.5 20h7M12 16v4"/></svg>Procesamiento local</span>
  <span class="version" id="version"></span>
</header>
<nav class="pestanas"><button data-tab="rapido" title="Una carpeta, de principio a fin, con un solo botón"><span class="paso">»</span>Rápido</button><button data-tab="entrada" class="activa"><span class="paso">1</span>Entrada</button><button data-tab="estudios"><span class="paso">2</span>Estudios</button><button data-tab="enviar"><span class="paso">3</span>Enviar</button><button data-tab="registro"><span class="paso">·</span>Registro</button></nav>
<main>
<section id="rapido" class="pestana">
  <h2>Anonimizar una carpeta, de principio a fin</h2>
  <p class="suave frase">Un botón encadena seis comprobaciones y deja un zip en esta computadora. Mirar las imágenes sigue siendo tarea de una persona: el zip sale al destino cuando alguien declara, con su nombre, que las revisó.</p>
  <div class="rap-formulario">
    <div class="campo"><label for="rap-origen">Carpeta del estudio</label><div class="fila"><input type="text" id="rap-origen" placeholder="Carpeta del estudio, en el disco local"><button id="btn-rap-origen">Elegir carpeta...</button></div></div>
    <div class="campo"><label for="rap-destino">Destino del zip revisado</label><div class="fila"><input type="text" id="rap-destino" placeholder="Dónde guardar el zip ya revisado"><button id="btn-rap-destino">Elegir destino...</button></div></div>
    <div class="fila accion"><button id="btn-rapido" class="primario">Anonimizar carpeta</button><span id="rap-reloj" class="terciario" aria-live="polite"></span></div>
  </div>
  <div id="rap-estado" aria-live="polite"></div>
  <div id="rap-franja" class="franja oculto" role="group" aria-label="Estado de la corrida, en tres partes">
    <div id="rap-f-proceso"><span class="que">Procesamiento</span><span class="como"></span><span class="detalle"></span></div>
    <div id="rap-f-revision"><span class="que">Revisión de píxeles</span><span class="como"></span><span class="detalle"></span></div>
    <div id="rap-f-paquete"><span class="que">Paquete</span><span class="como"></span><span class="detalle"></span></div>
  </div>
  <div id="rap-area-resumen" class="area oculto">
    <h3>Resumen de la corrida</h3>
    <p class="suave frase">Lo que consta de esta corrida, en tres partes. Es una lectura de los registros de la aplicación; el manifiesto del zip y los registros de revisión siguen siendo la fuente.</p>
    <p id="rap-frescura" class="terciario" aria-live="polite"></p>
    <div id="rap-resumen" class="resumen"></div>
    <div class="fila"><button id="btn-rap-copiar">Copiar resumen</button><button id="btn-rap-comprobar">Volver a comprobar</button><span id="rap-comprobado" class="terciario" aria-live="polite"></span></div>
    <textarea id="rap-copia" class="oculto" rows="8" readonly aria-label="Resumen de la corrida, para copiar"></textarea>
    <p class="terciario">El resumen copiado lleva estados, cifras y versiones. Deja fuera rutas, nombres de archivo, la etiqueta del estudio y el nombre de quien revisó. Describe registros de la aplicación; el permiso para compartir los datos lo da tu institución.</p>
  </div>
  <div id="rap-area-proceso" class="area oculto">
    <h3>Procesamiento</h3>
    <p id="rap-proceso-frase" class="suave frase"></p>
    <div id="rap-pasos" class="libro"></div>
  </div>
  <div id="rap-area-revision" class="area oculto">
    <h3>Revisión de píxeles</h3>
    <p id="rap-revision-frase" class="suave frase"></p>
    <div id="rap-revision-aviso"></div>
    <div id="rap-visor" class="oculto">
      <h4 id="rap-visor-titulo">Las imágenes de este paquete, en su orden</h4>
      <p class="terciario">Son exactamente los archivos del zip de esta corrida, con la numeración de sus nombres. Importaciones posteriores no cambian esta lista. La pestaña Estudios muestra todo lo guardado del paciente, que puede ser más.</p>
      <div class="visor-corrida"><div><p id="rap-etiqueta-estudio" class="etiqueta-estudio"></p><div id="rap-aviso-demografia"></div><div class="lienzo"><img id="rap-corte" alt="corte de la corrida"></div>
          <input type="range" id="rap-deslizador" min="1" max="1" value="1" aria-label="Corte de la serie">
          <div id="rap-cuadros" class="oculto"><label class="terciario" for="rap-cuadro">Cuadros de este archivo</label><input type="range" id="rap-cuadro" min="1" max="1" value="1"></div>
          <p id="rap-etiqueta" class="mono"></p></div>
        <div><div id="rap-series" class="series" role="group" aria-label="Series del paquete"></div>
          <div class="fila"><label>Ventana <select id="rap-ventana"><option value="tejido">tejido blando</option><option value="pulmon">pulmón</option><option value="hueso">hueso</option><option value="cerebro">cerebro</option><option value="auto">automática (PET y RM; solo cambia la vista)</option></select></label><button id="btn-rap-recorrer">Recorrer la serie</button></div>
          <div class="mostrados"><p id="rap-progreso"></p><span class="barra"><i id="rap-barra"></i></span>
            <p class="terciario">Un cuadro mostrado es un cuadro que estuvo en pantalla. Que alguien lo haya examinado es lo que declara quien firma.</p></div>
          <div id="rap-orden"></div></div></div>
    </div>
    <div id="rap-revision" class="declarar oculto">
      <h4>Declaración de quien revisa</h4>
      <div class="fila"><label for="rap-revisor" class="terciario">Tu nombre</label><input type="text" id="rap-revisor" placeholder="quien revisa" class="corto"><button id="btn-rap-revision" class="primario">Revisé todos los cortes: sin hallazgos</button></div>
      <p id="rap-porque" class="terciario" aria-live="polite"></p>
      <p class="terciario">Vale para los archivos exactos de esta corrida: si alguno cambia, hay que repetirla. Al registrar, el zip revisado se escribe en el destino y el pendiente se elimina de la carpeta de trabajo. Si encuentras texto grabado o un rostro, no declares: anótalo como hallazgo en la pestaña Estudios.</p>
    </div>
  </div>
  <div id="rap-area-paquete" class="area oculto">
    <h3>Paquete</h3>
    <p id="rap-paquete-frase" class="suave frase"></p>
    <dl id="rap-paquete-ficha" class="ficha"></dl>
    <div id="rap-paquete-avisos"></div>
  </div>
</section>
<section id="entrada" class="pestana activa">
  <h2>Carpeta con los DICOM originales</h2>
  <p class="suave">Solo se leen desde donde están. La copia limpia se escribe en la carpeta de trabajo. Los objetos que no son CT, PET, MR o segmentación van a cuarentena y no se copian.</p>
  <div class="fila"><input type="text" id="origen" placeholder="/Users/tu/estudios-originales"><button id="btn-origen">Elegir carpeta…</button><button id="btn-demo" title="Crea un caso CT + PET inventado, sin datos de pacientes, para probar el flujo completo">Crear caso de demostración</button></div>
  <div id="aviso-origen"></div>
  <p class="terciario">Carpeta de trabajo: <code id="trabajo"></code> <span id="aviso-trabajo"></span></p>
  <div class="fila"><label>Prefijo <input type="text" id="prefijo" class="corto"></label>
    <label><input type="checkbox" id="conservar" aria-describedby="ayuda-conservar"> conservar descripciones de estudio, serie y protocolo (revisadas contra nombre e ID)</label>
    <button id="btn-anonimizar" class="primario">Anonimizar</button></div>
  <details class="ayuda" id="ayuda-conservar"><summary>Qué hace esa casilla</summary>
    <p>Sin marcar, la limpieza elimina la descripción del estudio, la descripción de la serie y el nombre del protocolo; por ejemplo, «T1_3D_SAG». Marcada, los conserva, después de sustituir por [REDACTADO] el nombre o el ID del paciente si aparecen en ellos.</p>
    <p>No conserva ningún otro dato del paciente. El nombre, el ID, las fechas y el resto de la identidad se quitan igual, con la casilla o sin ella. Esos textos los escribe una persona en el equipo y pueden decir algo que la aplicación no reconoce: si la marcas, léelos en la pestaña Estudios antes de compartir.</p>
    <p>Se aplica a los estudios que todavía no están guardados en esta carpeta de trabajo. Volver a limpiar un estudio ya guardado con la casilla cambiada deja la copia guardada como estaba, y la limpieza lo dice. El modo rápido limpia siempre sin ella.</p>
  </details>
  <div id="estado-anon"></div>
  <div id="explicacion-anon"></div>
  <pre id="salida-anon" class="salida oculto"></pre>
  <details id="det-reporte" class="oculto"><summary>Reporte de la limpieza</summary><pre id="reporte-anon" class="salida"></pre></details>
</section>
<section id="estudios" class="pestana">
  <div class="fila"><h2 class="sin-margen">Estudios limpios</h2><button id="btn-refrescar">Refrescar</button><span id="resumen-estudios" class="terciario"></span></div>
  <div class="tabla-marco"><table id="tabla-series" class="datos"><thead><tr><th>Paciente</th><th>Sexo · edad en el estudio</th><th>Estudio</th><th>Serie</th><th>Mod.</th><th>Descripción</th><th class="num">Cortes</th><th>Matriz</th><th>Política</th></tr></thead><tbody></tbody></table></div>
  <div class="visor oculto" id="visor">
    <div><div class="lienzo"><img id="corte" alt="corte"></div><input type="range" id="deslizador" min="0" max="0" value="0">
      <div class="fila"><label>Ventana <select id="ventana"><option value="tejido">tejido blando</option><option value="pulmon">pulmón</option><option value="hueso">hueso</option><option value="cerebro">cerebro</option><option value="auto">automática (PET y RM; solo cambia la vista)</option></select></label><span id="pos" class="terciario"></span></div>
      <p class="terciario"><span id="nota-orden">Cortes ordenados por InstanceNumber.</span> Vista previa sin escala cuantitativa; el SUV se calcula en las comprobaciones, no aquí. Al exportar, los archivos se numeran con este mismo orden.</p>
      <div id="aviso-orden"></div>
      <div class="fila"><button id="btn-recorrer">Recorrer la serie</button><span id="rev-progreso" class="terciario"></span></div></div>
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
        <p class="terciario">La revisión queda ligada a los hashes exactos de los archivos del paciente; si cambian, pasa a pendiente. Para registrar "sin hallazgos", todos los cortes de todas las series del paciente tienen que haber estado en pantalla en este navegador. Que un corte se haya mostrado no significa que esté revisado: la revisión es tu declaración.</p>
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
<section id="registro" class="pestana">
  <h2>Revisiones de píxeles registradas</h2>
  <p class="suave">Una revisión se puede retirar: el registro original se conserva, y una retirada con tu nombre, la fecha y el motivo lo sustituye. Retirar no cambia los zips ya exportados; la lista de paquetes afectados dice cuáles ya no deben compartirse.</p>
  <div id="revisiones-estado"></div>
  <div id="diarios-problemas"></div>
  <div id="retiradas-a-medias" class="oculto"><div id="retiradas-a-medias-texto"></div><div class="fila"><button id="btn-reanudar">Terminar las retiradas pendientes</button></div></div>
  <div class="tabla-marco oculto" id="marco-revisiones"><table id="tabla-revisiones" class="datos"><thead><tr><th>Alcance</th><th>Estado</th><th>Quién y cuándo</th><th>Archivos</th><th>Paquete</th><th></th></tr></thead><tbody></tbody></table></div>
  <div id="retirar-form" class="oculto">
    <h3 id="retirar-titulo">Retirar revisión</h3>
    <div class="fila"><input type="text" id="retirar-quien" placeholder="quien retira" class="corto"><input type="text" id="retirar-motivo" placeholder="motivo de la retirada"></div>
    <div class="fila"><button id="btn-retirar" class="primario">Retirar esta revisión</button><button id="btn-retirar-cancelar">Cancelar</button></div>
  </div>
  <div id="retirada-resultado"></div>
  <h2>Registro</h2><p class="terciario">Bitácora privada; se queda en la carpeta de trabajo. Rutas y seudónimos, nunca valores.</p><pre id="texto-registro" class="salida"></pre></section>
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
const nombreEstudio = s => s.estudio_numero ? 'Estudio ' + s.estudio_numero : s.estudio;
const celda = (tr, v, cls) => { const td = document.createElement('td'); td.textContent = v == null ? '' : String(v); if (cls) td.className = cls; tr.appendChild(td); return td; };
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
// Consulta del estado de un trabajo que no se queda colgada: un fallo se reintenta; si persiste, se dice, se ofrece
// volver a consultar y el botón principal vuelve a estar disponible. El trabajo puede seguir en marcha en el servidor.
function seguirTrabajo(trabajo, opciones) {
  let fallos = 0;
  const reanudar = () => { fallos = 0; paso(); };
  const paso = async () => {
    if (opciones.vigente && !opciones.vigente()) return;
    let t;
    try { t = await api('trabajo/' + trabajo); fallos = 0; }
    catch (err) {
      fallos += 1;
      if (fallos <= 5) { opciones.reintentando(fallos); setTimeout(paso, 1500); return; }
      opciones.sinRespuesta(err, reanudar); return;
    }
    try {
      if (t.estado === 'corriendo') { opciones.cada(t); setTimeout(paso, 800); }
      else opciones.fin(t);
    } catch (err) { opciones.sinRespuesta(err, reanudar); }
  };
  paso();
}
function botonReintentar(sel, rotulo, alPulsar) {
  const cont = $(sel).querySelector('.mensaje') || $(sel); const b = document.createElement('button'); b.textContent = rotulo; b.className = 'reintentar';
  b.onclick = () => { b.disabled = true; alPulsar(); }; cont.appendChild(document.createTextNode(' ')); cont.appendChild(b);
}
// Un cuadro cuenta como mostrado cuando su imagen se ha cargado, sigue siendo la que está en el visor un instante después
// y la pestaña y la imagen están a la vista. Mostrado no es revisado.
function alMostrar(img, avisar) {
  img.addEventListener('load', () => {
    const src = img.src;
    setTimeout(() => {
      if (img.src !== src || document.visibilityState !== 'visible' || !img.naturalWidth) return;
      const r = img.getBoundingClientRect();
      if (r.width === 0 || r.bottom <= 0 || r.top >= window.innerHeight) return;
      avisar(src);
    }, 120);
  });
}
// Recorrer: avanza un paso cada vez que la imagen anterior ha terminado de cargarse y ha estado un instante en pantalla.
function recorrido(boton, img, avanzar) {
  let activo = false;
  const parar = () => { activo = false; boton.textContent = 'Recorrer la serie'; };
  const paso = () => { if (!activo) return; if (!avanzar()) parar(); };
  img.addEventListener('load', () => { if (activo) setTimeout(paso, 160); });
  img.addEventListener('error', parar);
  // El corte que ya está en pantalla también tiene que cumplir su instante a la vista antes de pasar al siguiente.
  boton.onclick = () => { if (activo) return parar(); activo = true; boton.textContent = 'Detener'; setTimeout(paso, 300); };
  return parar;
}
let RAPIDO = null, BLOQUEO_REVISION = false, AVANCE_CORRIDA = null, ULTIMA = null, COMPROBADO = null, VIGENTE = false, REGISTRANDO = false, CONSULTA = 0;
const ABIERTOS = new Set();
// ---- piezas pequeñas: iconos en línea (ningún recurso externo), elementos y plurales ----
const TRAZOS = {
  ok: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.7 2.7L16 9.8"/>',
  aviso: '<path d="M12 3.5 21.5 20h-19z"/><path d="M12 10v4.5M12 17.2v.3"/>',
  error: '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
  curso: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  espera: '<circle cx="12" cy="12" r="9" stroke-dasharray="3 3.4"/>',
  omitido: '<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>',
  paquete: '<path d="M3.5 8 12 3.5 20.5 8v8L12 20.5 3.5 16z"/><path d="M3.5 8 12 12.5 20.5 8M12 12.5v8"/>',
};
function icono(nombre) {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('class', 'ico'); s.setAttribute('aria-hidden', 'true');
  s.innerHTML = TRAZOS[nombre] || TRAZOS.espera; return s;
}
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = String(txt); return e; };
const plural = (n, uno, varios) => n + ' ' + (n === 1 ? uno : varios);
const fechaLegible = iso => { const m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)/.exec(iso || ''); return m ? m[3] + '/' + m[2] + '/' + m[1] + ', ' + m[4] + ':' + m[5] : (iso || ''); };
// Un aviso que responde a tres preguntas: qué pasó, a qué afecta y qué se puede hacer. Las acciones son las que la aplicación tiene.
function cajaAviso(tipo, titulo, filas, acciones) {
  const caja = el('div', 'caja-aviso ' + tipo); caja.setAttribute('role', tipo === 'error' ? 'alert' : 'status');
  const cab = el('div', 'caja-titulo'); cab.appendChild(icono(tipo === 'error' ? 'error' : 'aviso')); cab.appendChild(el('strong', null, titulo)); caja.appendChild(cab);
  const dl = el('dl');
  for (const [dt, dd] of filas) { if (!dd) continue; dl.appendChild(el('dt', null, dt)); const d = el('dd'); if (dd instanceof Node) d.appendChild(dd); else d.textContent = dd; dl.appendChild(d); }
  caja.appendChild(dl);
  if (acciones && acciones.length) { const f = el('div', 'fila'); for (const [rotulo, alPulsar, cls] of acciones) { const b = el('button', cls || '', rotulo); b.onclick = alPulsar; f.appendChild(b); } caja.appendChild(f); }
  return caja;
}
const irARegistro = () => document.querySelector('nav button[data-tab=registro]').click();

// ---- los seis pasos: nombre, estado con icono y texto, una frase, la cifra cuando el servidor la da, y el detalle ----
const ESTADO_PASO = {pendiente: ['espera', 'neutro', 'En espera'], corriendo: ['curso', 'activo', 'En curso'], ok: ['ok', 'ok', 'Correcto'],
  aviso: ['aviso', 'aviso', 'Aviso'], error: ['error', 'error', 'Detenido aquí'], omitido: ['omitido', 'neutro', 'No evaluado']};
const QUE_COMPRUEBA = {
  limpieza: 'Lee cada archivo de la carpeta, quita nombres, fechas e identificadores y guarda la copia limpia. Los archivos que no son imágenes DICOM se omiten. Un DICOM que no se puede limpiar con seguridad se aparta en cuarentena y queda fuera del paquete.',
  compuerta: 'Vuelve a leer cada archivo limpio de esta corrida y comprueba que sus etiquetas cumplen la política de anonimización.',
  cotejo: 'Repite la limpieza de cada original y compara el resultado, byte a byte, con la copia limpia guardada. Cubre exactamente los archivos de esta corrida.',
  emparejamiento: 'Comprueba si la serie CT y la serie PET de cada estudio se corresponden. Informa; el paquete se crea en cualquier caso.',
  suv: 'Calcula el SUV de cada serie PET con el original y con la copia limpia, y compara los dos. Una diferencia real detiene el proceso. Que falten datos para calcularlo es un aviso.',
  paquete: 'Reúne los archivos limpios de esta corrida en un zip, numerados por corte, con su manifiesto. El zip nace con la revisión de píxeles pendiente y se queda en la carpeta de trabajo.',
};
const HACIENDO = {
  limpieza: 'Leyendo y anonimizando los archivos de la carpeta.', compuerta: 'Comprobando las etiquetas de cada archivo limpio.',
  cotejo: 'Repitiendo la limpieza de cada archivo y comparándola con la copia guardada.', emparejamiento: 'Comparando la serie CT con la serie PET de cada estudio.',
  suv: 'Calculando el SUV con el original y con la copia limpia.', paquete: 'Escribiendo y verificando el zip.',
};
function detenidoAntes(t, i) { const k = (t.pasos || []).findIndex(x => x.estado === 'error'); return k >= 0 && i > k; }
function frasePaso(t, p, i) {
  const d = p.datos || {}, e = p.estado, c = p.clave;
  if (e === 'pendiente') return 'Empieza cuando termine el paso anterior.';
  if (e === 'corriendo') return HACIENDO[c] || 'En curso.';
  if (e === 'omitido' && detenidoAntes(t, i)) return 'No se ejecutó: el proceso se detuvo antes.';
  if (c === 'limpieza' && e === 'error') {
    const otra = Object.entries(d.cuarentena_motivos || {}).filter(([m]) => m.split(' / ')[0] === OTRA_OPCION).reduce((a, [, n]) => a + n, 0);
    if (otra) return plural(otra, 'imagen ya estaba guardada', 'imágenes ya estaban guardadas') + ' con la otra opción de descripciones y no se ' + (otra === 1 ? 'sustituyó' : 'sustituyeron') + '. No se creó ningún zip.';
    if (d.cuarentena_total) return (d.cuarentena_total === 1 ? 'El único DICOM de la carpeta quedó' : 'Los ' + d.cuarentena_total + ' DICOM de la carpeta quedaron') + ' en cuarentena. No se creó ningún zip.';
    return 'Ningún archivo de la carpeta quedó limpio. No se creó ningún zip.';
  }
  if (c === 'limpieza' && d.en_la_corrida != null) {
    let s = plural(d.en_la_corrida, 'archivo anonimizado', 'archivos anonimizados') + ', de ' + plural(d.pacientes, 'paciente', 'pacientes') + '.';
    if (d.omitidos_total) s += ' ' + plural(d.omitidos_total, 'archivo no es una imagen DICOM y se omitió', 'archivos no son imágenes DICOM y se omitieron') + '.';
    if (d.cuarentena_total) s += ' ' + plural(d.cuarentena_total, 'DICOM quedó en cuarentena, fuera del paquete', 'DICOM quedaron en cuarentena, fuera del paquete') + '.';
    else if (d.cuarentena_total === 0) s += ' Ninguno en cuarentena.';
    return s;
  }
  if (c === 'compuerta' && e === 'ok' && d.comprobados != null) return 'Los ' + plural(d.comprobados, 'archivo limpio pasa', 'archivos limpios pasan') + ' la comprobación de etiquetas.';
  if (c === 'compuerta' && e === 'error' && d.con_problemas != null) return plural(d.con_problemas, 'archivo limpio tiene', 'archivos limpios tienen') + ' etiquetas que la política no admite. No se creó ningún zip.';
  if (c === 'cotejo' && e === 'ok' && d.coinciden != null) return 'Al repetir la limpieza, ' + (d.coinciden === 1 ? 'el archivo coincide' : 'los ' + d.coinciden + ' coinciden') + ' byte a byte.';
  if (c === 'cotejo' && e === 'error') return (d.clase === 'conjunto_distinto' ? 'El cotejo y la limpieza no cubren los mismos archivos.' : 'La copia limpia no coincide con la limpieza repetida.') + ' No se creó ningún zip.';
  if (c === 'emparejamiento' && d.estudios != null) {
    const partes = [];
    if (d.compatibles) partes.push(plural(d.compatibles, 'estudio con CT y PET compatibles', 'estudios con CT y PET compatibles'));
    if (d.inconsistentes) partes.push(plural(d.inconsistentes, 'estudio en el que CT y PET no se corresponden', 'estudios en los que CT y PET no se corresponden'));
    if (d.no_evaluables) partes.push(plural(d.no_evaluables, 'estudio que no se pudo evaluar', 'estudios que no se pudieron evaluar') + ' (falta CT o PET, o información)');
    return partes.join('; ') + '.' + (e !== 'ok' ? ' El paquete se crea igual.' : '');
  }
  if (c === 'suv' && d.series === 0) return 'No hay series PET en esta carpeta; no hay SUV que comparar.';
  if (c === 'suv' && d.clases) {
    const k = d.clases, partes = [];
    if (k.identico) partes.push(plural(k.identico, 'serie PET con valores idénticos', 'series PET con valores idénticos') + ' antes y después de limpiar');
    if (k.discrepancia) partes.push(plural(k.discrepancia, 'serie PET cuyo SUV difiere', 'series PET cuyo SUV difiere') + ' entre el original y la copia limpia');
    if (k.no_calculable) partes.push(plural(k.no_calculable, 'serie PET en la que no se puede calcular el SUV', 'series PET en las que no se puede calcular el SUV') + ' porque faltan datos');
    const otras = (k.correspondencia_incompleta || 0) + (k.orden_incoherente || 0) + (k.sin_veredicto || 0);
    if (otras) partes.push(plural(otras, 'serie PET cuya comparación no se pudo completar', 'series PET cuya comparación no se pudo completar'));
    return partes.join('; ') + '.' + (e === 'error' ? ' No se creó ningún zip.' : (e === 'aviso' ? ' No es una discrepancia; el paquete se crea igual.' : ''));
  }
  if (c === 'paquete' && d.clase === 'creado') {
    let s = 'Zip creado con ' + plural(d.archivos, 'archivo', 'archivos') + ' en ' + plural(d.series, 'serie', 'series') + ', numerados por corte.';
    if (d.orden_no_verificado) s += ' En ' + plural(d.orden_no_verificado, 'serie', 'series') + ' el orden de los cortes no se pudo verificar.';
    if (d.no_mostrables) s += ' ' + plural(d.no_mostrables, 'imagen no se puede', 'imágenes no se pueden') + ' mostrar en el visor.';
    return s;
  }
  return p.resumen || '';                                           // sin datos: la frase del servidor, tal cual
}
function cifraPaso(p) {
  const d = p.datos || {}, c = p.clave;
  if (p.estado === 'pendiente' || p.estado === 'corriendo') return null;
  if (c === 'limpieza' && d.en_la_corrida != null && d.leidos != null) return [d.en_la_corrida, 'de ' + d.leidos + ' leídos'];
  if (c === 'compuerta' && d.comprobados != null) return [d.comprobados - (d.con_problemas || 0), 'de ' + d.comprobados];
  if (c === 'cotejo' && d.coinciden != null && d.esperados != null) return [d.coinciden, 'de ' + d.esperados];
  if (c === 'emparejamiento' && d.estudios != null) return [d.compatibles, 'de ' + plural(d.estudios, 'estudio', 'estudios')];
  if (c === 'suv' && d.series) return [(d.clases || {}).identico || 0, 'de ' + plural(d.series, 'serie PET', 'series PET')];
  if (c === 'paquete' && d.archivos != null) return [d.archivos, d.archivos === 1 ? 'archivo' : 'archivos'];
  return null;
}
function siguientePaso(t, p, i) {
  const d = p.datos || {}, e = p.estado, c = p.clave, k = d.clases || {};
  if (e === 'omitido' && detenidoAntes(t, i)) return 'Este paso se ejecutará cuando el proceso llegue hasta él. Corrige lo que detuvo el paso anterior y vuelve a pulsar Anonimizar carpeta.';
  if (c === 'limpieza' && e === 'error' && Object.keys(d.cuarentena_motivos || {}).some(m => m.split(' / ')[0] === OTRA_OPCION))
    return 'Esas imágenes se guardaron antes desde la pestaña Entrada, con la casilla de conservar descripciones marcada. El modo rápido limpia sin ella y no sustituye lo guardado. Siguen en la pestaña Estudios: desde allí se revisan, y desde Enviar se empaquetan.';
  if (c === 'limpieza' && e === 'error') return 'No se creó ningún zip. Si la carpeta no contiene imágenes CT, PET o MR, no hay nada que anonimizar; en otro caso, la salida del paso dice dónde se detuvo.';
  if (c === 'limpieza' && d.cuarentena_total) return 'Los DICOM en cuarentena son imágenes del estudio que quedan fuera del paquete; no son lo mismo que los archivos omitidos, que no eran imágenes. El reporte de la carpeta limpios_REPORTES dice el motivo de cada uno, sin valores.';
  if (c === 'compuerta' && e === 'error') return 'No se creó ningún zip. La salida del paso dice qué archivo y qué etiqueta; envíala a quien mantiene la aplicación. Los archivos limpios se quedan en esta computadora.';
  if (c === 'cotejo' && e === 'error') return 'No se creó ningún zip. Vuelve a ejecutar el modo rápido sobre la carpeta; si se repite, envía la salida del paso a quien mantiene la aplicación.';
  if (c === 'emparejamiento' && d.inconsistentes) return 'Es un aviso y no detiene el proceso. Tenlo en cuenta si vas a usar las dos series juntas: la salida del paso dice en qué difieren.';
  if (c === 'emparejamiento' && d.no_evaluables) return 'No indica un error en los archivos: falta una de las dos series, o los datos para compararlas. No detiene el proceso.';
  if (c === 'suv' && k.discrepancia) return 'No se creó ningún zip. La diferencia está en la cuantificación de esa serie; el emparejamiento CT + PET es otra comprobación y no la explica. Envía la salida del paso a quien mantiene la aplicación.';
  if (c === 'suv' && k.no_calculable) return 'A los archivos les faltan datos para calcular el SUV (por ejemplo peso, dosis u horas), tanto en el original como en la copia limpia. No es una diferencia entre los dos y no detiene el proceso.';
  if (c === 'suv' && e === 'aviso') return 'La comparación no se pudo completar; no se ha encontrado una diferencia de valores. La salida del paso dice el motivo.';
  if (c === 'paquete' && e === 'error') return 'El paquete no se creó. La salida del paso dice el motivo y, si quedó algún archivo intermedio, su ruta.';
  if (c === 'paquete' && d.orden_no_verificado) return 'Los metadatos de esas series no permiten verificar el orden; se usó un desempate fijo. Compruébalo en el visor al revisar.';
  return '';
}
const ROTULOS_DATOS = {
  limpieza: [['leidos', 'Archivos leídos en la carpeta'], ['en_la_corrida', 'Archivos limpios de esta corrida'], ['escritos', 'Escritos ahora'], ['ya_guardados', 'Ya guardados, verificados'],
             ['actualizados', 'Actualizados de una versión anterior'], ['omitidos_total', 'Omitidos por no ser imágenes DICOM'], ['cuarentena_total', 'DICOM apartados en cuarentena']],
  compuerta: [['comprobados', 'Archivos comprobados'], ['con_problemas', 'Con etiquetas no admitidas']],
  cotejo: [['coinciden', 'Coinciden byte a byte'], ['esperados', 'Archivos de esta corrida']],
  emparejamiento: [['estudios', 'Estudios'], ['compatibles', 'CT y PET compatibles'], ['inconsistentes', 'CT y PET no se corresponden'], ['no_evaluables', 'No evaluables']],
  suv: [['series', 'Series PET']],
  paquete: [['archivos', 'Archivos en el zip'], ['series', 'Series'], ['pacientes', 'Pacientes'], ['orden_no_verificado', 'Series con orden no verificado'],
            ['orden_por_posicion', 'Series ordenadas por posición'], ['con_varios_cuadros', 'Archivos con varios cuadros'], ['no_mostrables', 'Imágenes que el visor no puede mostrar'],
            ['de_importaciones_anteriores', 'Archivos del paciente de importaciones anteriores, fuera de este zip']],
};
const CLASES_SUV = {identico: 'Valores idénticos', discrepancia: 'Con diferencia real', no_calculable: 'No calculable (faltan datos)', correspondencia_incompleta: 'Correspondencia incompleta',
  orden_incoherente: 'Orden o geometría incoherentes', sin_veredicto: 'Sin veredicto'};
function detallePaso(t, p, i) {
  const caja = el('div', 'paso-detalle'), d = p.datos || {};
  caja.appendChild(el('p', null, QUE_COMPRUEBA[p.clave] || ''));
  const filas = (ROTULOS_DATOS[p.clave] || []).filter(([k]) => d[k] != null).map(([k, r]) => [r, d[k]]);
  if (p.clave === 'suv') for (const [k, n] of Object.entries(d.clases || {})) filas.push([CLASES_SUV[k] || k, n]);
  if (p.clave === 'limpieza') for (const [k, n] of Object.entries(d.cuarentena_motivos || {})) filas.push(['En cuarentena: ' + k, n]);
  if (filas.length) { const dl = el('dl', 'cifras'); for (const [r, v] of filas) { dl.appendChild(el('dt', null, r)); dl.appendChild(el('dd', null, v)); } caja.appendChild(dl); }
  const sig = siguientePaso(t, p, i); if (sig) { const q = el('p', 'siguiente'); q.appendChild(el('strong', null, 'Qué significa y qué puedes hacer. ')); q.appendChild(document.createTextNode(sig)); caja.appendChild(q); }
  if (p.salida) { const det = el('details', 'salida-paso'); det.appendChild(el('summary', null, 'Salida completa del paso')); det.appendChild(el('pre', 'salida', p.salida)); caja.appendChild(det); }
  return caja;
}
function pintarPasos(t) {
  const cont = $('#rap-pasos'); cont.textContent = '';
  (t.pasos || []).forEach((p, i) => {
    let [ico, cls, txt] = ESTADO_PASO[p.estado] || ESTADO_PASO.pendiente;
    if (p.estado === 'omitido' && detenidoAntes(t, i)) txt = 'No ejecutado';
    const fila = el('details', 'paso-fila ' + cls + (p.estado === 'corriendo' ? ' en-curso' : '')); fila.dataset.clave = p.clave; fila.dataset.estado = p.estado;
    if (ABIERTOS.has(p.clave)) fila.open = true;
    fila.addEventListener('toggle', () => { if (fila.open) ABIERTOS.add(p.clave); else ABIERTOS.delete(p.clave); });
    const cab = el('summary');
    cab.appendChild(el('span', 'paso-num', i + 1)); cab.appendChild(el('span', 'paso-nombre', p.titulo));
    const est = el('span', 'paso-estado ' + cls); est.appendChild(icono(ico)); est.appendChild(el('span', null, txt)); cab.appendChild(est);
    cab.appendChild(el('span', 'paso-resumen', frasePaso(t, p, i)));
    const cifra = cifraPaso(p), cc = el('span', 'paso-cifra'); if (cifra) { cc.appendChild(el('b', null, cifra[0])); cc.appendChild(el('small', null, cifra[1])); } cab.appendChild(cc);
    fila.appendChild(cab); fila.appendChild(detallePaso(t, p, i)); cont.appendChild(fila);
  });
}

// ---- tres estados que no se mezclan: procesamiento, revisión de píxeles, paquete ----
function celdaFranja(sel, cls, ico, como, detalle) {
  const c = $(sel); c.className = cls; const k = c.querySelector('.como'); k.textContent = ''; k.appendChild(icono(ico)); k.appendChild(el('span', null, como)); c.querySelector('.detalle').textContent = detalle || '';
}
function estadoProceso(t, s) {
  const p = s.procesamiento, pasos = t.pasos || [];
  if (p.estado === 'en_curso') return ['activo', 'curso', 'En curso, paso ' + (p.paso_en_curso || 1) + ' de ' + p.total, p.paso_en_curso ? 'Ahora: ' + pasos[p.paso_en_curso - 1].titulo.toLowerCase() + '.' : ''];
  if (p.estado === 'detenido') return ['error', 'error', p.paso_detenido ? 'Detenido en el paso ' + p.paso_detenido : 'Detenido', (p.paso_detenido ? pasos[p.paso_detenido - 1].titulo + '. ' : '') + 'No se creó ningún zip.'];
  const cuenta = p.correctos + ' de ' + p.total + ' comprobaciones correctas' + (p.con_aviso ? ', ' + p.con_aviso + ' con aviso' : '') + (p.no_evaluados ? ', ' + p.no_evaluados + ' sin evaluar' : '') + '.';
  return p.estado === 'terminado' && !p.no_evaluados ? ['ok', 'ok', 'Terminado', cuenta] : ['aviso', 'aviso', p.con_aviso ? 'Terminado con avisos' : 'Terminado', cuenta];
}
function estadoRevision(s) {
  const r = s.revision, m = r.cuadros_mostrados + ' de ' + r.cuadros_totales + ' cuadros mostrados.';
  if (r.estado === 'sin_paquete') return ['neutro', 'espera', 'Todavía no', s.procesamiento.estado === 'detenido' ? 'No hay paquete que revisar.' : 'Se abre cuando exista el paquete de esta corrida.'];
  if (r.estado === 'pendiente') return ['aviso', 'aviso', 'Pendiente', m + ' Nadie ha declarado la revisión.'];
  if (r.estado === 'registrada') return ['ok', 'ok', 'Registrada', 'Declarada por ' + r.revisor + ' el ' + fechaLegible(r.fecha) + '.'];
  if (r.estado === 'retirada') return ['aviso', 'aviso', 'Retirada', 'Por ' + r.retirada.quien + ' el ' + fechaLegible(r.retirada.fecha) + '.'];
  if (r.estado === 'no_verificable') return ['error', 'error', r.habia_registro ? 'No verificable' : 'No se puede registrar', 'El estado de las retiradas no se puede verificar.'];
  return ['error', 'error', 'No verificable', 'El registro de la revisión no se puede leer.'];
}
function estadoPaquete(s) {
  const q = s.paquete, r = s.revision;
  if (q.estado === 'no_creado') return s.procesamiento.estado === 'detenido' ? ['error', 'error', 'No se creó', 'El proceso se detuvo antes de crearlo.'] : ['neutro', 'espera', 'Todavía no existe', 'Se crea en el paso 6, en esta computadora.'];
  if (q.estado === 'no_encontrado') return ['aviso', 'aviso', 'No está donde se dejó', 'El zip ya no está en la carpeta en la que la aplicación lo escribió.'];
  if (q.estado === 'modificado') return ['error', 'error', 'El zip cambió', 'El archivo ya no coincide con el paquete publicado; no lo compartas como revisado.'];
  if (q.estado === 'no_comprobable') return ['error', 'error', 'No se pudo comprobar', 'No se pudo verificar la integridad del zip.'];
  if (q.estado === 'pendiente_local') return q.ultimo_intento_fallido ? ['error', 'paquete', 'Creado, sin publicar', 'El último intento de publicarlo falló. Sigue en esta computadora.']
    : ['aviso', 'paquete', 'Creado, sin publicar', 'Está en esta computadora. Aún no ha llegado al destino.'];
  if (r.estado === 'retirada') return ['aviso', 'paquete', 'Publicado; revisión retirada', 'El zip sigue en el destino y ya no debe compartirse.'];
  if (r.estado !== 'registrada') return ['aviso', 'paquete', 'Publicado; revisión sin verificar', 'El zip está en el destino, pero su revisión no se puede verificar ahora.'];
  return q.acciones_pendientes.length ? ['aviso', 'paquete', 'Publicado, con avisos', 'El zip revisado está en el destino. Queda algo por atender.'] : ['ok', 'paquete', 'Publicado en el destino', 'El zip revisado está en la carpeta que elegiste.'];
}
function fichaDe(sel, filas) {
  const dl = $(sel); dl.textContent = '';
  for (const [dt, dd, cls] of filas) { if (dd == null || dd === '') continue; dl.appendChild(el('dt', null, dt)); dl.appendChild(el('dd', cls || null, dd)); }
}
function pintarAreaRevision(t, s) {
  const r = s.revision, zona = $('#rap-revision-aviso'); zona.textContent = '';
  const hayPaquete = r.estado !== 'sin_paquete';
  $('#rap-area-revision').classList.toggle('oculto', !hayPaquete); if (!hayPaquete) return;
  const frases = {
    pendiente: 'Pendiente. Falta que una persona recorra todas las series buscando texto grabado en la imagen o un rostro reconocible, y que lo declare con su nombre.',
    registrada: 'Registrada. ' + (r.revisor || '') + ' declaró el ' + fechaLegible(r.fecha) + ' que revisó todos los cortes, sin hallazgos. Este registro deja constancia de esa declaración; el permiso para compartir los datos lo da tu institución.',
    retirada: 'Retirada. La declaración que había ya no vale en la aplicación.',
    no_verificable: 'No se puede saber ahora si la revisión de esta corrida vale.',
    registro_no_legible: 'El registro de la revisión falta, no se puede leer o cambió; no se da por vigente.',
  };
  texto('#rap-revision-frase', frases[r.estado] || '');
  if (r.estado === 'no_verificable') {
    const lista = el('span'); (r.problemas || []).forEach((x, j) => { if (j) lista.appendChild(document.createTextNode('; ')); lista.appendChild(el('span', 'mono', x.diario)); lista.appendChild(document.createTextNode(' (' + x.motivo + ')')); });
    zona.appendChild(cajaAviso('error', 'No se puede verificar el estado de las retiradas', [
      ['Qué pasó', (r.problemas || []).length ? (() => { const w = el('span'); w.appendChild(document.createTextNode('Un diario de retiradas no se puede leer: ')); w.appendChild(lista); w.appendChild(document.createTextNode('.')); return w; })() : 'El estado de las retiradas no se pudo leer.'],
      ['A qué afecta', 'Mientras ese diario no se lea, la aplicación no sabe qué revisiones están retiradas. Por eso no registra revisiones nuevas ni publica paquetes revisados' + (r.habia_registro ? ', y la revisión que esta corrida tenía no se da por vigente' : '') + '. El zip de esta corrida no cambia.'],
      ['Qué puedes hacer', 'Devuelve el permiso de lectura a ese archivo, o cierra el programa que lo tenga abierto, y vuelve a comprobar. Ningún registro se ha tocado: al leerse el diario, todo sigue donde estaba.'],
    ], [['Volver a comprobar', comprobarCorrida, 'primario'], ['Abrir Registro', irARegistro]]));
  }
  if (r.estado === 'registro_no_legible') zona.appendChild(cajaAviso('error', 'No se puede leer el registro de la revisión', [
    ['Qué pasó', 'El archivo del registro' + (r.registro ? ' ' + r.registro : '') + ' no está, no se puede leer o no coincide con la evidencia de esta corrida.'],
    ['A qué afecta', 'Sin ese registro la aplicación no puede decir que esta corrida esté revisada. El zip publicado no cambia.'],
    ['Qué puedes hacer', 'Mira en Registro si la revisión aparece allí y vuelve a comprobar. Si el archivo se perdió, ejecuta de nuevo el modo rápido sobre la carpeta y revisa.'],
  ], [['Volver a comprobar', comprobarCorrida, 'primario'], ['Abrir Registro', irARegistro]]));
  if (r.estado === 'retirada') {
    const inv = r.inventario_de_paquetes || {};
    zona.appendChild(cajaAviso('aviso', 'La revisión de esta corrida se retiró', [
      ['Qué pasó', r.retirada.quien + ' la retiró el ' + fechaLegible(r.retirada.fecha) + '. Motivo: ' + r.retirada.motivo],
      ['A qué afecta', 'El zip publicado sigue en su carpeta, sin cambios, y ya no debe compartirse. El registro original se conserva.' + (inv.completo === false || inv.completo == null ? ' La lista de paquetes afectados puede estar incompleta: mírala en Registro.' : '')],
      ['Qué puedes hacer', 'Para tener de nuevo un paquete revisado, vuelve a pulsar Anonimizar carpeta sobre la misma carpeta y revisa las imágenes.'],
    ], [['Abrir Registro', irARegistro]]));
  }
  const ciegos = t.no_mostrables || [];
  if (ciegos.length && r.estado === 'pendiente') zona.appendChild(cajaAviso('error', plural(ciegos.length, 'imagen no se puede', 'imágenes no se pueden') + ' mostrar en el visor', [
    ['Qué pasó', 'El visor no sabe dibujar ' + (ciegos.length === 1 ? 'esta imagen' : 'estas imágenes') + ' (' + ciegos[0].motivo + ').'],
    ['A qué afecta', 'Lo que no se ha podido ver no se puede dar por revisado: la revisión de esta corrida no se puede registrar aquí. El zip queda pendiente, en la carpeta de trabajo.'],
    ['Qué puedes hacer', 'Anota el caso y consúltalo con quien mantiene la aplicación. Los demás cortes se pueden ver con normalidad.'],
  ]));
  BLOQUEO_REVISION = ciegos.length > 0 || r.estado === 'no_verificable';
  $('#rap-revision').classList.toggle('oculto', !(r.estado === 'pendiente' || (r.estado === 'no_verificable' && !r.habia_registro)));
  $('#rap-visor-titulo').textContent = r.estado === 'registrada' ? 'Las imágenes de este paquete' : 'Las imágenes de este paquete, en su orden';
}
function pintarAreaPaquete(t, s) {
  const q = s.paquete, r = s.revision, zona = $('#rap-paquete-avisos'); zona.textContent = '';
  const visible = q.estado !== 'no_creado'; $('#rap-area-paquete').classList.toggle('oculto', !visible); if (!visible) return;
  const [cls, ico, como] = estadoPaquete(s);
  const frase = q.estado === 'pendiente_local' ? 'El zip existe y está completo, con la revisión marcada como pendiente en su manifiesto. Se queda en la carpeta de trabajo hasta que se registre la revisión.'
    : q.estado === 'publicado' ? (r.estado === 'registrada' ? 'El zip con la revisión registrada está en la carpeta de destino.' : 'El zip está en la carpeta de destino; lee el estado de su revisión antes de compartirlo.')
    : q.estado === 'no_encontrado' ? 'La aplicación escribió el zip, pero ya no lo encuentra en esa carpeta. Puede haberse movido o borrado.'
    : q.estado === 'modificado' ? 'El archivo en esa ruta cambió desde su creación. No corresponde al paquete verificado; no lo compartas como revisado.' : 'No se pudo verificar que el zip siga siendo el paquete creado por esta corrida.';
  texto('#rap-paquete-frase', frase);
  const estadoEl = el('span', 'paso-estado ' + cls); estadoEl.appendChild(icono(ico)); estadoEl.appendChild(el('span', null, como));
  const dl = $('#rap-paquete-ficha'); dl.textContent = '';
  const fila = (dt, dd) => { dl.appendChild(el('dt', null, dt)); const d = el('dd'); if (dd instanceof Node) d.appendChild(dd); else d.textContent = dd; dl.appendChild(d); };
  fila('Estado', estadoEl);
  fila('Contenido', plural(q.archivos, 'archivo DICOM', 'archivos DICOM') + ', numerados por corte, y su manifiesto');
  fila(q.estado === 'pendiente_local' ? 'Dónde está ahora' : 'Ubicación', el('span', 'mono', q.ruta));
  if (q.estado === 'pendiente_local') { const w = el('span'); w.appendChild(el('span', 'mono', q.destino || '')); w.appendChild(el('span', 'suave', ' · al registrar la revisión')); fila('Adónde irá', w); }
  if (q.destino_sincronizado) zona.appendChild(cajaAviso('aviso', 'El destino está sincronizado con la nube', [
    ['Qué pasó', 'La carpeta de destino pertenece a ' + q.destino_sincronizado + '.'],
    ['A qué afecta', 'Lo que se escriba allí puede subirse solo. La aplicación solo escribe en ese destino el zip ya revisado.'],
    ['Qué puedes hacer', 'El destino de esta corrida está fijado. Para usar otra carpeta, elige el nuevo destino y pulsa Anonimizar carpeta para iniciar una nueva corrida; cambiar el campo no modifica esta corrida.']]));
  if (q.ultimo_intento_fallido && q.estado === 'pendiente_local') zona.appendChild(cajaAviso('error', 'El zip revisado no se publicó', [
    ['Qué pasó', q.ultimo_intento_fallido.detalle],
    ['A qué afecta', 'En el destino no se escribió ningún zip y la revisión no quedó registrada. El zip pendiente sigue en la carpeta de trabajo.'],
    ['Qué puedes hacer', (q.ultimo_intento_fallido.restos || []).length ? 'Borra a mano los archivos intermedios que se indican y vuelve a registrar la revisión.' : 'Corrige la causa y vuelve a registrar la revisión.']]));
  if (q.acciones_pendientes.length) {
    const evidencias = q.acciones_pendientes.filter(a => a.clase === 'evidencia');
    const limpieza = q.acciones_pendientes.filter(a => a.clase === 'limpieza');
    const otras = q.acciones_pendientes.filter(a => !['evidencia', 'limpieza'].includes(a.clase));
    const lista = acciones => { const ul = el('ul'); for (const a of acciones) ul.appendChild(el('li', null, a.texto)); return ul; };
    const vigencia = r.estado === 'registrada' ? 'La revisión consta registrada; comprueba también el estado del zip.'
      : 'Estas acciones no restablecen una revisión retirada o no verificable. Lee su estado antes de compartir.';
    if (evidencias.length) zona.appendChild(cajaAviso('aviso', 'Evidencia pendiente de confirmación', [
      ['Qué pasó', lista(evidencias)], ['A qué afecta', vigencia],
      ['Qué puedes hacer', 'Conserva estos archivos: son evidencia de la publicación. No los borres. Abre Registro para intentar confirmar su nombre provisional.']], [['Abrir Registro', irARegistro]]));
    if (limpieza.length) zona.appendChild(cajaAviso('aviso', 'Archivos temporales pendientes de eliminar', [
      ['Qué pasó', lista(limpieza)], ['A qué afecta', vigencia],
      ['Qué puedes hacer', 'Puedes borrar únicamente los temporales enumerados en este recuadro. No borres registros ni manifiestos de evidencia.']]));
    if (otras.length) zona.appendChild(cajaAviso('aviso', 'Actualización de registros pendiente', [
      ['Qué pasó', lista(otras)], ['A qué afecta', vigencia], ['Qué puedes hacer', 'Consulta Registro. No borres archivos para resolver este aviso.']], [['Abrir Registro', irARegistro]]));
  }
}
function columnaResumen(titulo, cls, ico, filas) {
  const c = el('div'); const h = el('h4', cls); h.appendChild(icono(ico)); h.appendChild(el('span', null, titulo)); c.appendChild(h);
  const dl = el('dl'); for (const [dt, dd, k] of filas) { if (dd == null || dd === '') continue; dl.appendChild(el('dt', null, dt)); dl.appendChild(el('dd', k || null, dd)); } c.appendChild(dl); return c;
}
function pintarResumen(t, s) {
  const terminado = s.procesamiento.estado !== 'en_curso';
  $('#rap-area-resumen').classList.toggle('oculto', !terminado); if (!terminado) return;
  texto('#rap-frescura', VIGENTE ? 'Estado verificado en la última consulta.' : 'Sin comprobar: resumen histórico; el estado actual puede haber cambiado.');
  const cont = $('#rap-resumen'); cont.textContent = '';
  const limp = ((t.pasos || []).find(x => x.clave === 'limpieza') || {}).datos || {}, r = s.revision, q = s.paquete;
  const [pc, pi, pt, pd] = estadoProceso(t, s), [rc, ri, rt] = estadoRevision(s), [qc, qi, qt] = estadoPaquete(s);
  cont.appendChild(columnaResumen('Procesamiento', pc, pi, [
    ['Estado', pt + '. ' + pd],
    ['Versiones', 'PET-CT local ' + s.versiones.aplicacion + ' · política ' + s.versiones.politica],
    ['Alcance de la corrida', limp.leidos != null ? plural(limp.leidos, 'archivo leído', 'archivos leídos') + ' en la carpeta elegida' : 'La carpeta elegida'],
    ['Incluidos', limp.en_la_corrida != null ? plural(limp.en_la_corrida, 'DICOM', 'DICOM') + ', de ' + plural(limp.pacientes, 'paciente', 'pacientes') : null],
    ['Omitidos', limp.omitidos_total != null ? plural(limp.omitidos_total, 'archivo que no es', 'archivos que no son') + ' DICOM' : null],
    ['En cuarentena', limp.cuarentena_total != null ? limp.cuarentena_total + ' DICOM' : null],
  ]));
  const mostrados = r.mostrado ? r.mostrado.cuadros_mostrados + ' de ' + r.mostrado.cuadros_totales : (r.cuadros_totales != null ? r.cuadros_mostrados + ' de ' + r.cuadros_totales : null);
  cont.appendChild(columnaResumen('Revisión de píxeles', rc, ri, [
    [VIGENTE ? 'Estado, comprobado ahora' : 'Último estado conocido (sin comprobar)', {sin_paquete: 'No hay paquete que revisar', pendiente: 'Pendiente: nadie la ha declarado', registrada: VIGENTE ? 'Registrada y vigente' : 'Registrada en la última comprobación', retirada: 'Retirada', no_verificable: 'No se puede verificar', registro_no_legible: 'Registro ilegible: no se da por vigente'}[r.estado]],
    ['Declaración', r.estado === 'registrada' || r.estado === 'retirada' ? '«Revisé todos los cortes: sin hallazgos»' : null],
    ['Quién y cuándo', r.revisor ? r.revisor + ' · ' + fechaLegible(r.fecha) : null],
    ['Retirada', r.retirada ? r.retirada.quien + ' · ' + fechaLegible(r.retirada.fecha) + ' · ' + r.retirada.motivo : null],
    [r.mostrado ? 'Cuadros mostrados antes de declarar' : 'Cuadros mostrados en este navegador', mostrados ? mostrados + (r.mostrado ? ', en el navegador' : '') : null],
    ['Estudio', ((t.demografia || [])[0] || {}).etiqueta || null],
  ]));
  cont.appendChild(columnaResumen('Paquete', qc, qi, [
    ['Estado', q.estado === 'no_creado' ? 'No se creó ningún paquete' : qt],
    ['Contenido', q.archivos ? plural(q.archivos, 'archivo DICOM', 'archivos DICOM') + ' y su manifiesto' : null],
    ['Ubicación', q.ruta || null, 'mono'],
    ['Acciones pendientes', q.estado === 'no_creado' ? null : (q.acciones_pendientes.length ? plural(q.acciones_pendientes.length, 'pendiente', 'pendientes') + ': mira el apartado Paquete' : (q.estado === 'publicado' ? 'Ninguna' : 'Registrar la revisión de píxeles'))],
  ]));
  texto('#rap-comprobado', COMPROBADO ? (VIGENTE ? 'Comprobado a las ' : 'Sin comprobar ahora. Última consulta correcta a las ') + COMPROBADO.toLocaleTimeString('es', {hour: '2-digit', minute: '2-digit', second: '2-digit'}) + '.' : '');
}
// El texto que se copia sale de una lista cerrada de campos: estados, cifras y versiones. Ni rutas, ni nombres de archivo,
// ni el nombre de quien revisó, ni la etiqueta del estudio, ni la salida de los pasos.
function textoResumen(t, s) {
  const limp = ((t.pasos || []).find(x => x.clave === 'limpieza') || {}).datos || {}, r = s.revision, q = s.paquete, n = v => v == null ? 'sin dato' : v;
  const lineas = ['PET-CT local ' + s.versiones.aplicacion + ' · política ' + s.versiones.politica, '',
    'Procesamiento: ' + estadoProceso(t, s)[2] + '. ' + estadoProceso(t, s)[3],
    '  Archivos leídos: ' + n(limp.leidos) + ' · en la corrida: ' + n(limp.en_la_corrida) + ' · omitidos por no ser DICOM: ' + n(limp.omitidos_total) + ' · DICOM en cuarentena: ' + n(limp.cuarentena_total)];
  (t.pasos || []).forEach((p, i) => { let txt = (ESTADO_PASO[p.estado] || ESTADO_PASO.pendiente)[2]; if (p.estado === 'omitido' && detenidoAntes(t, i)) txt = 'No ejecutado'; const c = cifraPaso(p); lineas.push('  ' + (i + 1) + '. ' + p.titulo + ': ' + txt + (c ? ' (' + c[0] + ' ' + c[1] + ')' : '')); });
  lineas.push('', 'Revisión de píxeles: ' + estadoRevision(s)[2] + '.' + (r.fecha ? ' Fecha de la declaración: ' + fechaLegible(r.fecha) + '.' : '') + (r.retirada ? ' Retirada el ' + fechaLegible(r.retirada.fecha) + '.' : ''));
  if (r.cuadros_totales != null) lineas.push('  Cuadros mostrados en el navegador: ' + (r.mostrado ? r.mostrado.cuadros_mostrados + ' de ' + r.mostrado.cuadros_totales : r.cuadros_mostrados + ' de ' + r.cuadros_totales) + ' (mostrado no significa revisado).');
  lineas.push('', 'Paquete: ' + (q.estado === 'no_creado' ? 'no se creó' : estadoPaquete(s)[2]) + '.' + (q.archivos ? ' ' + plural(q.archivos, 'archivo DICOM', 'archivos DICOM') + '.' : '') + (q.acciones_pendientes && q.acciones_pendientes.length ? ' Acciones pendientes: ' + q.acciones_pendientes.length + '.' : ''));
  lineas.push('', 'Estado comprobado: ' + fechaLegible(s.comprobado) + '. Este resumen describe registros de la aplicación; no certifica el anonimato ni autoriza a compartir.');
  if (!VIGENTE) lineas.unshift('SIN COMPROBAR: resumen histórico. No se pudo verificar el estado actual; puede haber cambiado.', '');
  return lineas.join('\n');
}
function pintarCorrida(t) {
  ULTIMA = t; COMPROBADO = new Date(); VIGENTE = true;
  const s = t.situacion; if (!s) return;
  $('#rap-franja').classList.remove('oculto', 'caduco'); $('#rap-area-proceso').classList.remove('oculto');
  celdaFranja('#rap-f-proceso', ...estadoProceso(t, s)); celdaFranja('#rap-f-revision', ...estadoRevision(s)); celdaFranja('#rap-f-paquete', ...estadoPaquete(s));
  const frases = {en_curso: 'Seis comprobaciones, una tras otra, sobre los archivos de la carpeta elegida. La aplicación no puede medir cuánto falta de un paso: muestra cuál está en curso y el tiempo transcurrido.',
    terminado: 'Terminó sin errores. Este resultado habla de etiquetas, bytes y números; lo que se ve en las imágenes lo revisa una persona.',
    terminado_con_avisos: 'Terminó, con avisos. Un aviso informa y deja seguir; léelo antes de revisar las imágenes.',
    detenido: (t.mensaje || 'El proceso se detuvo.') + ' Abre el paso detenido para ver qué significa y qué puedes hacer.'};
  texto('#rap-proceso-frase', frases[s.procesamiento.estado] || '');
  pintarPasos(t); pintarResumen(t, s); pintarAreaRevision(t, s); pintarAreaPaquete(t, s);
  pintarAvanceCorrida(t.progreso || AVANCE_CORRIDA);
}
// Vuelve a preguntar al servidor por el estado de la corrida. Si no contesta, lo que hay en pantalla se marca como sin
// comprobar: un resumen que se quedó a la vista no se presenta como vigente.
async function comprobarCorrida() {
  const trabajo = RAPIDO, consulta = ++CONSULTA;
  if (!trabajo || (ULTIMA && ULTIMA.estado === 'corriendo')) return null;
  try {
    const t = await api('trabajo/' + trabajo);
    if (trabajo !== RAPIDO || consulta !== CONSULTA) return null;
    mensaje('#rap-estado', '', ''); pintarCorrida(t); return t;
  } catch (err) {
    if (trabajo !== RAPIDO || consulta !== CONSULTA) return null;
    VIGENTE = false;
    $('#rap-franja').classList.add('caduco');
    for (const sel of ['#rap-f-proceso', '#rap-f-revision', '#rap-f-paquete']) celdaFranja(sel, 'neutro', 'espera', 'Sin comprobar', 'El último resultado es histórico.');
    if (ULTIMA && ULTIMA.situacion) pintarResumen(ULTIMA, ULTIMA.situacion);
    pintarAvanceCorrida(AVANCE_CORRIDA);
    mensaje('#rap-estado', 'error', 'No se pudo comprobar el estado de la corrida (' + err.message + '). El resumen es histórico y puede haber cambiado.');
    botonReintentar('#rap-estado', 'Volver a comprobar', comprobarCorrida);
    return null;
  }
}
function pintarAvanceCorrida(p) {
  AVANCE_CORRIDA = p || null;
  const completo = !!(p && p.completo);
  if (p) {
    const enSerie = (p.series || []).find(x => x.indice === CORRIDA.indice);
    const completas = (p.series || []).filter(x => x.mostrados === x.total).length;
    texto('#rap-progreso', 'Mostrados en este navegador: ' + p.mostrados + ' de ' + p.total + ' cuadros de la corrida' + (enSerie ? ' · ' + enSerie.mostrados + ' de ' + enSerie.total + ' en esta serie' : '') +
      ' · ' + completas + ' de ' + (p.series || []).length + ' series completas. Mostrado no significa revisado.');
    $('#rap-barra').style.width = (p.total ? Math.round(100 * p.mostrados / p.total) : 0) + '%';
    document.querySelectorAll('#rap-series button').forEach((b, j) => { const x = (p.series || [])[j]; if (!x) return; b.querySelector('.avance').textContent = x.mostrados + ' de ' + x.total + ' mostrados'; b.querySelector('.barra i').style.width = (x.total ? Math.round(100 * x.mostrados / x.total) : 0) + '%'; });
  } else { texto('#rap-progreso', ''); $('#rap-barra').style.width = '0%'; }
  const b = $('#btn-rap-revision'), sinVerificar = ULTIMA && ULTIMA.situacion && ULTIMA.situacion.revision.estado === 'no_verificable';
  b.disabled = REGISTRANDO || !VIGENTE || BLOQUEO_REVISION || !completo;
  let porque = '';
  if (REGISTRANDO) porque = 'Registrando la revisión y publicando el zip…';
  else if (!VIGENTE) porque = 'Vuelve a comprobar el estado antes de declarar.';
  else if (sinVerificar) porque = 'Deshabilitado hasta que el estado de las retiradas se pueda verificar.';
  else if (BLOQUEO_REVISION) porque = 'Hay imágenes que el visor no puede mostrar; no se puede registrar una revisión sin verlas.';
  else if (p && !completo) { const faltan = (p.series || []).filter(x => x.mostrados < x.total).map(x => (x.total - x.mostrados) + ' de la serie ' + ((CORRIDA.series[x.indice] || {}).modalidad || (x.indice + 1)));
    porque = 'El botón se habilita cuando los ' + p.total + ' cuadros hayan estado en pantalla. Faltan ' + (p.total - p.mostrados) + (faltan.length ? ': ' + faltan.join(', ') : '') + '.'; }
  else if (completo) porque = 'Todos los cuadros han estado en pantalla. Declarar es afirmar, con tu nombre, que los examinaste.';
  b.title = porque; texto('#rap-porque', porque);
}
$('#btn-rap-origen').onclick = async () => { const r = await api('elegir-carpeta', {para: 'origen'}); if (r.ruta) $('#rap-origen').value = r.ruta; else mensaje('#rap-estado', 'aviso', r.mensaje); };
const avisarDestinoFijado = () => { if (RAPIDO) mensaje('#rap-estado', 'aviso', 'El destino de la corrida actual no cambia. Pulsa Anonimizar carpeta para iniciar otra corrida con el nuevo destino.'); };
$('#rap-destino').addEventListener('change', avisarDestinoFijado);
$('#btn-rap-destino').onclick = async () => { const r = await api('elegir-carpeta', {para: 'destino'}); if (r.ruta) { $('#rap-destino').value = r.ruta; avisarDestinoFijado(); } else mensaje('#rap-estado', 'aviso', r.mensaje); };
$('#btn-rapido').onclick = async () => {
  const origen = $('#rap-origen').value.trim();
  if (!origen) { mensaje('#rap-estado', 'aviso', 'Elige o escribe la carpeta del estudio.'); return; }
  $('#btn-rapido').disabled = true; ABIERTOS.clear(); ULTIMA = null; AVANCE_CORRIDA = null; VIGENTE = false; CONSULTA++;
  for (const sel of ['#rap-franja', '#rap-area-resumen', '#rap-area-proceso', '#rap-area-revision', '#rap-area-paquete', '#rap-visor', '#rap-revision']) $(sel).classList.add('oculto');
  $('#rap-pasos').textContent = '';
  const inicio = Date.now(); const reloj = () => { const s = Math.round((Date.now() - inicio) / 1000); return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0'); };
  mensaje('#rap-estado', '', ''); texto('#rap-reloj', 'Procesando… tiempo transcurrido 00:00');
  try {
    const r = await api('rapido', {origen, destino: $('#rap-destino').value.trim()});
    RAPIDO = r.trabajo;
    seguirTrabajo(r.trabajo, {
      vigente: () => RAPIDO === r.trabajo,
      cada: t => { mensaje('#rap-estado', '', ''); pintarCorrida(t); texto('#rap-reloj', 'Procesando… tiempo transcurrido ' + reloj()); },
      reintentando: n => { texto('#rap-reloj', 'Procesando… tiempo transcurrido ' + reloj()); mensaje('#rap-estado', 'neutro', 'Sin respuesta de la aplicación; reintentando (' + n + ' de 5).'); },
      sinRespuesta: (err, reanudar) => {
        mensaje('#rap-estado', 'error', 'No se pudo consultar el estado de la corrida (' + err.message + '). La corrida puede seguir en marcha en la aplicación.');
        botonReintentar('#rap-estado', 'Volver a consultar', reanudar); $('#btn-rapido').disabled = false; $('#rap-franja').classList.add('caduco');
      },
      fin: t => {
        mensaje('#rap-estado', '', ''); texto('#rap-reloj', (t.estado === 'error' ? 'El proceso se detuvo a los ' : 'El proceso terminó en ') + reloj() + '.');
        const fallo = (t.pasos || []).find(x => x.estado === 'error'); if (fallo) ABIERTOS.add(fallo.clave);
        pintarCorrida(t); $('#btn-rapido').disabled = false;
        if (t.zip) cargarVisorCorrida(r.trabajo);
        ACTUAL = null; $('#visor').classList.add('oculto'); SERIES = [];
      },
    });
  } catch (err) { mensaje('#rap-estado', 'error', err.message); texto('#rap-reloj', ''); $('#btn-rapido').disabled = false; }
};
$('#btn-rap-revision').onclick = async () => {
  const revisor = $('#rap-revisor').value.trim(), trabajo = RAPIDO;
  if (!revisor) { texto('#rap-porque', 'Escribe tu nombre antes de declarar.'); $('#rap-revisor').focus(); return; }
  if (REGISTRANDO || !VIGENTE) return;
  REGISTRANDO = true; pintarAvanceCorrida(AVANCE_CORRIDA);
  let fallo = null;
  try { await api('rapido/revision', {trabajo, revisor}); } catch (err) { fallo = err; }
  const t = trabajo === RAPIDO ? await comprobarCorrida() : null;
  REGISTRANDO = false;
  if (trabajo !== RAPIDO) return;
  pintarAvanceCorrida(AVANCE_CORRIDA);
  if (fallo) {
    if (t && t.revisado) mensaje('#rap-estado', 'aviso', 'La respuesta inicial no llegó correctamente. La consulta posterior confirma que la publicación ocurrió; consulta su estado actual abajo.');
    else if (t && t.situacion && t.situacion.revision.estado === 'pendiente') mensaje('#rap-estado', 'error', 'La revisión sigue pendiente según la consulta posterior. ' + fallo.message);
    else mensaje('#rap-estado', 'error', 'No se pudo confirmar el resultado de la solicitud. Vuelve a comprobar antes de repetirla. ' + fallo.message);
  }
};
$('#btn-rap-comprobar').onclick = comprobarCorrida;
$('#btn-rap-copiar').onclick = async () => {
  if (!ULTIMA || !ULTIMA.situacion) return;
  const txt = textoResumen(ULTIMA, ULTIMA.situacion);
  try { await navigator.clipboard.writeText(txt); texto('#rap-comprobado', 'Resumen copiado.'); }
  catch (err) { const a = $('#rap-copia'); a.value = txt; a.classList.remove('oculto'); a.focus(); a.select(); texto('#rap-comprobado', 'No se pudo copiar solo: el texto está seleccionado abajo, cópialo con el teclado.'); }
};
// Al volver a esta pestaña o a esta ventana, el estado de una corrida terminada se vuelve a pedir: no se muestra de memoria.
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && $('#rapido').classList.contains('activa')) comprobarCorrida(); });
document.querySelector('nav button[data-tab=rapido]').addEventListener('click', () => comprobarCorrida());
let CORRIDA = {trabajo: null, series: [], indice: 0};
function pintarCorteCorrida(conservarCuadro) {
  const s = CORRIDA.series[CORRIDA.indice]; if (!s) return;
  const k = Math.min(Math.max(parseInt($('#rap-deslizador').value, 10) || 1, 1), s.n);
  const cuadros = (s.cuadros || [])[k - 1] || 1, dc = $('#rap-cuadro');
  $('#rap-cuadros').classList.toggle('oculto', cuadros <= 1);
  if (!conservarCuadro || parseInt(dc.max, 10) !== cuadros) { dc.min = 1; dc.max = cuadros; dc.value = 1; }
  const c = Math.min(Math.max(parseInt(dc.value, 10) || 1, 1), cuadros);
  $('#rap-corte').src = '/api/rapido/' + encodeURIComponent(CORRIDA.trabajo) + '/corte/' + CORRIDA.indice + '/' + k + '?ventana=' + encodeURIComponent($('#rap-ventana').value) + '&cuadro=' + c + '&t=' + encodeURIComponent(T);
  $('#rap-corte').alt = 'Corte ' + k + ' de ' + s.n + ' de la serie ' + (s.modalidad || '');
  texto('#rap-etiqueta', 'corte ' + k + ' de ' + s.n + ' · ' + s.nombres[k - 1] + (cuadros > 1 ? ' · cuadro ' + c + ' de ' + cuadros : ''));
}
function elegirSerieCorrida(i) {
  CORRIDA.indice = i; const s = CORRIDA.series[i];
  document.querySelectorAll('#rap-series button').forEach((b, j) => { b.classList.toggle('activa', j === i); b.setAttribute('aria-pressed', j === i ? 'true' : 'false'); });
  const d = $('#rap-deslizador'); d.min = 1; d.max = s.n; d.value = 1;
  $('#rap-ventana').value = s.modalidad === 'CT' ? 'tejido' : 'auto';
  pintarDemografia('#rap-etiqueta-estudio', '#rap-aviso-demografia', s.demografia);
  pintarAvanceCorrida(AVANCE_CORRIDA);
  mensaje('#rap-orden', s.criterio === 'desempate determinista' ? 'aviso' : 'neutro', 'Cortes ordenados por ' + (s.criterio || 'InstanceNumber') + (s.aviso ? ': ' + s.aviso : '') + '.');
  pintarCorteCorrida(false);
}
async function cargarVisorCorrida(trabajo) {
  try {
    const series = await api('rapido/' + trabajo + '/series');
    if (trabajo !== RAPIDO) return;
    CORRIDA = {trabajo, series, indice: 0};
    const cont = $('#rap-series'); cont.textContent = '';
    series.forEach((s, i) => {
      const b = el('button', 'serie'); b.type = 'button'; b.title = s.serie.split('/').pop();
      b.appendChild(el('b', null, (s.modalidad || 'serie') + ' · ' + plural(s.n, 'corte', 'cortes'))); b.appendChild(el('span', 'avance', ''));
      const barra = el('span', 'barra'); barra.appendChild(el('i')); b.appendChild(barra);
      b.onclick = () => elegirSerieCorrida(i); cont.appendChild(b);
    });
    try { pintarAvanceCorrida(await api('rapido/' + trabajo + '/progreso')); } catch (err) { pintarAvanceCorrida(null); }
    $('#rap-visor').classList.toggle('oculto', series.length === 0);
    if (series.length) elegirSerieCorrida(0);
  } catch (err) { $('#rap-visor').classList.add('oculto'); }
}
alMostrar($('#rap-corte'), async src => {
  const m = src.match(/\/api\/rapido\/([^/]+)\/corte\/(\d+)\/(\d+)\?.*?cuadro=(\d+)/); if (!m || decodeURIComponent(m[1]) !== RAPIDO) return;
  try { const p = await api('rapido/' + m[1] + '/mostrado', {indice: Number(m[2]), k: Number(m[3]), cuadro: Number(m[4])}); if (decodeURIComponent(m[1]) === RAPIDO) { pintarAvanceCorrida(p);
    if (ULTIMA && ULTIMA.situacion && ULTIMA.situacion.revision.estado === 'pendiente') { ULTIMA.situacion.revision.cuadros_mostrados = p.mostrados; celdaFranja('#rap-f-revision', ...estadoRevision(ULTIMA.situacion)); pintarResumen(ULTIMA, ULTIMA.situacion); } } }
  catch (err) { /* el recuento se vuelve a pedir al cambiar de serie */ }
});
recorrido($('#btn-rap-recorrer'), $('#rap-corte'), () => {
  const s = CORRIDA.series[CORRIDA.indice]; if (!s) return false;
  const dc = $('#rap-cuadro'), d = $('#rap-deslizador');
  if (!$('#rap-cuadros').classList.contains('oculto') && Number(dc.value) < Number(dc.max)) { dc.value = Number(dc.value) + 1; pintarCorteCorrida(true); return true; }
  if (Number(d.value) >= s.n) return false;
  d.value = Number(d.value) + 1; pintarCorteCorrida(false); return true;
});
$('#rap-deslizador').oninput = () => pintarCorteCorrida(false);
$('#rap-cuadro').oninput = () => pintarCorteCorrida(true);
$('#rap-ventana').onchange = () => pintarCorteCorrida(true);
$('#rap-corte').onerror = async () => {
  texto('#rap-etiqueta', 'Este corte no se puede mostrar: el archivo cambió desde la corrida o el visor no sabe dibujar esta imagen.');
  try { const t = await api('trabajo/' + CORRIDA.trabajo); if (CORRIDA.trabajo === RAPIDO) pintarCorrida(t); } catch (err) { /* el estado se vuelve a pedir al registrar */ }
};
$('#btn-origen').onclick = async () => {
  const r = await api('elegir-carpeta', {});
  if (r.ruta) { $('#origen').value = r.ruta; mensaje('#aviso-origen', r.sincronizada ? 'aviso' : 'neutro', r.sincronizada ? 'Está dentro de una carpeta sincronizada (' + r.sincronizada + '). Se puede leer, pero conviene sacarla de la nube.' : 'Carpeta elegida.'); }
  else mensaje('#aviso-origen', 'aviso', r.mensaje);
};
$('#btn-demo').onclick = async () => {
  $('#btn-demo').disabled = true; mensaje('#aviso-origen', 'neutro', 'creando caso sintético…');
  try {
    const r = await api('demo', {}); $('#origen').value = r.ruta;
    $('#rap-origen').value = r.ruta;                                  // la pestaña Rápido queda lista con esta misma carpeta
    mensaje('#aviso-origen', 'ok', 'Caso sintético listo (identidad ficticia, sin datos reales). Su carpeta ya está escrita también en la pestaña Rápido: allí se anonimiza con un solo botón. Aquí, Anonimizar hace solo la limpieza.');
  }
  catch (err) { mensaje('#aviso-origen', 'error', err.message); } finally { $('#btn-demo').disabled = false; }
};
// Lo que pasó en la limpieza, con sus cifras, y por qué quedó algo en cuarentena. Cada motivo dice qué pasó, a qué afecta
// y qué se puede hacer. Las cifras y los motivos vienen del reporte de la limpieza.
const OTRA_OPCION = 'misma instancia, ya guardada con la otra opción de descripciones';
function explicarLimpieza(d, conservar) {
  const zona = $('#explicacion-anon'); zona.textContent = ''; if (!d) return;
  const dl = el('dl', 'ficha'); const fila = (dt, dd) => { if (dd == null) return; dl.appendChild(el('dt', null, dt)); dl.appendChild(el('dd', null, dd)); };
  fila('Archivos leídos', d.leidos); fila('Guardados ahora', d.escritos); fila('Ya guardados, verificados', d.ya_guardados);
  fila('Omitidos por no ser imágenes DICOM', d.omitidos_total); fila('DICOM en cuarentena', d.cuarentena_total);
  zona.appendChild(dl);
  for (const [motivo, n] of Object.entries(d.cuarentena_motivos || {})) {
    const corto = motivo.split(' / ')[0], imagenes = plural(n, 'imagen', 'imágenes');
    if (corto === OTRA_OPCION) zona.appendChild(cajaAviso('aviso', imagenes + (n === 1 ? ' ya estaba guardada' : ' ya estaban guardadas') + ' con la otra opción de descripciones', [
      ['Qué pasó', (n === 1 ? 'Esa imagen ya estaba guardada, limpiada ' : 'Esas imágenes ya estaban guardadas, limpiadas ') + (conservar ? 'sin' : 'con') + ' la casilla de conservar descripciones. Ahora se ' + (n === 1 ? 'limpió ' : 'limpiaron ') + (conservar ? 'con' : 'sin') + ' ella, y el resultado solo difiere en esos textos.'],
      ['A qué afecta', 'La copia guardada no cambió: sigue en Estudios como estaba, y la casilla no tuvo efecto en ' + (n === 1 ? 'esa imagen' : 'esas imágenes') + '. No se perdió ni se duplicó nada.'],
      ['Qué puedes hacer', 'La casilla se aplica a los estudios que todavía no están guardados en esta carpeta de trabajo. Cambiarla en un estudio ya guardado todavía no se puede hacer desde la aplicación; si lo necesitas, escríbenos.'],
    ]));
    else if (corto === 'misma instancia SOP con contenido distinto') zona.appendChild(cajaAviso('aviso', imagenes + ' con otro contenido que la copia guardada', [
      ['Qué pasó', 'En la carpeta hay otra copia de ' + (n === 1 ? 'una imagen ya guardada' : 'imágenes ya guardadas') + ', y no es igual a la guardada.'],
      ['A qué afecta', 'La copia guardada no cambió y la nueva no se copió. Ninguna de las nuevas entra en un paquete.'],
      ['Qué puedes hacer', 'Si las dos copias deberían ser la misma, compara las carpetas de origen. El reporte de la limpieza dice cuántas son.'],
    ]));
    else zona.appendChild(cajaAviso('aviso', plural(n, 'DICOM en cuarentena', 'DICOM en cuarentena') + ': ' + corto, [
      ['Qué pasó', 'La limpieza apartó ' + (n === 1 ? 'esa imagen' : 'esas imágenes') + ' y no las copió. Motivo: ' + corto + '.'],
      ['A qué afecta', 'No están en la copia limpia y no entran en ningún paquete. Los originales no se tocan.'],
      ['Qué puedes hacer', 'El reporte de la limpieza, aquí abajo, dice cuántas son por motivo.'],
    ]));
  }
  // El contador procede solo de salidas aceptadas; los avisos de archivos en cuarentena no acreditan conservación.
  if (d.descripciones_redactadas && d.en_la_corrida) zona.appendChild(cajaAviso('aviso', 'Se redactó el nombre o el ID del paciente en una descripción', [
    ['Qué pasó', 'Alguna descripción conservada contenía el nombre o el ID del paciente, y esa parte se sustituyó por [REDACTADO].'],
    ['A qué afecta', 'Esas descripciones se conservan con la parte redactada. Lo demás no cambia.'],
    ['Qué puedes hacer', 'Lee las descripciones en la pestaña Estudios antes de compartir: pueden decir algo más que la aplicación no reconoce.'],
  ]));
}
$('#btn-anonimizar').onclick = async () => {
  const origen = $('#origen').value.trim();
  if (!origen) { mensaje('#aviso-origen', 'aviso', 'Elige o escribe la carpeta de originales.'); return; }
  $('#btn-anonimizar').disabled = true; $('#salida-anon').classList.remove('oculto'); texto('#salida-anon', ''); $('#det-reporte').classList.add('oculto');
  const conservar = $('#conservar').checked; $('#explicacion-anon').textContent = '';
  const inicio = Date.now(); const reloj = () => { const s = Math.round((Date.now() - inicio) / 1000); return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0'); };
  mensaje('#estado-anon', 'neutro', 'limpieza en curso · 00:00');
  try {
    const {trabajo} = await api('anonimizar', {origen, prefijo: $('#prefijo').value.trim(), conservar_descripciones: conservar});
    seguirTrabajo(trabajo, {
      cada: t => {
        texto('#salida-anon', t.lineas.join('\n'));
        const prog = [...t.lineas].reverse().find(l => /archivos limpios/.test(l));
        mensaje('#estado-anon', 'neutro', 'limpieza en curso · ' + reloj() + (prog ? ' · ' + prog.trim().split(' (')[0] : ''));
      },
      reintentando: n => mensaje('#estado-anon', 'neutro', 'limpieza en curso · ' + reloj() + ' · sin respuesta de la aplicación, reintentando (' + n + ' de 5)'),
      sinRespuesta: (err, reanudar) => {
        mensaje('#estado-anon', 'error', 'No se pudo consultar el estado de la limpieza (' + err.message + '). Puede seguir en marcha en la aplicación.');
        botonReintentar('#estado-anon', 'Volver a consultar', reanudar); $('#btn-anonimizar').disabled = false;
      },
      fin: t => {
        texto('#salida-anon', t.lineas.join('\n'));
        const listo = t.lineas.find(l => l.startsWith('Listo')) || '';
        if (t.estado === 'ok') mensaje('#estado-anon', 'ok', 'Limpieza terminada en ' + reloj() + '. ' + listo.replace(/\s*\(done\)\s*$/, '') + '. La verificación posterior pasó en las etiquetas revisadas; los píxeles no se revisan.');
        else if (t.estado === 'con avisos') mensaje('#estado-anon', 'aviso', 'Terminó con cuarentena o avisos en ' + reloj() + '. ' + listo.replace(/\s*\(done\)\s*$/, '') + '. Revisa el reporte antes de enviar.');
        else mensaje('#estado-anon', 'error', 'Terminó con error. Revisa la salida.');
        if (t.reporte) { $('#det-reporte').classList.remove('oculto'); texto('#reporte-anon', t.reporte); }
        explicarLimpieza(t.datos, conservar);
        $('#btn-anonimizar').disabled = false; ACTUAL = null; $('#visor').classList.add('oculto'); SERIES = []; cargarEstudios(1);
      },
    });
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
    celda(tr, s.paciente); celda(tr, demografiaCorta(s.demografia)); celda(tr, nombreEstudio(s)).title = s.estudio; celda(tr, s.serie); celda(tr, s.modalidad); celda(tr, s.descripcion); celda(tr, s.n, 'num');
    celda(tr, s.filas + '×' + s.columnas); const tdp = document.createElement('td'); tdp.appendChild(insignia(s.politica ? 'info' : 'error', s.politica || 'sin política')); tr.appendChild(tdp);
    tr.dataset.id = s.id; tr.onclick = () => abrir(s); tb.appendChild(tr);
  }
  if (ACTUAL) { const fila = tb.querySelector('tr[data-id="' + ACTUAL.id + '"]'); if (fila) fila.classList.add('seleccionada'); else { ACTUAL = null; $('#visor').classList.add('oculto'); } }
}
$('#btn-refrescar').onclick = () => cargarEstudios(1);
function abrir(s) {
  pintarDemografia('#etiqueta-estudio', '#aviso-demografia', s.demografia);
  texto('#nota-orden', 'Cortes ordenados por ' + (s.orden_criterio || 'InstanceNumber') + '.');
  mensaje('#aviso-orden', (s.orden_criterio === 'desempate determinista' || s.cuadros_max > 1) ? 'aviso' : 'neutro',
    (s.orden_aviso ? 'Orden de los cortes: ' + s.orden_aviso + '. ' : '') + (s.cuadros_max > 1 ? 'Esta serie tiene archivos con varios cuadros (hasta ' + s.cuadros_max + '); aquí se muestra el primero de cada archivo. Para recorrerlos todos y registrar la revisión, usa el visor de una corrida en Rápido.' : ''));
  ACTUAL = s; $('#visor').classList.remove('oculto');
  document.querySelectorAll('#tabla-series tr.fila-serie').forEach(tr => tr.classList.toggle('seleccionada', tr.dataset.id === s.id));
  limpiarFormularioRevision(); cargarRevision(s.paciente); const d = $('#deslizador'); d.max = s.n - 1; d.value = Math.floor(s.n / 2);
  $('#antes-despues').classList.add('oculto'); $('#img-original').removeAttribute('src'); $('#img-limpio').removeAttribute('src'); $('#destacados').textContent = ''; $('#detalle-cambios').textContent = ''; texto('#ad-estado', '');
  $('#salida-comprobar').classList.add('oculto'); texto('#salida-comprobar', '');
  $('#ventana').value = s.modalidad === 'CT' ? 'tejido' : 'auto';
  document.querySelectorAll('#botones-comprobar button[data-que=suv], #botones-comprobar button[data-que=comparar_suv]').forEach(b => { b.disabled = s.modalidad !== 'PT'; b.title = s.modalidad === 'PT' ? '' : 'Solo para series PET: elige la serie PT de este paciente'; });
  const m = $('#meta'); m.textContent = '';
  for (const [k, v] of [['Paciente', s.paciente], ['Estudio', nombreEstudio(s) + (s.estudio_numero ? ' (' + s.estudio + ')' : '')], ['Serie', s.serie], ['Modalidad', s.modalidad], ['Descripción', s.descripcion], ['Protocolo', s.protocolo],
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
alMostrar($('#corte'), async src => {
  const m = src.match(/\/api\/serie\/([^/]+)\/corte\/(\d+)\?/); if (!m || !ACTUAL || decodeURIComponent(m[1]) !== ACTUAL.id) return;
  try { const p = await api('serie/' + m[1] + '/mostrado', {i: Number(m[2]), cuadro: 1}); if (ACTUAL && decodeURIComponent(m[1]) === ACTUAL.id) pintarAvancePaciente(p); }
  catch (err) { /* el recuento se vuelve a pedir al abrir la serie */ }
});
recorrido($('#btn-recorrer'), $('#corte'), () => {
  if (!ACTUAL) return false; const d = $('#deslizador');
  if (Number(d.value) >= ACTUAL.n - 1) return false;
  d.value = Number(d.value) + 1; mostrarCorte(); return true;
});
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
    const sinVerif = r.retiradas_no_verificables || [];
    const notaRet = sinVerif.length ? ' No se puede verificar el estado de las retiradas (' + sinVerif.map(x => x.diario + ': ' + x.motivo).join('; ') + '): ninguna revisión se da por vigente en este paquete.' : '';
    const notaOrden = sinOrden.length ? ' Orden de cortes no verificado en ' + sinOrden.length + ' serie(s): la numeración de esos archivos es un desempate, no un orden anatómico.' : '';
    mensaje('#estado-enviar', (pend.length || sinOrden.length || sinVerif.length) ? 'aviso' : 'ok', (pend.length ? 'Paquete creado localmente, con revisión de píxeles pendiente para ' + pend.join(', ') + '. Existe, pero no está listo para compartirse.' : 'Paquete creado localmente con revisión de píxeles vigente y sin hallazgos en todos los pacientes.') + notaOrden + notaRet);
    texto('#salida-enviar', r.ruta + '\n' + r.archivos + ' archivos de ' + r.pacientes.join(', ') + '\nIncluye manifiesto.json con el hash de cada archivo y el estado de la revisión. El mapa privado no entra.'); }
  catch (err) { mensaje('#estado-enviar', 'error', 'Rechazado: ' + err.message); texto('#salida-enviar', err.datos && err.datos.fallos ? Object.entries(err.datos.fallos).map(([p, s]) => p + ': ' + (Array.isArray(s) ? s.join('; ') : s)).join('\n') : ''); }
};
document.querySelectorAll('#botones-comprobar button').forEach(b => b.onclick = async () => {
  if (!ACTUAL) return; const o = $('#salida-comprobar'); o.classList.remove('oculto'); texto('#salida-comprobar', 'corriendo ' + b.dataset.que + '…');
  try { const r = await api('comprobar', {que: b.dataset.que, paciente: ACTUAL.paciente, serie: ACTUAL.id}); texto('#salida-comprobar', r.salida); }
  catch (err) { texto('#salida-comprobar', err.message); }
});
let AVANCE_PACIENTE = null;
function pintarAvancePaciente(p) {
  AVANCE_PACIENTE = p || null;
  if (!p || !ACTUAL) { texto('#rev-progreso', ''); return; }
  const enSerie = (p.series || []).find(x => x.serie === ACTUAL.id);
  texto('#rev-progreso', 'Mostrados en este navegador: ' + p.mostrados + ' de ' + p.total + ' cortes de este paciente' + (enSerie ? ' · ' + enSerie.mostrados + ' de ' + enSerie.total + ' en esta serie' : '') + '. Mostrado no significa revisado.');
  actualizarBotonesRevision();
}
function pintarRevision(r) {
  if (r.progreso) pintarAvancePaciente(r.progreso);
  if (r.estado === 'retirada') { mensaje('#rev-estado', 'aviso', 'Revisión de píxeles pendiente: ' + (r.detalle || 'revisión retirada') + '.'); return; }
  if (r.lista_para_compartir) mensaje('#rev-estado', 'ok', 'Revisión de píxeles: sin hallazgos, vigente. Registrada el ' + r.fecha + (r.revisor ? ' por ' + r.revisor : '') + ' sobre ' + r.archivos_revisados + ' archivos. La compuerta de metadatos se vuelve a comprobar al empaquetar.');
  else if (r.estado === 'con_hallazgos' && r.vigente) mensaje('#rev-estado', 'error', 'Revisión de píxeles: con hallazgos (' + (r.hallazgos || []).join(', ') + ') el ' + r.fecha + '. Requiere tratamiento antes de compartir.');
  else mensaje('#rev-estado', 'aviso', 'Revisión de píxeles pendiente: ' + (r.detalle || ''));
}
let REVSEQ = 0;
function limpiarFormularioRevision() { REVSEQ++; ['#rev-texto', '#rev-rostro', '#rev-otro'].forEach(s => { $(s).checked = false; }); $('#rev-notas').value = ''; mensaje('#rev-estado', 'neutro', 'cargando…'); $('#rev-sin').disabled = $('#rev-con').disabled = true; }
function hallazgosMarcados() { return [['#rev-texto', 'texto_grabado'], ['#rev-rostro', 'rostro'], ['#rev-otro', 'otro']].filter(([s]) => $(s).checked).map(([, v]) => v); }
function actualizarBotonesRevision() {
  if (REV_GUARDANDO) return; const h = hallazgosMarcados().length, rev = $('#rev-revisor').value.trim().length > 0;
  const completo = !!(AVANCE_PACIENTE && AVANCE_PACIENTE.completo);
  $('#rev-sin').disabled = h > 0 || !rev || !completo; $('#rev-con').disabled = h === 0 || !rev;
  $('#rev-sin').title = completo ? '' : 'Se habilita cuando todos los cortes de todas las series de este paciente han estado en pantalla en este navegador';
}
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
function notaInventario(inv) {
  if (!inv) return '';
  if (inv.completo == null) return ' ATENCIÓN: no se sabe si la lista de paquetes está completa. ' +
    (inv.motivo || 'Esta retirada no tiene un inventario verificable de paquetes.') + ' Trátala como incompleta hasta comprobarla.';
  const dudosos = inv.sin_determinar || [], recuperados = (inv.problemas || []).filter(x => x.recuperado_del_zip);
  return (inv.completo === false && !dudosos.length ? ' ATENCIÓN: la lista de paquetes está incompleta; requiere comprobación.' : '') +
    (dudosos.length ? ' ATENCIÓN: la lista de paquetes puede estar incompleta. No se pudo averiguar si dependen de esta revisión: ' +
      dudosos.map(e => e.zip + ' (' + e.motivo + (e.existe ? '; el zip sigue en ' + e.ruta : '') + ')').join('; ') + '. Trátalos como afectados hasta comprobarlos.' : '') +
    (recuperados.length ? ' Manifiestos locales ilegibles cuya información se tomó del propio zip: ' + recuperados.map(e => e.manifiesto + ' (' + e.motivo + ')').join('; ') + '.' : '');
}
let RETIRAR = null;
async function cargarRevisiones() {
  const tb = $('#tabla-revisiones tbody'); tb.textContent = '';
  let r;
  try { r = await api('revisiones'); } catch (err) { mensaje('#revisiones-estado', 'error', err.message); return; }
  const filas = [];
  for (const c of r.corridas) filas.push({tipo: 'corrida', id: c.registro, alcance: 'Corrida · ' + c.pacientes.join(', ') + (c.provisional ? ' (registro con nombre provisional)' : ''),
    estado: c.no_verificable ? ['aviso', '! no se puede verificar si está retirada'] : (c.retirada ? ['aviso', '! retirada el ' + c.retirada.fecha + ' por ' + c.retirada.quien + ': ' + c.retirada.motivo] : ['ok', '✓ registrada, sin hallazgos']),
    quien: (c.revisor || '') + ' · ' + (c.fecha || ''), archivos: c.archivos + (c.mostrado ? ' (' + c.mostrado.cuadros_mostrados + ' cuadros mostrados)' : ''),
    paquete: (c.zip && c.zip.nombre ? c.zip.nombre + (c.zip.existe ? ' · sigue en ' + c.zip.destino : ' · ya no está en ' + c.zip.destino) : '') + notaInventario(c.inventario_de_paquetes), retirable: c.se_puede_retirar});
  for (const p of r.pacientes) filas.push({tipo: 'paciente', id: p.paciente, alcance: 'Paciente · ' + p.paciente,
    estado: p.estado === 'retirada' ? ['aviso', '! ' + p.detalle] : (p.estado === 'sin_hallazgos' && p.vigente ? ['ok', '✓ sin hallazgos, vigente'] : ['aviso', '! ' + (p.estado === 'con_hallazgos' ? 'con hallazgos' : 'pendiente') + ': ' + (p.detalle || '')]),
    quien: p.estado === 'retirada' ? '' : (p.revisor || '') + ' · ' + (p.fecha || ''), archivos: p.archivos == null ? '' : String(p.archivos),
    paquete: (p.paquetes_afectados || []).map(e => e.zip).join(', ') + notaInventario(p.inventario_de_paquetes), retirable: p.se_puede_retirar});
  mensaje('#revisiones-estado', 'neutro', filas.length ? '' : 'No hay ninguna revisión registrada.');
  $('#marco-revisiones').classList.toggle('oculto', filas.length === 0);
  const prob = r.diarios_con_problemas || [];
  mensaje('#diarios-problemas', r.retiradas_no_verificables ? 'error' : 'aviso', !prob.length ? '' :
    (r.retiradas_no_verificables ? 'No se puede verificar el estado de las retiradas. Requiere atención: ' + prob.map(x => x.diario + ' (' + x.motivo + ')').join('; ') +
      '. Hasta que ese diario se pueda leer, ninguna revisión autoriza compartir y no se puede retirar ni terminar nada. Ningún registro se ha tocado; cuando el diario vuelva a leerse, todo sigue donde estaba.'
      : 'Diarios de retirada que requieren atención: ' + prob.map(x => x.diario + ' (' + x.motivo + ')').join('; ') + '.'));
  const medias = r.retiradas_incompletas || [];
  $('#retiradas-a-medias').classList.toggle('oculto', medias.length === 0);
  mensaje('#retiradas-a-medias-texto', 'aviso', medias.length ? medias.map(m => 'Retirada de ' + m.fecha + ' por ' + m.quien + ' (' + m.motivo + '): registrada, y sus revisiones ya no valen; falta ' + m.pendientes.join('; ') + '.').join(' ') : '');
  for (const f of filas) {
    const tr = document.createElement('tr');
    celda(tr, f.alcance); const td = document.createElement('td'); td.appendChild(insignia(f.estado[0], f.estado[1])); tr.appendChild(td);
    celda(tr, f.quien); celda(tr, f.archivos); celda(tr, f.paquete);
    const ta = document.createElement('td');
    if (f.retirable) { const b = document.createElement('button'); b.textContent = 'Retirar…'; b.onclick = () => { RETIRAR = f; texto('#retirar-titulo', 'Retirar la revisión: ' + f.alcance); $('#retirar-form').classList.remove('oculto'); $('#retirar-quien').focus(); }; ta.appendChild(b); }
    tr.appendChild(ta); tb.appendChild(tr);
  }
}
$('#btn-retirar-cancelar').onclick = () => { RETIRAR = null; $('#retirar-form').classList.add('oculto'); };
$('#btn-reanudar').onclick = async () => {
  $('#btn-reanudar').disabled = true;
  try {
    const r = await api('revisiones/retiradas/reanudar', {});
    mensaje('#retirada-resultado', r.incompletas ? 'aviso' : 'ok', r.incompletas ? 'Siguen quedando ' + r.incompletas + ' retirada(s) por terminar: ' + r.resultados.filter(x => x.pendientes && x.pendientes.length).map(x => x.pendientes.join('; ')).join(' · ') + '.' : 'Retiradas terminadas.');
  } catch (err) { mensaje('#retirada-resultado', 'error', err.message); }
  finally { $('#btn-reanudar').disabled = false; await cargarRevisiones(); }
};
$('#btn-retirar').onclick = async () => {
  if (!RETIRAR) return;
  const quien = $('#retirar-quien').value.trim(), motivo = $('#retirar-motivo').value.trim();
  if (!quien || motivo.length < 3) { mensaje('#retirada-resultado', 'aviso', 'Indica quién retira la revisión y por qué.'); return; }
  $('#btn-retirar').disabled = true;
  try {
    const r = await api('revisiones/retirar', {tipo: RETIRAR.tipo, id: RETIRAR.id, quien, motivo});
    const paquetes = r.paquetes_afectados || [];
    mensaje('#retirada-resultado', 'aviso', 'Revisión retirada (' + r.retiradas.map(x => x.tipo + ' ' + x.id).join('; ') + '). El registro original se conserva. ' +
      (paquetes.length ? 'Estos paquetes se crearon con esa revisión y ya no deben compartirse: ' + paquetes.map(e => e.ruta + (e.existe ? (e.es_el_mismo_archivo === false ? ' (existe, pero ya no es el mismo archivo)' : ' (sigue ahí)') : ' (ya no está en esa carpeta)')).join('; ') + '. ' : 'No hay paquetes revisados que dependan de ella. ') +
      'Retirar no cambia los zips ya exportados.' + notaInventario(r.inventario_de_paquetes));
    RETIRAR = null; $('#retirar-form').classList.add('oculto'); $('#retirar-motivo').value = ''; await cargarRevisiones();
    ACTUAL = null; $('#visor').classList.add('oculto');
  } catch (err) { mensaje('#retirada-resultado', 'error', err.message); await cargarRevisiones(); }
  finally { $('#btn-retirar').disabled = false; }
};
async function cargarRegistro() { cargarRevisiones(); texto('#texto-registro', (await api('registro')).join('\n')); }
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
    try:
        confirmar_evidencias_publicadas()
        problemas = estado_de_retiradas()
        if problemas["problemas"]:
            print("ATENCIÓN: hay diarios de retirada que requieren atención: " + texto_de_problemas(problemas["problemas"]) + "."
                  + (" Hasta que se puedan leer, ninguna revisión autoriza compartir." if problemas["bloqueo"] else ""), flush=True)
            registrar("retiradas: diarios que requieren atención: " + texto_de_problemas(problemas["problemas"]))
        a_medias = len(retiradas_incompletas())
        if a_medias:
            reanudar_retiradas()
            quedan = len(retiradas_incompletas())
            print(f"Retiradas de revisión a medias: {a_medias}; siguen incompletas: {quedan}. Las revisiones que alcanzan ya no valen en ningún caso."
                  + (" Termínalas en la pestaña Registro." if quedan else ""), flush=True)
    except Exception as e:
        registrar(f"retiradas: no se pudieron reanudar al arrancar ({type(e).__name__})")
    print(f"Listo. Abre en el navegador (open in your browser): {url}", flush=True)
    registrar(f"app {VERSION} iniciada en 127.0.0.1:{args.puerto}")
    if not args.sin_navegador:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.puerto, log_level="warning")


if __name__ == "__main__":
    main()
