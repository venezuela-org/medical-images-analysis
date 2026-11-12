"""
Modo rápido, en proceso: los casos que no se pueden provocar desde fuera con fiabilidad.
Express mode, in process: the cases that cannot be reliably provoked from outside.

- Un archivo que cambia entre la comprobación previa y el empaquetado: el sello se rechaza y el destino queda vacío.
- El sello mientras otro trabajo tiene el candado.
- El zip pendiente que no se puede eliminar: se informa su ruta.
- El texto del manifiesto de un zip revisado habla de los archivos del paquete, no de "todos los pacientes".
- Un archivo repetido dentro de la carpeta de entrada.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException

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


def correr(app, origen: Path, destino: Path, mostrar: bool = True) -> dict:
    """Lo mismo que hace POST /api/rapido, sin servidor y de forma síncrona."""
    assert app.CANDADO.acquire(blocking=False)
    tid = "prueba"
    app.TRABAJOS[tid] = {"nombre": "rapido", "estado": "corriendo", "lineas": [], "codigo": None, "pasos": [], "pacientes": [],
                         "origen": str(origen), "destino": str(app.validar_destino(destino, origen)), "zip": None, "mensaje": "",
                         "archivos": {}, "revisado": False}
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



def caso(tmp_path: Path, nombre: str = "A") -> Path:
    d = tmp_path / "estudios" / nombre
    demo.escribir_caso(d, 4, 1)
    return d


def test_cambio_entre_la_comprobacion_y_el_empaquetado_no_escribe_en_el_destino(app, tmp_path, monkeypatch):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso(tmp_path), destino)
    assert t["zip"] and len(t["archivos"]) == 8
    rel, congelado = next(iter(t["archivos"].items()))
    victima = app.TRABAJO / "limpios" / rel
    victima.write_bytes(victima.read_bytes() + b"\x00\x00")          # el archivo cambia en disco...
    real = app.sha256_archivo
    monkeypatch.setattr(app, "sha256_archivo", lambda p: congelado if Path(p).resolve() == victima.resolve() else real(p))
    # ...pero la comprobación previa "ve" el hash congelado: es la ventana entre comprobar y empaquetar
    with pytest.raises(app.PublicacionFallida) as e:
        app.aplicar_revision_rapido(t, "Prueba")
    assert e.value.codigo == 409 and "nada se escribió en el destino" in e.value.mensaje and e.value.restos == []
    assert list(destino.iterdir()) == []                              # ni zip, ni parcial, ni manifiesto
    assert t["revisado"] is False and Path(t["zip"]).exists()         # el pendiente sigue en local, sin sello
    assert not list((app.TRABAJO / "limpios_REPORTES").glob("revision_rapido_*.json"))
    assert not app.CANDADO.locked()


def test_sello_con_el_candado_tomado_se_rechaza(app, tmp_path):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso(tmp_path), destino)
    assert app.CANDADO.acquire(blocking=False)                        # otro trabajo en curso
    try:
        with pytest.raises(HTTPException) as e:
            app.aplicar_revision_rapido(t, "Prueba")
        assert e.value.status_code == 409 and "otro trabajo en curso" in e.value.detail
        assert list(destino.iterdir()) == [] and t["revisado"] is False
    finally:
        app.CANDADO.release()


def test_zip_pendiente_que_no_se_puede_eliminar_se_informa(app, tmp_path, monkeypatch):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso(tmp_path), destino)
    pendiente = Path(t["zip"])
    monkeypatch.setattr(app, "_eliminar", lambda ruta: False)         # el sistema no deja borrar
    r = app.aplicar_revision_rapido(t, "Prueba")
    assert str(pendiente) in r["pendiente_anterior_no_eliminado"] and pendiente.exists()
    assert Path(r["zip"]).parent == destino and r["revision_pixeles_pendiente"] == []


def test_manifiesto_del_zip_revisado_habla_de_los_archivos_del_paquete(app, tmp_path):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso(tmp_path), destino)
    with zipfile.ZipFile(t["zip"]) as z:
        assert json.loads(z.read("manifiesto.json"))["aviso"] == "paquete creado localmente; la revisión de píxeles de los archivos de este paquete está pendiente"
    r = app.aplicar_revision_rapido(t, "Prueba")
    with zipfile.ZipFile(r["zip"]) as z:
        m = json.loads(z.read("manifiesto.json"))
    assert m["aviso"] == "revisión de píxeles sin hallazgos en los archivos de este paquete"
    assert "todos los pacientes" not in json.dumps(m, ensure_ascii=False)


def test_archivo_repetido_en_la_carpeta_de_entrada(app, tmp_path):
    c = caso(tmp_path)
    shutil.copy(c / "SE1" / "IM0", c / "SE1" / "IM0_copia")           # la misma instancia dos veces
    t = correr(app, c, tmp_path)
    assert t["estado"] in ("ok", "con avisos"), t["mensaje"]
    assert len(t["archivos"]) == 8                                    # ocho salidas limpias, no nueve


# ---- la publicación como punto de compromiso (revisión del 11 de noviembre de 2026, tercera ronda) ----

def _sin_restos(app, destino: Path) -> None:
    assert list(destino.iterdir()) == []                                              # nada en el destino
    assert not list((app.TRABAJO / "paquetes").glob(".tmp_*"))                        # ni temporales
    assert not list((app.TRABAJO / "limpios_REPORTES").glob("revision_rapido_*.json"))  # ni registro de una revisión no publicada
    assert not list(app.TRABAJO.rglob("*" + app.SUFIJO_PREPARADO))                    # ni evidencia preparada
    assert not app.CANDADO.locked()


def test_si_no_se_puede_guardar_el_registro_de_revision_nada_se_publica(app, tmp_path, monkeypatch):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso(tmp_path), destino)
    pendiente = Path(t["zip"])
    real = app._escribir_json

    def falla_en_la_revision(ruta, datos):
        if Path(ruta).name.startswith("revision_rapido_"):
            raise OSError("disco lleno")
        return real(ruta, datos)

    monkeypatch.setattr(app, "_escribir_json", falla_en_la_revision)
    with pytest.raises(app.PublicacionFallida) as e:
        app.aplicar_revision_rapido(t, "Prueba")
    assert e.value.codigo == 500 and "no se publicó" in e.value.mensaje and e.value.restos == []
    _sin_restos(app, destino)
    assert t["revisado"] is False and t["zip"] == str(pendiente) and pendiente.exists()
    manifiestos = {p.name for p in (app.TRABAJO / "paquetes").glob("*.manifiesto.json")}
    assert manifiestos == {pendiente.name[:-4] + ".manifiesto.json"}                  # solo el del zip pendiente


def test_si_no_se_puede_guardar_el_manifiesto_local_nada_se_publica(app, tmp_path, monkeypatch):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso(tmp_path), destino)
    real = app._escribir_json

    def falla_en_el_manifiesto(ruta, datos):
        if ".manifiesto.json" in Path(ruta).name:
            raise OSError("sin permiso")
        return real(ruta, datos)

    monkeypatch.setattr(app, "_escribir_json", falla_en_el_manifiesto)
    with pytest.raises(app.PublicacionFallida) as e:
        app.aplicar_revision_rapido(t, "Prueba")
    assert e.value.codigo == 500 and "no se publicó" in e.value.mensaje and e.value.restos == []
    _sin_restos(app, destino)
    assert t["revisado"] is False and Path(t["zip"]).exists()


def test_si_falla_la_copia_al_destino_nada_queda_publicado_ni_registrado(app, tmp_path, monkeypatch):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso(tmp_path), destino)

    def copia_rota(origen, dst):
        Path(dst).write_bytes(b"medio zip")                                            # una copia parcial llega a existir
        raise OSError("se cortó la red")

    monkeypatch.setattr(app.shutil, "copyfile", copia_rota)
    with pytest.raises(app.PublicacionFallida) as e:
        app.aplicar_revision_rapido(t, "Prueba")
    assert e.value.codigo == 500 and "no se publicó" in e.value.mensaje and e.value.restos == []
    _sin_restos(app, destino)                                                          # la copia parcial también se retira
    assert t["revisado"] is False


def test_fallo_al_borrar_el_temporal_tras_publicar_es_un_aviso_y_conserva_la_ruta(app, tmp_path, monkeypatch):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso(tmp_path), destino)
    real = app._eliminar
    monkeypatch.setattr(app, "_eliminar", lambda ruta: False if Path(ruta).name.startswith(".tmp_") else real(ruta))
    r = app.aplicar_revision_rapido(t, "Prueba")                                       # no lanza: el zip ya está publicado
    publicado = Path(r["zip"])
    assert publicado.parent == destino and publicado.is_file() and t["revisado"] is True and t["zip"] == r["zip"]
    assert any("zip temporal" in a for a in r["avisos_posteriores"])
    registro = json.loads(Path(r["registro_revision"]).read_text(encoding="utf-8"))
    assert registro["revisor"] == "Prueba" and registro["zip"]["nombre"] == publicado.name
    assert registro["zip"]["sha256"] == app.sha256_archivo(publicado) and registro["archivos"] == t["archivos"]
    assert r["registro_confirmado"] is True and not r["registro_revision"].endswith(app.SUFIJO_PREPARADO)
    assert not list(app.TRABAJO.rglob("*" + app.SUFIJO_PREPARADO))


def test_fallo_al_actualizar_la_revision_del_paciente_tras_publicar_es_un_aviso(app, tmp_path, monkeypatch):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso(tmp_path), destino)

    def rota(*a, **k):
        raise OSError("sin permiso")

    monkeypatch.setattr(app, "guardar_revision", rota)
    r = app.aplicar_revision_rapido(t, "Prueba")
    assert Path(r["zip"]).is_file() and t["revisado"] is True
    assert r["revision_de_paciente_actualizada"] == [] and any("no se pudo actualizar la revisión" in a for a in r["avisos_posteriores"])
    assert Path(r["registro_revision"]).is_file()                                      # la evidencia de la corrida sí quedó


def test_la_evidencia_preparada_existe_antes_de_publicar_y_se_confirma_despues(app, tmp_path, monkeypatch):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso(tmp_path), destino)
    reportes, paquetes = app.TRABAJO / "limpios_REPORTES", app.TRABAJO / "paquetes"
    visto = {}
    real = app.os.replace

    def espia(a, b):
        if Path(b).parent == destino:                                                  # el instante de la publicación
            visto["registro_preparado"] = len(list(reportes.glob("revision_rapido_*.json" + app.SUFIJO_PREPARADO)))
            visto["registro_confirmado"] = len(list(reportes.glob("revision_rapido_*.json")))
            visto["manifiesto_preparado"] = (paquetes / (Path(b).name[:-4] + ".manifiesto.json" + app.SUFIJO_PREPARADO)).is_file()
            visto["en_destino_antes"] = [p.name for p in destino.iterdir() if not p.name.startswith(".parcial_")]
        return real(a, b)

    monkeypatch.setattr(app.os, "replace", espia)
    r = app.aplicar_revision_rapido(t, "Prueba")
    assert visto == {"registro_preparado": 1, "registro_confirmado": 0, "manifiesto_preparado": True, "en_destino_antes": []}
    # después de publicar: lo preparado quedó confirmado y no queda nada con el sufijo provisional
    assert len(list(reportes.glob("revision_rapido_*.json"))) == 1 and r["registro_confirmado"] is True
    assert (paquetes / (Path(r["zip"]).name[:-4] + ".manifiesto.json")).is_file()
    assert not list(app.TRABAJO.rglob("*" + app.SUFIJO_PREPARADO))


# ---- el retroceso que falla se dice, con rutas (revisión del 11 de noviembre de 2026, cuarta ronda) ----

def _copia_rota(origen, dst):
    Path(dst).write_bytes(b"medio zip")
    raise OSError("se cortó la red")


def test_copia_fallida_y_registro_que_no_se_deja_borrar_se_informa_con_su_ruta(app, tmp_path, monkeypatch):
    """Fallo combinado: la copia al destino falla y el sistema se niega a borrar el registro preparado de la revisión."""
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso(tmp_path), destino)
    reportes = app.TRABAJO / "limpios_REPORTES"
    real = app._eliminar
    monkeypatch.setattr(app.shutil, "copyfile", _copia_rota)
    monkeypatch.setattr(app, "_eliminar", lambda ruta: False if Path(ruta).name.startswith("revision_rapido_") else real(ruta))
    with pytest.raises(app.PublicacionFallida) as e:
        app.aplicar_revision_rapido(t, "Prueba")
    preparados = list(reportes.glob("revision_rapido_*.json" + app.SUFIJO_PREPARADO))
    assert e.value.codigo == 500 and "no se publicó" in e.value.mensaje
    assert len(preparados) == 1 and e.value.restos == [str(preparados[0])]          # la ruta que quedó, devuelta
    assert str(preparados[0]) in e.value.mensaje and "no un registro de revisión" in e.value.mensaje
    assert list(reportes.glob("revision_rapido_*.json")) == []                        # ningún registro CONFIRMADO
    assert list(destino.iterdir()) == [] and t["revisado"] is False
    assert not app.CANDADO.locked()


def test_manifiesto_preparado_y_temporal_que_no_se_dejan_borrar_se_informan(app, tmp_path, monkeypatch):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso(tmp_path), destino)
    paquetes = app.TRABAJO / "paquetes"
    real = app._eliminar
    monkeypatch.setattr(app.shutil, "copyfile", _copia_rota)
    monkeypatch.setattr(app, "_eliminar", lambda ruta: False if (Path(ruta).name.startswith(".tmp_") or Path(ruta).name.endswith(app.SUFIJO_PREPARADO)) else real(ruta))
    with pytest.raises(app.PublicacionFallida) as e:
        app.aplicar_revision_rapido(t, "Prueba")
    nombres = sorted(Path(r).name for r in e.value.restos)
    assert len(nombres) == 3 and nombres[0].startswith(".tmp_")                       # temporal, manifiesto y registro preparados
    assert sum(n.endswith(".manifiesto.json" + app.SUFIJO_PREPARADO) for n in nombres) == 1
    assert sum(n.startswith("revision_rapido_") for n in nombres) == 1
    assert all(Path(r).exists() for r in e.value.restos)
    assert not list(paquetes.glob("paquete_*.manifiesto.json")) or {p.name for p in paquetes.glob("paquete_*.manifiesto.json")} == {Path(t["zip"]).name[:-4] + ".manifiesto.json"}
    assert list(destino.iterdir()) == [] and t["revisado"] is False


def test_si_no_se_puede_confirmar_el_registro_tras_publicar_es_un_aviso(app, tmp_path, monkeypatch):
    destino = tmp_path / "destino"
    destino.mkdir()
    t = correr(app, caso(tmp_path), destino)
    real = app.os.replace

    def sin_renombrar_registros(a, b):
        if Path(b).name.startswith("revision_rapido_"):
            raise OSError("sin permiso")
        return real(a, b)

    monkeypatch.setattr(app.os, "replace", sin_renombrar_registros)
    r = app.aplicar_revision_rapido(t, "Prueba")                                       # no lanza: el zip está publicado
    assert Path(r["zip"]).is_file() and t["revisado"] is True
    assert r["registro_confirmado"] is False and r["registro_revision"].endswith(app.SUFIJO_PREPARADO)
    assert any("nombre provisional" in a and r["registro_revision"] in a for a in r["avisos_posteriores"])
