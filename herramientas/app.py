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
import datetime as dt
import hashlib
import io
import json
import os
import re
import secrets
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
from fastapi import APIRouter, FastAPI, HTTPException, Request  # noqa: E402
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

VERSION = "0.7.1"
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


def dialogo_carpeta() -> str | None:
    """Selector nativo con título fijo (sin interpolar texto en AppleScript o PowerShell)."""
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt "Carpeta con los DICOM originales")'],
                               capture_output=True, text=True, timeout=600)
            return (r.stdout.strip() or None) if r.returncode == 0 else None
        if sys.platform.startswith("win"):
            ps = ("Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                  "$d.Description = 'Carpeta con los DICOM originales'; if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=600)
            return r.stdout.strip() or None
        r = subprocess.run(["zenity", "--file-selection", "--directory", "--title=Carpeta con los DICOM originales"],
                           capture_output=True, text=True, timeout=600)
        return r.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def correr_en_segundo_plano(nombre: str, comando: list[str]) -> str:
    tid = secrets.token_hex(4)
    TRABAJOS[tid] = {"nombre": nombre, "estado": "corriendo", "lineas": [], "codigo": None}

    def hilo() -> None:
        try:
            p = subprocess.Popen(comando, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
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
    r = subprocess.run(comando, capture_output=True, text=True)
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

def indexar() -> None:
    INDICE.clear()
    limpios = TRABAJO / "limpios"
    if not limpios.is_dir():
        return
    for carpeta_serie in sorted(p for p in limpios.glob("*/*/*") if p.is_dir() and not p.is_symlink()):
        if not PACIENTE_RE.match(carpeta_serie.parts[-3]):
            continue
        archivos = sorted(p for p in carpeta_serie.iterdir() if p.suffix.lower() == ".dcm" and not p.is_symlink())
        if not archivos:
            continue
        cortes, meta = [], {}
        for a in archivos:
            try:
                ds = pydicom.dcmread(a, stop_before_pixels=True)
            except Exception:
                continue
            n = ds.get("InstanceNumber", None)
            cortes.append((int(n) if n not in (None, "") else len(cortes), str(a)))
            if not meta:
                meta = {"modalidad": str(ds.get("Modality", "")), "filas": int(ds.get("Rows", 0) or 0),
                        "descripcion": str(ds.get("SeriesDescription", "")), "protocolo": str(ds.get("ProtocolName", "")),
                        "columnas": int(ds.get("Columns", 0) or 0),
                        "espaciado": [float(x) for x in ds.get("PixelSpacing", [])] if "PixelSpacing" in ds else None,
                        "grosor": float(ds.SliceThickness) if "SliceThickness" in ds and str(ds.SliceThickness) else None,
                        "identidad_removida": str(ds.get("PatientIdentityRemoved", "")),
                        "politica": str(ds.get("DeidentificationMethod", "")).split(";")[0]}
        cortes.sort()
        rel = carpeta_serie.relative_to(limpios)
        sid = hashlib.sha1(str(rel).encode()).hexdigest()[:12]
        INDICE[sid] = {"id": sid, "paciente": rel.parts[0], "estudio": rel.parts[1], "serie": rel.parts[2],
                       "n": len(cortes), "cortes": cortes, **meta}
    registrar(f"índice: {len(INDICE)} series")


def render_corte(ruta: str, ventana: str) -> bytes:
    ds = pydicom.dcmread(ruta)
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
def elegir_carpeta(request: Request):
    auth(request)
    ruta = dialogo_carpeta()
    if not ruta:
        return {"ruta": None, "mensaje": "No se pudo abrir el selector; escribe la ruta a mano"}
    return {"ruta": ruta, "sincronizada": en_sincronizada(Path(ruta))}


@api.post("/anonimizar")
def anonimizar(request: Request, cuerpo: dict):
    auth(request)
    origen = Path(str(cuerpo.get("origen", ""))).expanduser()
    if not origen.is_dir():
        raise HTTPException(400, "la carpeta de originales no existe / originals folder not found")
    if not CONGELADO and not ANONIMIZAR.exists():
        raise HTTPException(500, "falta anonimizar.py junto a app.py")
    marca = en_sincronizada(TRABAJO)
    if marca:
        raise HTTPException(400, f"la carpeta de trabajo está dentro de una carpeta sincronizada ({marca}); elige otra")
    o = origen.resolve()
    for prohibida in (TRABAJO / "limpios", carpeta_privada(), carpeta_reportes(), TRABAJO / "paquetes"):
        pr = prohibida.resolve()
        if o == pr or o.is_relative_to(pr) or pr.is_relative_to(o):
            raise HTTPException(400, "la carpeta de originales se solapa con la copia limpia, la privada, los reportes o los paquetes; elige otra")
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
    return {k: v for k, v in t.items() if k != "indexado"}


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


@api.post("/empaquetar")
def empaquetar(request: Request, cuerpo: dict):
    auth(request)
    pacientes = sorted({str(p) for p in cuerpo.get("pacientes", [])})
    if not pacientes:
        raise HTTPException(400, "elige al menos un paciente / pick at least one patient")
    carpetas = {pac: carpeta_paciente(pac) for pac in pacientes}   # valida cada identificador antes de tocar nada
    if (TRABAJO / "limpios" / ".corriendo").exists():
        raise HTTPException(409, "hay una limpieza en curso; espera a que termine / a cleaning run is in progress")

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
                rel = str(a.relative_to(limpios))
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
                entradas.append((rel, datos, hashlib.sha256(datos).hexdigest()))
    if fallos:
        registrar(f"empaquetar: rechazado, {len(fallos)} archivo(s) con problemas en {', '.join(pacientes)}")
        return JSONResponse(status_code=409, content={"error": "la compuerta falló; nada se empaquetó", "fallos": fallos})
    if not entradas:
        raise HTTPException(409, "no hay archivos DICOM limpios en la selección / nothing to package")

    try:
        politica = json.loads((limpios / ".politica.json").read_text(encoding="utf-8")).get("politica", "")
    except (OSError, ValueError):
        politica = ""
    revisiones = {pac: leer_revision(pac) for pac in pacientes}
    pendientes = [pac for pac, r in revisiones.items() if not r.get("lista_para_compartir")]
    manifiesto = {"version_app": VERSION, "politica": politica, "prefijo": prefijo,
                  "creado": dt.datetime.now().isoformat(timespec="seconds"), "pacientes": pacientes,
                  "revision_pixeles": {pac: {k: r.get(k) for k in ("estado", "vigente", "fecha", "hallazgos", "detalle")} for pac, r in revisiones.items()},
                  "revision_pixeles_pendiente": pendientes,
                  "aviso": "paquete creado localmente; la revisión de píxeles está pendiente para los pacientes listados" if pendientes else "revisión de píxeles vigente y sin hallazgos en todos los pacientes",
                  "archivos": [{"ruta": rel, "sha256": h, "bytes": len(datos)} for rel, datos, h in entradas],
                  "verificacion": "cada archivo verificado a partir de los mismos bytes que se empaquetaron"}

    # 2. zip temporal desde esos bytes, relectura y comparación de hashes, publicación atómica con nombre único
    destino_dir = TRABAJO / "paquetes"
    destino_dir.mkdir(parents=True, exist_ok=True)
    nombre = f"paquete_{dt.datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(3)}.zip"
    fd, tmp = tempfile.mkstemp(dir=destino_dir, prefix=".tmp_", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for rel, datos, _ in entradas:
                z.writestr(rel, datos)
            z.writestr("manifiesto.json", json.dumps(manifiesto, ensure_ascii=False, indent=2))
        with zipfile.ZipFile(tmp) as z:
            nombres = set(z.namelist())
            for rel, _, h in entradas:
                if rel not in nombres or hashlib.sha256(z.read(rel)).hexdigest() != h:
                    raise RuntimeError(rel)
        os.replace(tmp, destino_dir / nombre)
    except Exception as e:
        Path(tmp).unlink(missing_ok=True)
        registrar(f"empaquetar: falló la comprobación del zip ({type(e).__name__}); nada publicado")
        raise HTTPException(500, "la comprobación del paquete falló; no se publicó / package check failed")
    (destino_dir / (nombre[:-4] + ".manifiesto.json")).write_text(json.dumps(manifiesto, ensure_ascii=False, indent=2), encoding="utf-8")
    registrar(f"empaquetar: {nombre} con {len(entradas)} archivos de {', '.join(pacientes)}; revisión de píxeles pendiente: {', '.join(pendientes) or 'ninguno'}")
    return {"ruta": str(destino_dir / nombre), "archivos": len(entradas), "pacientes": pacientes,
            "revision_pixeles_pendiente": pendientes}


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
    return {str(p.relative_to(carpeta)): sha256_archivo(p) for p in archivos_regulares(carpeta) if p.suffix.lower() == ".dcm"}


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
    registro = {"paciente": pac, "estado": estado, "hallazgos": hallazgos, "notas": str(cuerpo.get("notas", ""))[:2000],
                "revisor": revisor, "fecha": dt.datetime.now().isoformat(timespec="seconds"),
                "app": VERSION, "politica": prefijo_actual() and json.loads((TRABAJO / "limpios" / ".politica.json").read_text(encoding="utf-8")).get("politica", ""),
                "archivos": hashes_paciente(pac)}
    carpeta_reportes().mkdir(parents=True, exist_ok=True)
    (carpeta_reportes() / f"revision_pixeles_{pac}.json").write_text(json.dumps(registro, ensure_ascii=False, indent=2), encoding="utf-8")
    registrar(f"revisión de píxeles: {pac} → {estado} ({len(registro['archivos'])} archivos)")
    return leer_revision(pac)


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
<header><h1>PET/CT local</h1><span>anonimizar · revisar · enviar · todo en 127.0.0.1 · política pet-ct-local/0.4</span></header>
<nav><button data-tab="entrada" class="activa">1. Entrada</button><button data-tab="estudios">2. Estudios</button><button data-tab="enviar">3. Enviar</button><button data-tab="registro">Registro</button></nav>
<main>
<section id="entrada" class="activa">
  <h2>Carpeta con los DICOM originales</h2>
  <p class="suave">Solo se leen desde donde están. La copia limpia se escribe en la carpeta de trabajo. Los objetos que no son CT, PET, MR o segmentación van a cuarentena y no se copian.</p>
  <div class="fila"><input type="text" id="origen" placeholder="/Users/tu/estudios-originales"><button id="btn-origen">Elegir carpeta…</button><button id="btn-demo" title="Crea un caso CT + PET inventado, sin datos de pacientes, para probar el flujo completo">Crear caso de demostración</button></div>
  <p id="aviso-origen" class="aviso"></p>
  <p>Carpeta de trabajo: <code id="trabajo"></code> <span id="aviso-trabajo" class="mal"></span></p>
  <div class="fila"><label>Prefijo <input type="text" id="prefijo" class="corto"></label>
    <label><input type="checkbox" id="conservar"> conservar descripciones de serie y protocolo (revisadas contra nombre e ID)</label>
    <button id="btn-anonimizar" class="primario">Anonimizar</button><span id="estado-anon" class="suave"></span></div>
  <pre id="salida-anon" class="oculto"></pre>
  <pre id="reporte-anon" class="oculto"></pre>
</section>
<section id="estudios">
  <div class="fila"><h2 class="sin-margen">Estudios limpios</h2><button id="btn-refrescar">Refrescar</button><span id="resumen-estudios" class="suave"></span></div>
  <table id="tabla-series"><thead><tr><th>Paciente</th><th>Estudio</th><th>Serie</th><th>Mod.</th><th>Descripción</th><th>Cortes</th><th>Matriz</th><th>Política</th></tr></thead><tbody></tbody></table>
  <div class="visor oculto" id="visor">
    <div><img id="corte" alt="corte"><input type="range" id="deslizador" min="0" max="0" value="0">
      <div class="fila"><label>Ventana <select id="ventana"><option value="tejido">tejido blando</option><option value="pulmon">pulmón</option><option value="hueso">hueso</option><option value="cerebro">cerebro</option><option value="auto">auto (PET, solo vista)</option></select></label><span id="pos" class="suave"></span></div>
      <p class="suave">Vista previa ordenada por número de instancia, sin escala cuantitativa. El SUV se calcula en el paso de procesamiento, no aquí.</p></div>
    <div><div class="meta" id="meta"></div>
      <div class="fila" id="botones-comprobar"><button data-que="emparejar">Emparejar CT + PET</button><button data-que="cotejar">Cotejar con el original</button><button data-que="suv">SUV de esta serie</button><button data-que="comparar_suv">Comparar SUV original / limpio</button></div>
      <p class="suave">Alcance: emparejar y cotejar actúan sobre el paciente en pantalla; SUV y la comparación, sobre esta serie.</p>
      <pre id="salida-comprobar" class="oculto"></pre>
      <div class="revision" id="revision">
        <h3>Revisión de píxeles de este paciente</h3>
        <p class="suave" id="rev-estado"></p>
        <div class="fila"><label><input type="checkbox" id="rev-texto"> texto grabado</label><label><input type="checkbox" id="rev-rostro"> rostro identificable</label><label><input type="checkbox" id="rev-otro"> otro identificador visible</label></div>
        <div class="fila"><input type="text" id="rev-revisor" placeholder="quien revisa" class="corto"><input type="text" id="rev-notas" placeholder="notas (opcional)"></div>
        <div class="fila"><button id="rev-sin">Registrar: revisada, sin hallazgos</button><button id="rev-con">Registrar: con hallazgos</button></div>
        <p class="suave">La revisión queda ligada a los hashes exactos de los archivos del paciente; si cambian, pasa a pendiente. Revisar toda la serie, no solo algunos cortes.</p>
      </div></div>
  </div>
  <div class="antes-despues oculto" id="antes-despues">
    <h2>Antes y después de este corte</h2>
    <p class="suave" id="ad-estado"></p>
    <div class="par"><div><p class="etq">Original</p><img id="img-original" alt="original"></div><div><p class="etq">Limpio</p><img id="img-limpio" alt="limpio"></div></div>
    <div class="destacados" id="destacados"></div>
    <details><summary>Todas las etiquetas por acción</summary><div class="meta" id="detalle-cambios"></div></details>
  </div>
</section>
<section id="enviar">
  <h2>Paquete para el colega</h2>
  <p class="suave">Elige pacientes uno por uno. Cada archivo se lee una sola vez: esos mismos bytes se verifican contra la política, se hashean en el manifiesto y se escriben al zip, que se relee y comprueba antes de publicarse. Solo entran DICOM limpios. Si algo falla, no se crea nada. El zip se crea localmente; que un paciente tenga la revisión de píxeles pendiente se muestra aquí y queda escrito en el manifiesto.</p>
  <div id="lista-pacientes"></div>
  <div class="fila"><button id="btn-verificar">Verificar</button><button id="btn-empaquetar" class="primario">Verificar y empaquetar</button></div>
  <pre id="salida-enviar" class="oculto"></pre>
</section>
<section id="registro"><h2>Registro (privado, se queda en la carpeta de trabajo)</h2><pre id="texto-registro"></pre></section>
</main>
<script src="/static/app.js"></script></body></html>"""

CSS = """:root{--fondo:#f7f7f5;--tinta:#1d1d1b;--suave:#6b6b66;--borde:#d9d9d4;--panel:#fff;--acento:#2f6f9f;--ok:#2d7a4f;--mal:#b3402b;--aviso:#9a6b00}
@media(prefers-color-scheme:dark){:root{--fondo:#161614;--tinta:#ecece8;--suave:#a3a39c;--borde:#33332f;--panel:#1f1f1c;--acento:#7fb3dd;--ok:#6cc08f;--mal:#e27a62;--aviso:#e0b24a}}
*{box-sizing:border-box}body{margin:0;background:var(--fondo);color:var(--tinta);font:15px/1.45 -apple-system,Segoe UI,Helvetica,Arial,sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--borde);display:flex;gap:14px;align-items:baseline}header h1{font-size:18px;margin:0}header span{color:var(--suave);font-size:13px}
nav{display:flex;gap:4px;padding:10px 20px 0}nav button{background:none;border:1px solid var(--borde);border-bottom:none;border-radius:8px 8px 0 0;padding:8px 14px;color:var(--suave);cursor:pointer;font-size:14px}
nav button.activa{background:var(--panel);color:var(--tinta);font-weight:600}
main{padding:20px;max-width:1100px}section{display:none;background:var(--panel);border:1px solid var(--borde);border-radius:0 10px 10px 10px;padding:18px}section.activa{display:block}
h2{font-size:16px;margin:0 0 10px}.sin-margen{margin:0}p{margin:6px 0}.suave{color:var(--suave)}.oculto{display:none}
.fila{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}
input[type=text]{flex:1;min-width:240px;padding:8px 10px;border:1px solid var(--borde);border-radius:8px;background:var(--fondo);color:var(--tinta);font-size:14px}input.corto{min-width:90px;max-width:130px;flex:0}
button{padding:8px 14px;border:1px solid var(--borde);border-radius:8px;background:var(--fondo);color:var(--tinta);cursor:pointer;font-size:14px}
button.primario{background:var(--acento);border-color:var(--acento);color:#fff}button:disabled{opacity:.5;cursor:default}
pre{background:var(--fondo);border:1px solid var(--borde);border-radius:8px;padding:10px;font-size:12.5px;max-height:320px;overflow:auto;white-space:pre-wrap}
.aviso{color:var(--aviso)}.ok{color:var(--ok)}.mal{color:var(--mal)}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:6px 8px;border-bottom:1px solid var(--borde);text-align:left}tr.fila-serie{cursor:pointer}tr.fila-serie:hover{background:var(--fondo)}tr.fila-serie.seleccionada{background:var(--fondo);box-shadow:inset 3px 0 0 var(--acento);font-weight:600}
.revision{margin-top:14px;border-top:1px solid var(--borde);padding-top:10px}.revision h3{font-size:14px;margin:0 0 6px}.pendiente{color:var(--mal);font-weight:600}.vigente{color:var(--ok);font-weight:600}
.visor{display:grid;grid-template-columns:minmax(260px,520px) 1fr;gap:16px;margin-top:14px}.visor.oculto{display:none}
.visor img{width:100%;background:#000;border-radius:8px}.meta{font-size:13px}.meta div{padding:3px 0;border-bottom:1px solid var(--borde)}code{font-size:13px}
input[type=range]{width:100%}label{font-size:13px}#lista-pacientes label{display:block;font-size:14px}
.antes-despues{margin-top:18px;border-top:1px solid var(--borde);padding-top:14px}.antes-despues.oculto{display:none}
.par{display:grid;grid-template-columns:1fr 1fr;gap:12px;max-width:720px}.par img{width:100%;background:#000;border-radius:8px}.etq{font-size:12px;color:var(--suave);margin:0 0 4px}
.destacados{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px;margin:12px 0}.destacados div{border:1px solid var(--borde);border-radius:8px;padding:8px 10px;font-size:13px}.destacados b{display:block;font-size:12px;color:var(--suave);font-weight:500}
details summary{cursor:pointer;font-size:13px;color:var(--suave)}"""

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
const celda = (tr, v, cls) => { const td = document.createElement('td'); td.textContent = v == null ? '' : String(v); if (cls) td.className = cls; tr.appendChild(td); };
document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button, main section').forEach(x => x.classList.remove('activa'));
  b.classList.add('activa'); $('#' + b.dataset.tab).classList.add('activa');
  if (b.dataset.tab === 'estudios') cargarEstudios(); if (b.dataset.tab === 'enviar') cargarPacientes(); if (b.dataset.tab === 'registro') cargarRegistro();
});
async function estado() {
  try {
    const e = await api('estado');
    texto('#trabajo', e.trabajo); $('#prefijo').value = e.prefijo || 'ANON';
    texto('#aviso-trabajo', e.sincronizada ? ' Esta carpeta está sincronizada con la nube (' + e.sincronizada + '). Usa otra con --trabajo.' : '');
    if (!e.anonimizar_presente) texto('#estado-anon', 'Falta anonimizar.py junto a app.py');
  } catch (err) { texto('#estado-anon', 'Abre la dirección completa que imprimió la terminal, con el token.'); }
}
$('#btn-origen').onclick = async () => {
  const r = await api('elegir-carpeta', {});
  if (r.ruta) { $('#origen').value = r.ruta; texto('#aviso-origen', r.sincronizada ? 'Está dentro de una carpeta sincronizada (' + r.sincronizada + '). Se puede leer, pero conviene sacarla de la nube.' : ''); }
  else texto('#aviso-origen', r.mensaje);
};
$('#btn-demo').onclick = async () => {
  $('#btn-demo').disabled = true; texto('#aviso-origen', 'creando caso sintético…');
  try { const r = await api('demo', {}); $('#origen').value = r.ruta; texto('#aviso-origen', 'Caso sintético listo (identidad ficticia, sin datos reales). Pulsa Anonimizar para recorrer el flujo completo.'); }
  catch (err) { texto('#aviso-origen', err.message); } finally { $('#btn-demo').disabled = false; }
};
$('#btn-anonimizar').onclick = async () => {
  const origen = $('#origen').value.trim();
  if (!origen) { texto('#aviso-origen', 'Elige o escribe la carpeta de originales.'); return; }
  $('#btn-anonimizar').disabled = true; $('#salida-anon').classList.remove('oculto'); texto('#salida-anon', ''); $('#reporte-anon').classList.add('oculto'); texto('#estado-anon', 'corriendo…');
  try {
    const {trabajo} = await api('anonimizar', {origen, prefijo: $('#prefijo').value.trim(), conservar_descripciones: $('#conservar').checked});
    const esperar = async () => {
      const t = await api('trabajo/' + trabajo); texto('#salida-anon', t.lineas.join('\n'));
      if (t.estado === 'corriendo') return setTimeout(esperar, 800);
      const el = texto('#estado-anon', t.estado === 'ok' ? 'limpieza terminada; la verificación posterior pasó en las etiquetas revisadas (los píxeles no se revisan)' :
        t.estado === 'con avisos' ? 'terminó con cuarentena o avisos; revisa el reporte antes de enviar' : 'terminó con error; revisa la salida');
      el.className = t.estado === 'ok' ? 'ok' : (t.estado === 'con avisos' ? 'aviso' : 'mal');
      if (t.reporte) { $('#reporte-anon').classList.remove('oculto'); texto('#reporte-anon', t.reporte); }
      $('#btn-anonimizar').disabled = false; ACTUAL = null; $('#visor').classList.add('oculto'); SERIES = []; cargarEstudios(1);
    };
    esperar();
  } catch (err) { texto('#estado-anon', err.message).className = 'mal'; $('#btn-anonimizar').disabled = false; }
};
let SERIES = [], ACTUAL = null, ADSEQ = 0;
async function cargarEstudios(refrescar) {
  SERIES = await api('estudios' + (refrescar ? '?refrescar=1' : ''));
  const tb = $('#tabla-series tbody'); tb.textContent = '';
  texto('#resumen-estudios', SERIES.length + ' series, ' + new Set(SERIES.map(s => s.paciente)).size + ' paciente(s)');
  for (const s of SERIES) {
    const tr = document.createElement('tr'); tr.className = 'fila-serie';
    celda(tr, s.paciente); celda(tr, s.estudio); celda(tr, s.serie); celda(tr, s.modalidad); celda(tr, s.descripcion); celda(tr, s.n);
    celda(tr, s.filas + '×' + s.columnas); celda(tr, s.politica || 'sin política', s.politica ? 'ok' : 'mal');
    tr.dataset.id = s.id; tr.onclick = () => abrir(s); tb.appendChild(tr);
  }
  if (ACTUAL) { const fila = tb.querySelector('tr[data-id="' + ACTUAL.id + '"]'); if (fila) fila.classList.add('seleccionada'); else { ACTUAL = null; $('#visor').classList.add('oculto'); } }
}
$('#btn-refrescar').onclick = () => cargarEstudios(1);
function abrir(s) {
  ACTUAL = s; $('#visor').classList.remove('oculto');
  document.querySelectorAll('#tabla-series tr.fila-serie').forEach(tr => tr.classList.toggle('seleccionada', tr.dataset.id === s.id));
  limpiarFormularioRevision(); cargarRevision(s.paciente); const d = $('#deslizador'); d.max = s.n - 1; d.value = Math.floor(s.n / 2);
  $('#antes-despues').classList.add('oculto'); $('#img-original').removeAttribute('src'); $('#img-limpio').removeAttribute('src'); $('#destacados').textContent = ''; $('#detalle-cambios').textContent = ''; texto('#ad-estado', '');
  $('#salida-comprobar').classList.add('oculto'); texto('#salida-comprobar', '');
  $('#ventana').value = s.modalidad === 'PT' ? 'auto' : 'tejido';
  const m = $('#meta'); m.textContent = '';
  for (const [k, v] of [['Paciente', s.paciente], ['Estudio', s.estudio], ['Serie', s.serie], ['Modalidad', s.modalidad], ['Descripción', s.descripcion], ['Protocolo', s.protocolo],
       ['Matriz', s.filas + '×' + s.columnas], ['Espaciado (mm)', s.espaciado ? s.espaciado.join(' × ') : ''], ['Grosor (mm)', s.grosor ?? ''], ['Cortes', s.n], ['Política', s.politica]]) {
    const div = document.createElement('div'); const b = document.createElement('b'); b.textContent = k; div.appendChild(b); div.appendChild(document.createTextNode(': ' + (v == null ? '' : v))); m.appendChild(div);
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
      texto('#ad-estado', r.motivo); $('#img-original').removeAttribute('src'); $('#destacados').textContent = ''; $('#detalle-cambios').textContent = '';
      if (r.raiz) { const b = document.createElement('button'); b.textContent = 'Indicar dónde están ahora los originales…'; b.onclick = async () => { const x = await api('raiz', {anterior: r.raiz}); if (x.ok) antesDespues(i); else texto('#ad-estado', x.mensaje); }; $('#ad-estado').appendChild(document.createTextNode(' ')); $('#ad-estado').appendChild(b); }
      return;
    }
    $('#img-original').src = '/api/serie/' + sid + '/corte_original/' + i + '?ventana=' + v + '&t=' + encodeURIComponent(T);
    texto('#ad-estado', 'Misma ventana en ambos lados. Los valores originales nunca se muestran; aquí solo aparece qué se hizo con cada etiqueta.');
    const d = r.destacados, s = r.resumen, n = k => s[k] || 0;
    const tarjetas = [['Nombre del paciente', 'oculto → ' + d.nombre], ['ID del paciente', 'oculto → ' + d.id], ['Fecha de nacimiento', 'oculta → ' + d.fecha_nacimiento],
      ['Píxeles (bytes de PixelData)', d.pixeles_identicos ? 'idénticos al original, SHA-256' : 'DISTINTOS al original'], ['Fechas', n('fecha desplazada') + ' desplazadas, horas intactas'],
      ['UID', n('UID reescrito') + ' reescritos'], ['Etiquetas eliminadas', (n('eliminado') + n('privada eliminada')) + ' (' + n('privada eliminada') + ' privadas)'],
      ['Sin cambio', r.sin_cambio + ' etiquetas técnicas'], ['Política', d.politica]];
    const cont = $('#destacados'); cont.textContent = '';
    for (const [k, val] of tarjetas) { const div = document.createElement('div'); const b = document.createElement('b'); b.textContent = k; div.appendChild(b); div.appendChild(document.createTextNode(val)); cont.appendChild(div); }
    const det = $('#detalle-cambios'); det.textContent = '';
    for (const [accion, lista] of Object.entries(r.detalle)) { const div = document.createElement('div'); const b = document.createElement('b'); b.textContent = accion + ' (' + lista.length + ')'; div.appendChild(b); div.appendChild(document.createTextNode(': ' + lista.join(', '))); det.appendChild(div); }
  } catch (err) { texto('#ad-estado', err.message); }
}
$('#deslizador').oninput = mostrarCorte; $('#ventana').onchange = mostrarCorte;
async function cargarPacientes() {
  if (!SERIES.length) SERIES = await api('estudios');
  const pacs = [...new Set(SERIES.map(s => s.paciente))]; const cont = $('#lista-pacientes'); cont.textContent = '';
  if (!pacs.length) { texto('#lista-pacientes', 'Aún no hay estudios limpios.'); return; }
  for (const p of pacs) {
    const lab = document.createElement('label'); const cb = document.createElement('input'); cb.type = 'checkbox'; cb.value = p;
    lab.appendChild(cb); lab.appendChild(document.createTextNode(' ' + p + ' (' + SERIES.filter(s => s.paciente === p).length + ' series) '));
    const est = document.createElement('span'); est.textContent = '…'; lab.appendChild(est); cont.appendChild(lab);
    api('revision/' + encodeURIComponent(p)).then(r => { const ok = !!r.lista_para_compartir; est.textContent = ok ? 'revisión de píxeles vigente, sin hallazgos' : 'REVISIÓN DE PÍXELES PENDIENTE'; est.className = ok ? 'vigente' : 'pendiente'; }).catch(() => { est.textContent = ''; });
  }
}
const elegidos = () => [...document.querySelectorAll('#lista-pacientes input:checked')].map(i => i.value);
$('#btn-verificar').onclick = async () => {
  const o = $('#salida-enviar'); o.classList.remove('oculto');
  try { const r = await api('verificar', {pacientes: elegidos()}); texto('#salida-enviar', Object.entries(r).map(([p, v]) => p + ': ' + (v.codigo === 0 ? 'LIMPIO' : 'PROBLEMAS') + '\n' + v.salida).join('\n')); }
  catch (err) { texto('#salida-enviar', err.message); }
};
$('#btn-empaquetar').onclick = async () => {
  const o = $('#salida-enviar'); o.classList.remove('oculto'); texto('#salida-enviar', 'verificando y empaquetando…');
  try { const r = await api('empaquetar', {pacientes: elegidos()}); texto('#salida-enviar', 'Paquete creado localmente: ' + r.ruta + '\n' + r.archivos + ' archivos de ' + r.pacientes.join(', ') + '\nIncluye manifiesto.json con el hash de cada archivo. El mapa privado no entra.' + (r.revision_pixeles_pendiente && r.revision_pixeles_pendiente.length ? '\nREVISIÓN DE PÍXELES PENDIENTE para: ' + r.revision_pixeles_pendiente.join(', ') + '. El paquete existe, pero no está listo para compartirse.' : '\nRevisión de píxeles vigente y sin hallazgos en todos los pacientes.')); }
  catch (err) { texto('#salida-enviar', 'RECHAZADO: ' + err.message + (err.datos && err.datos.fallos ? '\n' + Object.entries(err.datos.fallos).map(([p, s]) => p + ': ' + (Array.isArray(s) ? s.join('; ') : s)).join('\n') : '')); }
};
document.querySelectorAll('#botones-comprobar button').forEach(b => b.onclick = async () => {
  if (!ACTUAL) return; const o = $('#salida-comprobar'); o.classList.remove('oculto'); texto('#salida-comprobar', 'corriendo ' + b.dataset.que + '…');
  try { const r = await api('comprobar', {que: b.dataset.que, paciente: ACTUAL.paciente, serie: ACTUAL.id}); texto('#salida-comprobar', r.salida); }
  catch (err) { texto('#salida-comprobar', err.message); }
});
function pintarRevision(r) {
  const el = $('#rev-estado'); el.className = '';
  if (r.lista_para_compartir) { el.textContent = 'Revisada sin hallazgos el ' + r.fecha + (r.revisor ? ' por ' + r.revisor : '') + ' (' + r.archivos_revisados + ' archivos, vigente).'; el.classList.add('vigente'); }
  else if (r.estado === 'con_hallazgos' && r.vigente) { el.textContent = 'Con hallazgos (' + (r.hallazgos || []).join(', ') + ') el ' + r.fecha + '. Requiere tratamiento antes de compartir.'; el.classList.add('pendiente'); }
  else { el.textContent = 'Revisión de píxeles pendiente: ' + (r.detalle || ''); el.classList.add('pendiente'); }
}
let REVSEQ = 0;
function limpiarFormularioRevision() { REVSEQ++; ['#rev-texto', '#rev-rostro', '#rev-otro'].forEach(s => { $(s).checked = false; }); $('#rev-notas').value = ''; texto('#rev-estado', 'cargando…').className = ''; $('#rev-sin').disabled = $('#rev-con').disabled = true; }
function hallazgosMarcados() { return [['#rev-texto', 'texto_grabado'], ['#rev-rostro', 'rostro'], ['#rev-otro', 'otro']].filter(([s]) => $(s).checked).map(([, v]) => v); }
function actualizarBotonesRevision() { if (REV_GUARDANDO) return; const h = hallazgosMarcados().length, rev = $('#rev-revisor').value.trim().length > 0; $('#rev-sin').disabled = h > 0 || !rev; $('#rev-con').disabled = h === 0 || !rev; }
['#rev-texto', '#rev-rostro', '#rev-otro'].forEach(s => { $(s).onchange = actualizarBotonesRevision; }); $('#rev-revisor').oninput = actualizarBotonesRevision;
async function cargarRevision(pac) {
  const seq = ++REVSEQ;
  const vigente = () => seq === REVSEQ && ACTUAL && ACTUAL.paciente === pac;
  try { const r = await api('revision/' + encodeURIComponent(pac)); if (vigente()) pintarRevision(r); }
  catch (err) { if (vigente()) texto('#rev-estado', err.message).className = 'pendiente'; }
  finally { if (vigente()) actualizarBotonesRevision(); }
}
let REV_GUARDANDO = false;
async function registrarRevision(estado) {
  if (!ACTUAL || REV_GUARDANDO) return;
  const pac = ACTUAL.paciente, seq = ++REVSEQ;
  const hallazgos = hallazgosMarcados(), revisor = $('#rev-revisor').value.trim();
  if (!revisor) { texto('#rev-estado', 'indica quién revisa').className = 'pendiente'; return; }
  if (estado === 'sin_hallazgos' && hallazgos.length) { texto('#rev-estado', 'hay hallazgos marcados: el resultado no puede ser sin hallazgos').className = 'pendiente'; return; }
  REV_GUARDANDO = true; $('#rev-sin').disabled = $('#rev-con').disabled = true; texto('#rev-estado', 'guardando…').className = '';
  const vigente = () => seq === REVSEQ && ACTUAL && ACTUAL.paciente === pac;   // misma petición, misma selección
  try { const r = await api('revision', {paciente: pac, estado, hallazgos, revisor, notas: $('#rev-notas').value.trim()}); if (vigente()) pintarRevision(r); }
  catch (err) { if (vigente()) texto('#rev-estado', err.message).className = 'pendiente'; }
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
    return PlainTextResponse(CSS, media_type="text/css")


@app.get("/static/app.js")
def js():
    return PlainTextResponse(JS, media_type="application/javascript")


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
    print(f"Abre en el navegador (open in your browser): {url}")
    indexar()
    registrar(f"app {VERSION} iniciada en 127.0.0.1:{args.puerto}")
    if not args.sin_navegador:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.puerto, log_level="warning")


if __name__ == "__main__":
    main()
