"""
"No hay ninguna retirada" y "no se puede verificar el estado de las retiradas" son cosas distintas.
"No withdrawal exists" is different from "withdrawal state cannot be verified".

Un diario de retirada que no se puede leer, que no es JSON válido, cuya estructura no es válida, o una carpeta de
retiradas que no se deja listar, no pueden devolver a ninguna revisión la autorización para compartir. Mientras el alcance
de un diario no se pueda determinar, ninguna revisión autoriza, se dice qué diario requiere atención, no se toca ningún
registro, y todo vuelve a su sitio cuando el diario se puede leer otra vez.

El escenario de partida es el más delicado: una retirada comprometida e interrumpida, con el registro original del
paciente todavía en su sitio. Ahí el diario es lo único que dice que esa revisión está retirada.
"""
from __future__ import annotations

import builtins
import hashlib
import importlib.util
import io
import json
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


durable = cargar("pruebas_retirada_durable", RAIZ / "pruebas" / "test_retirada_durable.py")
Escenario, Fallo, sha = durable.Escenario, durable.Fallo, durable.sha
MOTIVO = "falta decidir qué hacer con la cabeza"


def retirada_interrumpida(tmp_path, monkeypatch, pacientes: int = 1):
    """Corrida revisada; se retira; el diario se escribe y falla la sustitución del registro del paciente. Devuelve el
    escenario, la ruta del diario y la fecha de la retirada. El registro original del paciente sigue en su sitio."""
    e = Escenario(tmp_path, pacientes)
    fallo = Fallo(e.app, monkeypatch).en_escritura(durable.es_registro_de_paciente)
    with pytest.raises(e.app.RetiradaIncompleta) as x:
        e.app.retirar_revision("corrida", e.registro.name, "Arturo", MOTIVO)
    fallo.quitar()
    diario = next((e.reportes / "retiradas").glob("retirada_*.json"))
    for p in e.pacientes:                                            # el original sigue ahí: solo el diario dice que está retirado
        assert (e.reportes / f"revision_pixeles_{p}.json").read_bytes() == e.originales[p]
        assert e.app.leer_revision(p)["estado"] == "retirada" and e.app.leer_revision(p)["lista_para_compartir"] is False
    return e, diario, x.value.cuerpo()["fecha"]


# ---- las cuatro maneras de no poder verificar ----

def sin_permiso_de_lectura(e, diario, monkeypatch):
    """Abrir el diario lanza PermissionError, se lea como se lea: el fallo se provoca en la apertura del archivo."""
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


def json_no_valido(e, diario, monkeypatch):
    original = diario.read_bytes()
    diario.write_bytes(original[: len(original) // 2])               # truncado: ya no es JSON
    return lambda: diario.write_bytes(original)


def estructura_no_valida(e, diario, monkeypatch):
    original = diario.read_bytes()
    d = json.loads(original)
    d["alcance"] = "ANON-0001"                                       # el alcance no se puede determinar
    diario.write_text(json.dumps(d), encoding="utf-8")
    return lambda: diario.write_bytes(original)


def carpeta_que_no_se_deja_listar(e, diario, monkeypatch):
    real = e.app.os.listdir
    carpeta = str(diario.parent)

    def falso(ruta="."):
        if str(ruta) == carpeta:
            raise PermissionError("sin permiso")
        return real(ruta)

    monkeypatch.setattr(e.app.os, "listdir", falso)
    return lambda: monkeypatch.setattr(e.app.os, "listdir", real)


CAUSAS = {
    "sin permiso para leer el diario": (sin_permiso_de_lectura, "no se puede leer (PermissionError)"),
    "el diario no es JSON válido": (json_no_valido, "no es JSON válido"),
    "la estructura del diario no es válida": (estructura_no_valida, "estructura no válida"),
    "la carpeta de retiradas no se deja listar": (carpeta_que_no_se_deja_listar, "no se puede listar la carpeta de retiradas (PermissionError)"),
}


def nadie_autoriza_y_se_dice_por_que(e, diario, fragmento: str, causa: str):
    app = e.app
    senalado = "retiradas/" if "carpeta" in causa else diario.name
    # 1. estado de la revisión: no verificable, nunca vigente
    for p in e.pacientes:
        r = app.leer_revision(p)
        assert r["lista_para_compartir"] is False and r["vigente"] is False, r
        assert r["estado"] == "no_verificable" and fragmento in r["detalle"] and senalado in r["detalle"]
    # 2. un paquete nuevo no puede darlos por revisados, y su manifiesto dice por qué
    nuevo = app.crear_paquete(e.pacientes)
    assert nuevo["revision_pixeles_pendiente"] == e.pacientes
    with zipfile.ZipFile(nuevo["ruta"]) as z:
        m = json.loads(z.read("manifiesto.json"))
    assert m["revision_pixeles_pendiente"] == e.pacientes
    assert all(m["revision_pixeles"][p]["estado"] == "no_verificable" and m["revision_pixeles"][p]["vigente"] is False for p in e.pacientes)
    # 3. Registro: el diario que requiere atención, con su motivo; nada se presenta como vigente ni como retirable
    lista = app.revisiones_registradas()
    assert lista["retiradas_no_verificables"] is True
    assert [(x["diario"], x["alcance_conocido"]) for x in lista["diarios_con_problemas"]] == [(senalado, False)]
    assert fragmento in lista["diarios_con_problemas"][0]["motivo"]
    assert all(x["estado"] == "no_verificable" and x["se_puede_retirar"] is False for x in lista["pacientes"])
    assert all(c["no_verificable"] is True and c["se_puede_retirar"] is False and c["retirada"] is None for c in lista["corridas"])
    # 4. retirar o terminar se rechaza, diciendo qué diario requiere atención
    for tipo, identificador in (("corrida", e.registro.name), ("paciente", e.pacientes[0])):
        with pytest.raises(HTTPException) as x:
            app.retirar_revision(tipo, identificador, "Otra persona", "otro motivo")
        assert x.value.status_code == 409 and senalado in x.value.detail and "no se puede verificar" in x.value.detail
    # 5. la recuperación del arranque no falla ni toca nada
    assert app.reanudar_retiradas() == [] and app.retiradas_incompletas() == []
    assert not app.CANDADO.locked()
    e.nada_original_cambio()


@pytest.mark.parametrize("causa", list(CAUSAS))
def test_un_diario_que_no_se_puede_verificar_no_devuelve_la_autorizacion(tmp_path, monkeypatch, causa):
    e, diario, fecha = retirada_interrumpida(tmp_path, monkeypatch)
    provocar, fragmento = CAUSAS[causa]
    bytes_del_diario = diario.read_bytes()
    restaurar = provocar(e, diario, monkeypatch)

    nadie_autoriza_y_se_dice_por_que(e, diario, fragmento, causa)
    # los registros siguen todos ahí: el diario (tal como quedó), el original del paciente, el de la corrida y el zip
    assert diario.exists() and all((e.reportes / f"revision_pixeles_{p}.json").read_bytes() == e.originales[p] for p in e.pacientes)

    # cuando el diario vuelve a poder leerse, todo sigue donde estaba: retirada comprometida, incompleta y terminable
    restaurar()
    assert diario.read_bytes() == bytes_del_diario
    assert e.app.estado_de_retiradas()["bloqueo"] is False and e.app.estado_de_retiradas()["problemas"] == []
    e.nadie_autoriza()                                               # estado 'retirada', por el diario
    assert len(e.app.retiradas_incompletas()) == 1
    r = e.app.retirar_revision("corrida", e.registro.name, "Otra persona", "otro motivo")
    assert r["estado"] == "completa" and r["reanudada"] is True and r["quien"] == "Arturo"
    e.completa_y_atribuida_a("Arturo", MOTIVO, fecha)
    e.nadie_autoriza()
    e.nada_original_cambio()


def test_los_cinco_pasos_de_la_revision(tmp_path, monkeypatch):
    """1. corrida revisada que crea la revisión del paciente; 2. se compromete su retirada y falla la sustitución del
    registro; 3. lista_para_compartir es False; 4. leer el diario lanza PermissionError; 5. la misma llamada sigue
    devolviendo False."""
    e, diario, _ = retirada_interrumpida(tmp_path, monkeypatch)
    pac = e.pacientes[0]
    assert e.app.leer_revision(pac)["lista_para_compartir"] is False
    sin_permiso_de_lectura(e, diario, monkeypatch)
    r = e.app.leer_revision(pac)
    assert r["lista_para_compartir"] is False and r["estado"] == "no_verificable"


def test_tambien_bloquea_a_un_paciente_ajeno_a_esa_retirada_y_lo_suelta_despues(tmp_path, monkeypatch):
    """Si no se sabe a quién alcanza un diario, no se puede afirmar que la revisión de nadie siga valiendo."""
    e, diario, _ = retirada_interrumpida(tmp_path, monkeypatch)
    app = e.app
    # otro paciente, con su propia revisión registrada en Estudios; nada que ver con la corrida retirada
    otro_origen = tmp_path / "estudios" / "otro"
    durable.demo.escribir_caso(otro_origen, 4, 7)
    import pydicom
    for f in otro_origen.rglob("IM*"):
        ds = pydicom.dcmread(f)
        ds.PatientID = "DEMO-000777"
        ds.save_as(f, enforce_file_format=True)
    assert app.CANDADO.acquire(blocking=False)
    app.TRABAJOS["o"] = {"nombre": "rapido", "estado": "corriendo", "lineas": [], "codigo": None, "pasos": [], "pacientes": [], "origen": str(otro_origen),
                         "destino": str(e.destino), "zip": None, "mensaje": "", "archivos": {}, "revisado": False, "servidos": set(), "mostrados": set()}
    app._rapido("o", otro_origen, e.destino, "ANON")
    ajeno = app.TRABAJOS["o"]["pacientes"][0]
    assert ajeno not in e.pacientes
    app.indexar()
    assert app.guardar_revision(ajeno, "sin_hallazgos", [], "Miguel")["lista_para_compartir"] is True
    original_ajeno = (e.reportes / f"revision_pixeles_{ajeno}.json").read_bytes()

    restaurar = json_no_valido(e, diario, monkeypatch)
    r = app.leer_revision(ajeno)
    assert r["estado"] == "no_verificable" and r["lista_para_compartir"] is False and diario.name in r["detalle"]
    assert app.crear_paquete([ajeno])["revision_pixeles_pendiente"] == [ajeno]
    restaurar()
    r = app.leer_revision(ajeno)                                      # su revisión no se tocó y vuelve a valer
    assert r["lista_para_compartir"] is True and r["revisor"] == "Miguel"
    assert (e.reportes / f"revision_pixeles_{ajeno}.json").read_bytes() == original_ajeno


def test_un_diario_con_defectos_pero_alcance_legible_sigue_retirando_solo_lo_suyo(tmp_path, monkeypatch):
    e, diario, fecha = retirada_interrumpida(tmp_path, monkeypatch)
    app = e.app
    original = diario.read_bytes()
    d = json.loads(original)
    del d["acciones"]                                                # no se puede terminar, pero se sabe a qué alcanza
    diario.write_text(json.dumps(d), encoding="utf-8")
    estado = app.estado_de_retiradas()
    assert estado["bloqueo"] is False and [(p["diario"], p["alcance_conocido"]) for p in estado["problemas"]] == [(diario.name, True)]
    r = app.leer_revision(e.pacientes[0])                             # sigue retirado, por el alcance del diario
    assert r["estado"] == "retirada" and r["lista_para_compartir"] is False and r["retirada"]["quien"] == "Arturo"
    assert app.crear_paquete(e.pacientes)["revision_pixeles_pendiente"] == e.pacientes
    assert app.retiradas_incompletas() == []                         # no está entre las que se pueden terminar
    lista = app.revisiones_registradas()
    assert lista["retiradas_no_verificables"] is False and lista["diarios_con_problemas"][0]["diario"] == diario.name
    with pytest.raises(HTTPException) as x:
        app.retirar_revision("corrida", e.registro.name, "Otra persona", "otro motivo")
    assert x.value.status_code == 409 and "requiere atención" in x.value.detail and diario.name in x.value.detail
    e.nada_original_cambio()
    diario.write_bytes(original)                                     # arreglado el diario, se termina con la atribución original
    assert app.retirar_revision("corrida", e.registro.name, "Otra persona", "otro motivo")["estado"] == "completa"
    e.completa_y_atribuida_a("Arturo", MOTIVO, fecha)


@pytest.mark.parametrize("contenido, fragmento", [
    ("[]", "no es un objeto"), ("{}", "falta el identificador"), ('{"id": "x"}', "no corresponde al nombre del archivo"),
    ('{"id": "ID", "retirada": {"quien": "A"}}', "falta quién retiró, cuándo o por qué"),
    ('{"id": "ID", "retirada": {"quien": "A", "fecha": "f", "motivo": "m"}, "alcance": []}', "el alcance falta o no es válido"),
    ('{"id": "ID", "retirada": {"quien": "A", "fecha": "f", "motivo": "m"}, "alcance": [{"tipo": "paciente", "id": "ANON-0001"}]}', "el alcance falta o no es válido"),
    ('{"id": "ID", "retirada": {"quien": "A", "fecha": "f", "motivo": "m"}, "alcance": [{"tipo": "otro", "id": "x", "sha256": "' + "a" * 64 + '"}]}', "el alcance falta o no es válido"),
])
def test_cada_defecto_de_estructura_sin_alcance_legible_bloquea(tmp_path, contenido, fragmento):
    app = cargar("app_en_prueba", RAIZ / "herramientas" / "app.py")
    app.TRABAJO = tmp_path / "datos-pet"
    carpeta = app.TRABAJO / "limpios_REPORTES" / "retiradas"
    carpeta.mkdir(parents=True)
    (carpeta / "retirada_ID.json").write_text(contenido, encoding="utf-8")
    estado = app.estado_de_retiradas()
    assert estado["bloqueo"] is True and fragmento in estado["problemas"][0]["motivo"] and estado["problemas"][0]["diario"] == "retirada_ID.json"
    with pytest.raises(app.RetiradasNoVerificables) as x:
        app.retirada_que_cubre("paciente", "ANON-0001", "0" * 64)
    assert "retirada_ID.json" in str(x.value)


def test_sin_carpeta_de_retiradas_no_hay_retiradas_y_los_temporales_no_son_diarios(tmp_path):
    app = cargar("app_en_prueba", RAIZ / "herramientas" / "app.py")
    app.TRABAJO = tmp_path / "datos-pet"
    (app.TRABAJO / "limpios_REPORTES").mkdir(parents=True)
    assert app.estado_de_retiradas() == {"diarios": [], "problemas": [], "bloqueo": False}          # no existe: no hay ninguna
    carpeta = app.TRABAJO / "limpios_REPORTES" / "retiradas"
    carpeta.mkdir()
    (carpeta / ".retirada_20261112_101010_abcdef.json.1a2b3c.tmp").write_text("{medio escrito", encoding="utf-8")
    (carpeta / "notas.txt").write_text("no es un diario", encoding="utf-8")
    assert app.estado_de_retiradas() == {"diarios": [], "problemas": [], "bloqueo": False}
    assert app.retirada_que_cubre("paciente", "ANON-0001", "0" * 64) is None


def test_al_arrancar_con_un_diario_ilegible_la_aplicacion_no_falla_ni_autoriza(tmp_path, monkeypatch):
    e, diario, fecha = retirada_interrumpida(tmp_path, monkeypatch)
    original = diario.read_bytes()
    diario.write_bytes(b"\xff\xfe no es texto")                      # ni siquiera es UTF-8
    monkeypatch.undo()
    e.app = cargar("app_tras_reinicio", RAIZ / "herramientas" / "app.py")     # otra instancia: nada en memoria
    e.app.TRABAJO = tmp_path / "datos-pet"
    estado = e.app.estado_de_retiradas()
    assert estado["bloqueo"] is True and estado["problemas"][0]["diario"] == diario.name
    assert e.app.reanudar_retiradas() == []                          # lo que hace el arranque: no falla
    for p in e.pacientes:
        assert e.app.leer_revision(p)["lista_para_compartir"] is False
    assert e.app.crear_paquete(e.pacientes)["revision_pixeles_pendiente"] == e.pacientes
    diario.write_bytes(original)
    assert [r["estado"] for r in e.app.reanudar_retiradas()] == ["completa"]
    e.completa_y_atribuida_a("Arturo", MOTIVO, fecha)


def test_un_registro_que_ya_dice_retirada_no_necesita_el_diario(tmp_path, monkeypatch):
    """Retirada terminada: el registro del paciente ya es la retirada. Aunque después el diario no se pueda leer, ese
    paciente sigue constando como retirado, con quién y por qué."""
    e = Escenario(tmp_path)
    r = e.app.retirar_revision("corrida", e.registro.name, "Arturo", MOTIVO)
    assert r["estado"] == "completa"
    diario = next((e.reportes / "retiradas").glob("retirada_*.json"))
    sin_permiso_de_lectura(e, diario, monkeypatch)
    estado = e.app.leer_revision(e.pacientes[0])
    assert estado["estado"] == "retirada" and estado["lista_para_compartir"] is False and estado["retirada"]["quien"] == "Arturo"
    assert e.app.crear_paquete(e.pacientes)["revision_pixeles_pendiente"] == e.pacientes
