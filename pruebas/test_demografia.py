"""
Demografía por estudio: identificador anónimo, sexo registrado y edad en la fecha del estudio.
Study-level demographics: anonymous identifier, recorded sex and age on the date of the study.

La edad se calcula con la fecha de nacimiento y la fecha del estudio originales, antes de anonimizarlas, y de ahí solo
salen la edad o el motivo por el que no se pudo calcular. El sexo es el de PatientSex; no se infiere. Nada de esto
cambia los archivos limpios ni sus píxeles.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
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


anon = cargar("anonimizar_demo", RAIZ / "herramientas" / "anonimizar.py")
demo = cargar("demo", RAIZ / "herramientas" / "demo.py")
NACIMIENTO, ESTUDIO = "19700310", "20240305"            # las fechas originales de los casos de estas pruebas


@pytest.fixture
def app(tmp_path):
    a = cargar("app_en_prueba", RAIZ / "herramientas" / "app.py")
    a.TRABAJO = tmp_path / "datos-pet"
    a.TRABAJO.mkdir()
    return a


def correr(app, origen: Path, destino: Path, tid: str = "corrida", mostrar: bool = True) -> dict:
    assert app.CANDADO.acquire(blocking=False)
    app.TRABAJOS[tid] = {"nombre": "rapido", "estado": "corriendo", "lineas": [], "codigo": None, "pasos": [], "pacientes": [],
                         "origen": str(origen), "destino": str(app.validar_destino(destino, origen)), "zip": None, "mensaje": "",
                         "archivos": {}, "revisado": False, "exportacion": {}, "orden_de_cortes": {}, "series_corrida": [], "demografia": []}
    app._rapido(tid, origen, destino, "ANON")
    t = app.TRABAJOS[tid]
    if mostrar and t.get("zip"):
        mostrar_todo(app, t)
    return t

def mostrar_todo(app, t: dict) -> None:
    """Lo que hace el navegador al recorrer la corrida: cada cuadro se sirve al visor y después se avisa de que se mostró."""
    for indice, s in enumerate(t.get("series_corrida") or []):
        for k, cuadros in enumerate(s["cuadros"], 1):
            for cuadro in range(1, cuadros + 1):
                t.setdefault("servidos", set()).add((indice, k, cuadro))
                app.anotar_mostrado_corrida(t, indice, k, cuadro)



def caso(tmp_path: Path, nombre: str, semilla: int = 1, editar=None, nacimiento: str = NACIMIENTO, estudio: str = ESTUDIO,
         sexo: str | None = "F", edad_as: str | None = None) -> Path:
    """Caso sintético de 4 + 4 cortes con las fechas, el sexo y la edad informada que se pidan."""
    d = tmp_path / "estudios" / nombre
    demo.escribir_caso(d, 4, semilla)
    for serie in ("SE0", "SE1"):
        for i in range(4):
            f = d / serie / f"IM{i}"
            ds = pydicom.dcmread(f)
            ds.PatientBirthDate = nacimiento
            ds.StudyDate = ds.SeriesDate = ds.AcquisitionDate = ds.ContentDate = estudio
            if sexo is None:
                if "PatientSex" in ds:
                    del ds.PatientSex
            else:
                ds.PatientSex = sexo
            if edad_as is None:
                if "PatientAge" in ds:
                    del ds.PatientAge
            else:
                ds.PatientAge = edad_as
            if editar:
                editar(serie, i, ds)
            ds.save_as(f, enforce_file_format=True)
    return d


def manifiesto(ruta: str) -> dict:
    with zipfile.ZipFile(ruta) as z:
        return json.loads(z.read("manifiesto.json"))


def unico(t: dict) -> dict:
    assert len(t["demografia"]) == 1, t["demografia"]
    return t["demografia"][0]


# ---------------------------------------------------------------------------------------------------------------
# cálculo de la edad
# ---------------------------------------------------------------------------------------------------------------

def edad(nacimiento, estudio) -> str:
    r = anon.edad_en_estudio(nacimiento, estudio)
    return anon.texto_edad(r["valor"], r["unidad"], r["agrupada"]) if r["estado"] == "calculada" else "NO: " + r["motivo"]


def test_antes_el_dia_y_despues_del_cumpleanos():
    assert edad("19700310", "20240309") == "53 años"
    assert edad("19700310", "20240310") == "54 años"
    assert edad("19700310", "20240311") == "54 años"
    assert edad("19700310", "19710310") == "1 año"


def test_nacido_un_29_de_febrero():
    assert edad("20000229", "20230228") == "22 años"                  # año no bisiesto: todavía no ha cumplido
    assert edad("20000229", "20230301") == "23 años"                  # cumple el 1 de marzo
    assert edad("20000229", "20240228") == "23 años"                  # año bisiesto: la víspera
    assert edad("20000229", "20240229") == "24 años"                  # y el día


def test_menores_de_un_ano_conservan_precision():
    assert edad("20240101", "20241231") == "11 meses"
    assert edad("20240101", "20240305") == "2 meses"
    assert edad("20240205", "20240305") == "1 mes"
    assert edad("20240220", "20240305") == "2 semanas"
    assert edad("20240303", "20240305") == "2 días"
    assert edad("20240305", "20240305") == "menos de 1 día"
    assert "0 años" not in {edad("20240101", "20240305"), edad("20240303", "20240305"), edad("20240305", "20240305")}


def test_noventa_o_mas_se_agrupan_como_en_la_politica():
    assert edad("19340305", "20240305") == "90 años o más" and edad("19100101", "20240305") == "90 años o más"
    assert edad("19340306", "20240305") == "89 años"
    assert anon.edad_informada("095Y") == {"valor": 90, "unidad": "años", "agrupada": True}


@pytest.mark.parametrize("nacimiento, estudio, fragmento", [
    ("", ESTUDIO, "falta la fecha de nacimiento"), (NACIMIENTO, "", "falta la fecha del estudio"),
    ("1970", ESTUDIO, "la fecha de nacimiento no es válida"), ("19701301", ESTUDIO, "la fecha de nacimiento no es válida"),
    ("19700230", ESTUDIO, "la fecha de nacimiento no es válida"), (NACIMIENTO, "2024-03-05", "la fecha del estudio no es válida"),
    ("20250101", ESTUDIO, "anterior a la de nacimiento"), ("18500101", ESTUDIO, "fuera de un rango plausible"),
])
def test_fechas_ausentes_mal_formadas_o_incoherentes(nacimiento, estudio, fragmento):
    r = anon.edad_en_estudio(nacimiento, estudio)
    assert r["estado"] == "no_disponible" and fragmento in r["motivo"]
    assert not any(c.isdigit() for c in r["motivo"])                  # el motivo no arrastra ninguna fecha


def test_la_edad_no_depende_de_la_fecha_de_hoy(monkeypatch):
    class OtroDia(anon.dt.date):
        @classmethod
        def today(cls):
            return cls(2099, 1, 1)
    monkeypatch.setattr(anon.dt, "date", OtroDia)
    assert edad("19700310", "20240310") == "54 años"


# ---------------------------------------------------------------------------------------------------------------
# resumen por estudio y etiqueta
# ---------------------------------------------------------------------------------------------------------------

def obs(e=("c", 54, "años", False), i=None, s="F"):
    return {"e": list(e), "i": list(i) if i else None, "s": s}


def test_etiqueta_identificador_sexo_y_edad_separados():
    r = anon.resumen_demografia([obs(), obs()])
    assert anon.etiqueta_demografia("ANON-0001", r) == "ANON-0001 · F · 54 años en el estudio"
    assert r["edad_en_el_estudio"]["estado"] == "calculada" and r["sexo"] == {**r["sexo"], "estado": "registrado", "valor": "F"}


@pytest.mark.parametrize("valores, estado, texto", [
    (["F", "F"], "registrado", "ANON-0001 · F ·"), (["M"], "registrado", "ANON-0001 · M ·"), (["O"], "registrado", "ANON-0001 · O ·"),
    (["", ""], "no_registrado", "· sexo no registrado ·"), (["?"], "no_registrado", "· sexo no registrado ·"),
    (["F", "M", "F"], "conflicto", "· sexo en conflicto ·"),
])
def test_valores_de_sexo(valores, estado, texto):
    r = anon.resumen_demografia([obs(s=v) for v in valores])
    assert r["sexo"]["estado"] == estado and texto in anon.etiqueta_demografia("ANON-0001", r)
    if estado == "conflicto":
        assert r["sexo"]["valor"] is None and r["sexo"]["valores"] == ["F", "M"] and "no se elige ninguno" in r["sexo"]["avisos"][0]


def test_sexo_registrado_tal_como_consta_sin_inferir():
    assert [anon.sexo_registrado(v) for v in ("F", "m", " O ", "", None, "X", "FEMALE")] == ["F", "M", "O", "", "", "?", "?"]


def test_edades_distintas_en_un_estudio_no_dan_una_edad():
    r = anon.resumen_demografia([obs(), obs(e=("c", 55, "años", False))])
    assert r["edad_en_el_estudio"]["estado"] == "no_disponible" and "edades distintas" in r["edad_en_el_estudio"]["motivo"]
    assert anon.etiqueta_demografia("ANON-0001", r).endswith("· edad no disponible")


def test_edad_informada_solo_como_respaldo_y_con_su_nombre():
    sin_fechas = ("n", "falta la fecha de nacimiento")
    r = anon.resumen_demografia([obs(e=sin_fechas, i=(54, "años", False))])
    assert r["edad_en_el_estudio"]["estado"] == "informada" and "no calculada" in r["edad_en_el_estudio"]["fuente"]
    assert anon.etiqueta_demografia("ANON-0001", r) == "ANON-0001 · F · 54 años (edad informada por el equipo)"
    # con edad calculada, la informada no la sustituye; si difiere, se avisa
    r = anon.resumen_demografia([obs(i=(60, "años", False))])
    assert r["edad_en_el_estudio"]["estado"] == "calculada" and r["edad_en_el_estudio"]["valor"] == 54
    assert "no coincide con la calculada" in r["edad_en_el_estudio"]["avisos"][0]


# ---------------------------------------------------------------------------------------------------------------
# de punta a punta
# ---------------------------------------------------------------------------------------------------------------

def test_una_corrida_muestra_identificador_sexo_y_edad_en_el_visor_y_en_el_manifiesto(app, tmp_path):
    t = correr(app, caso(tmp_path, "A"), tmp_path)
    assert t["estado"] == "ok", t["mensaje"]
    d = unico(t)
    assert d["etiqueta"] == "ANON-0001 · F · 53 años en el estudio"          # 5 de marzo, cumple el 10: todavía 53
    assert d["sexo"]["estado"] == "registrado" and d["edad_en_el_estudio"] == {**d["edad_en_el_estudio"], "estado": "calculada", "valor": 53, "unidad": "años"}
    m = manifiesto(t["zip"])
    assert m["demografia"]["estudios"] == t["demografia"]                     # el manifiesto dice lo mismo que la corrida
    por_estudio = {f"{x['paciente']}/{x['estudio']}": x for x in t["demografia"]}
    for s in t["series_corrida"]:                                             # y el visor de la corrida, por serie
        assert por_estudio[s["serie"].rsplit("/", 1)[0]] == d
    app.indexar()                                                             # y la pestaña Estudios
    assert {s["etiqueta"] for s in app.INDICE.values()} == {d["etiqueta"]}
    assert all(s["demografia"] == d for s in app.INDICE.values())


def test_el_mismo_paciente_tiene_edades_distintas_en_dos_estudios(app, tmp_path):
    ta = correr(app, caso(tmp_path, "A", 1, estudio="20240305"), tmp_path, "A")
    tb = correr(app, caso(tmp_path, "B", 2, estudio="20260601"), tmp_path, "B")
    da, db = unico(ta), unico(tb)
    assert da["paciente"] == db["paciente"] == "ANON-0001" and da["estudio"] != db["estudio"]     # una identidad, dos estudios
    assert da["etiqueta"] == "ANON-0001 · F · 53 años en el estudio" and db["etiqueta"] == "ANON-0001 · F · 56 años en el estudio"
    app.indexar()
    assert {s["etiqueta"] for s in app.INDICE.values()} == {da["etiqueta"], db["etiqueta"]}
    mapa = json.loads((app.TRABAJO / "limpios_PRIVADO" / "mapa_seudonimos.json").read_text(encoding="utf-8"))
    assert len(mapa["pacientes"]) == 1                                         # la edad no es un atributo del paciente:
    assert "edad" not in json.dumps(mapa, ensure_ascii=False).lower()          # el mapa de seudónimos no la guarda


def test_identidad_estable_en_corridas_e_importaciones_repetidas(app, tmp_path):
    c = caso(tmp_path, "A")
    carpetas = set()
    for k in range(3):
        t = correr(app, c, tmp_path, f"c{k}")
        assert t["pacientes"] == ["ANON-0001"] and unico(t)["etiqueta"] == "ANON-0001 · F · 53 años en el estudio"
        carpetas.add(tuple(sorted(p.name for p in (app.TRABAJO / "limpios").iterdir() if p.is_dir())))
    assert carpetas == {("ANON-0001",)}                                        # una sola carpeta de paciente, sin sufijos de sexo
    otro = caso(tmp_path, "otro-paciente", 3, sexo="M", editar=lambda s, i, ds: setattr(ds, "PatientID", "DEMO-000999"))
    t = correr(app, otro, tmp_path, "otro")
    assert t["pacientes"] == ["ANON-0002"] and unico(t)["etiqueta"] == "ANON-0002 · M · 53 años en el estudio"   # una sola numeración


def test_sexo_ausente_y_sexo_en_conflicto_dentro_de_un_estudio(app, tmp_path):
    t = correr(app, caso(tmp_path, "sin-sexo", sexo=None), tmp_path, "uno")
    d = unico(t)
    assert d["sexo"]["estado"] == "no_registrado" and d["etiqueta"] == "ANON-0001 · sexo no registrado · 53 años en el estudio"

    def mitad_m(serie, i, ds):
        if serie == "SE1":
            ds.PatientSex = "M"
    t = correr(app, caso(tmp_path, "conflicto", 2, editar=mitad_m), tmp_path, "dos")
    d = unico(t)
    assert d["sexo"]["estado"] == "conflicto" and d["sexo"]["valor"] is None and d["sexo"]["valores"] == ["F", "M"]
    assert d["etiqueta"] == "ANON-0001 · sexo en conflicto · 53 años en el estudio"
    assert manifiesto(t["zip"])["demografia"]["estudios"][0]["sexo"]["avisos"]


def test_fechas_ausentes_o_incoherentes_dan_edad_no_disponible_con_motivo(app, tmp_path):
    t = correr(app, caso(tmp_path, "sin-nacimiento", nacimiento=""), tmp_path, "uno")
    d = unico(t)
    assert d["edad_en_el_estudio"]["estado"] == "no_disponible" and "falta la fecha de nacimiento" in d["edad_en_el_estudio"]["motivo"]
    assert d["etiqueta"] == "ANON-0001 · F · edad no disponible"

    def otra_fecha_en_pet(serie, i, ds):
        if serie == "SE1":
            ds.PatientBirthDate = "19600101"
    t = correr(app, caso(tmp_path, "incoherente", 2, editar=otra_fecha_en_pet), tmp_path, "dos")
    d = unico(t)
    assert d["edad_en_el_estudio"]["estado"] == "no_disponible" and "edades distintas" in d["edad_en_el_estudio"]["motivo"]

    t = correr(app, caso(tmp_path, "informada", 3, nacimiento="", edad_as="054Y"), tmp_path, "tres")
    d = unico(t)
    assert d["edad_en_el_estudio"]["estado"] == "informada" and d["etiqueta"] == "ANON-0001 · F · 54 años (edad informada por el equipo)"


def test_ni_la_fecha_de_nacimiento_ni_la_del_estudio_aparecen_en_lo_nuevo(app, tmp_path):
    t = correr(app, caso(tmp_path, "A"), tmp_path)
    r = app.aplicar_revision_rapido(t, "Prueba")
    app.indexar()
    formas = [NACIMIENTO, ESTUDIO, "1970-03-10", "2024-03-05", "10/03/1970", "05/03/2024", "1970.03.10"]
    visibles = {
        "trabajo": json.dumps({k: v for k, v in t.items() if k not in ("archivos",)}, ensure_ascii=False, default=str),
        "manifiesto del zip": json.dumps(manifiesto(r["zip"]), ensure_ascii=False),
        "manifiesto local": (app.TRABAJO / "paquetes" / (Path(r["zip"]).name[:-4] + ".manifiesto.json")).read_text(encoding="utf-8"),
        "registro de demografía": (app.TRABAJO / "limpios_PRIVADO" / "demografia.json").read_text(encoding="utf-8"),
        "registro de la revisión": Path(r["registro_revision"]).read_text(encoding="utf-8"),
        "índice de Estudios": json.dumps([{k: v for k, v in s.items() if k != "cortes"} for s in app.INDICE.values()], ensure_ascii=False),
        "nombres del zip": "\n".join(zipfile.ZipFile(r["zip"]).namelist()),
    }
    for donde, contenido in visibles.items():
        for forma in formas:
            assert forma not in contenido, f"{forma} aparece en {donde}"
    # y los archivos limpios siguen sin fecha de nacimiento y con la fecha del estudio desplazada
    with zipfile.ZipFile(r["zip"]) as z:
        for n in z.namelist():
            if n.endswith(".dcm"):
                ds = pydicom.dcmread(io.BytesIO(z.read(n)), stop_before_pixels=True)
                assert str(ds.get("PatientBirthDate", "")) == "" and str(ds.StudyDate) != ESTUDIO


def test_los_archivos_limpios_y_sus_pixeles_no_cambian(app, tmp_path):
    c = caso(tmp_path, "A")
    t = correr(app, c, tmp_path, "uno")
    limpios = app.TRABAJO / "limpios"
    antes = {rel: hashlib.sha256((limpios / rel).read_bytes()).hexdigest() for rel in t["archivos"]}
    assert antes == t["archivos"]
    # los píxeles del archivo limpio son los del original
    originales = {pydicom.dcmread(f).PixelData for f in c.rglob("IM*")}
    assert {pydicom.dcmread(limpios / rel).PixelData for rel in t["archivos"]} == originales
    # reimportar para rellenar la demografía no reescribe ningún archivo limpio
    (app.TRABAJO / "limpios_PRIVADO" / "demografia.json").unlink()
    t2 = correr(app, c, tmp_path, "dos")
    assert "0 archivos limpios" in t2["pasos"][0]["resumen"] and "8 duplicados" in t2["pasos"][0]["resumen"]
    assert {rel: hashlib.sha256((limpios / rel).read_bytes()).hexdigest() for rel in t2["archivos"]} == antes
    assert unico(t2)["etiqueta"] == "ANON-0001 · F · 53 años en el estudio"
    # y las comprobaciones de la corrida siguen en verde
    assert [p["estado"] for p in t2["pasos"]] == ["ok"] * 6


def test_estudios_importados_con_una_version_anterior(app, tmp_path):
    """Sin registro de demografía (así quedan los estudios ya importados): el sexo sale del archivo limpio; la edad
    calculada no existe hasta reimportar, y se dice. Reimportar la carpeta original la calcula sin tocar los limpios ni
    invalidar una revisión ya registrada."""
    c = caso(tmp_path, "A", edad_as="053Y")
    t = correr(app, c, tmp_path, "uno")
    app.aplicar_revision_rapido(t, "Prueba")                                   # cubre al paciente entero: queda revisado
    assert app.leer_revision("ANON-0001")["lista_para_compartir"] is True
    (app.TRABAJO / "limpios_PRIVADO" / "demografia.json").unlink()             # como si se hubiera importado antes de esta versión
    app.indexar()
    d = next(iter(app.INDICE.values()))["demografia"]
    assert d["sexo"] == {**d["sexo"], "estado": "registrado", "valor": "F"}    # el sexo se conserva en el archivo limpio
    assert d["edad_en_el_estudio"]["estado"] == "informada"                    # PatientAge, como edad informada
    assert "versión anterior" in d["edad_en_el_estudio"]["motivo"] and "vuelve a importar" in d["edad_en_el_estudio"]["motivo"]
    assert d["etiqueta"] == "ANON-0001 · F · 53 años (edad informada por el equipo)"
    t2 = correr(app, c, tmp_path, "dos")                                       # reimportación de la carpeta original
    assert unico(t2)["edad_en_el_estudio"]["estado"] == "calculada"
    assert t2["archivos"] == t["archivos"]                                     # ni un byte distinto
    assert app.leer_revision("ANON-0001")["lista_para_compartir"] is True      # la revisión anterior sigue vigente


def test_la_demografia_de_una_corrida_no_cambia_con_importaciones_posteriores(app, tmp_path):
    destino = tmp_path / "destino"
    destino.mkdir()
    c = caso(tmp_path, "A")
    t = correr(app, c, destino, "corrida")
    congelada = json.loads(json.dumps(t["demografia"]))
    pendiente = manifiesto(t["zip"])["demografia"]
    # después llega, del mismo estudio, otra copia con otro sexo registrado: el estudio guardado queda en conflicto
    otra = tmp_path / "estudios" / "misma-serie-otro-sexo"
    otra.mkdir(parents=True)
    for i, f in enumerate(sorted((c / "SE1").iterdir())):
        ds = pydicom.dcmread(f)
        ds.PatientSex = "M"
        ds.SOPInstanceUID = pydicom.uid.generate_uid()
        ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        ds.InstanceNumber = 100 + i
        ds.save_as(otra / f"X{i}", enforce_file_format=True)
    correr(app, otra, destino, "posterior")
    app.indexar()
    general = next(s["demografia"] for s in app.INDICE.values() if s["modalidad"] == "PT")
    assert general["sexo"]["estado"] == "conflicto"                            # Estudios ve todo lo guardado
    assert t["demografia"] == congelada                                        # la corrida, no
    r = app.aplicar_revision_rapido(t, "Prueba")
    assert manifiesto(r["zip"])["demografia"] == pendiente                     # el zip revisado dice lo mismo que el pendiente
    assert manifiesto(r["zip"])["demografia"]["estudios"][0]["etiqueta"] == "ANON-0001 · F · 53 años en el estudio"


def test_el_manifiesto_solo_lleva_los_estudios_del_paquete(app, tmp_path):
    ta = correr(app, caso(tmp_path, "A", 1, estudio="20240305"), tmp_path, "A")
    tb = correr(app, caso(tmp_path, "B", 2, estudio="20260601"), tmp_path, "B")
    assert [d["estudio"] for d in manifiesto(tb["zip"])["demografia"]["estudios"]] == [unico(tb)["estudio"]]
    clasico = app.crear_paquete(["ANON-0001"])                                 # la pestaña Enviar: el paciente entero
    estudios = manifiesto(clasico["ruta"])["demografia"]["estudios"]
    assert sorted(d["etiqueta"] for d in estudios) == ["ANON-0001 · F · 53 años en el estudio", "ANON-0001 · F · 56 años en el estudio"]
    assert {d["estudio"] for d in estudios} == {unico(ta)["estudio"], unico(tb)["estudio"]}
    with pytest.raises(app.PaqueteRechazado):                                  # una demografía que no es la del paquete se rechaza
        app.crear_paquete(["ANON-0001"], demografia=ta["demografia"])


# ---------------------------------------------------------------------------------------------------------------
# revisión del 11 de noviembre de 2026, séptima ronda: dos originales que dan el mismo archivo limpio
# ---------------------------------------------------------------------------------------------------------------

def test_dos_copias_de_una_instancia_con_fechas_distintas_dan_conflicto_y_no_la_ultima_edad(app, tmp_path):
    """La fecha de nacimiento se vacía al limpiar, así que dos copias de una misma instancia con nacimientos distintos
    dan el mismo archivo limpio. Sus edades (53 y 63) no coinciden: el estudio no puede mostrar ninguna de las dos."""
    c = caso(tmp_path, "A")
    for f in sorted(c.rglob("IM*")):
        ds = pydicom.dcmread(f)
        ds.PatientBirthDate = "19600310"                              # la copia dice diez años más
        ds.save_as(f.with_name(f.name + "_copia"), enforce_file_format=True)
    t = correr(app, c, tmp_path)
    assert t["zip"], t["mensaje"]
    assert "8 duplicados" in t["pasos"][0]["resumen"] and len(t["archivos"]) == 8       # el mismo archivo limpio para las dos copias
    d = unico(t)
    assert d["edad_en_el_estudio"]["estado"] == "no_disponible" and "edades distintas" in d["edad_en_el_estudio"]["motivo"]
    assert d["etiqueta"] == "ANON-0001 · F · edad no disponible" and d["archivos"] == 8
    registro = json.loads((app.TRABAJO / "limpios_PRIVADO" / "demografia.json").read_text(encoding="utf-8"))["archivos"]
    assert all(sorted(o["e"][1] for o in obs) == [53, 63] for obs in registro.values())  # las dos observaciones se conservan


def test_dos_copias_con_sexos_distintos_no_se_resuelven_en_silencio(app, tmp_path):
    """PatientSex se conserva en el archivo limpio, así que una copia con otro sexo es otro contenido para la misma
    instancia: va a cuarentena, el cotejo de la corrida lo marca y no hay zip. Y el estudio guardado muestra el conflicto."""
    c = caso(tmp_path, "A")
    for f in sorted((c / "SE1").iterdir()):
        ds = pydicom.dcmread(f)
        ds.PatientSex = "M"
        ds.save_as(f.with_name(f.name + "_copia"), enforce_file_format=True)
    t = correr(app, c, tmp_path)
    pasos = {p["clave"]: p for p in t["pasos"]}
    assert pasos["limpieza"]["estado"] == "aviso" and "4 en cuarentena" in pasos["limpieza"]["resumen"]
    assert pasos["cotejo"]["estado"] == "error" and t["estado"] == "error" and t["zip"] is None      # no sale ningún paquete
    app.indexar()
    d = next(s["demografia"] for s in app.INDICE.values() if s["modalidad"] == "PT")
    assert d["sexo"]["estado"] == "conflicto" and d["sexo"]["valores"] == ["F", "M"] and d["sexo"]["valor"] is None
    assert d["etiqueta"] == "ANON-0001 · sexo en conflicto · 53 años en el estudio"


def test_el_registro_antiguo_con_una_sola_observacion_por_archivo_se_sigue_leyendo():
    una = {"e": ["c", 54, "años", False], "i": None, "s": "F"}
    assert anon.observaciones_de_registro(una) == [una]
    assert anon.observaciones_de_registro([una, {"e": ["c", 64, "años", False], "i": None, "s": "F"}])[1]["e"][1] == 64
    assert anon.observaciones_de_registro(None) == [] and anon.observaciones_de_registro("basura") == []
    registro = {}
    anon.anotar_observacion(registro, "a.dcm", una)
    anon.anotar_observacion(registro, "a.dcm", dict(una))            # idéntica: no se repite
    anon.anotar_observacion(registro, "a.dcm", {**una, "s": "M"})    # distinta: se conserva
    assert [o["s"] for o in registro["a.dcm"]] == ["F", "M"]
