"""
Carpetas del paquete y la casilla de conservar descripciones. / Package folders and the keep-descriptions option.

Dos cosas que pidió y preguntó quien probó la aplicación con un estudio de resonancia:

1. Que las carpetas del zip se puedan leer: el paciente con su sexo, el estudio con un número y la edad en ese estudio.
   ANON-0002/estudio_7b7191db84  ->  ANON-0002_F/estudio_01_37a
   Son nombres de presentación del paquete. Los archivos limpios guardados y los DICOM no cambian.
2. Qué hace la casilla "conservar descripciones" y por qué, al volver a limpiar con ella un estudio ya guardado, la
   limpieza terminaba con todo en cuarentena.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pydicom
import pytest

RAIZ = Path(__file__).resolve().parents[1]


def cargar(nombre: str, ruta: Path):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


demo = cargar("demo", RAIZ / "herramientas" / "demo.py")
anon = cargar("anonimizar_nd", RAIZ / "herramientas" / "anonimizar.py")


@pytest.fixture
def app(tmp_path):
    a = cargar("app_en_prueba", RAIZ / "herramientas" / "app.py")
    a.TRABAJO = tmp_path / "datos-pet"
    a.TRABAJO.mkdir()
    return a


def caso(tmp_path: Path, nombre: str, semilla: int = 1, sexo: str = "F", nacimiento: str = "19870110", estudio: str | None = None, editar=None) -> Path:
    """Caso sintético de 3 + 3 cortes. Con la fecha de estudio de la demostración (5 de marzo de 2024), 37 años."""
    d = tmp_path / "estudios" / nombre
    demo.escribir_caso(d, 3, semilla)
    for f in d.rglob("IM*"):
        ds = pydicom.dcmread(f)
        ds.PatientSex, ds.PatientBirthDate = sexo, nacimiento
        if estudio:
            ds.StudyDate = ds.SeriesDate = ds.AcquisitionDate = ds.ContentDate = estudio
        if editar:
            editar(ds)
        ds.save_as(f, enforce_file_format=True)
    return d


def correr(app, origen: Path, destino: Path, tid: str) -> dict:
    destino.mkdir(parents=True, exist_ok=True)
    assert app.CANDADO.acquire(blocking=False)
    app.TRABAJOS[tid] = {"nombre": "rapido", "estado": "corriendo", "lineas": [], "codigo": None, "pasos": [], "pacientes": [],
                         "origen": str(origen), "destino": str(destino), "zip": None, "mensaje": "", "archivos": {},
                         "revisado": False, "servidos": set(), "mostrados": set()}
    app._rapido(tid, origen, destino, "ANON")
    return app.TRABAJOS[tid]


def entradas(ruta) -> list[str]:
    with zipfile.ZipFile(ruta) as z:
        return sorted(n for n in z.namelist() if n != "manifiesto.json")


def manifiesto(ruta) -> dict:
    with zipfile.ZipFile(ruta) as z:
        return json.loads(z.read("manifiesto.json"))


def carpetas(ruta) -> set[str]:
    return {"/".join(n.split("/")[:2]) for n in entradas(ruta)}


def limpiar(origen: Path, trabajo: Path, *extra: str) -> tuple[int, dict]:
    """La limpieza tal como la lanza la pestaña Entrada. Devuelve el código de salida y el reporte."""
    r = subprocess.run([sys.executable, str(RAIZ / "herramientas" / "anonimizar.py"), "limpiar", str(origen), str(trabajo / "limpios"),
                        "--mapa", str(trabajo / "limpios_PRIVADO" / "mapa_seudonimos.json"), *extra], capture_output=True, text=True)
    return r.returncode, json.loads((trabajo / "limpios_REPORTES" / "ultimo_reporte.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------------------------
# 1. las carpetas del paquete
# ---------------------------------------------------------------------------------------------------------------

def test_las_carpetas_del_paquete_llevan_el_sexo_el_numero_del_estudio_y_la_edad(app, tmp_path):
    t = correr(app, caso(tmp_path, "A"), tmp_path / "destino", "a")
    pac = t["pacientes"][0]
    assert carpetas(t["zip"]) == {f"{pac}_F/estudio_01_37a"}
    m = manifiesto(t["zip"])
    interno = next(iter(m["carpetas"]["estudios"]))
    assert m["carpetas"]["pacientes"] == {pac: f"{pac}_F"} and m["carpetas"]["estudios"] == {interno: f"{pac}_F/estudio_01_37a"}
    estudio = m["demografia"]["estudios"][0]
    assert (estudio["carpeta"], estudio["numero"], estudio["etiqueta"]) == (f"{pac}_F/estudio_01_37a", 1, f"{pac} · F · 37 años en el estudio")
    assert all(serie.startswith(f"{pac}_F/estudio_01_37a/serie_") for serie in m["orden_de_cortes"])
    assert {a["ruta"] for a in m["archivos"]} == set(entradas(t["zip"]))
    # son nombres de presentación: lo guardado conserva sus carpetas y sus bytes, y el zip lleva esos mismos bytes
    guardados = {rel: hashlib.sha256((app.TRABAJO / "limpios" / rel).read_bytes()).hexdigest() for rel in t["archivos"]}
    assert guardados == t["archivos"] and all(rel.startswith(interno + "/") for rel in guardados)
    with zipfile.ZipFile(t["zip"]) as z:
        assert sorted(hashlib.sha256(z.read(n)).hexdigest() for n in entradas(t["zip"])) == sorted(guardados.values())
        ds = pydicom.dcmread(z.open(entradas(t["zip"])[0]), stop_before_pixels=True)
    assert str(ds.PatientID) == pac and str(ds.PatientSex) == "F"       # el DICOM no cambia por llamarse distinto su carpeta


def test_el_mismo_paciente_con_estudios_a_edades_distintas_lleva_la_edad_en_cada_estudio(app, tmp_path):
    """La edad es del estudio. El paciente tiene 37 años en el primero y 39 en el segundo: una carpeta de paciente y dos de estudio."""
    correr(app, caso(tmp_path, "primero", 1), tmp_path / "destino", "a")
    t2 = correr(app, caso(tmp_path, "segundo", 2, estudio="20260601"), tmp_path / "destino", "b")
    pac = t2["pacientes"][0]
    assert carpetas(t2["zip"]) == {f"{pac}_F/estudio_02_39a"}           # la corrida lleva solo su estudio, con su número
    todo = app.crear_paquete([pac])                                      # el paquete de Enviar lleva los dos
    assert carpetas(todo["ruta"]) == {f"{pac}_F/estudio_01_37a", f"{pac}_F/estudio_02_39a"}
    numeros = {e["carpeta"]: e["numero"] for e in manifiesto(todo["ruta"])["demografia"]["estudios"]}
    assert numeros == {f"{pac}_F/estudio_01_37a": 1, f"{pac}_F/estudio_02_39a": 2}


def test_el_numero_de_un_estudio_no_cambia_de_un_paquete_a_otro(app, tmp_path):
    primera = caso(tmp_path, "A", 1)
    ta = correr(app, primera, tmp_path / "destino", "a")
    pac = ta["pacientes"][0]
    antes = carpetas(app.crear_paquete([pac])["ruta"])
    correr(app, caso(tmp_path, "B", 2, estudio="20260601"), tmp_path / "destino", "b")
    correr(app, primera, tmp_path / "destino", "c")                      # se vuelve a pasar la primera carpeta
    despues = carpetas(app.crear_paquete([pac])["ruta"])
    assert antes == {f"{pac}_F/estudio_01_37a"} and antes < despues and f"{pac}_F/estudio_02_39a" in despues
    registro = json.loads((app.TRABAJO / "limpios_PRIVADO" / "estudios.json").read_text(encoding="utf-8"))
    assert sorted(registro["pacientes"][pac].values()) == [1, 2]


def test_los_nombres_de_una_corrida_quedan_fijados_con_ella(app, tmp_path):
    ta = correr(app, caso(tmp_path, "A", 1), tmp_path / "destino", "a")
    pendiente = entradas(ta["zip"])
    correr(app, caso(tmp_path, "B", 2, estudio="20260601"), tmp_path / "destino", "b")
    for i, s in enumerate(ta["series_corrida"]):
        for k, n in enumerate(s["cuadros"], 1):
            ta["servidos"].add((i, k, 1))
            app.anotar_mostrado_corrida(ta, i, k, 1)
    r = app.aplicar_revision_rapido(ta, "Ana")
    assert entradas(r["zip"]) == pendiente                              # el zip revisado lleva los mismos nombres que el pendiente


@pytest.mark.parametrize("sexo, esperado", [("", "{pac}"), ("O", "{pac}_O"), ("M", "{pac}_M")])
def test_la_carpeta_del_paciente_lleva_el_sexo_solo_si_esta_registrado(app, tmp_path, sexo, esperado):
    t = correr(app, caso(tmp_path, "A", sexo=sexo), tmp_path / "destino", "a")
    pac = t["pacientes"][0]
    assert carpetas(t["zip"]) == {esperado.format(pac=pac) + "/estudio_01_37a"}


def test_con_el_sexo_en_conflicto_la_carpeta_del_paciente_no_elige(app, tmp_path):
    vistos = {"n": 0}

    def alternar(ds):
        vistos["n"] += 1
        ds.PatientSex = "F" if vistos["n"] % 2 else "M"

    t = correr(app, caso(tmp_path, "A", editar=alternar), tmp_path / "destino", "a")
    pac = t["pacientes"][0]
    assert carpetas(t["zip"]) == {f"{pac}/estudio_01_37a"}
    assert manifiesto(t["zip"])["demografia"]["estudios"][0]["sexo"]["estado"] == "conflicto"


def test_sin_edad_la_carpeta_del_estudio_lleva_solo_su_numero(app, tmp_path):
    def sin_fechas(ds):
        ds.PatientBirthDate = ""
        if "PatientAge" in ds:
            del ds.PatientAge

    t = correr(app, caso(tmp_path, "A", editar=sin_fechas), tmp_path / "destino", "a")
    assert carpetas(t["zip"]) == {f"{t['pacientes'][0]}_F/estudio_01"}


def test_la_edad_en_la_carpeta_dice_la_unidad_y_si_es_informada_o_agrupada(app):
    corta = app.edad_para_carpeta
    assert corta({"estado": "calculada", "valor": 37, "unidad": "años", "agrupada": False}) == "37a"
    assert corta({"estado": "calculada", "valor": 8, "unidad": "meses", "agrupada": False}) == "8m"
    assert corta({"estado": "calculada", "valor": 3, "unidad": "semanas", "agrupada": False}) == "3sem"
    assert corta({"estado": "calculada", "valor": 12, "unidad": "días", "agrupada": False}) == "12d"
    assert corta({"estado": "calculada", "valor": 90, "unidad": "años", "agrupada": True}) == "90a-o-mas"
    assert corta({"estado": "informada", "valor": 54, "unidad": "años", "agrupada": False}) == "54a-informada"
    assert corta({"estado": "no_disponible", "valor": None, "unidad": None, "agrupada": False}) is None
    for nombre in ("estudio_01", "estudio_01_37a", "estudio_12_8m", "estudio_03_3sem", "estudio_02_12d", "estudio_01_90a-o-mas", "estudio_01_54a-informada"):
        assert app.CARPETA_ESTUDIO_RE.match(nombre), nombre             # todo lo que se puede generar, el empaquetador lo reconoce


def test_si_el_registro_de_numeros_no_se_puede_leer_se_usan_los_nombres_internos_y_no_se_pisa(app, tmp_path):
    t = correr(app, caso(tmp_path, "A"), tmp_path / "destino", "a")
    pac = t["pacientes"][0]
    registro = app.TRABAJO / "limpios_PRIVADO" / "estudios.json"
    registro.write_text('{"version": 1, "pacientes": {"' + pac, encoding="utf-8")          # truncado
    roto = registro.read_bytes()
    r = app.crear_paquete([pac])
    interno = next(iter(t["archivos"])).split("/")[1]
    assert carpetas(r["ruta"]) == {f"{pac}_F/{interno}"}                # sin número: el nombre interno del estudio
    assert manifiesto(r["ruta"])["demografia"]["estudios"][0]["numero"] is None
    assert registro.read_bytes() == roto                                # y el registro ilegible no se sobrescribe
    assert app.numeros_de_estudio({pac: {interno}}) == ({}, "el registro de números de estudio (estudios.json) no se puede leer")


def test_el_empaquetador_solo_acepta_las_carpetas_que_reconoce(app):
    ok = app.exportacion_coherente
    i = ["ANON-0001/estudio_aa/serie_x_CT_001/a.dcm", "ANON-0001/estudio_aa/serie_x_CT_001/b.dcm", "ANON-0001/estudio_bb/serie_y_PT_002/c.dcm"]
    assert ok(i, ["ANON-0001_F/estudio_01_37a/serie_x_CT_001/corte_000001_a.dcm", "ANON-0001_F/estudio_01_37a/serie_x_CT_001/corte_000002_b.dcm",
                  "ANON-0001_F/estudio_02_39a/serie_y_PT_002/corte_000001_c.dcm"])
    assert ok(i, [x.rsplit("/", 1)[0] + "/corte_" + x.rsplit("/", 1)[1] for x in i])                       # sin renombrar carpetas también vale
    mal = lambda e: not ok(i, e)
    assert mal(["ANON-0009_F/estudio_01/serie_x_CT_001/a.dcm", "ANON-0009_F/estudio_01/serie_x_CT_001/b.dcm", "ANON-0009_F/estudio_02/serie_y_PT_002/c.dcm"])   # otro paciente
    assert mal(["ANON-0001_F/estudio_01/serie_x_CT_001/a.dcm", "ANON-0001_F/estudio_01/serie_x_CT_001/b.dcm", "ANON-0001_F/estudio_01/serie_y_PT_002/c.dcm"])   # dos estudios en una carpeta
    assert mal(["ANON-0001_F/estudio_01/serie_x_CT_001/a.dcm", "ANON-0001_M/estudio_01/serie_x_CT_001/b.dcm", "ANON-0001_F/estudio_02/serie_y_PT_002/c.dcm"])   # un paciente en dos carpetas
    assert mal(["ANON-0001_F/estudio_01/otra_serie/a.dcm", "ANON-0001_F/estudio_01/otra_serie/b.dcm", "ANON-0001_F/estudio_02/serie_y_PT_002/c.dcm"])            # la serie no se renombra
    assert mal(["ANON-0001_F/Juan Perez/serie_x_CT_001/a.dcm", "ANON-0001_F/Juan Perez/serie_x_CT_001/b.dcm", "ANON-0001_F/estudio_02/serie_y_PT_002/c.dcm"])   # texto libre en una carpeta
    assert mal(["ANON-0001_F/estudio_01/serie_x_CT_001/a.txt", "ANON-0001_F/estudio_01/serie_x_CT_001/b.dcm", "ANON-0001_F/estudio_02/serie_y_PT_002/c.dcm"])


def test_estudios_muestra_el_numero_de_cada_estudio(app, tmp_path):
    correr(app, caso(tmp_path, "A", 1), tmp_path / "destino", "a")
    correr(app, caso(tmp_path, "B", 2, estudio="20260601"), tmp_path / "destino", "b")
    app.indexar()
    por_estudio = {s["estudio"]: s["estudio_numero"] for s in app.INDICE.values()}
    assert sorted(por_estudio.values()) == [1, 2] and len(por_estudio) == 2


# ---------------------------------------------------------------------------------------------------------------
# 2. la casilla de conservar descripciones
# ---------------------------------------------------------------------------------------------------------------

def descripciones(trabajo: Path) -> set[tuple]:
    return {(str(ds.get("StudyDescription", "")), str(ds.get("SeriesDescription", "")), str(ds.get("ProtocolName", "")))
            for ds in (pydicom.dcmread(f, stop_before_pixels=True) for f in (trabajo / "limpios").rglob("*.dcm"))}


def con_protocolo(ds):
    """Un protocolo, y una identidad que no aparece dentro de las descripciones del caso de demostración."""
    ds.ProtocolName = "CEREBRO RUTINA"
    ds.PatientName, ds.PatientID = "PRUEBA^UNO", "HC-778899"


def test_sin_la_casilla_las_descripciones_se_eliminan_y_con_ella_se_conservan(tmp_path):
    origen = caso(tmp_path, "A", editar=con_protocolo)
    codigo, reporte = limpiar(origen, tmp_path / "sin")
    assert codigo == 0 and reporte["conservar_descripciones"] is False and descripciones(tmp_path / "sin") == {("", "", "")}
    codigo, reporte = limpiar(origen, tmp_path / "con", "--conservar-descripciones")
    assert codigo == 0 and reporte["conservar_descripciones"] is True
    assert descripciones(tmp_path / "con") == {("PET CT DEMO SINTETICO", "CT demo", "CEREBRO RUTINA"), ("PET CT DEMO SINTETICO", "PT demo", "CEREBRO RUTINA")}
    for f in (tmp_path / "con" / "limpios").rglob("*.dcm"):             # la casilla no conserva nada más del paciente
        ds = pydicom.dcmread(f, stop_before_pixels=True)
        assert "PRUEBA" not in str(ds.get("PatientName", "")) and str(ds.PatientID).startswith("ANON-") and str(ds.get("PatientBirthDate", "")) == ""


def test_el_nombre_o_el_id_del_paciente_dentro_de_una_descripcion_se_redactan(tmp_path):
    """El caso de demostración ya trae el ejemplo: su paciente se llama DEMO^PACIENTE^SINTETICO y su estudio se describe
    como 'PET CT DEMO SINTETICO'. Con la casilla, esos trozos del nombre se redactan; lo demás se conserva."""
    def con_identidad(ds):
        ds.SeriesDescription = "control de SINTETICO, historia DEMO-000123"

    codigo, reporte = limpiar(caso(tmp_path, "A", editar=con_identidad), tmp_path / "con", "--conservar-descripciones")
    textos = {d[1] for d in descripciones(tmp_path / "con")}
    assert {d[0] for d in descripciones(tmp_path / "con")} == {"PET CT [REDACTADO] [REDACTADO]"}
    assert codigo == 0 and all("SINTETICO" not in x and "DEMO-000123" not in x and "[REDACTADO]" in x for x in textos)
    assert any("se redactó" in a for a in reporte["avisos"])


def test_volver_a_limpiar_un_estudio_guardado_con_la_casilla_cambiada_se_explica_y_no_cambia_lo_guardado(tmp_path):
    """Lo que encontró quien lo probó: limpió sin la casilla, volvió a limpiar con ella, y todo quedó en cuarentena."""
    origen, trabajo = caso(tmp_path, "A"), tmp_path / "trabajo"
    assert limpiar(origen, trabajo)[0] == 0
    guardado = {f: f.read_bytes() for f in (trabajo / "limpios").rglob("*.dcm")}
    codigo, reporte = limpiar(origen, trabajo, "--conservar-descripciones")
    assert codigo == 1 and reporte["archivos_escritos"] == 0
    assert {c["motivo"] for c in reporte["cuarentena"]} == {anon.MOTIVO_OTRA_OPCION} and len(reporte["cuarentena"]) == 6
    assert {f: f.read_bytes() for f in (trabajo / "limpios").rglob("*.dcm")} == guardado       # nada se sustituye
    # y al revés: guardado con la casilla, limpiado después sin ella
    otro = tmp_path / "otro"
    assert limpiar(origen, otro, "--conservar-descripciones")[0] == 0
    codigo, reporte = limpiar(origen, otro)
    assert codigo == 1 and {c["motivo"] for c in reporte["cuarentena"]} == {anon.MOTIVO_OTRA_OPCION}


def test_otra_copia_de_la_misma_imagen_con_otros_pixeles_no_se_confunde_con_la_casilla(tmp_path):
    origen, trabajo = caso(tmp_path, "A"), tmp_path / "trabajo"
    assert limpiar(origen, trabajo)[0] == 0
    copia = tmp_path / "estudios" / "copia"
    copia.mkdir()
    for i, f in enumerate(sorted(origen.rglob("IM*"))):
        ds = pydicom.dcmread(f)
        pixeles = bytearray(ds.PixelData)
        pixeles[0] ^= 0xFF
        ds.PixelData = bytes(pixeles)
        ds.save_as(copia / f"C{i}", enforce_file_format=True)
    for extra in ((), ("--conservar-descripciones",)):                  # con la casilla o sin ella, es otro contenido
        codigo, reporte = limpiar(copia, trabajo, *extra)
        assert codigo == 1 and {c["motivo"] for c in reporte["cuarentena"]} == {anon.MOTIVO_OTRO_CONTENIDO}


def test_las_cifras_de_la_limpieza_dicen_el_motivo_para_la_pestana_entrada(app, tmp_path):
    origen = caso(tmp_path, "A")
    assert limpiar(origen, app.TRABAJO)[0] == 0
    primera = app.datos_de_limpieza()
    assert (primera["leidos"], primera["escritos"], primera["cuarentena_total"], primera["conservar_descripciones"]) == (6, 6, 0, False)
    limpiar(origen, app.TRABAJO, "--conservar-descripciones")
    segunda = app.datos_de_limpieza()
    assert (segunda["escritos"], segunda["en_la_corrida"], segunda["cuarentena_total"], segunda["conservar_descripciones"]) == (0, 0, 6, True)
    assert segunda["cuarentena_motivos"] == {anon.MOTIVO_OTRA_OPCION: 6}


def test_el_modo_rapido_dice_por_que_no_acepta_un_estudio_guardado_con_descripciones(app, tmp_path):
    """El modo rápido limpia sin la casilla. Si el estudio se guardó antes con ella, no lo sustituye y lo dice; antes
    preguntaba si los archivos eran CT, PET o MR."""
    origen = caso(tmp_path, "A")
    assert limpiar(origen, app.TRABAJO, "--conservar-descripciones")[0] == 0
    t = correr(app, origen, tmp_path / "destino", "a")
    limpieza = next(p for p in t["pasos"] if p["clave"] == "limpieza")
    assert t["estado"] == "error" and t["zip"] is None and limpieza["estado"] == "error"
    assert "ya están guardadas, limpiadas con la casilla de conservar descripciones" in t["mensaje"] and "CT, PET o MR" not in t["mensaje"]
    assert limpieza["datos"]["cuarentena_motivos"] == {anon.MOTIVO_OTRA_OPCION: 6}
    assert app.situacion_de_corrida(t)["procesamiento"]["paso_detenido"] == 1


def test_una_carpeta_sin_imagenes_sigue_diciendo_lo_de_siempre(app, tmp_path):
    vacia = tmp_path / "estudios" / "vacia"
    vacia.mkdir(parents=True)
    (vacia / "notas.txt").write_text("nada que anonimizar")
    t = correr(app, vacia, tmp_path / "destino", "a")
    assert t["estado"] == "error" and t["zip"] is None


def test_descripciones_cambiadas_con_la_misma_casilla_son_otro_contenido(tmp_path):
    origen = caso(tmp_path, "A", editar=con_protocolo)
    trabajo = tmp_path / "trabajo"
    assert limpiar(origen, trabajo, "--conservar-descripciones")[0] == 0
    guardado = {f: f.read_bytes() for f in (trabajo / "limpios").rglob("*.dcm")}
    for f in origen.rglob("IM*"):
        ds = pydicom.dcmread(f)
        ds.ProtocolName = "CEREBRO CONTROL"
        ds.save_as(f, enforce_file_format=True)
    codigo, reporte = limpiar(origen, trabajo, "--conservar-descripciones")
    assert codigo == 1 and len(reporte["cuarentena"]) == 6
    assert {c["motivo"] for c in reporte["cuarentena"]} == {anon.MOTIVO_OTRO_CONTENIDO}
    assert {f: f.read_bytes() for f in (trabajo / "limpios").rglob("*.dcm")} == guardado


def test_redacciones_en_cuarentena_no_se_atribuyen_a_archivos_aceptados(app, tmp_path):
    origen = caso(tmp_path, "A", editar=con_protocolo)
    assert limpiar(origen, app.TRABAJO, "--conservar-descripciones")[0] == 0
    guardado = {f: f.read_bytes() for f in (app.TRABAJO / "limpios").rglob("*.dcm")}
    f = next(origen.rglob("IM*"))
    ds = pydicom.dcmread(f)
    ds.SeriesDescription = "control HC-778899"
    pixeles = bytearray(ds.PixelData)
    pixeles[0] ^= 0xFF
    ds.PixelData = bytes(pixeles)
    ds.save_as(f, enforce_file_format=True)
    codigo, reporte = limpiar(origen, app.TRABAJO, "--conservar-descripciones")
    assert codigo == 1 and reporte["duplicados_omitidos"] == 5 and len(reporte["cuarentena"]) == 1
    assert any("se redactó" in a for a in reporte["avisos"])
    assert app.datos_de_limpieza()["en_la_corrida"] == 5
    assert app.datos_de_limpieza()["descripciones_redactadas"] == 0
    assert {f: f.read_bytes() for f in (app.TRABAJO / "limpios").rglob("*.dcm")} == guardado


def test_redacciones_aceptadas_se_cuentan_al_guardar_y_verificar_duplicados(app, tmp_path):
    origen = caso(tmp_path, "A")  # el nombre sintético figura en StudyDescription
    for duplicados in (0, 6):
        codigo, reporte = limpiar(origen, app.TRABAJO, "--conservar-descripciones")
        assert codigo == 0 and reporte["duplicados_omitidos"] == duplicados
        assert app.datos_de_limpieza()["descripciones_redactadas"] > 0


def test_reporte_antiguo_no_infiere_redacciones_aceptadas(app):
    reportes = app.carpeta_reportes()
    reportes.mkdir(parents=True)
    (reportes / "ultimo_reporte.json").write_text(json.dumps({
        "archivos_escritos": 1, "avisos": ["se redactó una descripción"]}), encoding="utf-8")
    assert app.datos_de_limpieza()["descripciones_redactadas"] is None


def ejecutar_importacion(app, origen):
    resultado = app.anonimizar(None, {"origen": str(origen)})
    assert app.CANDADO.acquire(timeout=30), "la importación no terminó"
    app.CANDADO.release()
    return resultado["trabajo"]


def test_importacion_fallida_no_muestra_el_reporte_anterior(app, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "auth", lambda request: None)
    origen = caso(tmp_path, "A")
    assert limpiar(origen, app.TRABAJO)[0] == 0
    anterior = (app.carpeta_reportes() / "ultimo_reporte.json").read_bytes()
    (app.TRABAJO / "limpios" / ".corriendo").write_text("candado externo", encoding="utf-8")
    tid = ejecutar_importacion(app, origen)
    t = app.trabajo(None, tid)
    assert t["estado"] == "error" and t["codigo"] == 2
    assert t.get("datos") is None and not t.get("reporte")
    assert (app.carpeta_reportes() / "ultimo_reporte.json").read_bytes() == anterior


def test_reporte_del_trabajo_se_conserva_aunque_se_consulte_despues_de_otro(app, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "auth", lambda request: None)
    origen = caso(tmp_path, "A")
    primero = ejecutar_importacion(app, origen)
    segundo = ejecutar_importacion(app, origen)
    # No se consulta el primero hasta que el segundo ya reemplazó ultimo_reporte.
    a, b = app.trabajo(None, primero), app.trabajo(None, segundo)
    assert a["datos"]["escritos"] == 6 and a["datos"]["ya_guardados"] == 0
    assert b["datos"]["escritos"] == 0 and b["datos"]["ya_guardados"] == 6
    assert a["reporte"] != b["reporte"]
