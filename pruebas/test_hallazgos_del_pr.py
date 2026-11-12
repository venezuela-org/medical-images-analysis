"""
Los cinco hallazgos de la revisión automática del PR #2. / The five findings of the automated review of PR #2.

1. Un paquete de Enviar no puede publicarse como revisado si la revisión se retira mientras se empaqueta.
2. El registro de demografía se escribe de forma atómica y un registro ilegible no se da por vacío.
3. Una copia en cuarentena por contenido distinto no pisa lo que se sabe del archivo limpio guardado.
4. Lo mostrado en Estudios se liga al contenido de los archivos, no a su tamaño y fecha.
5. Los paquetes afectados por una retirada se reconocen por el registro exacto que citan, no por la fecha.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import zipfile
from pathlib import Path

import pydicom
import pytest
from fastapi import HTTPException

RAIZ = Path(__file__).resolve().parents[1]


def cargar(nombre: str, ruta: Path):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


demo = cargar("demo", RAIZ / "herramientas" / "demo.py")
anon = cargar("anonimizar_pr", RAIZ / "herramientas" / "anonimizar.py")


@pytest.fixture
def app(tmp_path):
    a = cargar("app_en_prueba", RAIZ / "herramientas" / "app.py")
    a.TRABAJO = tmp_path / "datos-pet"
    a.TRABAJO.mkdir()
    return a


def correr(app, origen: Path, destino: Path, tid: str) -> dict:
    assert app.CANDADO.acquire(blocking=False)
    app.TRABAJOS[tid] = {"nombre": "rapido", "estado": "corriendo", "lineas": [], "codigo": None, "pasos": [], "pacientes": [],
                         "origen": str(origen), "destino": str(destino), "zip": None, "mensaje": "", "archivos": {},
                         "revisado": False, "servidos": set(), "mostrados": set()}
    app._rapido(tid, origen, destino, "ANON")
    return app.TRABAJOS[tid]


def caso(tmp_path: Path, nombre: str = "A", semilla: int = 1, sexo: str = "F") -> Path:
    d = tmp_path / "estudios" / nombre
    demo.escribir_caso(d, 4, semilla)
    for f in d.rglob("IM*"):
        ds = pydicom.dcmread(f)
        ds.PatientSex = sexo
        ds.save_as(f, enforce_file_format=True)
    return d


def mostrar_en_estudios(app, pac: str) -> None:
    """Lo que hace el navegador en Estudios: cada corte se sirve (se leen sus bytes) y después se avisa de que se mostró."""
    app.indexar()
    for s in app.INDICE.values():
        if s["paciente"] == pac:
            for _, ruta in s["cortes"]:
                app.anotar_servido_estudios(pac, ruta, 1, hashlib.sha256(Path(ruta).read_bytes()).hexdigest())
                assert app.anotar_mostrado_estudios(pac, ruta, 1)


def revisar_en_estudios(app, pac: str, revisor: str) -> dict:
    mostrar_en_estudios(app, pac)
    actuales = app.hashes_paciente(pac)
    assert app.progreso_paciente(pac, actuales)["completo"]
    return app.guardar_revision(pac, "sin_hallazgos", [], revisor, archivos=actuales)


def paciente_revisado(app, tmp_path, revisor: str = "Ana") -> str:
    t = correr(app, caso(tmp_path), tmp_path, "c")
    pac = t["pacientes"][0]
    assert revisar_en_estudios(app, pac, revisor)["lista_para_compartir"] is True
    return pac


def zips(app) -> list[str]:
    return sorted(p.name for p in (app.TRABAJO / "paquetes").glob("paquete_*.zip"))


def manifiesto(ruta) -> dict:
    with zipfile.ZipFile(ruta) as z:
        return json.loads(z.read("manifiesto.json"))


# ---------------------------------------------------------------------------------------------------------------
# 1. la revisión se retira mientras se empaqueta
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("momento", ["antes de dejar evidencias", "con las evidencias ya preparadas"])
def test_si_la_revision_se_retira_mientras_se_empaqueta_el_paquete_no_sale_como_revisado(app, tmp_path, monkeypatch, momento):
    pac = paciente_revisado(app, tmp_path)
    antes = zips(app)
    hecho = {"si": False}

    def retirar_ahora():
        if not hecho["si"]:
            hecho["si"] = True                                        # una retirada válida, comprometida a mitad del empaquetado
            assert app.retirar_revision("paciente", pac, "Arturo", "retirada mientras se empaqueta")["estado"] == "completa"

    if momento == "antes de dejar evidencias":
        real = app.sha256_archivo

        def al_hashear_el_zip(ruta):
            if Path(ruta).parent.name == "paquetes" and not hecho["si"]:
                retirar_ahora()
            return real(ruta)

        monkeypatch.setattr(app, "sha256_archivo", al_hashear_el_zip)
    else:
        real = app._escribir_json

        def al_preparar_el_manifiesto(ruta, datos):
            real(ruta, datos)
            if str(ruta).endswith(".manifiesto.json" + app.SUFIJO_PREPARADO):
                retirar_ahora()

        monkeypatch.setattr(app, "_escribir_json", al_preparar_el_manifiesto)

    with pytest.raises(app.PaqueteRechazado) as x:
        app.crear_paquete([pac])
    assert hecho["si"] and x.value.codigo == 409
    assert x.value.cuerpo["revisiones_que_cambiaron"] == {pac: "su revisión se retiró mientras se empaquetaba"} or \
        x.value.cuerpo["revisiones_que_cambiaron"] == {pac: "su registro de revisión cambió mientras se empaquetaba"}
    assert x.value.cuerpo["restos"] == [] and "dejó de valer" in x.value.cuerpo["detail"]
    assert zips(app) == antes                                         # no se publicó nada
    assert not list((app.TRABAJO / "paquetes").glob("*" + app.SUFIJO_PREPARADO))       # ni quedó evidencia preparada
    monkeypatch.undo()
    r = app.crear_paquete([pac])                                      # al volver a empaquetar, sale pendiente
    assert r["revision_pixeles_pendiente"] == [pac] and manifiesto(r["ruta"])["revision_pixeles"][pac]["estado"] == "retirada"


def test_si_el_registro_de_la_revision_cambia_mientras_se_empaqueta_tampoco_se_publica(app, tmp_path, monkeypatch):
    pac = paciente_revisado(app, tmp_path)
    antes = zips(app)
    real = app.sha256_archivo
    hecho = {"si": False}

    def al_hashear_el_zip(ruta):
        if Path(ruta).parent.name == "paquetes" and not hecho["si"]:
            hecho["si"] = True                                        # alguien registra un hallazgo mientras se empaqueta
            app.guardar_revision(pac, "con_hallazgos", ["rostro"], "Miguel")
        return real(ruta)

    monkeypatch.setattr(app, "sha256_archivo", al_hashear_el_zip)
    with pytest.raises(app.PaqueteRechazado) as x:
        app.crear_paquete([pac])
    assert x.value.cuerpo["revisiones_que_cambiaron"] == {pac: "su registro de revisión cambió mientras se empaquetaba"}
    assert zips(app) == antes


def test_el_empaquetado_de_enviar_usa_el_mismo_candado_que_las_retiradas(app, tmp_path):
    pac = paciente_revisado(app, tmp_path)
    assert app.CANDADO.acquire(blocking=False)                        # una retirada, una corrida o una revisión en curso
    try:
        with pytest.raises(HTTPException) as x:
            app.empaquetar_para_enviar([pac])
        assert x.value.status_code == 409 and "otro trabajo en curso" in x.value.detail
    finally:
        app.CANDADO.release()
    r = app.empaquetar_para_enviar([pac])
    assert r["revision_pixeles_pendiente"] == [] and not app.CANDADO.locked()
    assert manifiesto(r["ruta"])["revision_pixeles"][pac]["registro_sha256"] == app.leer_revision(pac)["registro_sha256"]


# ---------------------------------------------------------------------------------------------------------------
# 2 y 3. el registro de demografía
# ---------------------------------------------------------------------------------------------------------------

F = {"e": ["c", 54, "años", False], "i": None, "s": "F"}
M = {"e": ["c", 54, "años", False], "i": None, "s": "M"}


def test_una_copia_en_cuarentena_no_pisa_lo_que_se_sabe_del_archivo_limpio(app, tmp_path):
    """Primero se importa el estudio (sexo F). Después llega una carpeta que solo trae copias de las mismas instancias con
    sexo M: van a cuarentena, porque el archivo limpio guardado dice F. El estudio no puede pasar a mostrar M."""
    a = caso(tmp_path, "A", sexo="F")
    t = correr(app, a, tmp_path, "uno")
    pac = t["pacientes"][0]
    copias = tmp_path / "estudios" / "copias"
    copias.mkdir()
    for i, f in enumerate(sorted(a.rglob("IM*"))):
        ds = pydicom.dcmread(f)
        ds.PatientSex = "M"
        ds.save_as(copias / f"C{i}", enforce_file_format=True)
    t2 = correr(app, copias, tmp_path, "dos")
    assert "8 en cuarentena" in t2["pasos"][0]["resumen"] and t2["zip"] is None      # ninguna copia entra
    limpio = next((app.TRABAJO / "limpios" / pac).rglob("*.dcm"))
    assert str(pydicom.dcmread(limpio, stop_before_pixels=True).PatientSex) == "F"   # lo guardado sigue diciendo F
    app.indexar()
    d = next(iter(app.INDICE.values()))["demografia"]
    assert d["sexo"]["estado"] == "conflicto" and d["sexo"]["valores"] == ["F", "M"] and d["sexo"]["valor"] is None
    assert d["etiqueta"].startswith(f"{pac} · sexo en conflicto ·")
    m = manifiesto(app.crear_paquete([pac])["ruta"])
    assert m["demografia"]["estudios"][0]["sexo"]["estado"] == "conflicto"            # y el manifiesto tampoco dice M


def test_lo_mismo_cuando_el_estudio_se_importo_antes_de_que_existiera_el_registro(app, tmp_path):
    a = caso(tmp_path, "A", sexo="F")
    t = correr(app, a, tmp_path, "uno")
    (app.TRABAJO / "limpios_PRIVADO" / "demografia.json").unlink()                    # como un estudio importado con una versión anterior
    copias = tmp_path / "estudios" / "copias"
    copias.mkdir()
    for i, f in enumerate(sorted(a.rglob("IM*"))):
        ds = pydicom.dcmread(f)
        ds.PatientSex = "M"
        ds.save_as(copias / f"C{i}", enforce_file_format=True)
    correr(app, copias, tmp_path, "dos")
    app.indexar()
    d = next(iter(app.INDICE.values()))["demografia"]
    assert d["sexo"]["estado"] == "conflicto" and d["sexo"]["valores"] == ["F", "M"]  # parte de lo que dice el archivo limpio


def test_el_registro_de_demografia_sustituye_lo_aceptado_y_suma_lo_que_esta_en_conflicto(tmp_path):
    ruta = tmp_path / "demografia.json"
    assert anon.guardar_registro_demografia(ruta, {"a.dcm": [F], "b.dcm": [F]}, {}, tmp_path) is None
    assert anon.guardar_registro_demografia(ruta, {"a.dcm": [M]}, {"b.dcm": [M]}, tmp_path) is None
    archivos = json.loads(ruta.read_text(encoding="utf-8"))["archivos"]
    assert archivos["a.dcm"] == [M]                                   # reimportado y aceptado: lo nuevo sustituye
    assert archivos["b.dcm"] == [F, M]                                # en cuarentena: se suma, no sustituye
    anon.guardar_registro_demografia(ruta, {}, {"b.dcm": [M]}, tmp_path)
    assert json.loads(ruta.read_text(encoding="utf-8"))["archivos"]["b.dcm"] == [F, M]      # repetirlo no duplica


def test_el_registro_de_demografia_se_escribe_entero_o_no_se_toca(tmp_path, monkeypatch):
    ruta = tmp_path / "demografia.json"
    anon.guardar_registro_demografia(ruta, {"a.dcm": [F]}, {}, tmp_path)
    original = ruta.read_bytes()
    real = anon.os.replace

    def se_corta(a, b):
        raise OSError("disco lleno")

    monkeypatch.setattr(anon.os, "replace", se_corta)
    with pytest.raises(OSError):
        anon.guardar_registro_demografia(ruta, {"b.dcm": [M]}, {}, tmp_path)
    monkeypatch.setattr(anon.os, "replace", real)
    assert ruta.read_bytes() == original                              # el registro anterior, entero
    assert sorted(p.name for p in tmp_path.iterdir()) == ["demografia.json"]          # sin temporales


def test_un_registro_de_demografia_ilegible_se_aparta_y_se_avisa(tmp_path):
    ruta = tmp_path / "demografia.json"
    ruta.write_text('{"version": 1, "archivos": {"a.dcm": [{"e": ["c", 5', encoding="utf-8")     # truncado
    roto = ruta.read_bytes()
    aviso = anon.guardar_registro_demografia(ruta, {"b.dcm": [M]}, {}, tmp_path)
    apartados = [p for p in tmp_path.iterdir() if p.name.startswith("demografia.json.ilegible_")]
    assert len(apartados) == 1 and apartados[0].read_bytes() == roto and aviso and apartados[0].name in aviso
    assert json.loads(ruta.read_text(encoding="utf-8"))["archivos"] == {"b.dcm": [M]}


def test_el_aviso_de_un_registro_ilegible_llega_a_la_pantalla_y_al_reporte(app, tmp_path):
    a = caso(tmp_path, "A")
    correr(app, a, tmp_path, "uno")
    registro = app.TRABAJO / "limpios_PRIVADO" / "demografia.json"
    roto = registro.read_bytes()[:40]
    registro.write_bytes(roto)                                        # truncado por una escritura interrumpida de otra época
    t = correr(app, a, tmp_path, "dos")                               # se vuelve a importar la misma carpeta
    salida = t["pasos"][0]["salida"]
    assert "AVISO: el registro de demografía no se pudo leer" in salida and "demografia.json.ilegible_" in salida
    apartados = list(registro.parent.glob("demografia.json.ilegible_*"))
    assert len(apartados) == 1 and apartados[0].read_bytes() == roto  # se conserva tal cual
    assert "el registro de demografía no se pudo leer" in (app.TRABAJO / "limpios_REPORTES" / "ultimo_reporte.md").read_text(encoding="utf-8")
    assert t["demografia"][0]["edad_en_el_estudio"]["estado"] == "calculada"            # y el registro nuevo ya sirve


# ---------------------------------------------------------------------------------------------------------------
# 4. lo mostrado se liga al contenido
# ---------------------------------------------------------------------------------------------------------------

def test_un_archivo_sustituido_con_el_mismo_tamano_y_la_misma_fecha_no_cuenta_como_mostrado(app, tmp_path):
    t = correr(app, caso(tmp_path), tmp_path, "c")
    pac = t["pacientes"][0]
    mostrar_en_estudios(app, pac)
    assert app.progreso_paciente(pac, app.hashes_paciente(pac))["completo"] is True
    victima = Path(next(iter(app.INDICE.values()))["cortes"][0][1])
    st = os.stat(victima)
    datos = bytearray(victima.read_bytes())
    datos[-1] ^= 0xFF                                                 # otros píxeles, mismo tamaño
    victima.write_bytes(bytes(datos))
    os.utime(victima, ns=(st.st_atime_ns, st.st_mtime_ns))            # y la misma fecha de modificación
    assert (os.stat(victima).st_size, os.stat(victima).st_mtime_ns) == (st.st_size, st.st_mtime_ns)
    assert app.progreso_paciente(pac)["completo"] is True             # el contador de la pantalla no lo nota...
    avance = app.progreso_paciente(pac, app.hashes_paciente(pac))     # ...la comprobación que autoriza, sí
    assert avance["completo"] is False and avance["mostrados"] == 7 and avance["verificado_por_contenido"] is True


# ---------------------------------------------------------------------------------------------------------------
# 5. paquetes afectados por el registro exacto
# ---------------------------------------------------------------------------------------------------------------

def test_dos_revisiones_en_el_mismo_segundo_no_se_confunden(app, tmp_path, monkeypatch):
    t = correr(app, caso(tmp_path), tmp_path, "c")
    pac = t["pacientes"][0]

    class Reloj(app.dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 12, 10, 0, 0)

    monkeypatch.setattr(app.dt, "datetime", Reloj)                    # todo ocurre en el mismo segundo
    primera = revisar_en_estudios(app, pac, "Ana")
    paquete_1 = app.crear_paquete([pac])
    segunda = revisar_en_estudios(app, pac, "Miguel")
    paquete_2 = app.crear_paquete([pac])
    assert primera["fecha"] == segunda["fecha"] and primera["registro_sha256"] != segunda["registro_sha256"]
    assert manifiesto(paquete_1["ruta"])["revision_pixeles"][pac]["registro_sha256"] == primera["registro_sha256"]
    r = app.retirar_revision("paciente", pac, "Arturo", "se retira la segunda")
    afectados = [e["zip"] for e in r["paquetes_afectados"]]
    assert afectados == [Path(paquete_2["ruta"]).name]                # solo el paquete hecho con la revisión retirada


def test_un_manifiesto_anterior_sin_identificador_se_sigue_reconociendo_por_la_fecha(app, tmp_path):
    pac = paciente_revisado(app, tmp_path)
    paquete = app.crear_paquete([pac])
    local = app.TRABAJO / "paquetes" / (Path(paquete["ruta"]).name[:-4] + ".manifiesto.json")
    m = json.loads(local.read_text(encoding="utf-8"))
    del m["revision_pixeles"][pac]["registro_sha256"]                 # como lo escribía una versión anterior
    local.write_text(json.dumps(m), encoding="utf-8")
    r = app.retirar_revision("paciente", pac, "Arturo", "manifiesto antiguo")
    assert [e["zip"] for e in r["paquetes_afectados"]] == [Path(paquete["ruta"]).name]
