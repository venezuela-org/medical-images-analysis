"""
Estados de la pestaña Rápido, con respuestas del servidor controladas. / Rápido tab states, with controlled server responses.

Esta prueba NO ejecuta el flujo real: abre la aplicación y sustituye las respuestas de la API por datos preparados, para
poner la pantalla en estados que con el caso sintético no se dan (avisos, un bloqueo real, fallos al publicar, una corrida
grande). Comprueba que la pantalla dice lo que el servidor dice, sin inventar atajos ni mezclar estados.
El recorrido completo, de verdad, está en prueba_navegador.py.

  uv run --with playwright --with fastapi --with uvicorn --with pydicom --with numpy --with pillow --with nibabel \\
      python pruebas/interfaz/prueba_estados.py --capturas capturas
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

RAIZ = Path(__file__).resolve().parents[2]
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAAAAADhZOFXAAAAEklEQVR4nGNgYGD4z4AHMAxNBQBmkAEAyoNfVwAAAABJRU5ErkJggg==")
TITULOS = [("limpieza", "Limpieza"), ("compuerta", "Compuerta de metadatos"), ("cotejo", "Cotejo con el original"),
           ("emparejamiento", "Emparejamiento CT + PET"), ("suv", "SUV original / limpio"), ("paquete", "Paquete")]
DATOS_OK = {
    "limpieza": {"leidos": 195, "en_la_corrida": 192, "escritos": 192, "ya_guardados": 0, "actualizados": 0, "omitidos": {"no es DICOM / not DICOM": 3},
                 "omitidos_total": 3, "cuarentena_total": 0, "cuarentena_motivos": {}, "pacientes": 1},
    "compuerta": {"comprobados": 192, "con_problemas": 0}, "cotejo": {"clase": "coincide", "coinciden": 192, "esperados": 192},
    "emparejamiento": {"estudios": 1, "compatibles": 1, "inconsistentes": 0, "no_evaluables": 0}, "suv": {"series": 1, "clases": {"identico": 1}},
    "paquete": {"clase": "creado", "archivos": 192, "series": 2, "pacientes": 1, "orden_no_verificado": 0, "orden_por_posicion": 0, "con_varios_cuadros": 0,
                "no_mostrables": 0, "de_importaciones_anteriores": 0},
}
ZIP_PENDIENTE = "/Users/demo/datos-pet/paquetes/paquete_20261114_103918_2f0d43.zip"
ZIP_PUBLICADO = "/Users/demo/Desktop/para-enviar/paquete_20261114_104233_623764.zip"


def corrida(estados: list[str] | None = None, **cambios) -> dict:
    """Una corrida terminada sin avisos, con la revisión pendiente. `cambios` sustituye lo que haga falta."""
    estados = estados or ["ok"] * 6
    pasos = [{"clave": c, "titulo": t, "estado": e, "resumen": "frase del servidor para " + t, "salida": "salida de " + t if e not in ("pendiente", "corriendo") else "",
              "datos": copy.deepcopy(DATOS_OK[c]) if e in ("ok", "aviso") else {}} for (c, t), e in zip(TITULOS, estados)]
    hay_zip = estados[-1] in ("ok", "aviso")
    cuenta = lambda x: sum(1 for e in estados if e == x)
    proc = {"estado": "en_curso" if "corriendo" in estados else ("detenido" if "error" in estados else ("terminado_con_avisos" if "aviso" in estados else "terminado")),
            "paso_en_curso": estados.index("corriendo") + 1 if "corriendo" in estados else None, "paso_detenido": estados.index("error") + 1 if "error" in estados else None,
            "correctos": cuenta("ok"), "con_aviso": cuenta("aviso"), "no_evaluados": cuenta("omitido"), "total": 6}
    t = {"nombre": "rapido", "estado": "corriendo" if "corriendo" in estados else ("error" if "error" in estados else ("con avisos" if "aviso" in estados else "ok")),
         "pasos": pasos, "mensaje": "", "zip": ZIP_PENDIENTE if hay_zip else None, "destino": "/Users/demo/Desktop/para-enviar", "revisado": False, "pacientes": ["ANON-0001"],
         "no_mostrables": [], "retiradas_no_verificables": [], "demografia": [{"etiqueta": "ANON-0001 · F · 54 años en el estudio"}],
         "progreso": {"mostrados": 0, "total": 192, "completo": False, "series": [{"indice": 0, "mostrados": 0, "total": 96}, {"indice": 1, "mostrados": 0, "total": 96}]},
         "situacion": {"procesamiento": proc, "versiones": {"aplicacion": "0.10.0", "politica": "pet-ct-local/0.4"}, "comprobado": "2026-11-14T10:40:00",
                       "revision": {"estado": "pendiente", "cuadros_mostrados": 0, "cuadros_totales": 192, "no_mostrables": 0} if hay_zip else {"estado": "sin_paquete"},
                       "paquete": {"estado": "pendiente_local", "ruta": ZIP_PENDIENTE, "existe": True, "con_revision_registrada": False, "destino": "/Users/demo/Desktop/para-enviar",
                                   "destino_sincronizado": None, "archivos": 192, "pacientes": ["ANON-0001"], "acciones_pendientes": [], "ultimo_intento_fallido": None}
                       if hay_zip else {"estado": "no_creado"}}}
    for clave, valor in cambios.items():
        destino = t
        partes = clave.split("__")
        for parte in partes[:-1]:
            destino = destino[int(parte)] if isinstance(destino, list) else destino[parte]
        destino[partes[-1]] = valor
    return t


def series(n_series: int = 2, cortes: int = 96, cuadros: list[int] | None = None) -> list[dict]:
    return [{"serie": f"ANON-0001/estudio_abc/serie_{i:02d}_{'CT' if i % 2 == 0 else 'PT'}_{i + 1:03d}", "modalidad": "CT" if i % 2 == 0 else "PT", "n": cortes,
             "nombres": [f"corte_{k:06d}_{i:02d}aa.dcm" for k in range(1, cortes + 1)], "cuadros": (cuadros or [1] * cortes), "criterio": "InstanceNumber", "aviso": "",
             "demografia": {"etiqueta": "ANON-0001 · F · 54 años en el estudio", "sexo": {"estado": "registrado", "valor": "F"}, "edad_en_el_estudio": {"estado": "calculada"}}}
            for i in range(n_series)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capturas", default=None)
    ap.add_argument("--puerto", type=int, default=8791)
    a = ap.parse_args()
    capturas = Path(a.capturas) if a.capturas else None
    if capturas:
        capturas.mkdir(parents=True, exist_ok=True)
    fallos: list[str] = []          # comprobaciones fallidas; decide el código de salida. Ningún otro uso de este nombre.

    def ok(cond: bool, msg: str) -> None:
        print(("  OK   " if cond else "  FALLA ") + msg)
        if not cond:
            fallos.append(msg)

    with tempfile.TemporaryDirectory() as tmp:
        log = open(Path(tmp) / "app.log", "w")
        proc = subprocess.Popen([sys.executable, str(RAIZ / "herramientas" / "app.py"), "--trabajo", str(Path(tmp) / "datos-pet"), "--puerto", str(a.puerto), "--sin-navegador"],
                                stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(160):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{a.puerto}/static/app.js", timeout=1)
                    break
                except Exception:
                    time.sleep(0.5)
            token = re.search(r"t=([A-Za-z0-9_-]+)", (Path(tmp) / "app.log").read_text()).group(1)
            estado = {"trabajo": corrida(), "series": series(), "cortar": False}
            with sync_playwright() as pw:
                nav = pw.chromium.launch()
                pag = nav.new_page(viewport={"width": 1280, "height": 900}, bypass_csp=True)
                responder = lambda cuerpo: (lambda ruta: ruta.fulfill(status=200, content_type="application/json", body=json.dumps(cuerpo() if callable(cuerpo) else cuerpo)))
                pag.route("**/api/rapido", responder({"trabajo": "fijo"}))
                pag.route("**/api/trabajo/fijo", lambda ruta: ruta.abort() if estado["cortar"] else ruta.fulfill(status=200, content_type="application/json", body=json.dumps(estado["trabajo"])))
                pag.route("**/api/rapido/fijo/series", responder(lambda: estado["series"]))
                pag.route("**/api/rapido/fijo/progreso", responder(lambda: estado["trabajo"]["progreso"]))
                pag.route("**/api/rapido/fijo/mostrado", responder(lambda: estado["trabajo"]["progreso"]))
                pag.route("**/api/rapido/fijo/corte/**", lambda ruta: ruta.fulfill(status=200, content_type="image/png", body=PNG))
                pag.goto(f"http://127.0.0.1:{a.puerto}/?t={token}")
                expect(pag.locator("#trabajo")).not_to_be_empty()
                pag.click("nav.pestanas button[data-tab=rapido]")
                pag.fill("#rap-origen", "/Users/demo/estudios/caso-sintetico-01")
                franja = lambda parte: pag.locator(f"#rap-f-{parte}")
                paso = lambda clave: pag.locator(f"#rap-pasos .paso-fila[data-clave={clave}]")

                def correr(trabajo: dict, espera: str, nombre: str | None = None, s: list[dict] | None = None) -> None:
                    estado["trabajo"], estado["series"] = trabajo, s or series()
                    pag.click("#btn-rapido")
                    expect(franja("proceso")).to_contain_text(espera, timeout=20000)
                    if capturas and nombre:
                        pag.wait_for_timeout(300)
                        pag.screenshot(path=str(capturas / f"estado-{nombre}.png"), full_page=True)

                texto_rapido = lambda: pag.inner_text("#rapido")

                # 1. en curso: se dice el paso y el tiempo, sin porcentajes ni barras de avance inventadas
                estado["trabajo"] = corrida(["ok", "ok", "corriendo", "pendiente", "pendiente", "pendiente"])
                pag.click("#btn-rapido")
                expect(franja("proceso")).to_contain_text("En curso, paso 3 de 6", timeout=20000)
                expect(franja("proceso")).to_contain_text("Ahora: cotejo con el original")
                expect(franja("revision")).to_contain_text("Todavía no")
                expect(franja("paquete")).to_contain_text("Todavía no existe")
                expect(paso("cotejo").locator(".paso-estado")).to_have_text("En curso")
                expect(paso("suv").locator(".paso-estado")).to_have_text("En espera")
                expect(pag.locator("#rap-reloj")).to_contain_text("tiempo transcurrido")
                expect(pag.locator("#btn-rapido")).to_be_disabled()
                ok("%" not in texto_rapido() and pag.locator("#rap-area-proceso progress, #rap-area-proceso .barra").count() == 0,
                   "en curso: paso y tiempo transcurrido, sin porcentaje ni barra de avance del proceso")
                ok(pag.locator("#rap-area-revision").is_hidden() and pag.locator("#rap-area-paquete").is_hidden() and pag.locator("#rap-area-resumen").is_hidden(),
                   "en curso: los apartados de revisión, paquete y resumen no aparecen todavía")
                if capturas:
                    pag.screenshot(path=str(capturas / "estado-en-curso.png"), full_page=True)
                estado["trabajo"] = corrida()                        # la corrida termina; la consulta siguiente lo recoge
                expect(franja("proceso")).to_contain_text("Terminado", timeout=20000)
                expect(franja("revision")).to_contain_text("Pendiente")
                expect(franja("paquete")).to_contain_text("Creado, sin publicar")
                ok(True, "al terminar: procesamiento terminado, revisión pendiente y paquete sin publicar, cada uno por su lado")

                # 2. avisos: se explican y no se ofrece ningún atajo
                avisos = corrida(["aviso", "ok", "ok", "omitido", "aviso", "ok"])
                avisos["pasos"][0]["datos"].update(cuarentena_total=2, cuarentena_motivos={"modalidad no admitida": 2}, en_la_corrida=190)
                avisos["pasos"][3]["datos"] = {"estudios": 1, "compatibles": 0, "inconsistentes": 0, "no_evaluables": 1}
                avisos["pasos"][4]["datos"] = {"series": 1, "clases": {"no_calculable": 1}}
                correr(avisos, "Terminado con avisos", "avisos")
                expect(paso("limpieza").locator(".paso-resumen")).to_contain_text("3 archivos no son imágenes DICOM y se omitieron")
                expect(paso("limpieza").locator(".paso-resumen")).to_contain_text("2 DICOM quedaron en cuarentena, fuera del paquete")
                expect(paso("emparejamiento").locator(".paso-estado")).to_have_text("No evaluado")
                expect(paso("emparejamiento").locator(".paso-resumen")).to_contain_text("1 estudio que no se pudo evaluar")
                expect(paso("suv").locator(".paso-estado")).to_have_text("Aviso")
                expect(paso("suv").locator(".paso-resumen")).to_contain_text("no se puede calcular el SUV porque faltan datos")
                expect(paso("suv").locator(".paso-resumen")).to_contain_text("No es una discrepancia")
                paso("suv").locator("> summary").click()
                expect(paso("suv").locator(".paso-detalle")).to_contain_text("tanto en el original como en la copia limpia")
                paso("limpieza").locator("> summary").click()
                expect(paso("limpieza").locator(".paso-detalle")).to_contain_text("no son lo mismo que los archivos omitidos")
                expect(paso("limpieza").locator(".paso-detalle")).to_contain_text("En cuarentena: modalidad no admitida")
                ok(pag.locator("#rapido button", has_text=re.compile("Continuar", re.I)).count() == 0 and "Continuar sin SUV" not in texto_rapido(),
                   "avisos: cuarentena, par no evaluable y SUV no calculable se explican; no hay ningún botón para saltarse nada")
                expect(franja("revision")).to_contain_text("Pendiente")
                expect(pag.locator("#btn-rap-revision")).to_be_disabled()
                inconsistente = corrida(["ok", "ok", "ok", "aviso", "ok", "ok"])
                inconsistente["pasos"][3]["datos"] = {"estudios": 1, "compatibles": 0, "inconsistentes": 1, "no_evaluables": 0}
                correr(inconsistente, "Terminado con avisos")
                expect(paso("emparejamiento").locator(".paso-resumen")).to_contain_text("CT y PET no se corresponden")
                expect(paso("emparejamiento").locator(".paso-resumen")).to_contain_text("El paquete se crea igual")
                ok(True, "avisos: un par inconsistente se distingue de uno no evaluable, y ninguno detiene el proceso")

                # 3. un bloqueo real: una discrepancia de SUV detiene el proceso y no hay paquete
                bloqueo = corrida(["ok", "ok", "ok", "ok", "error", "omitido"], mensaje="El SUV calculado sobre la copia limpia difiere del original; no se creó ningún zip.")
                bloqueo["pasos"][4]["datos"] = {"series": 1, "clases": {"discrepancia": 1}}
                correr(bloqueo, "Detenido en el paso 5", "bloqueo-suv")
                expect(franja("proceso")).to_have_class(re.compile(r"\berror\b"))
                expect(franja("paquete")).to_contain_text("No se creó")
                expect(franja("revision")).to_contain_text("No hay paquete que revisar")
                expect(paso("suv").locator(".paso-estado")).to_have_text("Detenido aquí")
                expect(paso("suv").locator(".paso-resumen")).to_contain_text("SUV difiere")
                expect(paso("suv")).to_have_attribute("open", "")     # el paso detenido se abre solo
                expect(paso("suv").locator(".paso-detalle")).to_contain_text("el emparejamiento CT + PET es otra comprobación y no la explica")
                expect(paso("paquete").locator(".paso-estado")).to_have_text("No ejecutado")
                expect(pag.locator("#rap-resumen")).to_contain_text("No se creó ningún paquete")
                ok(pag.locator("#rap-area-revision").is_hidden() and pag.locator("#rap-area-paquete").is_hidden(),
                   "bloqueo real: discrepancia de SUV, proceso detenido en su paso, sin paquete y sin apartado de revisión")
                cotejo = corrida(["ok", "ok", "error", "omitido", "omitido", "omitido"], mensaje="La copia limpia no coincide con la limpieza recalculada; no se creó ningún zip.")
                cotejo["pasos"][2]["datos"] = {"clase": "no_coincide", "esperados": 192}
                correr(cotejo, "Detenido en el paso 3")
                expect(paso("cotejo").locator(".paso-resumen")).to_contain_text("La copia limpia no coincide con la limpieza repetida")
                expect(paso("emparejamiento").locator(".paso-estado")).to_have_text("No ejecutado")
                ok(True, "bloqueo real: un cotejo que no coincide detiene el proceso; los pasos siguientes constan como no ejecutados")

                # 4. la publicación falló: no se dice 'publicado' y el motivo del servidor queda a la vista
                fallo = corrida()
                fallo["progreso"] = {"mostrados": 192, "total": 192, "completo": True, "series": [{"indice": 0, "mostrados": 96, "total": 96}, {"indice": 1, "mostrados": 96, "total": 96}]}
                fallo["situacion"]["revision"].update(cuadros_mostrados=192)
                fallo["situacion"]["paquete"]["ultimo_intento_fallido"] = {"fecha": "2026-11-14T10:41:00", "restos": ["/Users/demo/Desktop/para-enviar/.tmp_paquete.zip"],
                                                                            "detalle": "no se pudo escribir en el destino (PermissionError). No se pudieron retirar estos archivos intermedios: /Users/demo/Desktop/para-enviar/.tmp_paquete.zip"}
                correr(fallo, "Terminado", "publicacion-fallida")
                expect(franja("paquete")).to_contain_text("Creado, sin publicar")
                expect(franja("paquete")).to_contain_text("El último intento de publicarlo falló")
                expect(pag.locator("#rap-paquete-avisos")).to_contain_text("El zip revisado no se publicó")
                expect(pag.locator("#rap-paquete-avisos")).to_contain_text(".tmp_paquete.zip")          # los archivos pendientes de retirar se conservan en el mensaje
                expect(franja("revision")).to_contain_text("Pendiente")
                ok("Publicado" not in pag.inner_text("#rap-franja"), "publicación fallida: el paquete sigue como no publicado, con el motivo y los archivos intermedios a la vista")

                # 5. publicado, con avisos posteriores: no se dice que no se publicó
                pub = corrida(revisado=True, zip=ZIP_PUBLICADO)
                pub["situacion"]["revision"] = {"estado": "registrada", "revisor": "Revisora de Prueba", "fecha": "2026-11-14T10:42:33", "declaracion": "sin_hallazgos",
                                                "mostrado": {"cuadros_mostrados": 192, "cuadros_totales": 192}, "registro": "revision_rapido_20261114_104233_9c1e.json",
                                                "registro_provisional": True, "cuadros_mostrados": 192, "cuadros_totales": 192, "no_mostrables": 0}
                pub["situacion"]["paquete"].update(estado="publicado", ruta=ZIP_PUBLICADO, con_revision_registrada=True, acciones_pendientes=[
                    {"clase": "evidencia", "texto": "el registro de la revisión quedó con su nombre provisional aunque el zip está publicado: /Users/demo/datos-pet/limpios_REPORTES/revision_rapido_20261114_104233_9c1e.json.preparada"},
                    {"clase": "limpieza", "texto": "no se pudo eliminar de la carpeta de trabajo: " + ZIP_PENDIENTE}])
                correr(pub, "Terminado", "publicado-con-avisos")
                expect(franja("paquete")).to_contain_text("Publicado, con avisos")
                expect(franja("paquete")).to_have_class(re.compile(r"\baviso\b"))
                expect(franja("revision")).to_contain_text("Registrada")
                expect(pag.locator("#rap-paquete-avisos")).to_contain_text("El zip está publicado; queda algo por atender")
                expect(pag.locator("#rap-paquete-avisos")).to_contain_text("nombre provisional")
                expect(pag.locator("#rap-paquete-avisos")).to_contain_text("no se pudo eliminar de la carpeta de trabajo")
                ok("no se publicó" not in texto_rapido().lower(), "publicado con avisos: se dice que está publicado y qué queda por atender, nunca que no se publicó")
                expect(pag.locator("#rap-revision")).to_be_hidden()

                # 6. una corrida grande: resúmenes compactos, no miles de filas
                grande = corrida()
                grande["pasos"][0]["datos"].update(leidos=5981, en_la_corrida=5968, escritos=0, ya_guardados=5968, omitidos={"no es DICOM / not DICOM": 13}, omitidos_total=13, pacientes=10)
                grande["pasos"][1]["datos"] = {"comprobados": 5968, "con_problemas": 0}
                grande["pasos"][2]["datos"] = {"clase": "coincide", "coinciden": 5968, "esperados": 5968}
                grande["pasos"][3]["datos"] = {"estudios": 10, "compatibles": 10, "inconsistentes": 0, "no_evaluables": 0}
                grande["pasos"][4]["datos"] = {"series": 10, "clases": {"identico": 10}}
                grande["pasos"][5]["datos"].update(archivos=5968, series=20, pacientes=10)
                grande["progreso"] = {"mostrados": 0, "total": 5968, "completo": False, "series": [{"indice": i, "mostrados": 0, "total": 298} for i in range(20)]}
                grande["situacion"]["revision"].update(cuadros_totales=5968)
                grande["situacion"]["paquete"].update(archivos=5968)
                correr(grande, "Terminado", "corrida-grande", series(20, 298))
                expect(paso("limpieza").locator(".paso-resumen")).to_contain_text("5968 archivos anonimizados, de 10 pacientes")
                expect(pag.locator("#rap-series button")).to_have_count(20)
                elementos = pag.evaluate("document.querySelectorAll('#rapido *').length")
                desplaza = pag.evaluate("(() => { const s = document.querySelector('#rap-series'); return s.scrollHeight > s.clientHeight + 4; })()")
                ok(elementos < 900 and desplaza, f"corrida grande (5.968 archivos, 20 series): {elementos} elementos en la pestaña; las series se desplazan en su caja")
                expect(pag.locator("#rap-porque")).to_contain_text("Faltan 5968")

                # 7. archivos con varios cuadros: el segundo deslizador y su etiqueta
                correr(corrida(), "Terminado", None, series(2, 2, [3, 1]))
                expect(pag.locator("#rap-cuadros")).to_be_visible()
                pag.eval_on_selector("#rap-cuadro", "el => { el.value = 2; el.dispatchEvent(new Event('input', {bubbles: true})); }")
                expect(pag.locator("#rap-etiqueta")).to_contain_text("cuadro 2 de 3")
                pag.eval_on_selector("#rap-deslizador", "el => { el.value = 2; el.dispatchEvent(new Event('input', {bubbles: true})); }")
                expect(pag.locator("#rap-cuadros")).to_be_hidden()
                ok(True, "varios cuadros: el deslizador de cuadros aparece en el archivo que los tiene y la etiqueta dice cuál se ve")

                # 8. teclado: los pasos se abren con Intro o Espacio; Escape no cambia nada
                correr(corrida(), "Terminado")
                resumen_paso = paso("compuerta").locator("> summary")
                resumen_paso.focus()
                pag.keyboard.press("Enter")
                expect(paso("compuerta")).to_have_attribute("open", "")
                contorno = resumen_paso.evaluate("el => getComputedStyle(el).outlineStyle")     # tras usar el teclado, el foco se ve
                pag.keyboard.press("Escape")
                expect(paso("compuerta")).to_have_attribute("open", "")
                expect(franja("revision")).to_contain_text("Pendiente")
                pag.keyboard.press("Space")
                ok(paso("compuerta").get_attribute("open") is None and contorno != "none", "teclado: un paso se abre y se cierra con Intro y Espacio, con el foco a la vista; Escape no cambia nada")
                orden = pag.evaluate("""(() => { const ids = []; let n = 0; document.querySelector('#rap-origen').focus();
                    const visibles = [...document.querySelectorAll('#rapido input, #rapido button, #rapido summary, #rapido select')].filter(e => e.offsetParent !== null && !e.disabled);
                    return visibles.slice(0, 6).map(e => e.id || e.tagName.toLowerCase()); })()""")
                ok(orden[:5] == ["rap-origen", "btn-rap-origen", "rap-destino", "btn-rap-destino", "btn-rapido"], f"teclado: el orden de tabulación sigue el de la pantalla ({', '.join(orden[:5])})")

                # 9. pantalla estrecha: una columna, sin desplazamiento horizontal
                pag.set_viewport_size({"width": 640, "height": 900})
                pag.wait_for_timeout(200)
                ancho = pag.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
                cajas = [franja(x).bounding_box() for x in ("proceso", "revision", "paquete")]
                ok(ancho[0] <= ancho[1] + 1 and cajas[0]["y"] < cajas[1]["y"] < cajas[2]["y"] and abs(cajas[0]["x"] - cajas[2]["x"]) < 2,
                   f"pantalla estrecha (640 px): los tres estados se apilan y no hay desplazamiento horizontal ({ancho[0]} de {ancho[1]} px)")
                expect(pag.locator("#rap-corte")).to_be_visible()
                if capturas:
                    pag.screenshot(path=str(capturas / "estado-pantalla-estrecha.png"), full_page=True)
                pag.set_viewport_size({"width": 1280, "height": 900})

                # 10. si no se puede volver a comprobar, lo que queda en pantalla se marca como sin comprobar
                estado["cortar"] = True
                pag.click("#btn-rap-comprobar")
                expect(pag.locator("#rap-estado")).to_contain_text("No se pudo comprobar el estado de la corrida", timeout=20000)
                expect(pag.locator("#rap-franja")).to_have_class(re.compile(r"\bcaduco\b"))
                estado["cortar"] = False
                pag.click("#rap-estado button.reintentar")
                expect(pag.locator("#rap-franja")).not_to_have_class(re.compile(r"\bcaduco\b"), timeout=20000)
                ok(True, "sin respuesta al volver a comprobar: el estado mostrado se marca como sin comprobar y se recupera al responder")
                nav.close()
        finally:
            proc.terminate()
            log.close()
    assert isinstance(fallos, list), "la lista de comprobaciones fallidas se ha sobrescrito"
    if fallos:
        print("FALLOS:", fallos)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
