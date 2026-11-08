"""
Reglas del registro de revisión de píxeles de la aplicación. / Pixel-review record rules of the app.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1] / "herramientas"
spec = importlib.util.spec_from_file_location("app_local", RAIZ / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

ARCHIVOS = {"a.dcm": "0" * 64, "b.dcm": "1" * 64}


def test_sin_hallazgos_exige_lista_vacia_y_revisor():
    assert app.validar_registro_revision("sin_hallazgos", ["rostro"], "Arturo") is not None
    assert app.validar_registro_revision("sin_hallazgos", [], "") is not None
    assert app.validar_registro_revision("sin_hallazgos", [], "Arturo") is None


def test_con_hallazgos_exige_al_menos_uno_valido():
    assert app.validar_registro_revision("con_hallazgos", [], "Arturo") is not None
    assert app.validar_registro_revision("con_hallazgos", ["inventado"], "Arturo") is not None
    assert app.validar_registro_revision("con_hallazgos", ["texto_grabado"], "Arturo") is None


def test_registro_contradictorio_se_lee_como_pendiente():
    r = {"estado": "sin_hallazgos", "hallazgos": ["rostro"], "revisor": "Arturo", "archivos": ARCHIVOS}
    e = app.evaluar_registro_revision(r, ARCHIVOS)
    assert e["estado"] == "pendiente" and e["lista_para_compartir"] is False


def test_hallazgos_registrados_nunca_estan_listos_para_compartir():
    r = {"estado": "con_hallazgos", "hallazgos": ["texto_grabado"], "revisor": "Arturo", "archivos": ARCHIVOS}
    e = app.evaluar_registro_revision(r, ARCHIVOS)
    assert e["vigente"] is True and e["lista_para_compartir"] is False


def test_archivos_cambiados_invalidan_la_revision():
    r = {"estado": "sin_hallazgos", "hallazgos": [], "revisor": "Arturo", "archivos": ARCHIVOS}
    assert app.evaluar_registro_revision(r, ARCHIVOS)["lista_para_compartir"] is True
    cambiados = dict(ARCHIVOS, **{"a.dcm": "f" * 64})
    e = app.evaluar_registro_revision(r, cambiados)
    assert e["estado"] == "pendiente" and e["lista_para_compartir"] is False
