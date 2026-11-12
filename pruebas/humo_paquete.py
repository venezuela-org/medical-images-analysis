#!/usr/bin/env python3
"""
Prueba de humo del paquete de escritorio, a través de la API de la aplicación.
Smoke test of the packaged app through its API. Pure standard library; runs on the release runners.

Arranca el ejecutable con --sin-navegador, crea el caso sintético, lo importa, comprueba el estado,
corre la comparación SUV, registra una revisión de píxeles sin hallazgos, exporta un paquete y lo verifica:
solo DICOM limpios y manifiesto, ningún archivo privado, hashes del manifiesto iguales a los del zip, y la
compuerta (`herramienta anonimizar verificar`) en verde sobre el contenido extraído. Comprueba también que
un paquete con la revisión pendiente queda marcado como tal.

Uso / Usage
  python pruebas/humo_paquete.py --binario "herramientas/dist/PET-CT local/PET-CT local" [--puerto 8765]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

PRIVADOS = ("mapa_seudonimos", "indice_origen", "raices.json", "corridas.jsonl", "revision_pixeles", "registro.log", "PRIVADO")


def api(puerto: int, token: str, ruta: str, cuerpo: dict | None = None, metodo: str | None = None):
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{puerto}/api/{ruta}", data=datos, method=metodo or ("POST" if cuerpo is not None else "GET"),
                                 headers={"X-Token": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def esperar(puerto: int, segundos: int = 90) -> bool:
    for _ in range(segundos * 2):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{puerto}/static/app.js", timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def ok(condicion: bool, mensaje: str) -> None:
    print(("  OK   " if condicion else "  FALLA ") + mensaje)
    if not condicion:
        raise SystemExit(f"prueba de humo fallida: {mensaje}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--binario", required=True)
    p.add_argument("--puerto", type=int, default=8765)
    a = p.parse_args()
    binario = str(Path(a.binario).resolve())
    trabajo = Path(tempfile.mkdtemp(prefix="humo_")).resolve()
    log = open(trabajo / "servidor.log", "w", encoding="utf-8")
    proc = subprocess.Popen([binario, "--trabajo", str(trabajo), "--puerto", str(a.puerto), "--sin-navegador"],
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        ok(esperar(a.puerto), "el servidor local responde")
        texto = (trabajo / "servidor.log").read_text(encoding="utf-8", errors="replace")
        m = re.search(r"t=([A-Za-z0-9_-]+)", texto)
        ok(m is not None, "la terminal muestra la dirección con el token")
        token = m.group(1)
        estado, _ = api(a.puerto, "", "estado")
        ok(estado == 401, "sin token la API responde 401")
        estado, r = api(a.puerto, token, "estado")
        ok(estado == 200 and r.get("congelado") is True, "estado con token; la aplicación corre empaquetada")

        estado, demo = api(a.puerto, token, "demo", {}, "POST")
        ok(estado == 200 and Path(demo["ruta"]).is_dir(), "caso sintético creado desde la API")
        estado, job = api(a.puerto, token, "anonimizar", {"origen": demo["ruta"]})
        ok(estado == 200, "importación lanzada")
        for _ in range(120):
            estado, t = api(a.puerto, token, f"trabajo/{job['trabajo']}")
            if t.get("estado") != "corriendo":
                break
            time.sleep(1)
        ok(t.get("estado") == "ok", "importación terminada sin cuarentena: " + next((l for l in t.get("lineas", []) if l.startswith("Listo")), "")[:80])

        estado, series = api(a.puerto, token, "estudios?refrescar=1")
        pacientes = sorted({s["paciente"] for s in series})
        ok(len(series) == 2 and len(pacientes) == 1, f"inventario: {len(series)} series de {len(pacientes)} paciente")
        pac = pacientes[0]
        pt = next(s for s in series if s["modalidad"] == "PT")
        estado, cambios = api(a.puerto, token, f"serie/{pt['id']}/cambios/0")
        ok(cambios.get("disponible") and cambios["destacados"]["pixeles_identicos"], "panel antes/después: píxeles idénticos")
        estado, comp = api(a.puerto, token, "comprobar", {"que": "comparar_suv", "paciente": pac, "serie": pt["id"]})
        ok("Veredicto: IDENTICO" in comp.get("salida", ""), "comparación SUV original/limpio: IDENTICO")
        estado, comp = api(a.puerto, token, "comprobar", {"que": "emparejar", "paciente": pac})
        ok("CANDIDATO COMPATIBLE" in comp.get("salida", ""), "emparejamiento: CANDIDATO COMPATIBLE")

        # exportación con la revisión de píxeles pendiente: se crea, pero marcado
        estado, paq = api(a.puerto, token, "empaquetar", {"pacientes": [pac]})
        ok(estado == 200 and paq.get("revision_pixeles_pendiente") == [pac], "paquete creado y marcado con revisión de píxeles pendiente")
        # revisión contradictoria rechazada, revisión válida aceptada
        estado, _ = api(a.puerto, token, "revision", {"paciente": pac, "estado": "sin_hallazgos", "hallazgos": ["rostro"], "revisor": "humo"})
        ok(estado == 400, "revisión contradictoria rechazada")
        estado, rev = api(a.puerto, token, "revision", {"paciente": pac, "estado": "sin_hallazgos", "hallazgos": [], "revisor": "humo"})
        ok(estado == 200 and rev.get("lista_para_compartir") is True, "revisión sin hallazgos registrada y vigente")
        estado, paq = api(a.puerto, token, "empaquetar", {"pacientes": [pac]})
        ok(estado == 200 and paq.get("revision_pixeles_pendiente") == [], "paquete creado con revisión vigente")

        # verificación del paquete: contenido, manifiesto, hashes y compuerta sobre lo extraído
        zpath = Path(paq["ruta"])
        with zipfile.ZipFile(zpath) as z:
            nombres = z.namelist()
            ok(len(nombres) == len(set(nombres)), "el zip no tiene entradas repetidas")
            ok(all(n.endswith(".dcm") or n == "manifiesto.json" for n in nombres), "el zip solo contiene DICOM limpios y manifiesto.json")
            ok(not any(any(p in n for p in PRIVADOS) for n in nombres), "ningún archivo privado dentro del zip")
            manifiesto = json.loads(z.read("manifiesto.json"))
            ok(manifiesto.get("revision_pixeles_pendiente") == [], "el manifiesto registra la revisión vigente")
            dicoms = sorted(n for n in nombres if n != "manifiesto.json")
            ok(all(n.rsplit("/", 1)[1].startswith("corte_") for n in dicoms), "los archivos exportados se llaman corte_NNNNNN_<hash>.dcm")
            import io as _io
            import pydicom as _pydicom
            por_serie = {}
            for n in dicoms:
                por_serie.setdefault(n.rsplit("/", 1)[0], []).append(int(_pydicom.dcmread(_io.BytesIO(z.read(n)), stop_before_pixels=True).InstanceNumber))
            ok(all(v == sorted(v) and v[0] == 1 for v in por_serie.values()), "en cada serie, el orden alfabético de los nombres es el orden de los cortes")
            rutas_manifestadas = [e["ruta"] for e in manifiesto["archivos"]]
            ok(len(rutas_manifestadas) == len(set(rutas_manifestadas)), "el manifiesto no repite archivos")
            ok(set(rutas_manifestadas) == set(nombres) - {"manifiesto.json"}, "el manifiesto enumera exactamente los DICOM del zip")
            for e in manifiesto["archivos"]:
                ok(hashlib.sha256(z.read(e["ruta"])).hexdigest() == e["sha256"], f"hash del manifiesto coincide: {e['ruta'][-20:]}")
            extraido = trabajo / "extraido"
            z.extractall(extraido)
        (extraido / "manifiesto.json").unlink()
        r = subprocess.run([binario, "herramienta", "anonimizar", "verificar", str(extraido), "--prefijo", "ANON"], capture_output=True, text=True)
        ok(r.returncode == 0, "la compuerta pasa sobre el contenido extraído del paquete")
        # modo rápido: otra carpeta sintética, de principio a fin, a un destino elegido, y el sello de revisión
        estado, demo2 = api(a.puerto, token, "demo", {}, "POST")
        destino = trabajo / "destino-elegido"
        destino.mkdir()
        estado, r = api(a.puerto, token, "rapido", {"origen": demo2["ruta"], "destino": str(trabajo / "limpios_PRIVADO")})
        ok(estado == 400, "modo rápido: destino dentro de la carpeta privada, rechazado")
        estado, r = api(a.puerto, token, "rapido", {"origen": demo2["ruta"], "destino": str(destino)})
        ok(estado == 200, "modo rápido lanzado")
        for _ in range(180):
            estado, t = api(a.puerto, token, f"trabajo/{r['trabajo']}")
            if t.get("estado") != "corriendo":
                break
            time.sleep(1)
        ok(t.get("estado") in ("ok", "con avisos") and t.get("zip"), "modo rápido terminado con zip: " + str(t.get("mensaje"))[:70])
        ok([p_["estado"] for p_ in t["pasos"]].count("error") == 0 and len(t["pasos"]) == 6, "modo rápido: seis pasos sin fallos")
        ok(Path(t["zip"]).parent.resolve() == (trabajo / "paquetes").resolve() and Path(t["zip"]).is_file(), "el zip pendiente se queda en la carpeta de trabajo")
        estado, series = api(a.puerto, token, f"rapido/{r['trabajo']}/series")
        ok(estado == 200 and len(series) == 2 and all(s_["nombres"] == sorted(s_["nombres"]) and s_["nombres"][0].startswith("corte_000001_") for s_ in series),
           "visor de la corrida: dos series con sus nombres exportados en orden")
        req = urllib.request.Request(f"http://127.0.0.1:{a.puerto}/api/rapido/{r['trabajo']}/corte/0/1?ventana=tejido", headers={"X-Token": token})
        with urllib.request.urlopen(req, timeout=60) as resp:
            ok(resp.status == 200 and resp.read()[:8] == b"\x89PNG\r\n\x1a\n", "visor de la corrida: el primer corte se renderiza")
        ok(list(destino.iterdir()) == [], "nada llega al destino antes de la revisión")
        ok(len(t.get("revision_pixeles_pendiente", [])) == 1, "el zip del modo rápido nace con la revisión de píxeles pendiente")
        with zipfile.ZipFile(t["zip"]) as z:
            contenido = set(z.namelist()) - {"manifiesto.json"}
        estado, rv = api(a.puerto, token, "rapido/revision", {"trabajo": r["trabajo"], "revisor": ""})
        ok(estado == 400, "sello de revisión sin revisor, rechazado")
        estado, rv = api(a.puerto, token, "rapido/revision", {"trabajo": r["trabajo"], "revisor": "humo"})
        ok(estado == 200 and rv.get("revision_pixeles_pendiente") == [] and Path(rv["zip"]).parent.resolve() == destino.resolve(),
           "sello de revisión: el zip revisado está en el destino elegido")
        ok(not Path(t["zip"]).exists() and rv.get("pendiente_anterior_no_eliminado") == [], "el zip pendiente se eliminó de la carpeta de trabajo")
        with zipfile.ZipFile(rv["zip"]) as z:
            m2 = json.loads(z.read("manifiesto.json"))
            ok(set(z.namelist()) - {"manifiesto.json"} == contenido, "el zip revisado contiene exactamente los archivos de la corrida")
        ok(m2["revision_pixeles"]["estado"] == "sin_hallazgos" and m2["revision_pixeles"]["revisor"] == "humo" and m2["revision_pixeles_pendiente"] == [],
           "el manifiesto del zip rehecho registra la revisión de esa corrida")
        ok({e["ruta"] for e in m2["archivos"]} == contenido, "el manifiesto del zip rehecho enumera esos mismos archivos")
        demo_m = m2.get("demografia", {}).get("estudios", [])
        ok(len(demo_m) == 1 and demo_m[0]["etiqueta"] == demo_m[0]["paciente"] + " · O · 63 años en el estudio"
           and demo_m[0]["edad_en_el_estudio"]["estado"] == "calculada" and demo_m[0]["sexo"]["estado"] == "registrado",
           "el manifiesto lleva la demografía del estudio: " + (demo_m[0]["etiqueta"] if demo_m else "ausente"))
        ok("19600615" not in json.dumps(m2) and "20240305" not in json.dumps(m2), "el manifiesto no lleva la fecha de nacimiento ni la fecha original del estudio")
        print("prueba de humo del paquete: todo correcto")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
