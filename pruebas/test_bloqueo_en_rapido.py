"""
Con el estado de las retiradas sin verificar, el modo rápido tampoco registra una revisión ni publica un paquete revisado.
With withdrawal state unverifiable, the express mode does not record a review or publish a reviewed package either.

La regla vive en el empaquetador común, así que vale para todos los que empaquetan, traigan o no su propia revisión de
corrida. La operación de revisión del modo rápido se rechaza antes de escribir ninguna evidencia. El zip pendiente sigue
en la carpeta de trabajo y dice en su manifiesto que el estado de las retiradas no se pudo verificar. Al destino no llega
nada, tampoco un paquete con el estado rebajado. Arreglado el diario, la operación funciona.
"""
from __future__ import annotations

import builtins
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
    """Una carpeta de trabajo, un destino, y lo necesario para correr, mostrar, revisar y retirar corridas."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.app = cargar("app_en_prueba", RAIZ / "herramientas" / "app.py")
        self.app.TRABAJO = tmp_path / "datos-pet"
        self.app.TRABAJO.mkdir()
        self.destino = tmp_path / "destino"
        self.destino.mkdir()
        self.reportes = self.app.TRABAJO / "limpios_REPORTES"
        self.n = 0

    def corrida(self, mostrar: bool = True) -> dict:
        """Una corrida de un paciente nuevo, con todos sus cuadros mostrados en el navegador."""
        self.n += 1
        origen = self.tmp / "estudios" / f"caso{self.n}"
        demo.escribir_caso(origen, 4, self.n)
        for f in origen.rglob("IM*"):
            ds = pydicom.dcmread(f)
            ds.PatientID = f"DEMO-00088{self.n}"
            ds.save_as(f, enforce_file_format=True)
        app, tid = self.app, f"c{self.n}"
        assert app.CANDADO.acquire(blocking=False)
        app.TRABAJOS[tid] = {"nombre": "rapido", "estado": "corriendo", "lineas": [], "codigo": None, "pasos": [], "pacientes": [],
                             "origen": str(origen), "destino": str(self.destino), "zip": None, "mensaje": "", "archivos": {},
                             "revisado": False, "servidos": set(), "mostrados": set()}
        app._rapido(tid, origen, self.destino, "ANON")
        t = app.TRABAJOS[tid]
        assert t["zip"], t["mensaje"]
        if mostrar:
            for i, s in enumerate(t["series_corrida"]):
                for k, n in enumerate(s["cuadros"], 1):
                    for c in range(1, n + 1):
                        t["servidos"].add((i, k, c))
                        app.anotar_mostrado_corrida(t, i, k, c)
        return t

    def corrida_revisada_y_retirada(self) -> Path:
        """Pasos 1 y 2 de la reproducción: una corrida revisada cuya revisión se retira con éxito. Devuelve su diario."""
        t = self.corrida()
        r = self.app.aplicar_revision_rapido(t, "Ana")
        ret = self.app.retirar_revision("corrida", Path(r["registro_revision"]).name, "Arturo", "retirada de prueba")
        assert ret["estado"] == "completa"
        return next((self.reportes / "retiradas").glob("retirada_*.json"))

    def en_destino(self) -> list[str]:
        return sorted(p.name for p in self.destino.iterdir())

    def registros_de_corrida(self) -> list[str]:
        return sorted(p.name for p in self.reportes.iterdir() if p.name.startswith("revision_rapido_") or p.name.startswith(".revision_rapido_"))


def manifiesto(ruta) -> dict:
    with zipfile.ZipFile(ruta) as z:
        return json.loads(z.read("manifiesto.json"))


# ---- las causas: cada una devuelve cómo deshacerla ----

def truncar(m: Mesa, diario: Path, monkeypatch):
    original = diario.read_bytes()
    diario.write_bytes(original[: len(original) // 2])
    return lambda: diario.write_bytes(original)


def sin_permiso(m: Mesa, diario: Path, monkeypatch):
    real_io, real_builtin = io.open, builtins.open

    def falso(archivo, *a, **k):
        if isinstance(archivo, (str, Path)) and Path(archivo).name == diario.name:
            raise PermissionError(13, "sin permiso", str(archivo))
        return real_io(archivo, *a, **k)

    monkeypatch.setattr(io, "open", falso)
    monkeypatch.setattr(builtins, "open", falso)

    def restaurar():
        monkeypatch.setattr(io, "open", real_io)
        monkeypatch.setattr(builtins, "open", real_builtin)
    return restaurar


def carpeta_sin_listar(m: Mesa, diario: Path, monkeypatch):
    real = m.app.os.listdir
    carpeta = str(diario.parent)

    def falso(ruta="."):
        if str(ruta) == carpeta:
            raise PermissionError(13, "sin permiso", carpeta)
        return real(ruta)

    monkeypatch.setattr(m.app.os, "listdir", falso)
    return lambda: monkeypatch.setattr(m.app.os, "listdir", real)


CAUSAS = {"diario truncado (JSON no válido)": (truncar, "no es JSON válido"),
          "sin permiso para leer el diario": (sin_permiso, "no se puede leer (PermissionError)"),
          "la carpeta de retiradas no se deja listar": (carpeta_sin_listar, "no se puede listar la carpeta de retiradas (PermissionError)")}


@pytest.mark.parametrize("cuando", ["la corrida se creó antes del problema", "la corrida se creó después del problema"])
@pytest.mark.parametrize("causa", list(CAUSAS))
def test_el_modo_rapido_no_registra_ni_publica_una_revision_con_las_retiradas_sin_verificar(tmp_path, monkeypatch, causa, cuando):
    m = Mesa(tmp_path)
    app = m.app
    diario = m.corrida_revisada_y_retirada()                         # pasos 1 y 2
    provocar, fragmento = CAUSAS[causa]
    senalado = "retiradas/" if "carpeta" in causa else diario.name
    if "antes" in cuando:
        t = m.corrida()                                              # creada y mostrada con todo en orden
        restaurar = provocar(m, diario, monkeypatch)                 # paso 3
    else:
        restaurar = provocar(m, diario, monkeypatch)                 # paso 3
        t = m.corrida()                                              # paso 4: otra corrida, con todos sus cuadros mostrados
    assert app.estado_de_retiradas()["bloqueo"] is True              # paso 5
    assert app.progreso_corrida(t)["completo"] is True
    destino_antes, registros_antes = m.en_destino(), m.registros_de_corrida()
    pendiente = Path(t["zip"])

    with pytest.raises(app.RevisionBloqueada) as x:                  # paso 6
        app.aplicar_revision_rapido(t, "Miguel")
    cuerpo = x.value.cuerpo()
    # se rechaza diciendo qué diario y por qué
    assert cuerpo["retiradas_no_verificables"] is True and cuerpo["restos"] == []
    assert [p["diario"] for p in cuerpo["diarios_con_problemas"]] == [senalado] and fragmento in cuerpo["diarios_con_problemas"][0]["motivo"]
    assert senalado in cuerpo["detail"] and fragmento in cuerpo["detail"] and "no se registra ninguna revisión" in cuerpo["detail"]
    # nada llegó al destino: ni un zip revisado ni uno con el estado rebajado
    assert m.en_destino() == destino_antes
    # ninguna evidencia de revisión: ni registro confirmado ni preparado
    assert m.registros_de_corrida() == registros_antes
    assert not list(m.reportes.glob("*.preparada")) and not list((app.TRABAJO / "paquetes").glob("*.preparada"))
    # la corrida sigue pendiente, con su zip en la carpeta de trabajo, y el candado libre
    assert t["revisado"] is False and pendiente.is_file() and pendiente.parent == app.TRABAJO / "paquetes"
    assert manifiesto(pendiente)["revision_pixeles"]["estado"] == "pendiente" and manifiesto(pendiente)["revision_pixeles_pendiente"] == t["pacientes"]
    assert not app.CANDADO.locked()
    assert app.leer_revision(t["pacientes"][0])["lista_para_compartir"] is False
    if "después" in cuando:
        # el paquete pendiente se creó con el problema presente, y lo dice
        ret = manifiesto(pendiente)["retiradas"]
        assert ret["verificables"] is False and [p["diario"] for p in ret["diarios_con_problemas"]] == [senalado]
        paquete = next(p for p in t["pasos"] if p["clave"] == "paquete")
        assert paquete["estado"] == "aviso" and "No se puede verificar el estado de las retiradas" in paquete["resumen"] and senalado in paquete["resumen"]
        assert t["estado"] == "con avisos"
    else:
        assert manifiesto(pendiente)["retiradas"]["verificables"] is True

    # arreglado el diario, la misma operación funciona
    restaurar()
    assert app.estado_de_retiradas()["bloqueo"] is False
    r = app.aplicar_revision_rapido(t, "Miguel")
    assert Path(r["zip"]).parent == m.destino and r["revision_pixeles_pendiente"] == []
    final = manifiesto(r["zip"])
    assert final["revision_pixeles"]["estado"] == "sin_hallazgos" and final["revision_pixeles"]["revisor"] == "Miguel"
    assert final["retiradas"]["verificables"] is True and final["retiradas"]["diarios_con_problemas"] == []
    assert len(m.en_destino()) == len(destino_antes) + 1 and len(m.registros_de_corrida()) == len(registros_antes) + 1


def test_el_empaquetador_comun_rechaza_una_revision_de_corrida_con_las_retiradas_sin_verificar(tmp_path, monkeypatch):
    """La regla está en crear_paquete(): quien llega con su propia revisión de corrida no se la salta."""
    m = Mesa(tmp_path)
    app = m.app
    diario = m.corrida_revisada_y_retirada()
    t = m.corrida()
    truncar(m, diario, monkeypatch)
    destino_antes = m.en_destino()
    with pytest.raises(app.PaqueteRechazado) as x:
        app.crear_paquete(t["pacientes"], m.destino, solo=set(t["archivos"]), hashes_esperados=dict(t["archivos"]),
                          exportacion=dict(t["exportacion"]), orden_series=dict(t["orden_de_cortes"]), demografia=t["demografia"],
                          revision_corrida={"estado": "sin_hallazgos", "revisor": "Miguel", "fecha": "2026-11-12T10:00:00", "hallazgos": []})
    assert x.value.codigo == 409 and x.value.cuerpo["retiradas_no_verificables"] is True
    assert x.value.cuerpo["diarios_con_problemas"][0]["diario"] == diario.name and diario.name in x.value.cuerpo["detail"]
    assert m.en_destino() == destino_antes and not list(m.destino.glob(".parcial_*"))
    # un paquete pendiente sí se puede crear, en local, y dice el estado en que se creó
    r = app.crear_paquete(t["pacientes"], app.TRABAJO / "paquetes", solo=set(t["archivos"]), exportacion=dict(t["exportacion"]),
                          orden_series=dict(t["orden_de_cortes"]), demografia=t["demografia"], revision_corrida={"estado": "pendiente", "detalle": "sin registrar"})
    assert r["retiradas_no_verificables"][0]["diario"] == diario.name
    assert manifiesto(r["ruta"])["retiradas"]["verificables"] is False


@pytest.mark.parametrize("llamadas_antes", [1, 2, 3])
def test_si_las_retiradas_dejan_de_poder_verificarse_a_mitad_tampoco_se_publica(tmp_path, monkeypatch, llamadas_antes):
    """El estado se comprueba al empezar la revisión, al empezar a empaquetar, antes de dejar evidencias y justo antes de
    publicar. Aquí deja de poder verificarse después de cada una de las tres primeras comprobaciones."""
    m = Mesa(tmp_path)
    app = m.app
    diario = m.corrida_revisada_y_retirada()
    t = m.corrida()
    destino_antes, registros_antes = m.en_destino(), m.registros_de_corrida()
    real = app.estado_de_retiradas
    cuenta = {"n": 0}

    def cambia_a_mitad():
        cuenta["n"] += 1
        if cuenta["n"] > llamadas_antes:
            return {"diarios": [], "bloqueo": True, "problemas": [{"diario": diario.name, "motivo": "no se puede leer (PermissionError)", "alcance_conocido": False}]}
        return real()

    monkeypatch.setattr(app, "estado_de_retiradas", cambia_a_mitad)
    with pytest.raises(app.RevisionBloqueada) as x:
        app.aplicar_revision_rapido(t, "Miguel")
    monkeypatch.setattr(app, "estado_de_retiradas", real)
    assert cuenta["n"] == llamadas_antes + 1                         # falló exactamente en la comprobación siguiente
    assert x.value.cuerpo()["diarios_con_problemas"][0]["diario"] == diario.name and x.value.cuerpo()["restos"] == []
    assert m.en_destino() == destino_antes and m.registros_de_corrida() == registros_antes     # nada publicado, ninguna evidencia
    assert not list(m.reportes.glob("*.preparada")) and not list((app.TRABAJO / "paquetes").glob("*.preparada"))
    assert not list(m.destino.glob(".parcial_*")) and not list((app.TRABAJO / "paquetes").glob(".tmp_*"))
    assert t["revisado"] is False and Path(t["zip"]).is_file() and not app.CANDADO.locked()
    assert app.aplicar_revision_rapido(t, "Miguel")["revision_pixeles_pendiente"] == []         # y después funciona


def test_el_paquete_de_enviar_tambien_dice_que_no_se_pudo_verificar(tmp_path, monkeypatch):
    m = Mesa(tmp_path)
    app = m.app
    diario = m.corrida_revisada_y_retirada()
    t = m.corrida()
    assert app.aplicar_revision_rapido(t, "Miguel")["revision_pixeles_pendiente"] == []
    pac = t["pacientes"][0]
    assert app.leer_revision(pac)["lista_para_compartir"] is True    # revisado, con todo en orden
    restaurar = truncar(m, diario, monkeypatch)
    r = app.crear_paquete([pac])                                     # la pestaña Enviar
    assert r["revision_pixeles_pendiente"] == [pac] and r["retiradas_no_verificables"][0]["diario"] == diario.name
    mf = manifiesto(r["ruta"])
    assert mf["retiradas"]["verificables"] is False and mf["revision_pixeles"][pac]["estado"] == "no_verificable"
    assert Path(r["ruta"]).parent == app.TRABAJO / "paquetes"
    restaurar()
    r2 = app.crear_paquete([pac])
    assert r2["revision_pixeles_pendiente"] == [] and manifiesto(r2["ruta"])["retiradas"]["verificables"] is True


def test_un_diario_con_defectos_pero_alcance_legible_no_bloquea_el_modo_rapido(tmp_path):
    m = Mesa(tmp_path)
    app = m.app
    diario = m.corrida_revisada_y_retirada()
    d = json.loads(diario.read_text(encoding="utf-8"))
    del d["acciones"]
    diario.write_text(json.dumps(d), encoding="utf-8")
    assert app.estado_de_retiradas()["bloqueo"] is False and len(app.estado_de_retiradas()["problemas"]) == 1
    t = m.corrida()
    r = app.aplicar_revision_rapido(t, "Miguel")                     # se sabe a qué alcanza ese diario: lo demás sigue funcionando
    mf = manifiesto(r["zip"])
    assert mf["revision_pixeles"]["estado"] == "sin_hallazgos" and mf["retiradas"]["verificables"] is True
    assert [p["diario"] for p in mf["retiradas"]["diarios_con_problemas"]] == [diario.name]      # y el manifiesto lo deja dicho


def test_una_revision_sin_hallazgos_en_estudios_tampoco_se_registra(tmp_path, monkeypatch):
    m = Mesa(tmp_path)
    app = m.app
    diario = m.corrida_revisada_y_retirada()
    t = m.corrida()
    pac = t["pacientes"][0]
    restaurar = truncar(m, diario, monkeypatch)
    registro = m.reportes / f"revision_pixeles_{pac}.json"
    assert not registro.exists()
    with pytest.raises(app.RevisionBloqueada) as x:
        app.guardar_revision(pac, "sin_hallazgos", [], "Miguel")
    assert x.value.cuerpo()["diarios_con_problemas"][0]["diario"] == diario.name
    assert not registro.exists() and not list((m.reportes / "historial_revisiones").glob(f"revision_pixeles_{pac}_*"))   # ninguna evidencia
    # un hallazgo sí se puede anotar, porque no autoriza nada; su estado se lee igualmente como no verificable
    r = app.guardar_revision(pac, "con_hallazgos", ["rostro"], "Miguel")
    assert r["estado"] == "no_verificable" and r["lista_para_compartir"] is False
    assert json.loads(registro.read_text(encoding="utf-8"))["estado"] == "con_hallazgos"
    restaurar()
    assert app.leer_revision(pac)["estado"] == "con_hallazgos" and app.leer_revision(pac)["lista_para_compartir"] is False
    assert app.guardar_revision(pac, "sin_hallazgos", [], "Miguel")["lista_para_compartir"] is True
