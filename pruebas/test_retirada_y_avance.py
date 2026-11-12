"""
Retirar una revisión registrada, y el avance de una revisión contado por cuadros mostrados en el navegador.
Withdrawing a recorded review, and review progress counted by frames displayed in the browser.

Retirar no borra: el registro original se conserva, una retirada con quién, cuándo y por qué lo sustituye, el estado de
la revisión deja de valer en la aplicación y se enumeran los paquetes revisados que dependían de ella. Los zips ya
exportados no cambian.

El avance cuenta cuadros que el navegador dice haber tenido en pantalla, después de pedirlos al visor. Mostrado no es
revisado: la revisión sigue siendo la declaración de quien firma, y solo se admite cuando todo se ha mostrado.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pydicom
import pytest
from fastapi import HTTPException

RAIZ = Path(__file__).resolve().parents[1]
ENHANCED_CT = "1.2.840.10008.5.1.4.1.1.2.1"


def cargar(nombre: str, ruta: Path):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


demo = cargar("demo", RAIZ / "herramientas" / "demo.py")


@pytest.fixture
def app(tmp_path):
    a = cargar("app_en_prueba", RAIZ / "herramientas" / "app.py")
    a.TRABAJO = tmp_path / "datos-pet"
    a.TRABAJO.mkdir()
    return a


def caso(tmp_path: Path, nombre: str = "A", semilla: int = 1, dos_cuadros: bool = False) -> Path:
    d = tmp_path / "estudios" / nombre
    demo.escribir_caso(d, 4, semilla)
    if dos_cuadros:
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


def correr(app, origen: Path, destino: Path, tid: str = "corrida") -> dict:
    """Lo mismo que hace POST /api/rapido, sin servidor. No simula el navegador: nada queda mostrado."""
    assert app.CANDADO.acquire(blocking=False)
    app.TRABAJOS[tid] = {"nombre": "rapido", "estado": "corriendo", "lineas": [], "codigo": None, "pasos": [], "pacientes": [],
                         "origen": str(origen), "destino": str(app.validar_destino(destino, origen)), "zip": None, "mensaje": "",
                         "archivos": {}, "revisado": False, "servidos": set(), "mostrados": set()}
    app._rapido(tid, origen, destino, "ANON")
    return app.TRABAJOS[tid]


def cuadros(t: dict) -> list[tuple[int, int, int]]:
    return [(i, k, c) for i, s in enumerate(t["series_corrida"]) for k, n in enumerate(s["cuadros"], 1) for c in range(1, n + 1)]


def navegador_muestra(app, t: dict, cuales: list[tuple[int, int, int]]) -> dict:
    """Lo que hace el navegador: pide el cuadro al visor (queda servido) y, cuando lo ha tenido en pantalla, avisa."""
    avance = app.progreso_corrida(t)
    for indice, k, cuadro in cuales:
        assert app.png_de_corrida(t, indice, k, "auto", cuadro)[:4] == b"\x89PNG"
        t["servidos"].add((indice, k, cuadro))                       # lo que añade el punto de acceso HTTP al servir
        avance = app.anotar_mostrado_corrida(t, indice, k, cuadro)
    return avance


def mostrar_en_estudios(app, pac: str) -> None:
    """Lo que hace el navegador en Estudios: cada corte se sirve (se leen sus bytes) y después se avisa de que se mostró."""
    app.indexar()
    for s in app.INDICE.values():
        if s["paciente"] == pac:
            for _, ruta in s["cortes"]:
                app.anotar_servido_estudios(pac, ruta, 1, hashlib.sha256(Path(ruta).read_bytes()).hexdigest())
                assert app.anotar_mostrado_estudios(pac, ruta, 1)


def revision_clasica(app, pac: str, revisor: str = "Prueba") -> dict:
    """Lo mismo que hace la pestaña Estudios: se muestran todos los cortes del paciente y se registra 'sin hallazgos'."""
    mostrar_en_estudios(app, pac)
    actuales = app.hashes_paciente(pac)
    avance = app.progreso_paciente(pac, actuales)
    assert avance["completo"] and avance["verificado_por_contenido"]
    return app.guardar_revision(pac, "sin_hallazgos", [], revisor, archivos=actuales,
                                mostrado={"cuadros_mostrados": avance["mostrados"], "cuadros_totales": avance["total"]})


def sha(ruta) -> str:
    return hashlib.sha256(Path(ruta).read_bytes()).hexdigest()


def manifiesto(ruta: str) -> dict:
    with zipfile.ZipFile(ruta) as z:
        return json.loads(z.read("manifiesto.json"))


# ---------------------------------------------------------------------------------------------------------------
# avance: cuadros mostrados en el navegador
# ---------------------------------------------------------------------------------------------------------------

def test_las_comprobaciones_automaticas_no_cuentan_como_mostrado(app, tmp_path):
    t = correr(app, caso(tmp_path), tmp_path)
    assert t["zip"] and t["no_mostrables"] == []                     # la corrida ya dibujó imágenes por su cuenta...
    avance = app.progreso_corrida(t)
    assert avance["mostrados"] == 0 and avance["total"] == 8 and avance["completo"] is False     # ...y nada cuenta como mostrado
    assert "mostrado no significa revisado" in avance["nota"]


def test_solo_cuenta_lo_que_se_sirvio_al_visor_y_cada_cuadro_una_vez(app, tmp_path):
    t = correr(app, caso(tmp_path), tmp_path)
    with pytest.raises(HTTPException) as e:                           # avisar de un cuadro que el visor no ha servido
        app.anotar_mostrado_corrida(t, 0, 1, 1)
    assert e.value.status_code == 409 and app.progreso_corrida(t)["mostrados"] == 0
    assert app.png_de_corrida(t, 0, 1, "auto")                        # dibujarlo sin pasar por el visor tampoco lo sirve
    with pytest.raises(HTTPException):
        app.anotar_mostrado_corrida(t, 0, 1, 1)
    avance = navegador_muestra(app, t, [(0, 1, 1), (0, 1, 1), (0, 2, 1)])
    assert avance["mostrados"] == 2                                   # el mismo cuadro dos veces cuenta una
    assert [s["mostrados"] for s in avance["series"]] == [2, 0] and [s["total"] for s in avance["series"]] == [4, 4]


def test_sin_haber_mostrado_todo_no_se_puede_declarar_la_revision(app, tmp_path):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso(tmp_path), destino)
    todos = cuadros(t)
    navegador_muestra(app, t, todos[:-1])                             # falta uno
    with pytest.raises(HTTPException) as e:
        app.aplicar_revision_rapido(t, "Prueba")
    assert e.value.status_code == 409 and "se han mostrado 7 de 8 cuadros" in e.value.detail
    assert list(destino.iterdir()) == [] and t["revisado"] is False
    navegador_muestra(app, t, todos[-1:])
    r = app.aplicar_revision_rapido(t, "Prueba")                      # mostrado todo, la declaración se admite
    registro = json.loads(Path(r["registro_revision"]).read_text(encoding="utf-8"))
    assert registro["revisor"] == "Prueba" and registro["mostrado"]["cuadros_mostrados"] == registro["mostrado"]["cuadros_totales"] == 8
    assert "mostrado no significa revisado" in registro["mostrado"]["nota"]
    assert manifiesto(r["zip"])["revision_pixeles"]["mostrado"]["cuadros_totales"] == 8
    assert manifiesto(r["zip"])["revision_pixeles"]["revisor"] == "Prueba"                       # la declaración sigue siendo de quien firma


def test_cada_cuadro_de_un_archivo_con_varios_cuenta(app, tmp_path):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso(tmp_path, dos_cuadros=True), destino)
    indice = next(i for i, s in enumerate(t["series_corrida"]) if s["cuadros"] == [2])
    assert app.progreso_corrida(t)["total"] == 10                     # 4 + 4 archivos de un cuadro y uno de dos
    navegador_muestra(app, t, [c for c in cuadros(t) if c != (indice, 1, 2)])     # el segundo cuadro de ese archivo no se ve
    with pytest.raises(HTTPException) as e:
        app.aplicar_revision_rapido(t, "Prueba")
    assert e.value.status_code == 409 and "9 de 10" in e.value.detail
    navegador_muestra(app, t, [(indice, 1, 2)])
    assert app.aplicar_revision_rapido(t, "Prueba")["revision_pixeles_pendiente"] == []


def test_en_estudios_sin_hallazgos_exige_haber_mostrado_todos_los_cortes_del_paciente(app, tmp_path):
    t = correr(app, caso(tmp_path), tmp_path)
    pac = t["pacientes"][0]
    app.indexar()
    inicial = app.progreso_paciente(pac)
    assert (inicial["mostrados"], inicial["total"], inicial["completo"]) == (0, 8, False)
    rutas = [ruta for s in app.INDICE.values() for _, ruta in s["cortes"]]
    assert app.anotar_mostrado_estudios(pac, rutas[0], 1) is False    # avisar de un corte que no se ha servido no cuenta
    mostrar_en_estudios(app, pac)
    assert app.progreso_paciente(pac)["completo"] is True
    assert app.progreso_paciente(pac, app.hashes_paciente(pac))["completo"] is True
    Path(rutas[0]).write_bytes(Path(rutas[0]).read_bytes() + b"\x00")          # un archivo cambia: lo mostrado ya no cuenta
    assert app.progreso_paciente(pac)["mostrados"] == 7
    assert app.progreso_paciente(pac, app.hashes_paciente(pac))["mostrados"] == 7


# ---------------------------------------------------------------------------------------------------------------
# retirar una revisión
# ---------------------------------------------------------------------------------------------------------------

def test_retirar_la_revision_de_un_paciente_conserva_el_original_y_lo_sustituye(app, tmp_path):
    t = correr(app, caso(tmp_path), tmp_path)
    pac = t["pacientes"][0]
    revision_clasica(app, pac, "Ana")
    ruta = app.TRABAJO / "limpios_REPORTES" / f"revision_pixeles_{pac}.json"
    original, sha_original = ruta.read_bytes(), sha(ruta)
    paquete = app.crear_paquete([pac])                                # un paquete revisado, como el de la pestaña Enviar
    assert paquete["revision_pixeles_pendiente"] == [] and app.leer_revision(pac)["lista_para_compartir"] is True
    sha_zip = sha(paquete["ruta"])

    r = app.retirar_revision("paciente", pac, "Arturo", "se registró como prueba, sin revisar los cortes")
    assert r["retiradas"] == [{"tipo": "paciente", "id": pac}] and r["quien"] == "Arturo" and "prueba" in r["motivo"]
    estado = app.leer_revision(pac)
    assert estado["estado"] == "retirada" and estado["vigente"] is False and estado["lista_para_compartir"] is False
    assert "Arturo" in estado["detalle"] and "sin revisar los cortes" in estado["detalle"]
    # el registro original sigue existiendo, intacto, y la retirada lo señala
    actual = json.loads(ruta.read_text(encoding="utf-8"))
    sustituido = actual["sustituye_a"]
    copia = app.TRABAJO / "limpios_REPORTES" / sustituido["archivo"]
    assert copia.read_bytes() == original and sustituido["sha256"] == sha_original == sha(copia)
    assert sustituido["revisor"] == "Ana" and sustituido["estado"] == "sin_hallazgos" and actual["retirada"]["quien"] == "Arturo"
    # el paquete revisado queda identificado, y el zip no se ha tocado
    afectados = r["paquetes_afectados"]
    assert [e["zip"] for e in afectados] == [Path(paquete["ruta"]).name]
    assert afectados[0]["existe"] is True and afectados[0]["es_el_mismo_archivo"] is True and afectados[0]["ruta"] == paquete["ruta"]
    assert sha(paquete["ruta"]) == sha_zip and "No modifica ni elimina los zips ya exportados" in r["nota"]
    # un paquete nuevo de ese paciente ya sale con la revisión pendiente
    assert app.crear_paquete([pac])["revision_pixeles_pendiente"] == [pac]


def test_retirar_exige_quien_y_por_que_y_algo_que_retirar(app, tmp_path):
    t = correr(app, caso(tmp_path), tmp_path)
    pac = t["pacientes"][0]
    for quien, motivo, codigo in (("", "motivo válido", 400), ("Arturo", "", 400), ("Arturo", "x", 400)):
        with pytest.raises(HTTPException) as e:
            app.retirar_revision("paciente", pac, quien, motivo)
        assert e.value.status_code == codigo
    with pytest.raises(HTTPException) as e:                           # no hay revisión registrada
        app.retirar_revision("paciente", pac, "Arturo", "no debería haber nada")
    assert e.value.status_code == 409
    revision_clasica(app, pac)
    app.retirar_revision("paciente", pac, "Arturo", "primera retirada")
    with pytest.raises(HTTPException) as e:                           # ya retirada
        app.retirar_revision("paciente", pac, "Arturo", "segunda retirada")
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:
        app.retirar_revision("otra-cosa", pac, "Arturo", "tipo desconocido")
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        app.retirar_revision("corrida", "../../mapa_seudonimos.json", "Arturo", "ruta no válida")
    assert e.value.status_code == 400
    assert not app.CANDADO.locked()


def test_retirar_la_revision_de_una_corrida_no_toca_su_registro_ni_su_zip(app, tmp_path):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso(tmp_path), destino)
    navegador_muestra(app, t, cuadros(t))
    r = app.aplicar_revision_rapido(t, "Ana")
    registro = Path(r["registro_revision"])
    sha_registro, sha_zip = sha(registro), sha(r["zip"])
    pac = t["pacientes"][0]
    assert app.leer_revision(pac)["lista_para_compartir"] is True     # la corrida cubría al paciente entero

    ret = app.retirar_revision("corrida", registro.name, "Arturo", "falta decidir qué hacer con la cabeza")
    assert {(x["tipo"], x["id"]) for x in ret["retiradas"]} == {("corrida", registro.name), ("paciente", pac)}
    assert sha(registro) == sha_registro                              # el registro de la revisión no se toca
    retirada = json.loads((registro.parent / (registro.name[:-5] + ".retirada.json")).read_text(encoding="utf-8"))
    assert retirada["sha256_registro"] == sha_registro and retirada["retirada"] == {"quien": "Arturo", "fecha": ret["fecha"], "motivo": "falta decidir qué hacer con la cabeza"}
    assert retirada["revisor"] == "Ana"
    afectado = ret["paquetes_afectados"][0]
    assert afectado["zip"] == Path(r["zip"]).name and afectado["ruta"] == r["zip"] and afectado["existe"] is True and afectado["es_el_mismo_archivo"] is True
    assert sha(r["zip"]) == sha_zip and Path(r["zip"]).is_file()      # el zip exportado sigue ahí, sin cambios
    assert manifiesto(r["zip"])["revision_pixeles"]["estado"] == "sin_hallazgos"     # y sigue diciendo lo que decía: por eso se enumera
    assert app.leer_revision(pac)["estado"] == "retirada" and t["revision_retirada"]["quien"] == "Arturo"
    with pytest.raises(HTTPException) as e:
        app.retirar_revision("corrida", registro.name, "Arturo", "otra vez")
    assert e.value.status_code == 409
    lista = app.revisiones_registradas()
    assert lista["corridas"][0]["retirada"]["quien"] == "Arturo" and lista["corridas"][0]["se_puede_retirar"] is False
    assert lista["pacientes"][0]["estado"] == "retirada" and lista["pacientes"][0]["se_puede_retirar"] is False


def test_retirar_la_revision_de_un_paciente_que_vino_de_una_corrida_retira_tambien_la_corrida(app, tmp_path):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso(tmp_path), destino)
    navegador_muestra(app, t, cuadros(t))
    r = app.aplicar_revision_rapido(t, "Ana")
    pac = t["pacientes"][0]
    ret = app.retirar_revision("paciente", pac, "Arturo", "se registró por error")
    assert {(x["tipo"], x["id"]) for x in ret["retiradas"]} == {("paciente", pac), ("corrida", Path(r["registro_revision"]).name)}
    assert Path(r["zip"]).name in [e["zip"] for e in ret["paquetes_afectados"]]


def test_un_zip_que_ya_no_esta_o_que_cambio_se_dice(app, tmp_path):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso(tmp_path), destino)
    navegador_muestra(app, t, cuadros(t))
    r = app.aplicar_revision_rapido(t, "Ana")
    Path(r["zip"]).rename(destino / "movido.zip")                     # alguien lo movió o lo envió y lo borró
    ret = app.retirar_revision("corrida", Path(r["registro_revision"]).name, "Arturo", "retirada con el zip ya fuera de su carpeta")
    afectado = ret["paquetes_afectados"][0]
    assert afectado["existe"] is False and afectado["zip"] == Path(r["zip"]).name
    assert (destino / "movido.zip").is_file()                         # la retirada no busca ni toca nada fuera de su registro


def test_una_revision_nueva_tras_una_retirada_conserva_todo_el_historial(app, tmp_path):
    t = correr(app, caso(tmp_path), tmp_path)
    pac = t["pacientes"][0]
    revision_clasica(app, pac, "Ana")
    app.retirar_revision("paciente", pac, "Arturo", "se repite la revisión")
    revision_clasica(app, pac, "Miguel")
    estado = app.leer_revision(pac)
    assert estado["lista_para_compartir"] is True and estado["revisor"] == "Miguel"
    historial = sorted((app.TRABAJO / "limpios_REPORTES" / "historial_revisiones").glob(f"revision_pixeles_{pac}_*.json"))
    estados = [json.loads(h.read_text(encoding="utf-8"))["estado"] for h in historial]
    assert sorted(estados) == ["retirada", "sin_hallazgos"]           # la primera revisión y su retirada siguen guardadas
    actual = json.loads((app.TRABAJO / "limpios_REPORTES" / f"revision_pixeles_{pac}.json").read_text(encoding="utf-8"))
    assert actual["registro_anterior"]["sha256"] in {sha(h) for h in historial}


def test_retirar_con_otro_trabajo_en_curso_se_rechaza(app, tmp_path):
    t = correr(app, caso(tmp_path), tmp_path)
    pac = t["pacientes"][0]
    revision_clasica(app, pac)
    assert app.CANDADO.acquire(blocking=False)
    try:
        with pytest.raises(HTTPException) as e:
            app.retirar_revision("paciente", pac, "Arturo", "con el candado tomado")
        assert e.value.status_code == 409
    finally:
        app.CANDADO.release()
    assert app.leer_revision(pac)["lista_para_compartir"] is True     # nada cambió


def test_solo_se_senalan_los_paquetes_que_dependen_de_la_revision_retirada(app, tmp_path):
    """El mismo paciente tiene una revisión propia, con su paquete de la pestaña Enviar, y después una corrida revisada de
    otro estudio suyo. Retirar la revisión de la corrida señala el zip de la corrida y ninguno más; la revisión del
    paciente, que es otra, sigue valiendo."""
    destino = tmp_path / "destino"
    destino.mkdir()
    correr(app, caso(tmp_path, "A", 1), tmp_path, "primera")
    pac = "ANON-0001"
    revision_clasica(app, pac, "Ana")
    clasico = app.crear_paquete([pac])                                # paquete revisado con la revisión de Ana
    assert clasico["revision_pixeles_pendiente"] == []
    t2 = correr(app, caso(tmp_path, "B", 2), destino, "segunda")      # otro estudio del mismo paciente
    assert t2["pacientes"] == [pac]
    navegador_muestra(app, t2, cuadros(t2))
    r2 = app.aplicar_revision_rapido(t2, "Miguel")
    assert r2["revision_de_paciente_actualizada"] == []               # la corrida no cubre al paciente entero: no escribe su revisión
    ret = app.retirar_revision("corrida", Path(r2["registro_revision"]).name, "Arturo", "solo la corrida")
    assert [e["zip"] for e in ret["paquetes_afectados"]] == [Path(r2["zip"]).name]
    assert ret["retiradas"] == [{"tipo": "corrida", "id": Path(r2["registro_revision"]).name}]
    assert Path(clasico["ruta"]).name not in [e["zip"] for e in ret["paquetes_afectados"]]
    # la revisión del paciente quedó invalidada por la segunda importación (sus archivos cambiaron), no por la retirada
    assert app.leer_revision(pac)["estado"] != "retirada"
