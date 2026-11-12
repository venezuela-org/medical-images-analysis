"""
Orden visible de los archivos exportados. / Visible order of exported files.

Los archivos limpios conservan su nombre interno (un hash). En el zip y en su manifiesto cada archivo aparece como
`corte_000001_<hash>.dcm`, numerado por serie con el mismo criterio que usa el visor:
InstanceNumber válido y sin repetir; si no, posición y orientación cuando forman una pila coherente; si no, un
desempate determinista con aviso. La numeración es un índice de presentación: los bytes de cada DICOM no cambian.
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


demo = cargar("demo", RAIZ / "herramientas" / "demo.py")
N = 12        # cortes por serie: suficiente para que '10' y '2' se distingan del orden alfabético


@pytest.fixture
def app(tmp_path):
    a = cargar("app_en_prueba", RAIZ / "herramientas" / "app.py")
    a.TRABAJO = tmp_path / "datos-pet"
    a.TRABAJO.mkdir()
    return a


def correr(app, origen: Path, destino: Path, tid: str = "corrida") -> dict:
    """Lo mismo que hace POST /api/rapido, sin servidor y de forma síncrona."""
    assert app.CANDADO.acquire(blocking=False)
    app.TRABAJOS[tid] = {"nombre": "rapido", "estado": "corriendo", "lineas": [], "codigo": None, "pasos": [], "pacientes": [],
                         "origen": str(origen), "destino": str(app.validar_destino(destino, origen)), "zip": None, "mensaje": "",
                         "archivos": {}, "revisado": False, "exportacion": {}, "orden_de_cortes": {}}
    app._rapido(tid, origen, destino, "ANON")
    return app.TRABAJOS[tid]


def caso_mezclado(tmp_path: Path, nombre: str = "A", semilla: int = 1, editar=None) -> Path:
    """Caso sintético cuyos archivos de entrada llevan nombres que no siguen el orden de los cortes."""
    d = tmp_path / "estudios" / nombre
    demo.escribir_caso(d, N, semilla)
    for serie in ("SE0", "SE1"):
        for i in range(N):
            f = d / serie / f"IM{i}"
            if editar:
                ds = pydicom.dcmread(f)
                editar(serie, i, ds)
                ds.save_as(f, enforce_file_format=True)
            f.rename(d / serie / (hashlib.sha1(f"{serie}{i}".encode()).hexdigest()[:10] + ".dcm"))   # nombres barajados
    return d


def series_del_zip(ruta: str) -> dict[str, list[str]]:
    """{carpeta de serie: [nombres de archivo en el orden en que un explorador los mostraría]}"""
    out: dict[str, list[str]] = {}
    with zipfile.ZipFile(ruta) as z:
        for n in z.namelist():
            if n != "manifiesto.json":
                out.setdefault(n.rsplit("/", 1)[0], []).append(n.rsplit("/", 1)[1])
    return {k: sorted(v) for k, v in out.items()}                    # orden alfabético: el de Finder y el Explorador


def dicom_del_zip(ruta: str, nombre: str):
    with zipfile.ZipFile(ruta) as z:
        return pydicom.dcmread(io.BytesIO(z.read(nombre)), stop_before_pixels=True)


def manifiesto(ruta: str) -> dict:
    with zipfile.ZipFile(ruta) as z:
        return json.loads(z.read("manifiesto.json"))


# ---------------------------------------------------------------------------------------------------------------
# criterio de orden, sin archivos
# ---------------------------------------------------------------------------------------------------------------

def cab(numero=None, z=None, fase=None, varias=False):
    return {"numero": numero, "ipp": None if z is None else [0.0, 0.0, z], "iop": None if z is None else [1.0, 0, 0, 0, 1.0, 0],
            "fase": fase, "varias_fases": varias}


def ordenar(app, datos: dict[str, dict]) -> dict:
    rutas = [Path("/serie") / n for n in datos]
    r = app.ordenar_serie(rutas, {str(Path("/serie") / n): d for n, d in datos.items()})
    return {"orden": [p.name for p in r["orden"]], "criterio": r["criterio"], "aviso": r["aviso"]}


def test_instance_number_valido_ordena_numericamente(app):
    datos = {f"{h}.dcm": cab(numero=n, z=float(-n)) for h, n in zip("zyxwvutsrqpo", range(1, 13))}   # nombres en orden inverso
    r = ordenar(app, datos)
    assert r["criterio"] == "InstanceNumber" and r["aviso"] is None
    assert r["orden"] == [f"{h}.dcm" for h in "zyxwvutsrqpo"]         # 1, 2, ... 10, 11, 12: numérico, y manda sobre la posición
    assert ordenar(app, dict(reversed(list(datos.items()))))["orden"] == r["orden"]   # no depende del orden de entrada


def test_sin_instance_number_ordena_por_posicion_si_la_pila_es_coherente(app):
    r = ordenar(app, {"c.dcm": cab(z=30.0), "a.dcm": cab(z=10.0), "b.dcm": cab(z=20.0)})
    assert r["criterio"] == "posición y orientación" and r["orden"] == ["a.dcm", "b.dcm", "c.dcm"]
    assert "ausente" in r["aviso"] and "no un orden anatómico" not in r["aviso"]


def test_numero_repetido_y_posicion_repetida_es_desempate_con_aviso(app):
    r = ordenar(app, {"c.dcm": cab(numero=1, z=5.0), "a.dcm": cab(numero=1, z=5.0), "b.dcm": cab(numero=2, z=5.0)})
    assert r["criterio"] == "desempate determinista" and "no un orden anatómico verificado" in r["aviso"]
    assert r["orden"] == ["a.dcm", "c.dcm", "b.dcm"]                  # número, luego nombre interno: siempre igual


def test_varias_fases_temporales_no_se_ordenan_por_posicion(app):
    datos = {"a.dcm": cab(z=10.0, fase=1), "b.dcm": cab(z=20.0, fase=1), "c.dcm": cab(z=10.0, fase=2), "d.dcm": cab(z=20.0, fase=2)}
    assert ordenar(app, datos)["criterio"] == "desempate determinista"
    dinamica = {"a.dcm": cab(z=10.0, varias=True), "b.dcm": cab(z=20.0, varias=True)}
    assert ordenar(app, dinamica)["criterio"] == "desempate determinista"   # aunque las posiciones sean distintas
    # con InstanceNumber válido y único, la serie se ordena por él aunque tenga varias fases
    con_numero = {"a.dcm": cab(numero=3, z=10.0, fase=2), "b.dcm": cab(numero=1, z=10.0, fase=1), "c.dcm": cab(numero=2, z=20.0, fase=1)}
    assert ordenar(app, con_numero) == {"orden": ["b.dcm", "c.dcm", "a.dcm"], "criterio": "InstanceNumber", "aviso": None}


def test_sin_numero_ni_posicion_es_desempate(app):
    r = ordenar(app, {"b.dcm": cab(), "a.dcm": cab()})
    assert r["criterio"] == "desempate determinista" and r["orden"] == ["a.dcm", "b.dcm"]


# ---------------------------------------------------------------------------------------------------------------
# exportación
# ---------------------------------------------------------------------------------------------------------------

def test_nombres_y_orden_de_entrada_mezclados_salen_numerados_por_corte(app, tmp_path):
    t = correr(app, caso_mezclado(tmp_path), tmp_path)
    assert t["estado"] == "ok", t["mensaje"]
    series = series_del_zip(t["zip"])
    assert len(series) == 2                                           # CT y PET, cada una en su carpeta
    for carpeta, nombres in series.items():
        assert len(nombres) == N
        assert [n[:13] for n in nombres] == [f"corte_{k:06d}_" for k in range(1, N + 1)]      # numeración independiente por serie
        numeros = [int(dicom_del_zip(t["zip"], f"{carpeta}/{n}").InstanceNumber) for n in nombres]
        assert numeros == list(range(1, N + 1))                       # el orden del explorador es el de los cortes
        assert int(dicom_del_zip(t["zip"], f"{carpeta}/{nombres[1]}").InstanceNumber) == 2     # corte_000002 es el 2...
        assert int(dicom_del_zip(t["zip"], f"{carpeta}/{nombres[9]}").InstanceNumber) == 10    # ...y corte_000010 es el 10
        assert carpeta.split("/")[0].startswith("ANON-") and carpeta.count("/") == 2           # paciente / estudio / serie


def test_bytes_hashes_cantidad_y_manifiesto_coinciden(app, tmp_path):
    t = correr(app, caso_mezclado(tmp_path), tmp_path)
    limpios = app.TRABAJO / "limpios"
    m = manifiesto(t["zip"])
    local = json.loads((app.TRABAJO / "paquetes" / (Path(t["zip"]).name[:-4] + ".manifiesto.json")).read_text(encoding="utf-8"))
    correspondencia = local["correspondencia_interna"]               # ruta exportada → ruta interna
    with zipfile.ZipFile(t["zip"]) as z:
        entradas = [n for n in z.namelist() if n != "manifiesto.json"]
        assert len(entradas) == len(set(entradas)) == len(t["archivos"]) == 2 * N                 # misma cantidad, sin repetidos
        assert [e["ruta"] for e in m["archivos"]] == sorted(entradas)                             # manifiesto = entradas del zip
        assert set(correspondencia) == set(entradas) and set(correspondencia.values()) == set(t["archivos"])
        for e in m["archivos"]:
            exportado = z.read(e["ruta"])
            interno = (limpios / correspondencia[e["ruta"]]).read_bytes()
            assert exportado == interno                                                           # los mismos bytes
            assert hashlib.sha256(exportado).hexdigest() == e["sha256"] == t["archivos"][correspondencia[e["ruta"]]]
            assert e["bytes"] == len(interno)
    assert all((limpios / rel).name == rel.rsplit("/", 1)[1] and not rel.rsplit("/", 1)[1].startswith("corte_") for rel in t["archivos"])
    assert "no modifica InstanceNumber" in m["nombres"]               # los nombres internos no cambian; la numeración es de presentación


def test_sin_instance_number_la_exportacion_sigue_la_posicion(app, tmp_path):
    def sin_numero(serie, i, ds):
        if serie == "SE1":
            del ds.InstanceNumber
    t = correr(app, caso_mezclado(tmp_path, editar=sin_numero), tmp_path)
    assert t["zip"], t["mensaje"]
    m = manifiesto(t["zip"])
    pet = next(k for k in m["orden_de_cortes"] if "_PT_" in k)
    ct = next(k for k in m["orden_de_cortes"] if "_CT_" in k)
    assert m["orden_de_cortes"][pet]["criterio"] == "posición y orientación" and m["orden_de_cortes"][ct]["criterio"] == "InstanceNumber"
    nombres = series_del_zip(t["zip"])[pet]
    zs = [float(dicom_del_zip(t["zip"], f"{pet}/{n}").ImagePositionPatient[2]) for n in nombres]
    assert zs == sorted(zs) and len(set(zs)) == N                     # creciente a lo largo de la normal
    assert "InstanceNumber" not in dicom_del_zip(t["zip"], f"{pet}/{nombres[0]}")   # la numeración no se escribe en el DICOM
    assert next(p for p in t["pasos"] if p["clave"] == "paquete")["estado"] == "ok"


def test_orden_no_determinable_da_aviso_y_nombres_estables(app, tmp_path):
    def ambiguo(serie, i, ds):
        if serie == "SE1":
            ds.InstanceNumber = 1                                     # repetido en toda la serie
            ds.ImagePositionPatient = [-124.0, -124.0, 0.0]           # y la misma posición
    c = caso_mezclado(tmp_path, editar=ambiguo)
    t = correr(app, c, tmp_path, "uno")
    assert t["zip"], t["mensaje"]
    paquete = next(p for p in t["pasos"] if p["clave"] == "paquete")
    assert paquete["estado"] == "aviso" and "NO verificado" in paquete["resumen"] and t["estado"] == "con avisos"
    m = manifiesto(t["zip"])
    pet = next(k for k in m["orden_de_cortes"] if "_PT_" in k)
    assert m["orden_de_cortes"][pet]["criterio"] == "desempate determinista"
    assert "no un orden anatómico verificado" in m["orden_de_cortes"][pet]["aviso"]
    t2 = correr(app, c, tmp_path, "dos")                              # el desempate es determinista: mismos nombres
    assert series_del_zip(t2["zip"]) == series_del_zip(t["zip"])


def test_pendiente_y_revisado_llevan_los_mismos_nombres_aunque_se_importe_mas(app, tmp_path):
    destino = tmp_path / "destino"
    destino.mkdir()
    ta = correr(app, caso_mezclado(tmp_path, "A", 1), destino, "A")
    pendiente = series_del_zip(ta["zip"])
    tb = correr(app, caso_mezclado(tmp_path, "B", 2), destino, "B")   # otro estudio del mismo paciente, importado después
    assert tb["pacientes"] == ta["pacientes"]
    r = app.aplicar_revision_rapido(ta, "Prueba")
    revisado = series_del_zip(r["zip"])
    assert revisado == pendiente                                      # mismos nombres, mismo orden, mismas series
    assert sum(len(v) for v in revisado.values()) == 2 * N            # ni un archivo de la importación posterior
    assert [e["ruta"] for e in manifiesto(r["zip"])["archivos"]] == sorted(f"{k}/{n}" for k, v in revisado.items() for n in v)


def test_serie_retirada_de_la_carpeta_no_entra_y_lo_que_queda_se_numera_desde_uno(app, tmp_path):
    c = caso_mezclado(tmp_path)
    t1 = correr(app, c, tmp_path, "uno")
    assert sum(len(v) for v in series_del_zip(t1["zip"]).values()) == 2 * N
    for f in (c / "SE0").iterdir():                                   # se retiran los CT originales
        f.unlink()
    t2 = correr(app, c, tmp_path, "dos")
    series = series_del_zip(t2["zip"])
    assert list(series) == [k for k in series if "_PT_" in k] and len(series) == 1      # solo la serie PET
    assert [n[:13] for n in next(iter(series.values()))] == [f"corte_{k:06d}_" for k in range(1, N + 1)]
    assert t2["otros_archivos_del_paciente"] == {t2["pacientes"][0]: N}                  # los CT guardados quedan fuera


def test_empaquetado_clasico_usa_el_mismo_mecanismo(app, tmp_path):
    t = correr(app, caso_mezclado(tmp_path), tmp_path)
    r = app.crear_paquete(t["pacientes"])                             # lo que hace la pestaña Enviar
    assert series_del_zip(r["ruta"]) == series_del_zip(t["zip"])      # mismos nombres que el modo rápido
    assert r["orden_sin_verificar"] == [] and set(r["exportacion"]) == set(t["archivos"])


def test_el_visor_y_la_exportacion_usan_el_mismo_orden(app, tmp_path):
    t = correr(app, caso_mezclado(tmp_path), tmp_path)
    limpios = (app.TRABAJO / "limpios").resolve()
    app.indexar()
    for s in app.INDICE.values():
        visor = [Path(ruta).resolve().relative_to(limpios).as_posix() for _, ruta in s["cortes"]]
        exportados = [t["exportacion"][rel] for rel in visor]
        assert exportados == sorted(exportados)                       # el corte k del visor es corte_00000k en el zip
        assert s["orden_criterio"] == "InstanceNumber" and s["orden_aviso"] is None


# ---------------------------------------------------------------------------------------------------------------
# revisión del 11 de noviembre de 2026, quinta ronda
# ---------------------------------------------------------------------------------------------------------------

cotejar = cargar("cotejar", RAIZ / "herramientas" / "cotejar.py")


def cotejo(app, origen: Path, lista: list[str] | None = None) -> tuple[int, dict]:
    """Ejecuta el cotejo como herramienta, con o sin conjunto exacto; devuelve (código, informe)."""
    informe = app.TRABAJO / "cotejo.json"
    args = [str(origen), str(app.TRABAJO / "limpios"), "--mapa", str(app.TRABAJO / "limpios_PRIVADO" / "mapa_seudonimos.json"), "--json", str(informe)]
    if lista is not None:
        f = app.TRABAJO / "lista.json"
        f.write_text(json.dumps({"archivos": lista}), encoding="utf-8")
        args += ["--solo-lista", str(f)]
    codigo = cotejar.main(args)
    return codigo, json.loads(informe.read_text(encoding="utf-8"))


def retirar_parte_de_la_serie_pet(c: Path, cuantos: int) -> None:
    for f in sorted((c / "SE1").iterdir())[:cuantos]:
        f.unlink()


def test_retirar_parte_de_una_serie_no_hace_fallar_el_cotejo_del_modo_rapido(app, tmp_path):
    """Reproducción de la revisión: caso completo importado; después se retiran seis archivos de la serie PET."""
    c = caso_mezclado(tmp_path)
    t1 = correr(app, c, tmp_path, "uno")
    assert len(t1["archivos"]) == 2 * N
    retirar_parte_de_la_serie_pet(c, 6)
    t2 = correr(app, c, tmp_path, "dos")
    assert t2["estado"] == "ok", t2["mensaje"]
    paso_cotejo = next(p for p in t2["pasos"] if p["clave"] == "cotejo")
    assert paso_cotejo["estado"] == "ok" and "coinciden byte a byte: 18" in paso_cotejo["resumen"]
    assert "conjunto exacto de 18 archivo(s)" in paso_cotejo["salida"] and "fuera del conjunto, sin examinar: 6" in paso_cotejo["salida"]
    series = series_del_zip(t2["zip"])
    pet = next(k for k in series if "_PT_" in k)
    ct = next(k for k in series if "_CT_" in k)
    assert len(series[ct]) == N and len(series[pet]) == 6             # la serie CT entera y la mitad que queda de la PET
    assert [n[:13] for n in series[pet]] == [f"corte_{k:06d}_" for k in range(1, 7)]
    assert set(t2["archivos"]) < set(t1["archivos"]) and t2["otros_archivos_del_paciente"] == {t2["pacientes"][0]: 6}


def test_el_cotejo_general_sigue_siendo_estricto_con_la_serie_entera(app, tmp_path):
    c = caso_mezclado(tmp_path)
    t = correr(app, c, tmp_path, "uno")
    retirar_parte_de_la_serie_pet(c, 6)
    codigo, informe = cotejo(app, c)                                  # sin conjunto: lo que hace "Cotejar con el original"
    assert codigo == 1 and informe["veredicto"] == "DIFERENCIAS" and informe["conteo"]["PROBLEMA: archivos limpios sin original"] == 6
    assert informe["conteo"]["coinciden byte a byte"] == 18 and informe["conjunto"] is None
    assert t["zip"]


def test_el_cotejo_de_un_conjunto_es_estricto_en_los_dos_sentidos(app, tmp_path):
    c = caso_mezclado(tmp_path)
    t = correr(app, c, tmp_path, "uno")
    todos = sorted(t["archivos"])
    codigo, informe = cotejo(app, c, todos)
    assert codigo == 0 and informe["veredicto"] == "COINCIDE" and informe["conjunto"] == 2 * N
    # un original presente cuyo archivo limpio no está en el conjunto
    codigo, informe = cotejo(app, c, todos[:-1])
    assert codigo == 1 and informe["conteo"]["PROBLEMA: original cuyo archivo limpio no está en el conjunto"] == 1
    # un archivo del conjunto sin original que lo explique
    retirar_parte_de_la_serie_pet(c, 1)
    codigo, informe = cotejo(app, c, todos)
    assert codigo == 1 and informe["conteo"]["PROBLEMA: archivos del conjunto sin original"] == 1
    # un archivo del conjunto alterado
    c2 = caso_mezclado(tmp_path, "otro", 2)
    t2 = correr(app, c2, tmp_path, "dos")
    victima = app.TRABAJO / "limpios" / sorted(t2["archivos"])[0]
    ds = pydicom.dcmread(victima)
    ds.PatientSex = "M"
    ds.save_as(victima, enforce_file_format=True)
    codigo, informe = cotejo(app, c2, sorted(t2["archivos"]))
    assert codigo == 1 and informe["conteo"]["PROBLEMA: archivo limpio distinto"] == 1


def test_el_criterio_se_evalua_sobre_el_conjunto_recibido(app):
    """Reproducción mínima de la revisión: InstanceNumber [2,2,1,1], posiciones [0,1,2,3]."""
    serie = {"a.dcm": cab(numero=2, z=0.0), "b.dcm": cab(numero=2, z=1.0), "c.dcm": cab(numero=1, z=2.0), "d.dcm": cab(numero=1, z=3.0)}
    completa = ordenar(app, serie)
    assert completa["criterio"] == "posición y orientación" and completa["orden"] == ["a.dcm", "b.dcm", "c.dcm", "d.dcm"]
    parte = ordenar(app, {k: serie[k] for k in ("a.dcm", "c.dcm")})
    assert parte["criterio"] == "InstanceNumber" and parte["orden"] == ["c.dcm", "a.dcm"]
    # Los dos resultados son correctos para su conjunto y son distintos: por eso el orden de un paquete se congela con
    # la corrida y se revisa en el visor de la corrida, no en el visor general de la serie.


def caso_con_numeros_repetidos(tmp_path: Path) -> Path:
    """Serie PET de cuatro cortes con InstanceNumber [2,2,1,1] y posiciones crecientes; CT normal."""
    d = tmp_path / "estudios" / "repetidos"
    demo.escribir_caso(d, 4, 1)
    for i, numero in enumerate([2, 2, 1, 1]):
        f = d / "SE1" / f"IM{i}"
        ds = pydicom.dcmread(f)
        ds.InstanceNumber = numero
        ds.save_as(f, enforce_file_format=True)
    return d


def clave_de(app, rel: str) -> tuple[int, float]:
    ds = pydicom.dcmread(app.TRABAJO / "limpios" / rel, stop_before_pixels=True)
    return int(ds.InstanceNumber), round(float(ds.ImagePositionPatient[2]), 2)


def test_el_visor_de_la_corrida_muestra_sus_archivos_en_el_orden_congelado_del_paquete(app, tmp_path):
    destino = tmp_path / "destino"
    destino.mkdir()
    c = caso_con_numeros_repetidos(tmp_path)
    correr(app, c, destino, "completa")                               # la serie PET entera queda guardada: cuatro cortes
    apartados = tmp_path / "apartados"
    apartados.mkdir()
    for i in (1, 3):                                                  # se retiran el segundo y el cuarto de la carpeta
        (c / "SE1" / f"IM{i}").rename(apartados / f"IM{i}")
    t = correr(app, c, destino, "parte")
    assert t["zip"], t["mensaje"]
    series = app.series_congeladas(t["exportacion"], t["orden_de_cortes"])
    assert series == t["series_corrida"]
    pet = next(i for i, s in enumerate(t["series_corrida"]) if s["modalidad"] == "PT")
    s = t["series_corrida"][pet]
    z0 = -100.0
    # el paquete: dos cortes, ordenados por InstanceNumber (1 y luego 2); es decir, el tercero de la serie y luego el primero
    assert s["n"] == 2 and s["criterio"] == "InstanceNumber"
    assert [clave_de(app, rel) for rel in s["archivos"]] == [(1, round(z0 + 2 * 3.27, 2)), (2, round(z0, 2))]
    assert [n[:13] for n in s["nombres"]] == ["corte_000001_", "corte_000002_"]
    # el visor de la corrida muestra exactamente esos archivos en ese orden
    limpios = app.TRABAJO / "limpios"
    for k, rel in enumerate(s["archivos"], 1):
        assert app.png_de_corrida(t, pet, k, "auto") == app.render_corte(str(limpios / rel), "auto")
    assert app.png_de_corrida(t, pet, 1, "auto") != app.png_de_corrida(t, pet, 2, "auto")
    with pytest.raises(Exception) as fuera:
        app.png_de_corrida(t, pet, 3, "auto")
    assert fuera.value.status_code == 404
    # el visor general de Estudios ve la serie guardada entera y la ordena por posición: otro conjunto, otro criterio
    app.indexar()
    general = next(x for x in app.INDICE.values() if x["modalidad"] == "PT")
    assert general["n"] == 4 and general["orden_criterio"] == "posición y orientación"
    # una importación posterior del mismo paciente no cambia ni la lista ni el orden de la corrida
    antes = [dict(x) for x in t["series_corrida"]]
    pendiente = series_del_zip(t["zip"])
    for i in (1, 3):
        (apartados / f"IM{i}").rename(c / "SE1" / f"IM{i}")
    correr(app, c, destino, "otra-vez")
    assert t["series_corrida"] == antes
    assert app.png_de_corrida(t, pet, 1, "auto") == app.render_corte(str(limpios / s["archivos"][0]), "auto")
    # y el zip revisado lleva los mismos nombres, en el mismo orden, que el pendiente
    r = app.aplicar_revision_rapido(t, "Prueba")
    assert series_del_zip(r["zip"]) == pendiente
    with zipfile.ZipFile(r["zip"]) as z:
        en_zip = [clave_de_bytes(z.read(f"{s['serie']}/{n}")) for n in s["nombres"]]
    assert en_zip == [(1, round(z0 + 2 * 3.27, 2)), (2, round(z0, 2))]


def clave_de_bytes(datos: bytes) -> tuple[int, float]:
    ds = pydicom.dcmread(io.BytesIO(datos), stop_before_pixels=True)
    return int(ds.InstanceNumber), round(float(ds.ImagePositionPatient[2]), 2)


def test_el_visor_de_la_corrida_no_muestra_un_archivo_que_cambio(app, tmp_path):
    t = correr(app, caso_mezclado(tmp_path), tmp_path)
    s = t["series_corrida"][0]
    victima = app.TRABAJO / "limpios" / s["archivos"][0]
    assert app.png_de_corrida(t, 0, 1, "tejido")
    victima.write_bytes(victima.read_bytes() + b"\x00\x00")
    with pytest.raises(Exception) as e:
        app.png_de_corrida(t, 0, 1, "tejido")
    assert e.value.status_code == 409 and "cambió desde la corrida" in e.value.detail
    assert app.png_de_corrida(t, 0, 2, "tejido")                      # los demás siguen a la vista


# ---------------------------------------------------------------------------------------------------------------
# revisión del 11 de noviembre de 2026, sexta ronda: lo que el visor de la corrida muestra son los bytes verificados
# ---------------------------------------------------------------------------------------------------------------

def con_pixeles_invertidos(datos: bytes) -> bytes:
    """El mismo DICOM con otros píxeles: una imagen visiblemente distinta."""
    ds = pydicom.dcmread(io.BytesIO(datos))
    ds.PixelData = bytes(255 - b for b in ds.PixelData)
    salida = io.BytesIO()
    ds.save_as(salida, enforce_file_format=True)
    return salida.getvalue()


def test_el_visor_muestra_los_bytes_verificados_aunque_el_archivo_cambie_antes_de_dibujar(app, tmp_path, monkeypatch):
    """Carrera entre la verificación y el dibujo: el archivo cambia en disco después de leerse los bytes verificados y
    antes de decodificar la imagen. Debe mostrarse la imagen verificada, nunca la modificada."""
    t = correr(app, caso_mezclado(tmp_path), tmp_path)
    rel = t["series_corrida"][0]["archivos"][0]
    victima = app.TRABAJO / "limpios" / rel
    originales = victima.read_bytes()
    modificados = con_pixeles_invertidos(originales)
    def imagen_de(datos: bytes) -> bytes:                             # referencia independiente: un archivo aparte, dibujado desde disco
        f = tmp_path / "referencia.dcm"
        f.write_bytes(datos)
        return app.render_corte(str(f), "tejido")

    verificada, modificada = imagen_de(originales), imagen_de(modificados)
    assert verificada != modificada
    real = app.pydicom.dcmread
    hecho = {"veces": 0}

    def decodificar_tras_el_cambio(origen, *a, **k):
        # El punto de inyección es la decodificación de la imagen, que ocurre después de cualquier verificación.
        if hecho["veces"] == 0:
            victima.write_bytes(modificados)                          # el archivo cambia en disco justo ahora
        hecho["veces"] += 1
        return real(origen, *a, **k)

    monkeypatch.setattr(app.pydicom, "dcmread", decodificar_tras_el_cambio)
    mostrado = app.png_de_corrida(t, 0, 1, "tejido")
    monkeypatch.setattr(app.pydicom, "dcmread", real)
    assert hecho["veces"] == 1 and victima.read_bytes() == modificados   # el cambio ocurrió, y antes de dibujar
    assert mostrado == verificada and mostrado != modificada              # se mostró lo verificado
    with pytest.raises(Exception) as e:                                   # y la siguiente petición ya ve el cambio
        app.png_de_corrida(t, 0, 1, "tejido")
    assert e.value.status_code == 409 and "cambió desde la corrida" in e.value.detail


def test_el_visor_de_la_corrida_lee_cada_archivo_una_sola_vez(app, tmp_path, monkeypatch):
    t = correr(app, caso_mezclado(tmp_path), tmp_path)
    victima = (app.TRABAJO / "limpios" / t["series_corrida"][0]["archivos"][0]).resolve()
    lecturas = []
    real_read_bytes, real_open = Path.read_bytes, app.Path.open

    def contar_read_bytes(self, *a, **k):
        if self.resolve() == victima:
            lecturas.append("read_bytes")
        return real_read_bytes(self, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", contar_read_bytes)
    monkeypatch.setattr(app, "sha256_archivo", lambda ruta: (_ for _ in ()).throw(AssertionError("el visor no debe volver a leer la ruta para el hash")))
    monkeypatch.setattr(app, "render_corte", lambda *a, **k: (_ for _ in ()).throw(AssertionError("el visor no debe volver a abrir la ruta para dibujar")))
    assert app.png_de_corrida(t, 0, 1, "tejido")[:8] == b"\x89PNG\r\n\x1a\n"
    assert lecturas == ["read_bytes"]


def test_el_visor_rechaza_un_archivo_que_ya_no_esta(app, tmp_path):
    t = correr(app, caso_mezclado(tmp_path), tmp_path)
    (app.TRABAJO / "limpios" / t["series_corrida"][0]["archivos"][0]).unlink()
    with pytest.raises(Exception) as e:
        app.png_de_corrida(t, 0, 1, "tejido")
    assert e.value.status_code == 409


# ---------------------------------------------------------------------------------------------------------------
# revisión del 11 de noviembre de 2026, séptima ronda
# ---------------------------------------------------------------------------------------------------------------

ENHANCED_CT = "1.2.840.10008.5.1.4.1.1.2.1"


def caso_con_archivo_de_dos_cuadros(tmp_path: Path) -> Path:
    """Caso sintético de 4 + 4 cortes más una serie con un solo archivo Enhanced CT de dos cuadros distintos."""
    d = tmp_path / "estudios" / "multicuadro"
    demo.escribir_caso(d, 4, 1)
    ds = pydicom.dcmread(d / "SE0" / "IM0")
    ds.SOPClassUID = ENHANCED_CT
    ds.file_meta.MediaStorageSOPClassUID = ENHANCED_CT
    ds.SOPInstanceUID = pydicom.uid.generate_uid()
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.SeriesInstanceUID = pydicom.uid.generate_uid()
    ds.SeriesNumber = 7
    ds.NumberOfFrames = 2
    primero = ds.PixelData
    ds.PixelData = primero + bytes(255 - b for b in primero)          # el segundo cuadro es visiblemente distinto
    (d / "SE2").mkdir()
    ds.save_as(d / "SE2" / "MF0", enforce_file_format=True)
    return d


def test_un_archivo_con_varios_cuadros_se_puede_recorrer_en_el_visor_de_la_corrida(app, tmp_path):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso_con_archivo_de_dos_cuadros(tmp_path), destino)
    assert t["zip"], t["mensaje"]
    indice = next(i for i, s in enumerate(t["series_corrida"]) if s["cuadros"] == [2])
    assert all(s["cuadros"] == [1] * s["n"] for i, s in enumerate(t["series_corrida"]) if i != indice)
    uno, dos = app.png_de_corrida(t, indice, 1, "tejido", 1), app.png_de_corrida(t, indice, 1, "tejido", 2)
    assert uno[:8] == dos[:8] == b"\x89PNG\r\n\x1a\n" and uno != dos                   # los dos cuadros se dibujan, y son distintos
    with pytest.raises(Exception) as fuera:
        app.png_de_corrida(t, indice, 1, "tejido", 3)
    assert fuera.value.status_code == 404
    assert t["no_mostrables"] == [] and "varios cuadros" in next(p for p in t["pasos"] if p["clave"] == "paquete")["resumen"]
    r = app.aplicar_revision_rapido(t, "Prueba")                                          # todo se pudo ver: la revisión se registra
    assert Path(r["zip"]).parent == destino
    app.indexar()
    assert max(s["cuadros_max"] for s in app.INDICE.values()) == 2


def test_una_imagen_que_el_visor_no_puede_dibujar_bloquea_la_revision(app, tmp_path, monkeypatch):
    destino = tmp_path / "destino"
    destino.mkdir()
    real = app.render_bytes

    def sin_soporte_para_varios_cuadros(datos, ventana, cuadro=1):
        if int(pydicom.dcmread(io.BytesIO(datos), stop_before_pixels=True).get("NumberOfFrames", 1) or 1) > 1:
            raise app.ImagenNoMostrable("formato de imagen no soportado")
        return real(datos, ventana, cuadro)

    monkeypatch.setattr(app, "render_bytes", sin_soporte_para_varios_cuadros)
    t = correr(app, caso_con_archivo_de_dos_cuadros(tmp_path), destino)
    assert t["zip"] and t["estado"] == "con avisos"                                       # el zip pendiente existe, en local
    assert len(t["no_mostrables"]) >= 1 and t["no_mostrables"][0]["motivo"] == "formato de imagen no soportado"
    paquete = next(p for p in t["pasos"] if p["clave"] == "paquete")
    assert paquete["estado"] == "aviso" and "no se pueden mostrar en el visor" in paquete["resumen"]
    indice = next(i for i, s in enumerate(t["series_corrida"]) if s["cuadros"] == [2])
    with pytest.raises(Exception) as e:
        app.png_de_corrida(t, indice, 1, "tejido", 1)
    assert e.value.status_code == 422 and "no se puede mostrar" in e.value.detail
    with pytest.raises(Exception) as e:                                                   # sin verla, no hay revisión
        app.aplicar_revision_rapido(t, "Prueba")
    assert e.value.status_code == 409 and "no se puede registrar una revisión de píxeles sin verlas" in e.value.detail
    assert list(destino.iterdir()) == [] and t["revisado"] is False and Path(t["zip"]).exists()


def test_un_fallo_al_dibujar_durante_la_revision_tambien_la_bloquea(app, tmp_path, monkeypatch):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso_mezclado(tmp_path), destino)
    assert t["no_mostrables"] == []

    def rota(datos, ventana, cuadro=1):
        raise app.ImagenNoMostrable("no se pudieron decodificar los píxeles (ValueError)")

    monkeypatch.setattr(app, "render_bytes", rota)                                        # el corte 7 no se deja dibujar
    with pytest.raises(Exception) as e:
        app.png_de_corrida(t, 0, 7, "tejido")
    assert e.value.status_code == 422
    assert [x["archivo"] for x in t["no_mostrables"]] == [t["series_corrida"][0]["nombres"][6]]
    with pytest.raises(Exception) as e:
        app.aplicar_revision_rapido(t, "Prueba")
    assert e.value.status_code == 409 and list(destino.iterdir()) == []


def test_un_pet_en_cuarentena_no_cuenta_como_faltante_en_el_suv_de_la_corrida(app, tmp_path):
    """Un corte PET con BurnedInAnnotation=YES va a cuarentena. Los demás forman la corrida; su comparación de SUV no
    debe tomar el corte en cuarentena por un original sin archivo limpio."""
    def uno_con_texto_grabado(serie, i, ds):
        if serie == "SE1" and i == 3:
            ds.BurnedInAnnotation = "YES"
    c = caso_mezclado(tmp_path, editar=uno_con_texto_grabado)
    t = correr(app, c, tmp_path)
    pasos = {p["clave"]: p for p in t["pasos"]}
    assert pasos["limpieza"]["estado"] == "aviso" and "1 en cuarentena" in pasos["limpieza"]["resumen"]
    assert pasos["cotejo"]["estado"] == "ok" and "coinciden byte a byte: 23" in pasos["cotejo"]["resumen"]
    assert pasos["suv"]["estado"] == "ok" and "IDENTICO" in pasos["suv"]["resumen"], pasos["suv"]["resumen"]
    assert len(t["archivos"]) == 2 * N - 1 and t["zip"]
    # sin conjunto, la comparación general sigue viendo la serie original entera y marca el faltante
    suv = cargar("suv_herramienta", RAIZ / "herramientas" / "suv.py")
    serie_pet = next(app.TRABAJO / "limpios" / s["serie"] for s in t["series_corrida"] if s["modalidad"] == "PT")
    informe = app.TRABAJO / "suv_general.json"
    codigo = suv.main(["comparar", str(serie_pet), "--privado", str(app.TRABAJO / "limpios_PRIVADO"), "--json", str(informe)])
    r = json.loads(informe.read_text(encoding="utf-8"))
    assert codigo == 1 and r["correspondencia_completa"] is False and r["cortes_serie_original"] == N
