"""
La situación de una corrida del modo rápido, como datos. / The state of a Rápido run, as data.

La pantalla de la v0.10.0 separa procesamiento, revisión de píxeles y paquete, y vuelve a preguntar por ellos. Aquí se
comprueba lo que el servidor contesta: las cifras y la clase de cada paso, y la situación calculada en cada consulta con
lo que hay en el disco. Nada de esto decide: las decisiones siguen siendo las de siempre y sus pruebas no cambian.
"""
from __future__ import annotations

import importlib.util
import json
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


@pytest.fixture
def app(tmp_path):
    a = cargar("app_en_prueba", RAIZ / "herramientas" / "app.py")
    a.TRABAJO = tmp_path / "datos-pet"
    a.TRABAJO.mkdir()
    return a


def correr(app, origen: Path, destino: Path, tid: str = "c") -> dict:
    destino.mkdir(parents=True, exist_ok=True)
    assert app.CANDADO.acquire(blocking=False)
    app.TRABAJOS[tid] = {"nombre": "rapido", "estado": "corriendo", "lineas": [], "codigo": None, "pasos": [], "pacientes": [],
                         "origen": str(origen), "destino": str(destino), "zip": None, "mensaje": "", "archivos": {},
                         "revisado": False, "servidos": set(), "mostrados": set()}
    app._rapido(tid, origen, destino, "ANON")
    return app.TRABAJOS[tid]


def caso(tmp_path: Path, nombre: str = "A", cortes: int = 4) -> Path:
    d = tmp_path / "estudios" / nombre
    demo.escribir_caso(d, cortes, 1)
    return d


def mostrar_todo(app, t: dict) -> None:
    for i, s in enumerate(t["series_corrida"]):
        for k, n in enumerate(s["cuadros"], 1):
            t["servidos"].add((i, k, 1))
            app.anotar_mostrado_corrida(t, i, k, 1)


def datos(t: dict, clave: str) -> dict:
    return next(p for p in t["pasos"] if p["clave"] == clave)["datos"]


# ---- las cifras y la clase de cada paso ----

def test_cada_paso_lleva_sus_cifras_y_los_no_dicom_se_cuentan_aparte(app, tmp_path):
    origen = caso(tmp_path)
    (origen / "LEEME.txt").write_text("no soy una imagen")
    (origen / "informe.pdf").write_bytes(b"%PDF-1.4")
    t = correr(app, origen, tmp_path / "destino")
    limpieza = datos(t, "limpieza")
    assert (limpieza["leidos"], limpieza["en_la_corrida"], limpieza["omitidos_total"], limpieza["cuarentena_total"], limpieza["pacientes"]) == (10, 8, 2, 0, 1)
    assert datos(t, "compuerta") == {"comprobados": 8, "con_problemas": 0}
    assert datos(t, "cotejo") == {"clase": "coincide", "coinciden": 8, "esperados": 8}
    assert datos(t, "emparejamiento") == {"estudios": 1, "compatibles": 1, "inconsistentes": 0, "no_evaluables": 0}
    assert datos(t, "suv") == {"series": 1, "clases": {"identico": 1}}
    paquete = datos(t, "paquete")
    assert (paquete["clase"], paquete["archivos"], paquete["series"], paquete["no_mostrables"]) == ("creado", 8, 2, 0)
    assert t["versiones"] == {"aplicacion": app.VERSION, "politica": app.ANON.POLITICA}
    texto = json.dumps([p["datos"] for p in t["pasos"]], ensure_ascii=False)
    assert "LEEME" not in texto and "informe.pdf" not in texto and str(origen) not in texto      # cifras y motivos; ningún nombre de archivo


def test_un_dicom_en_cuarentena_no_se_cuenta_como_un_archivo_omitido(app, tmp_path):
    origen = caso(tmp_path)
    victima = sorted(origen.rglob("IM*"))[0]
    ds = pydicom.dcmread(victima)
    ds.Modality = "US"                                                # una modalidad que la política no admite
    ds.save_as(victima, enforce_file_format=True)
    t = correr(app, origen, tmp_path / "destino")
    limpieza = datos(t, "limpieza")
    assert limpieza["cuarentena_total"] == 1 and limpieza["omitidos_total"] == 0 and limpieza["en_la_corrida"] == 7
    assert sum(limpieza["cuarentena_motivos"].values()) == 1
    assert next(p for p in t["pasos"] if p["clave"] == "limpieza")["estado"] == "aviso"     # la decisión es la de siempre


def test_un_estudio_sin_pet_no_bloquea_y_no_tiene_suv_que_comparar(app, tmp_path):
    """Una carpeta con solo la serie CT. No hay SUV que comparar y el paquete se crea.
    Hallazgo conocido, documentado en docs/CAMBIOS.md (0.10.0, fila 45): la aplicación clasifica hoy este caso como par
    inconsistente, cuando no hay par que comparar. Esta prueba fija lo que no debe cambiar: no bloquea y no inventa un SUV."""
    origen = caso(tmp_path)
    for f in origen.rglob("IM*"):
        if str(pydicom.dcmread(f, stop_before_pixels=True).Modality) == "PT":
            f.unlink()
    t = correr(app, origen, tmp_path / "destino")
    emparejamiento = datos(t, "emparejamiento")
    assert emparejamiento["estudios"] == 1 and emparejamiento["compatibles"] == 0
    assert emparejamiento["inconsistentes"] + emparejamiento["no_evaluables"] == 1
    assert datos(t, "suv") == {"series": 0, "clases": {}}
    assert t["zip"] and t["estado"] in ("ok", "con avisos")           # no bloquea


def test_la_clase_del_suv_distingue_no_calculable_de_discrepancia(app):
    clase = lambda *a: app.clasificar_suv_con_clase(*a)[2]
    assert clase(0, {"veredicto": "IDENTICO"}, "") == "identico"
    assert clase(1, {"voxeles_distintos": 12}, "") == "discrepancia"
    assert clase(2, {"detalles": ["SUVbw no calculable: falta PatientWeight"]}, "") == "no_calculable"
    assert clase(2, {"correspondencia_completa": False, "problemas_de_correspondencia": ["falta un corte"]}, "") == "correspondencia_incompleta"
    assert clase(2, {"orden_y_geometria_ok": False}, "") == "orden_incoherente"
    assert clase(2, {}, "") == "sin_veredicto"
    for args in ((0, {"veredicto": "IDENTICO"}, ""), (1, {"voxeles_distintos": 12}, ""), (2, {"detalles": ["SUVbw no calculable: falta PatientWeight"]}, "")):
        assert app.clasificar_suv(*args) == app.clasificar_suv_con_clase(*args)[:2]          # la clasificación de siempre no cambia


# ---- la situación: tres partes, calculadas en cada consulta ----

def test_la_situacion_separa_procesamiento_revision_y_paquete(app, tmp_path):
    t = correr(app, caso(tmp_path), tmp_path / "destino")
    s = app.situacion_de_corrida(t)
    assert s["procesamiento"] == {"estado": "terminado", "paso_en_curso": None, "paso_detenido": None, "correctos": 6, "con_aviso": 0, "no_evaluados": 0, "total": 6}
    assert s["revision"]["estado"] == "pendiente" and (s["revision"]["cuadros_mostrados"], s["revision"]["cuadros_totales"]) == (0, 8)
    assert s["paquete"]["estado"] == "pendiente_local" and s["paquete"]["existe"] is True and s["paquete"]["con_revision_registrada"] is False
    assert s["versiones"]["aplicacion"] == app.VERSION

    mostrar_todo(app, t)
    assert app.situacion_de_corrida(t)["revision"]["estado"] == "pendiente"       # mostrarlo todo no es revisarlo
    app.aplicar_revision_rapido(t, "Revisora de Prueba")
    s = app.situacion_de_corrida(t)
    assert s["revision"]["estado"] == "registrada" and s["revision"]["revisor"] == "Revisora de Prueba" and s["revision"]["mostrado"]["cuadros_mostrados"] == 8
    assert s["paquete"]["estado"] == "publicado" and s["paquete"]["acciones_pendientes"] == [] and Path(s["paquete"]["ruta"]).parent == tmp_path / "destino"

    app.retirar_revision("corrida", s["revision"]["registro"], "Arturo", "retirada de prueba")
    s = app.situacion_de_corrida(t)
    assert s["revision"]["estado"] == "retirada" and s["revision"]["retirada"]["quien"] == "Arturo" and s["revision"]["retirada"]["motivo"] == "retirada de prueba"
    assert s["paquete"]["estado"] == "publicado" and s["procesamiento"]["estado"] == "terminado"      # el zip sigue ahí; el proceso fue el que fue


def test_un_diario_ilegible_deja_la_revision_como_no_verificable_y_vuelve_al_leerse(app, tmp_path):
    t = correr(app, caso(tmp_path), tmp_path / "destino")
    mostrar_todo(app, t)
    app.aplicar_revision_rapido(t, "Ana")
    t2 = correr(app, caso(tmp_path, "B"), tmp_path / "destino-b", "d")
    mostrar_todo(app, t2)
    app.aplicar_revision_rapido(t2, "Ana")
    app.retirar_revision("corrida", app.situacion_de_corrida(t2)["revision"]["registro"], "Arturo", "otra corrida")
    diario = next((app.TRABAJO / "limpios_REPORTES" / "retiradas").glob("retirada_*.json"))
    bueno = diario.read_bytes()
    diario.write_bytes(bueno[:30])                                    # el diario de otra retirada deja de poder leerse
    s = app.situacion_de_corrida(t)
    assert s["revision"]["estado"] == "no_verificable" and s["revision"]["habia_registro"] is True
    assert s["revision"]["problemas"][0]["diario"] == diario.name
    assert s["paquete"]["estado"] == "publicado"                      # el zip no cambia; lo que no se sabe es si su revisión vale
    diario.write_bytes(bueno)
    assert app.situacion_de_corrida(t)["revision"]["estado"] == "registrada"


def test_si_el_registro_o_el_zip_ya_no_estan_no_se_dan_por_buenos(app, tmp_path):
    t = correr(app, caso(tmp_path), tmp_path / "destino")
    mostrar_todo(app, t)
    app.aplicar_revision_rapido(t, "Ana")
    registro = Path(t["registro_revision"])
    guardado = registro.read_bytes()
    registro.unlink()
    assert app.situacion_de_corrida(t)["revision"]["estado"] == "registro_no_legible"
    registro.write_bytes(guardado)
    Path(t["zip"]).unlink()
    s = app.situacion_de_corrida(t)
    assert s["revision"]["estado"] == "registrada" and s["paquete"]["estado"] == "no_encontrado" and s["paquete"]["existe"] is False


def test_lo_que_falla_despues_de_publicar_queda_como_accion_pendiente_y_el_paquete_sigue_publicado(app, tmp_path, monkeypatch):
    import re
    t = correr(app, caso(tmp_path), tmp_path / "destino")
    mostrar_todo(app, t)
    real = app.os.replace

    def sin_confirmar(a, b):
        if re.fullmatch(r"revision_rapido_\d{8}_\d{6}_[0-9a-f]{4}\.json", Path(b).name):
            raise OSError("sin permiso")
        return real(a, b)

    monkeypatch.setattr(app.os, "replace", sin_confirmar)
    app.aplicar_revision_rapido(t, "Ana")
    s = app.situacion_de_corrida(t)
    assert s["paquete"]["estado"] == "publicado" and s["revision"]["estado"] == "registrada" and s["revision"]["registro_provisional"] is True
    assert [a["clase"] for a in s["paquete"]["acciones_pendientes"]] == ["evidencia"] and "nombre provisional" in s["paquete"]["acciones_pendientes"][0]["texto"]


def test_un_intento_de_publicar_que_falla_queda_anotado_y_no_publica(app, tmp_path, monkeypatch):
    from fastapi.responses import JSONResponse
    t = correr(app, caso(tmp_path), tmp_path / "destino")
    mostrar_todo(app, t)

    def falla(*a, **k):
        raise app.PublicacionFallida(500, "no se pudo escribir en el destino.", ["/ruta/de/un/resto"])

    monkeypatch.setattr(app, "aplicar_revision_rapido", falla)
    monkeypatch.setattr(app, "auth", lambda request: None)
    respuesta = app.rapido_revision(None, {"trabajo": "c", "revisor": "Ana"})
    assert isinstance(respuesta, JSONResponse) and respuesta.status_code == 500
    s = app.situacion_de_corrida(t)
    assert s["paquete"]["estado"] == "pendiente_local" and s["revision"]["estado"] == "pendiente"
    assert s["paquete"]["ultimo_intento_fallido"]["detalle"] == "no se pudo escribir en el destino." and s["paquete"]["ultimo_intento_fallido"]["restos"] == ["/ruta/de/un/resto"]


def test_una_corrida_detenida_no_tiene_ni_revision_ni_paquete(app, tmp_path):
    vacia = tmp_path / "estudios" / "vacia"
    vacia.mkdir(parents=True)
    (vacia / "notas.txt").write_text("nada que anonimizar")
    t = correr(app, vacia, tmp_path / "destino")
    s = app.situacion_de_corrida(t)
    assert s["procesamiento"]["estado"] == "detenido" and s["revision"] == {"estado": "sin_paquete"} and s["paquete"] == {"estado": "no_creado"}
