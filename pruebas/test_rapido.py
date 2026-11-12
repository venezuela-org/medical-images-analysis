"""
Modo rápido, de extremo a extremo por la API, con la aplicación arrancada desde el código fuente y casos sintéticos.
Express mode, end to end through the API, with synthetic cases.

Cubre lo que la revisión del 11 de noviembre de 2026 pidió comprobar: el zip contiene exactamente los archivos de la
carpeta elegida; nace siempre con la revisión pendiente y dentro de la carpeta de trabajo; el sello de revisión se
aplica al conjunto congelado de la corrida y se rechaza si algo cambió; una corrida repetida sobre un caso ya revisado
vuelve a salir pendiente; el manifiesto del zip rehecho dice lo que debe.
"""
from __future__ import annotations

import importlib.util
import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pydicom
import pytest

RAIZ = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("demo", RAIZ / "herramientas" / "demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class Servidor:
    def __init__(self, trabajo: Path):
        self.trabajo = trabajo
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.puerto = s.getsockname()[1]
        self.log = open(trabajo.parent / "servidor.log", "w", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, str(RAIZ / "herramientas" / "app.py"), "--trabajo", str(trabajo),
                                      "--puerto", str(self.puerto), "--sin-navegador"], stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(240):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.puerto}/static/app.js", timeout=1)
                break
            except Exception:
                time.sleep(0.25)
        texto = (trabajo.parent / "servidor.log").read_text(encoding="utf-8", errors="replace")
        self.token = re.search(r"t=([A-Za-z0-9_-]+)", texto).group(1)

    def api(self, ruta: str, cuerpo: dict | None = None):
        datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.puerto}/api/{ruta}", data=datos,
                                     headers={"X-Token": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def png(self, ruta: str) -> tuple[int, bytes]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.puerto}/api/{ruta}", headers={"X-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def mostrar_todo(self, t: dict) -> dict:
        """Lo que hace el navegador al recorrer la corrida: pide cada cuadro al visor y avisa de que lo mostró."""
        _, series = self.api(f"rapido/{t['id']}/series")
        avance = {}
        for s in series:
            for k, cuadros in enumerate(s["cuadros"], 1):
                for cuadro in range(1, cuadros + 1):
                    estado, _ = self.png(f"rapido/{t['id']}/corte/{s['indice']}/{k}?cuadro={cuadro}")
                    assert estado == 200
                    estado, avance = self.api(f"rapido/{t['id']}/mostrado", {"indice": s["indice"], "k": k, "cuadro": cuadro})
                    assert estado == 200
        return avance

    def rapido(self, origen: Path, destino: Path | None = None, mostrar: bool = True) -> dict:
        estado, r = self.api("rapido", {"origen": str(origen), "destino": str(destino) if destino else ""})
        assert estado == 200, r
        for _ in range(240):
            _, t = self.api(f"trabajo/{r['trabajo']}")
            if t["estado"] != "corriendo":
                break
            time.sleep(0.5)
        t["id"] = r["trabajo"]
        if mostrar and t.get("zip"):
            self.mostrar_todo(t)
        return t

    def cerrar(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


@pytest.fixture
def srv(tmp_path):
    trabajo = tmp_path / "datos-pet"
    trabajo.mkdir()
    s = Servidor(trabajo)
    yield s
    s.cerrar()


def caso(tmp_path: Path, nombre: str, semilla: int = 1) -> Path:
    d = tmp_path / "estudios" / nombre
    demo.escribir_caso(d, 4, semilla)
    return d


def nombres_zip(ruta: str) -> set[str]:
    with zipfile.ZipFile(ruta) as z:
        return set(z.namelist()) - {"manifiesto.json"}


def manifiesto(ruta: str) -> dict:
    with zipfile.ZipFile(ruta) as z:
        return json.loads(z.read("manifiesto.json"))


def test_el_zip_nace_pendiente_y_en_la_carpeta_de_trabajo(srv, tmp_path):
    destino = tmp_path / "Dropbox" / "compartido"     # un destino que parece sincronizado con la nube
    destino.mkdir(parents=True)
    t = srv.rapido(caso(tmp_path, "A"), destino)
    assert t["estado"] in ("ok", "con avisos") and t["zip"]
    assert Path(t["zip"]).parent == srv.trabajo / "paquetes"          # pendiente: se queda en local
    assert list(destino.iterdir()) == []                              # nada llegó al destino sin revisar
    assert t["revision_pixeles_pendiente"] and t["destino_sincronizado"]
    m = manifiesto(t["zip"])
    assert m["revision_pixeles"]["estado"] == "pendiente" and m["revision_pixeles_pendiente"] == t["pacientes"]


def test_sello_escribe_en_el_destino_el_mismo_conjunto_y_elimina_el_pendiente(srv, tmp_path):
    destino = tmp_path / "salida"
    destino.mkdir()
    t = srv.rapido(caso(tmp_path, "A"), destino)
    pendiente, contenido = t["zip"], nombres_zip(t["zip"])
    estado, r = srv.api("rapido/revision", {"trabajo": t["id"], "revisor": ""})
    assert estado == 400
    estado, r = srv.api("rapido/revision", {"trabajo": t["id"], "revisor": "Prueba"})
    assert estado == 200, r
    assert Path(r["zip"]).parent == destino and nombres_zip(r["zip"]) == contenido
    assert r["revision_pixeles_pendiente"] == [] and r["pendiente_anterior_no_eliminado"] == []
    assert not Path(pendiente).exists()
    m = manifiesto(r["zip"])
    assert m["revision_pixeles"]["estado"] == "sin_hallazgos" and m["revision_pixeles"]["revisor"] == "Prueba"
    assert m["revision_pixeles_pendiente"] == [] and {e["ruta"] for e in m["archivos"]} == contenido
    estado, _ = srv.api("rapido/revision", {"trabajo": t["id"], "revisor": "Prueba"})
    assert estado == 409                                              # el sello no se aplica dos veces


def test_solo_entran_los_archivos_de_la_carpeta_elegida(srv, tmp_path):
    """El mismo paciente con dos estudios importados desde carpetas distintas: el zip del segundo no lleva el primero,
    y el sello de esa corrida no deja 'revisado' al paciente completo."""
    a, b = caso(tmp_path, "A", 1), caso(tmp_path, "B", 2)            # misma identidad sintética, estudios distintos
    ta = srv.rapido(a)
    tb = srv.rapido(b)
    assert ta["pacientes"] == tb["pacientes"]                         # mismo seudónimo
    za, zb = nombres_zip(ta["zip"]), nombres_zip(tb["zip"])
    assert len(za) == len(zb) == 8 and za.isdisjoint(zb)              # cada zip, solo su carpeta
    assert tb["otros_archivos_del_paciente"] == {tb["pacientes"][0]: 8}
    estado, r = srv.api("rapido/revision", {"trabajo": tb["id"], "revisor": "Prueba"})
    assert estado == 200 and nombres_zip(r["zip"]) == zb
    assert r["revision_de_paciente_actualizada"] == []                # la corrida no cubre todos los archivos del paciente
    _, rev = srv.api(f"revision/{tb['pacientes'][0]}")
    assert rev["lista_para_compartir"] is False


def test_corrida_repetida_sobre_un_caso_revisado_vuelve_a_salir_pendiente(srv, tmp_path):
    c = caso(tmp_path, "A")
    t1 = srv.rapido(c)
    estado, r = srv.api("rapido/revision", {"trabajo": t1["id"], "revisor": "Prueba"})
    assert estado == 200 and r["revision_de_paciente_actualizada"] == t1["pacientes"]   # aquí sí cubre al paciente entero
    t2 = srv.rapido(c)
    assert t2["revision_pixeles_pendiente"] == t2["pacientes"]        # la corrida nueva nace pendiente igualmente
    assert manifiesto(t2["zip"])["revision_pixeles"]["estado"] == "pendiente"


def test_el_sello_se_rechaza_si_un_archivo_cambio_desde_la_corrida(srv, tmp_path):
    t = srv.rapido(caso(tmp_path, "A"))
    limpio = next((srv.trabajo / "limpios").rglob("*.dcm"))
    ds = pydicom.dcmread(limpio)
    ds.PatientSex = "M"
    ds.save_as(limpio, enforce_file_format=True)
    estado, r = srv.api("rapido/revision", {"trabajo": t["id"], "revisor": "Prueba"})
    assert estado == 409 and "cambiaron" in r["detail"]
    assert Path(t["zip"]).exists()                                    # el pendiente sigue donde estaba, sin sello


def test_suv_no_calculable_es_aviso_y_no_discrepancia(srv, tmp_path):
    c = caso(tmp_path, "A")
    for f in (c / "SE1").iterdir():                                   # sin peso del paciente no hay SUV
        ds = pydicom.dcmread(f)
        del ds.PatientWeight
        ds.save_as(f, enforce_file_format=True)
    t = srv.rapido(c)
    suv = next(p for p in t["pasos"] if p["clave"] == "suv")
    assert suv["estado"] == "aviso" and "no se puede calcular" in suv["resumen"] and "DISCREPANCIA" not in suv["resumen"]
    assert t["zip"] and t["estado"] == "con avisos"                   # informa, no bloquea


def test_misma_carpeta_con_una_serie_retirada_solo_empaqueta_lo_que_queda(srv, tmp_path):
    """Regresión de la revisión del 11 de noviembre: tras importar CT + PET, se retiran los CT de la carpeta de entrada
    y se repite el modo rápido sobre esa misma carpeta. El índice histórico recuerda los CT; la corrida no los incluye."""
    c = caso(tmp_path, "A")
    t1 = srv.rapido(c)
    assert len(nombres_zip(t1["zip"])) == 8
    for f in (c / "SE0").iterdir():                                   # se van los cuatro CT originales
        f.unlink()
    t2 = srv.rapido(c)
    assert t2["estado"] in ("ok", "con avisos"), t2["mensaje"]
    contenido = nombres_zip(t2["zip"])
    assert len(contenido) == 4 and all("_PT_" in n for n in contenido)      # solo los PET que siguen en la carpeta
    cotejo = next(p for p in t2["pasos"] if p["clave"] == "cotejo")
    assert "coinciden byte a byte: 4" in cotejo["resumen"]
    assert {e["ruta"] for e in manifiesto(t2["zip"])["archivos"]} == contenido
    assert t2["otros_archivos_del_paciente"] == {t2["pacientes"][0]: 4}     # los CT guardados quedan fuera y se dice


def test_empaquetado_clasico_por_la_api_exporta_cortes_numerados(srv, tmp_path):
    t = srv.rapido(caso(tmp_path, "A"))
    estado, r = srv.api("empaquetar", {"pacientes": t["pacientes"]})
    assert estado == 200 and r["orden_sin_verificar"] == [] and "exportacion" not in r and "hashes" not in r
    nombres = sorted(nombres_zip(r["ruta"]))
    assert len(nombres) == 8 and all(n.rsplit("/", 1)[1].startswith("corte_00000") for n in nombres)
    assert {e["ruta"] for e in manifiesto(r["ruta"])["archivos"]} == set(nombres) == nombres_zip(t["zip"])


ENHANCED_CT = "1.2.840.10008.5.1.4.1.1.2.1"


def caso_con_dos_cuadros(tmp_path: Path) -> Path:
    d = caso(tmp_path, "multicuadro")
    ds = pydicom.dcmread(d / "SE0" / "IM0")
    ds.SOPClassUID = ENHANCED_CT
    ds.file_meta.MediaStorageSOPClassUID = ENHANCED_CT
    ds.SOPInstanceUID = pydicom.uid.generate_uid()
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.SeriesInstanceUID = pydicom.uid.generate_uid()
    ds.SeriesNumber = 7
    ds.NumberOfFrames = 2
    ds.PixelData = ds.PixelData + bytes(255 - b for b in ds.PixelData)
    (d / "SE2").mkdir()
    ds.save_as(d / "SE2" / "MF0", enforce_file_format=True)
    return d


def test_archivos_con_varios_cuadros_por_la_api(srv, tmp_path):
    t = srv.rapido(caso_con_dos_cuadros(tmp_path))
    assert t["zip"] and t["no_mostrables"] == []
    estado, series = srv.api(f"rapido/{t['id']}/series")
    indice = next(s["indice"] for s in series if s["cuadros"] == [2])

    def png(ruta: str) -> tuple[int, bytes]:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.puerto}/api/{ruta}", headers={"X-Token": srv.token})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    e1, uno = png(f"rapido/{t['id']}/corte/{indice}/1?cuadro=1")
    e2, dos = png(f"rapido/{t['id']}/corte/{indice}/1?cuadro=2")
    e3, _ = png(f"rapido/{t['id']}/corte/{indice}/1?cuadro=3")
    assert (e1, e2, e3) == (200, 200, 404) and uno[:4] == dos[:4] == b"\x89PNG" and uno != dos
    # Estudios muestra un cuadro por archivo; dibuja el que se le pida, pero no permite recorrerlos
    _, estudios = srv.api("estudios?refrescar=1")
    multi = next(s for s in estudios if s["cuadros_max"] == 2)
    e4, _ = png(f"serie/{multi['id']}/corte/0?cuadro=2")
    assert e4 == 200
    # por eso la revisión clásica "sin hallazgos" de ese paciente se rechaza, y se dice dónde hacerla
    estado, r = srv.api("revision", {"paciente": t["pacientes"][0], "estado": "sin_hallazgos", "hallazgos": [], "revisor": "Prueba"})
    assert estado == 409 and "varios cuadros" in r["detail"] and "modo rápido" in r["detail"]
    estado, r = srv.api("revision", {"paciente": t["pacientes"][0], "estado": "con_hallazgos", "hallazgos": ["texto_grabado"], "revisor": "Prueba"})
    assert estado == 200                                              # registrar hallazgos sí se puede
    estado, r = srv.api("rapido/revision", {"trabajo": t["id"], "revisor": "Prueba"})
    assert estado == 200 and r["revision_pixeles_pendiente"] == []    # y la corrida, que sí permite verlos todos, se revisa


def test_avance_y_retirada_por_la_api(srv, tmp_path):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = srv.rapido(caso(tmp_path, "A"), destino, mostrar=False)
    assert t["progreso"]["mostrados"] == 0 and t["progreso"]["total"] == 8      # la corrida no cuenta lo que dibujó por su cuenta
    estado, r = srv.api("rapido/revision", {"trabajo": t["id"], "revisor": "Prueba"})
    assert estado == 409 and "se han mostrado 0 de 8 cuadros" in r["detail"]
    estado, r = srv.api(f"rapido/{t['id']}/mostrado", {"indice": 0, "k": 1, "cuadro": 1})
    assert estado == 409                                              # avisar sin haber pedido el cuadro al visor no cuenta
    avance = srv.mostrar_todo(t)
    assert avance["completo"] is True and avance["mostrados"] == 8
    _, estado_trabajo = srv.api(f"trabajo/{t['id']}")
    assert estado_trabajo["progreso"]["completo"] is True and "servidos" not in estado_trabajo and "mostrados" not in estado_trabajo
    estado, r = srv.api("rapido/revision", {"trabajo": t["id"], "revisor": "Ana"})
    assert estado == 200 and Path(r["zip"]).parent == destino
    # la lista de revisiones registradas, y la retirada
    _, lista = srv.api("revisiones")
    corrida = lista["corridas"][0]
    assert corrida["revisor"] == "Ana" and corrida["se_puede_retirar"] is True and corrida["zip"]["existe"] is True
    assert corrida["mostrado"]["cuadros_mostrados"] == 8
    estado, _ = srv.api("revisiones/retirar", {"tipo": "corrida", "id": corrida["registro"], "quien": "", "motivo": "sin nombre"})
    assert estado == 400
    antes = Path(r["zip"]).read_bytes()
    estado, ret = srv.api("revisiones/retirar", {"tipo": "corrida", "id": corrida["registro"], "quien": "Arturo", "motivo": "registrada como prueba"})
    assert estado == 200 and ret["paquetes_afectados"][0]["ruta"] == r["zip"] and ret["paquetes_afectados"][0]["existe"] is True
    assert Path(r["zip"]).read_bytes() == antes                       # el zip exportado no cambia
    _, lista = srv.api("revisiones")
    assert lista["corridas"][0]["retirada"]["quien"] == "Arturo" and lista["corridas"][0]["se_puede_retirar"] is False
    _, rev = srv.api(f"revision/{t['pacientes'][0]}")
    assert rev["estado"] == "retirada" and rev["lista_para_compartir"] is False
    _, estado_trabajo = srv.api(f"trabajo/{t['id']}")
    assert estado_trabajo["revision_retirada"]["motivo"] == "registrada como prueba"


def test_revision_clasica_por_la_api_exige_mostrar_todos_los_cortes(srv, tmp_path):
    t = srv.rapido(caso(tmp_path, "A"))
    pac = t["pacientes"][0]
    _, series = srv.api("estudios?refrescar=1")
    estado, r = srv.api("revision", {"paciente": pac, "estado": "sin_hallazgos", "hallazgos": [], "revisor": "Prueba"})
    assert estado == 409 and "se han mostrado 0 de 8 cortes" in r["detail"]
    estado, _ = srv.api(f"serie/{series[0]['id']}/mostrado", {"i": 0})
    assert estado == 409                                              # sin haberlo pedido al visor, no cuenta
    for s in series:
        for i in range(s["n"]):
            assert srv.png(f"serie/{s['id']}/corte/{i}")[0] == 200
            estado, avance = srv.api(f"serie/{s['id']}/mostrado", {"i": i})
            assert estado == 200
    assert avance["completo"] is True
    _, rev = srv.api(f"revision/{pac}")
    assert rev["progreso"]["mostrados"] == 8
    estado, r = srv.api("revision", {"paciente": pac, "estado": "sin_hallazgos", "hallazgos": [], "revisor": "Prueba"})
    assert estado == 200 and r["lista_para_compartir"] is True and r["mostrado"]["cuadros_mostrados"] == 8
    estado, ret = srv.api("revisiones/retirar", {"tipo": "paciente", "id": pac, "quien": "Arturo", "motivo": "prueba de retirada"})
    assert estado == 200 and ret["retiradas"] == [{"tipo": "paciente", "id": pac}]


def test_con_un_diario_de_retirada_ilegible_la_api_rechaza_las_dos_revisiones(srv, tmp_path):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = srv.rapido(caso(tmp_path, "A"), destino)
    pac = t["pacientes"][0]
    _, series = srv.api("estudios?refrescar=1")
    for s in series:                                                  # todos los cortes del paciente, mostrados en Estudios
        for i in range(s["n"]):
            srv.png(f"serie/{s['id']}/corte/{i}")
            srv.api(f"serie/{s['id']}/mostrado", {"i": i})
    carpeta = srv.trabajo / "limpios_REPORTES" / "retiradas"
    carpeta.mkdir(parents=True, exist_ok=True)
    roto = carpeta / "retirada_20261112_101010_abcdef.json"
    roto.write_text('{"id": "20261112_101010_abcdef", "retirada": {"quien": "A', encoding="utf-8")      # un diario a medio escribir
    _, estado_trabajo = srv.api(f"trabajo/{t['id']}")
    assert estado_trabajo["retiradas_no_verificables"][0]["diario"] == roto.name

    estado, r = srv.api("rapido/revision", {"trabajo": t["id"], "revisor": "Prueba"})
    assert estado == 409 and r["retiradas_no_verificables"] is True and r["diarios_con_problemas"][0]["diario"] == roto.name
    assert roto.name in r["detail"] and "no es JSON válido" in r["detail"]
    estado, r = srv.api("revision", {"paciente": pac, "estado": "sin_hallazgos", "hallazgos": [], "revisor": "Prueba"})
    assert estado == 409 and r["retiradas_no_verificables"] is True and r["diarios_con_problemas"][0]["diario"] == roto.name
    assert list(destino.iterdir()) == []                              # nada publicado
    assert not list((srv.trabajo / "limpios_REPORTES").glob("revision_*"))                              # ninguna revisión registrada
    estado, lista = srv.api("revisiones")
    assert lista["retiradas_no_verificables"] is True and lista["diarios_con_problemas"][0]["diario"] == roto.name

    roto.unlink()                                                     # resuelto el diario, las dos funcionan
    estado, r = srv.api("rapido/revision", {"trabajo": t["id"], "revisor": "Prueba"})
    assert estado == 200 and Path(r["zip"]).parent == destino and r["revision_pixeles_pendiente"] == []
    _, estado_trabajo = srv.api(f"trabajo/{t['id']}")
    assert estado_trabajo["retiradas_no_verificables"] == []


def test_la_revision_de_estudios_se_liga_al_contenido_mostrado(srv, tmp_path):
    """Se muestran todos los cortes; después un archivo se sustituye por otro del mismo tamaño y con la misma fecha. La
    declaración 'sin hallazgos' se rechaza: esos píxeles no han estado en pantalla."""
    import os
    t = srv.rapido(caso(tmp_path, "A"))
    pac = t["pacientes"][0]
    _, series = srv.api("estudios?refrescar=1")
    for s in series:
        for i in range(s["n"]):
            assert srv.png(f"serie/{s['id']}/corte/{i}")[0] == 200
            assert srv.api(f"serie/{s['id']}/mostrado", {"i": i})[0] == 200
    victima = next((srv.trabajo / "limpios" / pac).rglob("*.dcm"))
    st = os.stat(victima)
    datos = bytearray(victima.read_bytes())
    datos[-1] ^= 0xFF
    victima.write_bytes(bytes(datos))
    os.utime(victima, ns=(st.st_atime_ns, st.st_mtime_ns))
    estado, r = srv.api("revision", {"paciente": pac, "estado": "sin_hallazgos", "hallazgos": [], "revisor": "Prueba"})
    assert estado == 409 and "se han mostrado 7 de 8 cortes" in r["detail"] and "tal como están ahora" in r["detail"]
    assert not list((srv.trabajo / "limpios_REPORTES").glob("revision_pixeles_*"))      # no se registró nada
