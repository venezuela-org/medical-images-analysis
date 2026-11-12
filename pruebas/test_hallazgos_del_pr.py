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
import re
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


# ---------------------------------------------------------------------------------------------------------------
# segunda revisión automática: el inventario que se recorrió tiene que ser el de los archivos que se registran
# ---------------------------------------------------------------------------------------------------------------

def test_los_archivos_que_llegaron_despues_del_ultimo_inventario_no_quedan_revisados(app, tmp_path):
    """Se recorre al paciente entero. Después se importa otro estudio suyo y el inventario de la pantalla no se rehace.
    La declaración 'sin hallazgos' no puede cubrir los archivos nuevos, que nadie ha visto."""
    ta = correr(app, caso(tmp_path, "A", 1), tmp_path, "A")
    pac = ta["pacientes"][0]
    mostrar_en_estudios(app, pac)                                     # los 8 cortes, mostrados
    inventario_viejo = dict(app.INDICE)
    correr(app, caso(tmp_path, "B", 2), tmp_path, "B")                # otro estudio del mismo paciente: 8 archivos más en disco
    app.INDICE.clear()
    app.INDICE.update(inventario_viejo)                               # la pantalla sigue con el inventario de antes
    assert len(app.hashes_paciente(pac)) == 16 and sum(s["n"] for s in app.INDICE.values()) == 8
    registro = app.TRABAJO / "limpios_REPORTES" / f"revision_pixeles_{pac}.json"
    with pytest.raises(HTTPException) as x:
        app.registrar_revision_de_paciente(pac, "sin_hallazgos", [], "Ana")
    assert x.value.status_code == 409 and "8 archivo(s) llegaron después" in x.value.detail and "Refrescar" in x.value.detail
    assert not registro.exists() and not app.CANDADO.locked()         # nada registrado
    assert sum(s["n"] for s in app.INDICE.values()) == 16             # y el inventario se rehízo: ahora se ve lo que falta
    with pytest.raises(HTTPException) as x:                           # con el inventario al día, faltan 8 por mostrar
        app.registrar_revision_de_paciente(pac, "sin_hallazgos", [], "Ana")
    assert "se han mostrado 8 de 16 cortes" in x.value.detail and not registro.exists()
    mostrar_en_estudios(app, pac)                                     # recorridos los 16, se registra
    r = app.registrar_revision_de_paciente(pac, "sin_hallazgos", [], "Ana")
    assert r["lista_para_compartir"] is True and r["archivos_revisados"] == 16 and r["mostrado"]["cuadros_mostrados"] == 16
    assert json.loads(registro.read_text(encoding="utf-8"))["archivos"] == app.hashes_paciente(pac)


def test_un_archivo_del_inventario_que_ya_no_esta_tampoco_deja_registrar(app, tmp_path):
    t = correr(app, caso(tmp_path), tmp_path, "c")
    pac = t["pacientes"][0]
    mostrar_en_estudios(app, pac)
    Path(next(iter(app.INDICE.values()))["cortes"][0][1]).unlink()
    with pytest.raises(HTTPException) as x:
        app.registrar_revision_de_paciente(pac, "sin_hallazgos", [], "Ana")
    assert x.value.status_code == 409 and "1 ya no están" in x.value.detail


def test_registrar_una_revision_usa_el_candado_comun(app, tmp_path):
    t = correr(app, caso(tmp_path), tmp_path, "c")
    pac = t["pacientes"][0]
    mostrar_en_estudios(app, pac)
    assert app.CANDADO.acquire(blocking=False)                        # una importación, una corrida, un empaquetado o una retirada en curso
    try:
        for estado, hallazgos in (("sin_hallazgos", []), ("con_hallazgos", ["rostro"])):
            with pytest.raises(HTTPException) as x:
                app.registrar_revision_de_paciente(pac, estado, hallazgos, "Ana")
            assert x.value.status_code == 409 and "otro trabajo en curso" in x.value.detail
    finally:
        app.CANDADO.release()
    assert app.registrar_revision_de_paciente(pac, "sin_hallazgos", [], "Ana")["lista_para_compartir"] is True
    assert not app.CANDADO.locked()


def test_la_revision_de_paciente_que_escribe_una_corrida_se_liga_a_los_bytes_publicados(app, tmp_path, monkeypatch):
    def corrida_mostrada(nombre, semilla, tid):
        t = correr(app, caso(tmp_path, nombre, semilla), tmp_path / f"destino-{tid}", tid)
        for i, s in enumerate(t["series_corrida"]):
            for k, n in enumerate(s["cuadros"], 1):
                t["servidos"].add((i, k, 1))
                app.anotar_mostrado_corrida(t, i, k, 1)
        return t

    (tmp_path / "destino-uno").mkdir()
    t = corrida_mostrada("A", 1, "uno")
    pac = t["pacientes"][0]
    r = app.aplicar_revision_rapido(t, "Ana")
    assert r["revision_de_paciente_actualizada"] == [pac]
    registro = json.loads((app.TRABAJO / "limpios_REPORTES" / f"revision_pixeles_{pac}.json").read_text(encoding="utf-8"))
    assert registro["archivos"] == {rel.split("/", 1)[1]: h for rel, h in t["archivos"].items()}   # los hashes congelados de la corrida

    # otra corrida, de otro paciente; tras publicar, uno de sus archivos ya no es el que se mostró
    (tmp_path / "destino-dos").mkdir()
    origen = caso(tmp_path, "otro", 5)
    for f in origen.rglob("IM*"):
        ds = pydicom.dcmread(f)
        ds.PatientID = "DEMO-000555"
        ds.save_as(f, enforce_file_format=True)
    t2 = correr(app, origen, tmp_path / "destino-dos", "dos")
    for i, s in enumerate(t2["series_corrida"]):
        for k, n in enumerate(s["cuadros"], 1):
            t2["servidos"].add((i, k, 1))
            app.anotar_mostrado_corrida(t2, i, k, 1)
    otro = t2["pacientes"][0]
    real = app.hashes_paciente

    def con_un_archivo_distinto(p):
        h = real(p)
        if p == otro:
            primero = sorted(h)[0]
            h[primero] = "0" * 64                                     # mismo conjunto de archivos, otros bytes en uno
        return h

    monkeypatch.setattr(app, "hashes_paciente", con_un_archivo_distinto)
    r2 = app.aplicar_revision_rapido(t2, "Ana")
    assert r2["revision_de_paciente_actualizada"] == []               # la corrida vale; la revisión del paciente entero, no
    assert not (app.TRABAJO / "limpios_REPORTES" / f"revision_pixeles_{otro}.json").exists()


# ---------------------------------------------------------------------------------------------------------------
# tercera revisión automática: evidencias que se quedan con nombre provisional después de publicar
# ---------------------------------------------------------------------------------------------------------------

def corrida_mostrada(app, tmp_path, tid: str = "c", semilla: int = 1) -> dict:
    destino = tmp_path / f"destino-{tid}"
    destino.mkdir()
    t = correr(app, caso(tmp_path, f"caso-{tid}", semilla), destino, tid)
    for i, s in enumerate(t["series_corrida"]):
        for k, n in enumerate(s["cuadros"], 1):
            t["servidos"].add((i, k, 1))
            app.anotar_mostrado_corrida(t, i, k, 1)
    return t


def sin_poder_renombrar(app, monkeypatch, cumple):
    """Hace fallar el renombrado de las evidencias cuyo nombre final cumple la condición. Devuelve cómo deshacerlo."""
    real = app.os.replace

    def falso(a, b):
        if cumple(Path(b).name):
            raise OSError("sin permiso")
        return real(a, b)

    monkeypatch.setattr(app.os, "replace", falso)
    return lambda: monkeypatch.setattr(app.os, "replace", real)


es_registro_confirmado = lambda n: re.fullmatch(r"revision_rapido_\d{8}_\d{6}_[0-9a-f]{4}\.json", n) is not None   # solo el registro, no su anotación de retirada
es_manifiesto_confirmado = lambda n: n.startswith("paquete_") and n.endswith(".manifiesto.json")


@pytest.mark.parametrize("direccion", ["corrida", "paciente"])
def test_un_registro_de_corrida_que_queda_provisional_tras_publicar_se_ve_y_se_puede_retirar(app, tmp_path, monkeypatch, direccion):
    t = corrida_mostrada(app, tmp_path)
    pac = t["pacientes"][0]
    deshacer = sin_poder_renombrar(app, monkeypatch, es_registro_confirmado)
    r = app.aplicar_revision_rapido(t, "Ana")                         # el zip se publica; el renombrado del registro falla
    provisional = Path(r["registro_revision"])
    canonico = provisional.name[:-len(app.SUFIJO_PREPARADO)]
    assert provisional.name.endswith(app.SUFIJO_PREPARADO) and Path(r["zip"]).is_file()
    assert r["registro_confirmado"] is False and any("nombre provisional" in a for a in r["avisos_posteriores"])
    sha_registro, sha_zip = hashlib.sha256(provisional.read_bytes()).hexdigest(), hashlib.sha256(Path(r["zip"]).read_bytes()).hexdigest()
    # la revisión de paciente que escribió cita la corrida por su nombre canónico
    origen = json.loads((app.TRABAJO / "limpios_REPORTES" / f"revision_pixeles_{pac}.json").read_text(encoding="utf-8"))["origen"]
    assert origen == {"corrida": canonico}
    # el renombrado sigue sin poder hacerse: la revisión se ve en Registro y se puede retirar
    fila = app.revisiones_registradas()["corridas"]
    assert len(fila) == 1 and fila[0]["registro"] == canonico and fila[0]["provisional"] is True
    assert fila[0]["se_puede_retirar"] is True and fila[0]["revisor"] == "Ana" and fila[0]["zip"]["existe"] is True
    ret = app.retirar_revision("corrida", canonico, "Arturo", "publicada con registro provisional") if direccion == "corrida" \
        else app.retirar_revision("paciente", pac, "Arturo", "publicada con registro provisional")
    assert ret["estado"] == "completa" and {(x["tipo"], x["id"]) for x in ret["retiradas"]} == {("corrida", canonico), ("paciente", pac)}
    assert [e["ruta"] for e in ret["paquetes_afectados"]] == [r["zip"]] and ret["paquetes_afectados"][0]["es_el_mismo_archivo"] is True
    assert app.leer_revision(pac)["estado"] == "retirada"
    assert (app.TRABAJO / "limpios_REPORTES" / (canonico[:-5] + ".retirada.json")).is_file()
    # nada original cambia
    assert hashlib.sha256(provisional.read_bytes()).hexdigest() == sha_registro and hashlib.sha256(Path(r["zip"]).read_bytes()).hexdigest() == sha_zip
    # cuando el renombrado ya se puede hacer, se termina; y la retirada sigue alcanzando a ese registro
    deshacer()
    assert app.confirmar_evidencias_publicadas() == [{"evidencia": canonico, "estado": "confirmada"}]
    confirmado = app.TRABAJO / "limpios_REPORTES" / canonico
    assert confirmado.is_file() and not provisional.exists() and hashlib.sha256(confirmado.read_bytes()).hexdigest() == sha_registro
    fila = app.revisiones_registradas()["corridas"][0]
    assert fila["provisional"] is False and fila["retirada"]["quien"] == "Arturo" and fila["se_puede_retirar"] is False
    with pytest.raises(HTTPException) as x:
        app.retirar_revision("corrida", canonico, "Arturo", "otra vez")
    assert x.value.status_code == 409


def test_tambien_se_puede_retirar_dando_el_nombre_provisional(app, tmp_path, monkeypatch):
    t = corrida_mostrada(app, tmp_path)
    sin_poder_renombrar(app, monkeypatch, es_registro_confirmado)
    r = app.aplicar_revision_rapido(t, "Ana")
    nombre = Path(r["registro_revision"]).name                        # con el sufijo provisional
    ret = app.retirar_revision("corrida", nombre, "Arturo", "por el nombre que quedó en disco")
    assert ret["estado"] == "completa" and ("corrida", nombre[:-len(app.SUFIJO_PREPARADO)]) in {(x["tipo"], x["id"]) for x in ret["retiradas"]}


def test_un_paquete_cuyo_manifiesto_quedo_provisional_tambien_se_senala_al_retirar(app, tmp_path, monkeypatch):
    pac = paciente_revisado(app, tmp_path)
    deshacer = sin_poder_renombrar(app, monkeypatch, es_manifiesto_confirmado)
    paquete = app.crear_paquete([pac])                                # se publica; el renombrado del manifiesto local falla
    nombre = Path(paquete["ruta"]).name
    carpeta = app.TRABAJO / "paquetes"
    assert paquete["revision_pixeles_pendiente"] == [] and paquete["avisos"]
    assert (carpeta / (nombre[:-4] + ".manifiesto.json" + app.SUFIJO_PREPARADO)).is_file() and not (carpeta / (nombre[:-4] + ".manifiesto.json")).exists()
    ret = app.retirar_revision("paciente", pac, "Arturo", "el paquete tiene el manifiesto con nombre provisional")
    assert [e["zip"] for e in ret["paquetes_afectados"]] == [nombre]  # el paquete publicado no se pierde de vista
    assert ret["paquetes_afectados"][0]["existe"] is True and ret["paquetes_afectados"][0]["es_el_mismo_archivo"] is True
    deshacer()
    assert app.confirmar_evidencias_publicadas() == [{"evidencia": nombre[:-4] + ".manifiesto.json", "estado": "confirmada"}]
    assert (carpeta / (nombre[:-4] + ".manifiesto.json")).is_file()


def test_una_evidencia_provisional_sin_zip_publicado_no_se_confirma_sola(app, tmp_path):
    """Resto de una publicación que no ocurrió y cuyo registro preparado no se pudo borrar: no se convierte en confirmado.
    Se ve en Registro como provisional, con su zip ausente, y se puede retirar para dejar constancia."""
    t = corrida_mostrada(app, tmp_path)
    reportes = app.TRABAJO / "limpios_REPORTES"
    reportes.mkdir(parents=True, exist_ok=True)
    resto = reportes / ("revision_rapido_20261112_101010_abcd.json" + app.SUFIJO_PREPARADO)
    resto.write_text(json.dumps({"estado": "sin_hallazgos", "revisor": "Ana", "fecha": "2026-11-12T10:10:10", "pacientes": t["pacientes"],
                                 "archivos": dict(t["archivos"]), "zip": {"nombre": "paquete_que_no_existe.zip", "destino": str(tmp_path), "sha256": "0" * 64}}),
                     encoding="utf-8")
    assert app.confirmar_evidencias_publicadas() == [] and resto.is_file()
    fila = app.revisiones_registradas()["corridas"][0]
    assert fila["provisional"] is True and fila["zip"]["existe"] is False and fila["se_puede_retirar"] is True
    ret = app.retirar_revision("corrida", "revision_rapido_20261112_101010_abcd.json", "Arturo", "resto de una publicación fallida")
    assert ret["estado"] == "completa" and ret["paquetes_afectados"][0]["existe"] is False
