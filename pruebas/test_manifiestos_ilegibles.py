"""
Al retirar una revisión, un manifiesto de paquete que no se puede leer no se salta en silencio.
When withdrawing a review, an unreadable package manifest is not silently skipped.

La retirada se hace igual. Lo que cambia es lo que se dice de los paquetes: si el zip sigue en la carpeta de paquetes, la
dependencia se averigua con el manifiesto que va dentro del zip; si no, ese paquete queda como "sin determinar" y la
lista de afectados se marca como incompleta, en la respuesta y en el diario de la retirada.
"""
from __future__ import annotations

import builtins
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


class Mesa:
    """Un paciente revisado en Estudios y un paquete de Enviar hecho con esa revisión."""

    def __init__(self, tmp_path: Path):
        self.app = cargar("app_en_prueba", RAIZ / "herramientas" / "app.py")
        app = self.app
        app.TRABAJO = tmp_path / "datos-pet"
        app.TRABAJO.mkdir()
        origen = tmp_path / "estudios" / "A"
        demo.escribir_caso(origen, 4, 1)
        assert app.CANDADO.acquire(blocking=False)
        app.TRABAJOS["c"] = {"nombre": "rapido", "estado": "corriendo", "lineas": [], "codigo": None, "pasos": [], "pacientes": [],
                             "origen": str(origen), "destino": str(tmp_path), "zip": None, "mensaje": "", "archivos": {},
                             "revisado": False, "servidos": set(), "mostrados": set()}
        app._rapido("c", origen, tmp_path, "ANON")
        self.pac = app.TRABAJOS["c"]["pacientes"][0]
        app.indexar()
        for s in app.INDICE.values():
            for _, ruta in s["cortes"]:
                app.anotar_servido_estudios(self.pac, ruta, 1, hashlib.sha256(Path(ruta).read_bytes()).hexdigest())
                app.anotar_mostrado_estudios(self.pac, ruta, 1)
        assert app.registrar_revision_de_paciente(self.pac, "sin_hallazgos", [], "Ana")["lista_para_compartir"] is True
        paquete = app.empaquetar_para_enviar([self.pac])
        assert paquete["revision_pixeles_pendiente"] == []
        self.zip = Path(paquete["ruta"])
        self.manifiesto = self.zip.with_name(self.zip.name[:-4] + ".manifiesto.json")
        assert self.manifiesto.is_file()
        self.sha_zip = hashlib.sha256(self.zip.read_bytes()).hexdigest()

    def retirar(self) -> dict:
        return self.app.retirar_revision("paciente", self.pac, "Arturo", "retirada con un manifiesto que no se puede leer")

    def diario(self) -> dict:
        ruta = next((self.app.TRABAJO / "limpios_REPORTES" / "retiradas").glob("retirada_*.json"))
        return json.loads(ruta.read_text(encoding="utf-8"))

    def la_retirada_vale_y_el_zip_no_cambia(self):
        estado = self.app.leer_revision(self.pac)
        assert estado["estado"] == "retirada" and estado["lista_para_compartir"] is False
        if self.zip.is_file():
            assert hashlib.sha256(self.zip.read_bytes()).hexdigest() == self.sha_zip


# ---- maneras de dejar ilegible el manifiesto local ----

def truncar(m: Mesa, monkeypatch):
    m.manifiesto.write_bytes(m.manifiesto.read_bytes()[:60])
    return "no es JSON válido"


def sin_permiso(m: Mesa, monkeypatch):
    real_io, real_builtin = io.open, builtins.open

    def falso(archivo, *a, **k):
        if isinstance(archivo, (str, Path)) and Path(archivo).name == m.manifiesto.name:
            raise PermissionError(13, "sin permiso", str(archivo))
        return real_io(archivo, *a, **k)

    monkeypatch.setattr(io, "open", falso)
    monkeypatch.setattr(builtins, "open", falso)
    return "no se puede leer (PermissionError)"


def otra_estructura(m: Mesa, monkeypatch):
    m.manifiesto.write_text('["esto", "no", "es", "un", "manifiesto"]', encoding="utf-8")
    return "su estructura no es válida"


CAUSAS = {"JSON truncado": truncar, "sin permiso de lectura": sin_permiso, "estructura no válida": otra_estructura}


def test_con_el_manifiesto_legible_la_lista_esta_completa(tmp_path):
    m = Mesa(tmp_path)
    r = m.retirar()
    assert [e["zip"] for e in r["paquetes_afectados"]] == [m.zip.name]
    assert r["inventario_de_paquetes"] == {"completo": True, "problemas": [], "sin_determinar": []}
    assert m.diario()["inventario_de_paquetes"]["completo"] is True


@pytest.mark.parametrize("causa", list(CAUSAS))
def test_manifiesto_ilegible_con_el_zip_presente_se_averigua_con_el_manifiesto_del_zip(tmp_path, monkeypatch, causa):
    m = Mesa(tmp_path)
    fragmento = CAUSAS[causa](m, monkeypatch)
    r = m.retirar()
    assert r["estado"] == "completa"                                  # la retirada se hace
    afectados = r["paquetes_afectados"]
    assert [e["zip"] for e in afectados] == [m.zip.name]              # y el paquete no se pierde de la lista
    assert afectados[0]["recuperado_del_zip"] is True and afectados[0]["existe"] is True and afectados[0]["ruta"] == str(m.zip)
    inv = r["inventario_de_paquetes"]
    assert inv["completo"] is True and inv["sin_determinar"] == []
    assert [(p["manifiesto"], p["recuperado_del_zip"], p["zip_en_paquetes"]) for p in inv["problemas"]] == [(m.manifiesto.name, True, True)]
    assert fragmento in inv["problemas"][0]["motivo"]                 # el manifiesto ilegible se dice, con su motivo
    assert m.diario()["inventario_de_paquetes"] == inv                # y queda en el diario
    m.la_retirada_vale_y_el_zip_no_cambia()


@pytest.mark.parametrize("causa", list(CAUSAS))
def test_manifiesto_ilegible_sin_el_zip_la_lista_se_marca_incompleta(tmp_path, monkeypatch, causa):
    m = Mesa(tmp_path)
    en_otro_sitio = tmp_path / "enviado" / m.zip.name                 # el zip ya no está en la carpeta de paquetes
    en_otro_sitio.parent.mkdir()
    m.zip.rename(en_otro_sitio)
    fragmento = CAUSAS[causa](m, monkeypatch)
    r = m.retirar()
    assert r["estado"] == "completa" and r["paquetes_afectados"] == []          # no se sabe si dependía...
    inv = r["inventario_de_paquetes"]
    assert inv["completo"] is False                                   # ...y se dice que la lista no está completa
    assert [(e["zip"], e["existe"]) for e in inv["sin_determinar"]] == [(m.zip.name, False)]
    assert fragmento in inv["sin_determinar"][0]["motivo"] and inv["problemas"][0]["recuperado_del_zip"] is False
    assert m.diario()["inventario_de_paquetes"]["completo"] is False
    assert m.app.leer_revision(m.pac)["inventario_de_paquetes"]["completo"] is False
    fila = next(p for p in m.app.revisiones_registradas()["pacientes"] if p["paciente"] == m.pac)
    assert fila["inventario_de_paquetes"]["completo"] is False        # y Registro lo muestra
    m.la_retirada_vale_y_el_zip_no_cambia()
    assert hashlib.sha256(en_otro_sitio.read_bytes()).hexdigest() == m.sha_zip


def test_zip_presente_pero_ilegible_tambien_queda_sin_determinar(tmp_path, monkeypatch):
    m = Mesa(tmp_path)
    truncar(m, monkeypatch)
    real = m.app.zipfile.ZipFile

    def no_se_abre(ruta, *a, **k):
        if Path(str(ruta)).name == m.zip.name:
            raise PermissionError(13, "sin permiso", str(ruta))
        return real(ruta, *a, **k)

    monkeypatch.setattr(m.app.zipfile, "ZipFile", no_se_abre)
    r = m.retirar()
    inv = r["inventario_de_paquetes"]
    assert r["estado"] == "completa" and inv["completo"] is False
    assert [(e["zip"], e["existe"]) for e in inv["sin_determinar"]] == [(m.zip.name, True)]       # el zip sigue ahí: se dice dónde
    assert inv["sin_determinar"][0]["ruta"] == str(m.zip)


def test_si_la_carpeta_de_paquetes_no_se_deja_listar_la_lista_se_marca_incompleta(tmp_path, monkeypatch):
    m = Mesa(tmp_path)
    real = m.app.os.listdir
    carpeta = str(m.app.TRABAJO / "paquetes")

    def falso(ruta="."):
        if str(ruta) == carpeta:
            raise PermissionError(13, "sin permiso", carpeta)
        return real(ruta)

    monkeypatch.setattr(m.app.os, "listdir", falso)
    r = m.retirar()
    inv = r["inventario_de_paquetes"]
    assert r["estado"] == "completa" and inv["completo"] is False
    assert "no se puede listar la carpeta de paquetes (PermissionError)" in inv["sin_determinar"][0]["motivo"]
    m.la_retirada_vale_y_el_zip_no_cambia()


def test_el_manifiesto_ilegible_de_un_paquete_ajeno_con_su_zip_no_ensucia_la_lista(tmp_path, monkeypatch):
    """Un paquete de otra revisión del mismo paciente, hecho antes: su manifiesto local está roto, pero su zip dice que no
    cita la revisión que se retira. No entra entre los afectados y la lista queda completa."""
    m = Mesa(tmp_path)
    app = m.app
    viejo_zip, viejo_manifiesto = m.zip, m.manifiesto                 # paquete hecho con la primera revisión (Ana)
    for s in app.INDICE.values():                                     # una segunda revisión, de Miguel, y su paquete
        for _, ruta in s["cortes"]:
            app.anotar_mostrado_estudios(m.pac, ruta, 1)
    assert app.registrar_revision_de_paciente(m.pac, "sin_hallazgos", [], "Miguel")["revisor"] == "Miguel"
    nuevo = Path(app.empaquetar_para_enviar([m.pac])["ruta"])
    viejo_manifiesto.write_bytes(viejo_manifiesto.read_bytes()[:60])  # el manifiesto del paquete viejo, ilegible
    r = m.retirar()                                                   # se retira la revisión vigente: la de Miguel
    assert [e["zip"] for e in r["paquetes_afectados"]] == [nuevo.name]
    inv = r["inventario_de_paquetes"]
    assert inv["completo"] is True and [p["manifiesto"] for p in inv["problemas"]] == [viejo_manifiesto.name]
    assert viejo_zip.name not in [e["zip"] for e in r["paquetes_afectados"]]


def test_la_funcion_que_devuelve_solo_la_lista_sigue_existiendo(tmp_path, monkeypatch):
    m = Mesa(tmp_path)
    app = m.app
    alcance = app.alcance_de_retirada("paciente", m.pac)
    assert [e["zip"] for e in app.paquetes_que_dependen_de(alcance)] == [m.zip.name]
    m.zip.rename(tmp_path / "fuera.zip")
    truncar(m, monkeypatch)
    assert app.paquetes_que_dependen_de(alcance) == []                # la lista sola no dice que está incompleta...
    assert app.inventario_de_paquetes(alcance)["completo"] is False   # ...el inventario, sí
