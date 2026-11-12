"""
La retirada de una revisión es duradera y se puede terminar. / Withdrawal is durable and recoverable.

Escribir el diario de la retirada es el punto de compromiso. Desde ese momento, las revisiones que alcanza dejan de
autorizar que se comparta nada, aunque fallen las actualizaciones de sus registros. Lo que quede a medias se dice con
exactitud, y un reintento —o el siguiente arranque— lo termina sin cambiar quién retiró, cuándo ni por qué, sin perder
el historial y sin tocar los zips ya exportados.

Estas pruebas provocan un fallo en cada frontera de escritura o de archivado, en las dos direcciones (retirar la
corrida; retirar al paciente cuya revisión escribió una corrida), y comprueban lo mismo en todas.
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


def cargar(nombre: str, ruta: Path):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


demo = cargar("demo", RAIZ / "herramientas" / "demo.py")


def sha(ruta) -> str:
    return hashlib.sha256(Path(ruta).read_bytes()).hexdigest()


class Escenario:
    """Una corrida revisada por Ana que cubre a sus pacientes enteros: registro de la corrida, registro de cada paciente
    escrito por la corrida, y un zip revisado en el destino."""

    def __init__(self, tmp_path: Path, pacientes: int = 1):
        self.tmp = tmp_path
        self.app = cargar("app_en_prueba", RAIZ / "herramientas" / "app.py")
        self.app.TRABAJO = tmp_path / "datos-pet"
        self.app.TRABAJO.mkdir()
        origen = tmp_path / "estudios" / "caso"
        demo.escribir_caso(origen / "p1", 4, 1)
        for n in range(2, pacientes + 1):                            # otros pacientes en la misma carpeta
            demo.escribir_caso(origen / f"p{n}", 4, n)
            for f in (origen / f"p{n}").rglob("IM*"):
                ds = pydicom.dcmread(f)
                ds.PatientID = f"DEMO-00099{n}"
                ds.save_as(f, enforce_file_format=True)
        self.destino = tmp_path / "destino"
        self.destino.mkdir()
        app = self.app
        assert app.CANDADO.acquire(blocking=False)
        app.TRABAJOS["c"] = {"nombre": "rapido", "estado": "corriendo", "lineas": [], "codigo": None, "pasos": [], "pacientes": [],
                             "origen": str(origen), "destino": str(self.destino), "zip": None, "mensaje": "", "archivos": {},
                             "revisado": False, "servidos": set(), "mostrados": set()}
        app._rapido("c", origen, self.destino, "ANON")
        t = app.TRABAJOS["c"]
        assert t["zip"], t["mensaje"]
        for i, s in enumerate(t["series_corrida"]):                  # el navegador muestra todos los cuadros
            for k, n in enumerate(s["cuadros"], 1):
                for c in range(1, n + 1):
                    t["servidos"].add((i, k, c))
                    app.anotar_mostrado_corrida(t, i, k, c)
        r = app.aplicar_revision_rapido(t, "Ana")
        self.t, self.zip = t, Path(r["zip"])
        self.registro = Path(r["registro_revision"])
        self.pacientes = list(t["pacientes"])
        self.reportes = app.TRABAJO / "limpios_REPORTES"
        assert all(app.leer_revision(p)["lista_para_compartir"] for p in self.pacientes)
        # lo que no debe cambiar nunca
        self.sha_zip, self.sha_registro = sha(self.zip), sha(self.registro)
        self.originales = {p: (self.reportes / f"revision_pixeles_{p}.json").read_bytes() for p in self.pacientes}

    # ---- comprobaciones comunes ----
    def nadie_autoriza(self):
        app = self.app
        for p in self.pacientes:
            estado = app.leer_revision(p)
            assert estado["estado"] == "retirada" and estado["vigente"] is False and estado["lista_para_compartir"] is False, (p, estado)
        nuevo = app.crear_paquete(self.pacientes)                    # un paquete nuevo no puede darlos por revisados
        assert nuevo["revision_pixeles_pendiente"] == self.pacientes
        with zipfile.ZipFile(nuevo["ruta"]) as z:
            m = json.loads(z.read("manifiesto.json"))
        assert m["revision_pixeles_pendiente"] == self.pacientes
        assert all(m["revision_pixeles"][p]["estado"] == "retirada" for p in self.pacientes)
        corrida = next(c for c in app.revisiones_registradas()["corridas"] if c["registro"] == self.registro.name)
        assert corrida["retirada"] is not None and corrida["se_puede_retirar"] is False

    def nada_original_cambio(self):
        assert sha(self.zip) == self.sha_zip                         # el zip exportado, byte a byte
        assert sha(self.registro) == self.sha_registro               # el registro de la corrida, en su sitio
        historial = self.reportes / "historial_revisiones"
        for p, original in self.originales.items():                  # el registro original de cada paciente: en su sitio o en el historial
            actual = (self.reportes / f"revision_pixeles_{p}.json").read_bytes()
            if actual != original:
                copias = [h for h in historial.glob(f"revision_pixeles_{p}_*.json") if h.read_bytes() == original]
                assert len(copias) == 1, f"{p}: {len(copias)} copias del original en el historial"

    def diario(self) -> dict:
        diarios = sorted((self.reportes / "retiradas").glob("retirada_*.json"))
        assert len(diarios) == 1, [d.name for d in diarios]
        return json.loads(diarios[0].read_text(encoding="utf-8"))

    def completa_y_atribuida_a(self, quien: str, motivo: str, fecha: str):
        d = self.diario()
        assert d["estado"] == "completa" and all(a["estado"] == "hecha" for a in d["acciones"])
        assert d["retirada"] == {"quien": quien, "fecha": fecha, "motivo": motivo}
        anotada = json.loads((self.reportes / (self.registro.name[:-5] + ".retirada.json")).read_text(encoding="utf-8"))
        assert anotada["retirada"] == d["retirada"] and anotada["sha256_registro"] == self.sha_registro
        for p in self.pacientes:
            actual = json.loads((self.reportes / f"revision_pixeles_{p}.json").read_text(encoding="utf-8"))
            assert actual["estado"] == "retirada" and actual["retirada"] == d["retirada"] and actual["retirada_id"] == d["id"]
            assert actual["sustituye_a"]["sha256"] == hashlib.sha256(self.originales[p]).hexdigest() and actual["sustituye_a"]["revisor"] == "Ana"
            assert (self.reportes / actual["sustituye_a"]["archivo"]).read_bytes() == self.originales[p]
        assert self.app.retiradas_incompletas() == [] and not self.app.CANDADO.locked()


def objetivo(e: Escenario, direccion: str) -> tuple[str, str]:
    return ("corrida", e.registro.name) if direccion == "corrida" else ("paciente", e.pacientes[0])


class Fallo:
    """Hace fallar una frontera concreta: la n-ésima escritura cuyo nombre cumple el filtro, o el archivado."""

    def __init__(self, app, monkeypatch):
        self.app, self.mp = app, monkeypatch
        self.escribir, self.archivar = app._escribir_json, app.archivar_registro
        self.activo = True

    def en_escritura(self, filtro, veces_antes: int = 0):
        cuenta = {"n": 0}

        def falso(ruta, datos):
            if self.activo and filtro(Path(ruta).name):
                cuenta["n"] += 1
                if cuenta["n"] > veces_antes:
                    raise OSError("disco lleno")
            return self.escribir(ruta, datos)

        self.mp.setattr(self.app, "_escribir_json", falso)
        return self

    def en_archivado(self):
        def falso(ruta):
            if self.activo:
                raise OSError("sin permiso")
            return self.archivar(ruta)

        self.mp.setattr(self.app, "archivar_registro", falso)
        return self

    def quitar(self):
        self.activo = False


es_diario = lambda n: n.startswith("retirada_")
es_anotacion_de_corrida = lambda n: n.endswith(".retirada.json")
es_registro_de_paciente = lambda n: n.startswith("revision_pixeles_")

FRONTERAS = {
    "anotar la corrida": lambda f: f.en_escritura(es_anotacion_de_corrida),
    "archivar el original del paciente": lambda f: f.en_archivado(),
    "sustituir el registro del paciente": lambda f: f.en_escritura(es_registro_de_paciente),
    "anotar en el diario la primera acción": lambda f: f.en_escritura(es_diario, veces_antes=1),
    "anotar en el diario la última acción": lambda f: f.en_escritura(es_diario, veces_antes=3),
}


@pytest.mark.parametrize("direccion", ["corrida", "paciente"])
@pytest.mark.parametrize("frontera", list(FRONTERAS))
def test_un_fallo_en_cada_frontera_deja_la_retirada_comprometida_y_terminable(tmp_path, monkeypatch, direccion, frontera):
    e = Escenario(tmp_path)
    app = e.app
    fallo = FRONTERAS[frontera](Fallo(app, monkeypatch))
    tipo, identificador = objetivo(e, direccion)

    with pytest.raises(app.RetiradaIncompleta) as x:
        app.retirar_revision(tipo, identificador, "Arturo", "falta decidir qué hacer con la cabeza")
    cuerpo = x.value.cuerpo()
    # 1. está comprometida y se dice con exactitud qué se hizo y qué falta
    assert cuerpo["comprometida"] is True and cuerpo["estado"] == "incompleta" and cuerpo["quien"] == "Arturo"
    assert len(cuerpo["pendientes"]) >= 1 and len(cuerpo["completadas"]) + len(cuerpo["pendientes"]) == 3
    assert cuerpo["completadas"] + cuerpo["pendientes"] == [a["descripcion"] for a in e.diario()["acciones"]]
    assert {(r["tipo"], r["id"]) for r in cuerpo["retiradas"]} == {("corrida", e.registro.name), ("paciente", e.pacientes[0])}
    assert "ya no autorizan compartir nada" in cuerpo["detail"] and all(p in cuerpo["detail"] for p in cuerpo["pendientes"])
    fecha = cuerpo["fecha"]
    # 2. desde ya, nada de lo que alcanza autoriza compartir, y nada original ha cambiado
    e.nadie_autoriza()
    e.nada_original_cambio()
    assert not app.CANDADO.locked()
    assert [m["retirada"] for m in app.revisiones_registradas()["retiradas_incompletas"]] == [cuerpo["retirada"]]
    # 3. reintentar con el fallo todavía presente no empeora nada ni cambia la atribución
    with pytest.raises(app.RetiradaIncompleta):
        app.retirar_revision(tipo, identificador, "Otra persona", "otro motivo distinto")
    assert e.diario()["retirada"] == {"quien": "Arturo", "fecha": fecha, "motivo": "falta decidir qué hacer con la cabeza"}
    e.nadie_autoriza()
    # 4. sin el fallo, el reintento la termina; quién, cuándo y por qué son los del primer intento
    fallo.quitar()
    r = app.retirar_revision(tipo, identificador, "Otra persona", "otro motivo distinto")
    assert r["estado"] == "completa" and r["reanudada"] is True and r["pendientes"] == [] and r["quien"] == "Arturo" and r["fecha"] == fecha
    e.completa_y_atribuida_a("Arturo", "falta decidir qué hacer con la cabeza", fecha)
    assert [x_["quien"] for x_ in e.diario()["reintentos"]][-1] == "Otra persona"        # el reintento queda anotado aparte
    e.nadie_autoriza()
    e.nada_original_cambio()
    # 5. y ya terminada, otro intento se rechaza
    with pytest.raises(HTTPException) as ya:
        app.retirar_revision(tipo, identificador, "Arturo", "otra vez")
    assert ya.value.status_code == 409


@pytest.mark.parametrize("direccion", ["corrida", "paciente"])
def test_si_el_diario_no_se_puede_escribir_no_se_retira_nada(tmp_path, monkeypatch, direccion):
    e = Escenario(tmp_path)
    app = e.app
    fallo = Fallo(app, monkeypatch).en_escritura(es_diario)          # la primera escritura del diario: el punto de compromiso
    tipo, identificador = objetivo(e, direccion)
    with pytest.raises(HTTPException) as x:
        app.retirar_revision(tipo, identificador, "Arturo", "no llega a registrarse")
    assert x.value.status_code == 500 and "no se ha retirado nada" in x.value.detail
    assert app.leer_revision(e.pacientes[0])["lista_para_compartir"] is True              # todo sigue como estaba
    assert not list((e.reportes / "retiradas").glob("retirada_*")) and not list((e.reportes / "retiradas").glob(".*"))
    assert app.revisiones_registradas()["retiradas_incompletas"] == [] and not app.CANDADO.locked()
    e.nada_original_cambio()
    fallo.quitar()
    r = app.retirar_revision(tipo, identificador, "Arturo", "ahora sí")                  # un intento nuevo, completo
    assert r["estado"] == "completa" and r["reanudada"] is False
    e.completa_y_atribuida_a("Arturo", "ahora sí", r["fecha"])


@pytest.mark.parametrize("direccion", ["corrida", "paciente"])
def test_tras_reiniciar_la_aplicacion_la_retirada_sigue_valiendo_y_se_termina(tmp_path, monkeypatch, direccion):
    e = Escenario(tmp_path)
    tipo, identificador = objetivo(e, direccion)
    Fallo(e.app, monkeypatch).en_escritura(es_registro_de_paciente)
    with pytest.raises(e.app.RetiradaIncompleta) as x:
        e.app.retirar_revision(tipo, identificador, "Arturo", "se apagó a medias")
    fecha = x.value.cuerpo()["fecha"]
    monkeypatch.undo()
    # otra instancia de la aplicación sobre la misma carpeta de trabajo: sin nada en memoria
    e.app = cargar("app_tras_reinicio", RAIZ / "herramientas" / "app.py")
    e.app.TRABAJO = tmp_path / "datos-pet"
    assert len(e.app.retiradas_incompletas()) == 1
    e.nadie_autoriza()                                               # antes de terminar nada
    e.nada_original_cambio()
    resultados = e.app.reanudar_retiradas()                          # lo que hace el arranque
    assert [r["estado"] for r in resultados] == ["completa"]
    e.completa_y_atribuida_a("Arturo", "se apagó a medias", fecha)
    e.nadie_autoriza()
    e.nada_original_cambio()


def test_una_corrida_con_dos_pacientes_cae_entera(tmp_path, monkeypatch):
    """Retirar la revisión de un paciente que escribió una corrida retira la corrida y la del otro paciente: la
    declaración de la corrida es un solo acto. Con un fallo a medias, ninguno de los dos autoriza."""
    e = Escenario(tmp_path, pacientes=2)
    app = e.app
    assert len(e.pacientes) == 2
    fallo = Fallo(app, monkeypatch).en_escritura(lambda n: n == f"revision_pixeles_{e.pacientes[1]}.json")   # falla el del otro paciente
    with pytest.raises(app.RetiradaIncompleta) as x:
        app.retirar_revision("paciente", e.pacientes[0], "Arturo", "se retira uno y cae la corrida")
    cuerpo = x.value.cuerpo()
    assert {(r["tipo"], r["id"]) for r in cuerpo["retiradas"]} == {("corrida", e.registro.name)} | {("paciente", p) for p in e.pacientes}
    assert any(e.pacientes[1] in p for p in cuerpo["pendientes"])
    por = {a["id"]: a["incluida_por"] for a in e.diario()["alcance"]}
    assert por[e.pacientes[0]] == "solicitada" and e.pacientes[0] in por[e.registro.name] and e.registro.name in por[e.pacientes[1]]
    e.nadie_autoriza()                                               # el segundo paciente tampoco, aunque su registro no se actualizó
    assert app.leer_revision(e.pacientes[1])["registro_pendiente_de_actualizar"] is True
    e.nada_original_cambio()
    fallo.quitar()
    r = app.retirar_revision("paciente", e.pacientes[1], "Otra persona", "reintento desde el otro paciente")
    assert r["estado"] == "completa" and r["reanudada"] is True and r["quien"] == "Arturo"
    e.completa_y_atribuida_a("Arturo", "se retira uno y cae la corrida", cuerpo["fecha"])
    e.nadie_autoriza()
    e.nada_original_cambio()


def test_una_revision_posterior_del_paciente_no_la_pisa_una_retirada_que_se_termina_despues(tmp_path, monkeypatch):
    e = Escenario(tmp_path)
    app, pac = e.app, e.pacientes[0]
    fallo = Fallo(app, monkeypatch).en_archivado()
    with pytest.raises(app.RetiradaIncompleta):
        app.retirar_revision("corrida", e.registro.name, "Arturo", "a medias")
    assert app.leer_revision(pac)["lista_para_compartir"] is False
    fallo.quitar()
    # mientras la retirada está a medias, alguien revisa de nuevo al paciente y lo registra
    app.indexar()
    nueva = app.guardar_revision(pac, "sin_hallazgos", [], "Miguel")
    assert nueva["lista_para_compartir"] is True and nueva["revisor"] == "Miguel"       # es otro registro: la retirada no lo alcanza
    r = app.reanudar_retiradas()[0]
    assert r["estado"] == "completa"
    assert app.leer_revision(pac)["lista_para_compartir"] is True and app.leer_revision(pac)["revisor"] == "Miguel"
    historial = [json.loads(h.read_text(encoding="utf-8")) for h in (e.reportes / "historial_revisiones").glob(f"revision_pixeles_{pac}_*.json")]
    assert [h["revisor"] for h in historial] == ["Ana"]              # el original de Ana quedó en el historial al registrar Miguel
    assert sha(e.zip) == e.sha_zip and sha(e.registro) == e.sha_registro


def test_el_cuerpo_del_error_lleva_lo_hecho_y_lo_pendiente(tmp_path, monkeypatch):
    e = Escenario(tmp_path)
    Fallo(e.app, monkeypatch).en_escritura(es_registro_de_paciente)
    with pytest.raises(e.app.RetiradaIncompleta) as x:
        e.app.retirar_revision("corrida", e.registro.name, "Arturo", "para ver el cuerpo del error")
    c = x.value.cuerpo()
    pac = e.pacientes[0]
    assert c["completadas"] == [f"anotar la retirada junto al registro de la corrida {e.registro.name}", f"guardar en el historial el registro original de {pac}"]
    assert c["pendientes"] == [f"sustituir el registro de {pac} por la retirada"]
    assert c["paquetes_afectados"][0]["ruta"] == str(e.zip) and c["paquetes_afectados"][0]["es_el_mismo_archivo"] is True
    assert set(c) >= {"detail", "retirada", "comprometida", "completadas", "pendientes", "error", "retiradas", "paquetes_afectados", "quien", "fecha", "motivo"}


def test_escribir_un_registro_es_todo_o_nada(tmp_path, monkeypatch):
    app = cargar("app_en_prueba", RAIZ / "herramientas" / "app.py")
    destino = tmp_path / "registro.json"
    app._escribir_json(destino, {"version": 1})
    real = app.os.replace

    def sin_renombrar(a, b):
        raise OSError("se cortó")

    monkeypatch.setattr(app.os, "replace", sin_renombrar)
    with pytest.raises(OSError):
        app._escribir_json(destino, {"version": 2, "relleno": "x" * 10000})
    monkeypatch.setattr(app.os, "replace", real)
    assert json.loads(destino.read_text(encoding="utf-8")) == {"version": 1}             # el anterior, entero
    assert [p.name for p in tmp_path.iterdir()] == ["registro.json"]                     # sin temporales
