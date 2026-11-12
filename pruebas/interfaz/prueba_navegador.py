#!/usr/bin/env python3
"""
Prueba de interfaz en navegador real (Playwright + Chromium), con el caso sintético: selección de fila, deslizador,
panel antes/después, botones de comprobación, formulario de revisión y Enviar. Guarda capturas de cada pestaña.
Usa localizadores y aserciones con reintento (expect): las filas se vuelven a pintar al refrescar el inventario, así
que nunca se guardan ElementHandles; se afirma el estado final de la serie y el corte elegidos, nunca un estado
transitorio de carga.

Browser test with the synthetic case; saves screenshots. Runs in CI (Linux); needs `playwright install chromium`.

Uso / Usage
  uv run --with playwright --with fastapi --with uvicorn --with pydicom --with numpy --with pillow --with nibabel \
     python pruebas/interfaz/prueba_navegador.py --capturas docs/capturas [--etiqueta despues]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--capturas", default="docs/capturas")
    p.add_argument("--etiqueta", default="despues")
    p.add_argument("--puerto", type=int, default=8791)
    a = p.parse_args()
    from playwright.sync_api import sync_playwright

    trabajo = Path(tempfile.mkdtemp(prefix="ui_"))
    log = open(trabajo / "servidor.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(RAIZ / "herramientas" / "app.py"), "--trabajo", str(trabajo), "--puerto", str(a.puerto), "--sin-navegador"],
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{a.puerto}/static/app.js", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        token = re.search(r"t=([A-Za-z0-9_-]+)", (trabajo / "servidor.log").read_text(encoding="utf-8")).group(1)
        destino = RAIZ / a.capturas
        destino.mkdir(parents=True, exist_ok=True)
        fallos: list[str] = []

        def ok(cond: bool, msg: str) -> None:
            print(("  OK   " if cond else "  FALLA ") + msg)
            if not cond:
                fallos.append(msg)

        with sync_playwright() as pw:
            from playwright.sync_api import expect
            expect.set_options(timeout=15000)
            nav = pw.chromium.launch()
            pag = nav.new_page(viewport={"width": 1366, "height": 800}, device_scale_factor=1, bypass_csp=True)  # la CSP de la app bloquea eval; Playwright lo necesita para sus propias evaluaciones
            pag.goto(f"http://127.0.0.1:{a.puerto}/?t={token}")
            expect(pag.locator("#trabajo")).not_to_be_empty()
            pag.click("#btn-demo")
            expect(pag.locator("#origen")).not_to_have_value("", timeout=60000)
            pag.screenshot(path=str(destino / f"{a.etiqueta}-1-entrada.png"))
            pag.click("#btn-anonimizar")
            expect(pag.locator("#estado-anon")).to_contain_text("Limpieza terminada", timeout=120000)
            pag.screenshot(path=str(destino / f"{a.etiqueta}-1-entrada-terminada.png"))

            # Localizadores, no ElementHandles: las filas se vuelven a pintar al refrescar el inventario.
            pag.click("nav.pestanas button[data-tab=estudios]")
            filas = pag.locator("#tabla-series tbody tr.fila-serie")
            expect(filas).to_have_count(2)
            ok(True, "inventario con 2 series")
            modalidad = lambda m: filas.filter(has=pag.locator("td:nth-child(5)", has_text=re.compile(rf"^{m}$")))
            fila_pt, fila_ct = modalidad("PT"), modalidad("CT")
            expect(fila_pt).to_have_count(1)
            expect(fila_ct).to_have_count(1)

            # selección: la fila elegida queda marcada, y solo ella
            fila_pt.click()
            expect(pag.locator("#visor")).not_to_have_class(re.compile(r"\boculto\b"))
            expect(fila_pt).to_have_class(re.compile(r"\bseleccionada\b"))
            expect(pag.locator("tr.fila-serie.seleccionada")).to_have_count(1)
            ok(True, "la fila PT seleccionada lleva la clase 'seleccionada' y es la única")
            id_pt = fila_pt.get_attribute("data-id")

            # deslizador: la imagen y el panel antes/después pasan al corte 2 de esta serie
            pag.eval_on_selector("#deslizador", "el => { el.value = 2; el.dispatchEvent(new Event('input', {bubbles: true})); }")
            expect(pag.locator("#corte")).to_have_attribute("src", re.compile(rf"/serie/{re.escape(id_pt)}/corte/2\?"))
            expect(pag.locator("#img-limpio")).to_have_attribute("src", re.compile(rf"/serie/{re.escape(id_pt)}/corte/2\?"), timeout=30000)
            expect(pag.locator("#img-original")).to_have_attribute("src", re.compile(rf"/serie/{re.escape(id_pt)}/corte_original/2\?"), timeout=30000)
            expect(pag.locator("#destacados")).to_contain_text("idénticos", timeout=30000)
            ok(True, "el deslizador cambia el corte y el panel antes/después muestra el mismo corte de la misma serie, con PixelData idéntico")
            expect(pag.locator("#etiqueta-estudio")).to_have_text("ANON-0001 · O · 63 años en el estudio")
            expect(pag.locator("#tabla-series tbody tr.fila-serie td:nth-child(2)").first).to_have_text("O · 63 años")
            ok(True, "Estudios muestra el identificador anónimo, el sexo registrado y la edad en el estudio")
            expect(pag.locator("#nota-orden")).to_have_text("Cortes ordenados por InstanceNumber.")
            ok(True, "el visor dice con qué criterio ordena los cortes")

            # cambiar de serie: la marca pasa a la fila CT y el visor muestra la serie CT
            fila_ct.click()
            expect(fila_ct).to_have_class(re.compile(r"\bseleccionada\b"))
            expect(fila_pt).not_to_have_class(re.compile(r"\bseleccionada\b"))
            id_ct = fila_ct.get_attribute("data-id")
            expect(pag.locator("#corte")).to_have_attribute("src", re.compile(rf"/serie/{re.escape(id_ct)}/corte/\d+\?"))
            expect(pag.locator("#destacados")).to_contain_text("idénticos", timeout=30000)
            expect(pag.locator("#img-original")).to_have_attribute("src", re.compile(rf"/serie/{re.escape(id_ct)}/corte_original/"), timeout=30000)
            ok(True, "al elegir la serie CT la selección, el visor y el panel antes/después pertenecen a la serie CT")

            # comprobaciones
            pag.click("#botones-comprobar button[data-que=emparejar]")
            expect(pag.locator("#salida-comprobar")).to_contain_text("CANDIDATO COMPATIBLE", timeout=60000)
            ok(True, "botón Emparejar muestra la salida")

            # revisión: botones deshabilitados hasta tener revisor; combinación contradictoria bloqueada
            expect(pag.locator("#rev-sin")).to_be_disabled()
            expect(pag.locator("#rev-con")).to_be_disabled()
            pag.fill("#rev-revisor", "prueba")
            pag.check("#rev-rostro")
            expect(pag.locator("#rev-sin")).to_be_disabled()
            expect(pag.locator("#rev-con")).to_be_enabled()
            pag.uncheck("#rev-rostro")
            expect(pag.locator("#rev-con")).to_be_disabled()
            expect(pag.locator("#rev-sin")).to_be_disabled()          # con nombre y sin hallazgos, pero aún no se ha mostrado todo
            expect(pag.locator("#rev-progreso")).to_contain_text("de 16 cortes de este paciente")
            ok(True, "botones de revisión: deshabilitados sin revisor; con un hallazgo solo 'con hallazgos'; 'sin hallazgos' espera a que se muestre todo")
            # se recorren las dos series con "Recorrer la serie": cada corte pasa por la pantalla
            for modalidad_ in ("CT", "PT"):
                modalidad(modalidad_).click()
                expect(pag.locator("#visor")).not_to_have_class(re.compile(r"\boculto\b"))
                pag.eval_on_selector("#deslizador", "el => { el.value = 0; el.dispatchEvent(new Event('input', {bubbles: true})); }")
                pag.click("#btn-recorrer")
                expect(pag.locator("#rev-progreso")).to_contain_text("8 de 8 en esta serie", timeout=60000)
            expect(pag.locator("#rev-progreso")).to_contain_text("16 de 16 cortes de este paciente")
            expect(pag.locator("#rev-progreso")).to_contain_text("Mostrado no significa revisado")
            pag.fill("#rev-revisor", "prueba")
            expect(pag.locator("#rev-sin")).to_be_enabled()
            ok(True, "Estudios: tras mostrar los 16 cortes del paciente, 'sin hallazgos' se habilita; el recuento se llama mostrado, no revisado")
            pag.click("#rev-sin")
            expect(pag.locator("#rev-estado")).to_contain_text("sin hallazgos, vigente", timeout=30000)
            ok(True, "revisión sin hallazgos registrada y vigente")
            pag.screenshot(path=str(destino / f"{a.etiqueta}-2-estudios.png"), full_page=True)

            pag.click("nav.pestanas button[data-tab=enviar]")
            expect(pag.locator("#lista-pacientes")).to_contain_text("revisión de píxeles vigente", timeout=30000)
            pag.check("#lista-pacientes input[type=checkbox]")
            pag.click("#btn-empaquetar")
            expect(pag.locator("#estado-enviar")).to_contain_text("Paquete creado", timeout=120000)
            expect(pag.locator("#estado-enviar")).to_contain_text("vigente")
            ok(True, "Enviar distingue el paquete con revisión vigente")
            pag.screenshot(path=str(destino / f"{a.etiqueta}-3-enviar.png"), full_page=True)

            # modo rápido: una carpeta, de principio a fin, y el sello de revisión
            caso = pag.input_value("#origen")
            pag.click("nav.pestanas button[data-tab=rapido]")
            expect(pag.locator("#rap-destino")).not_to_have_value("")
            pag.fill("#rap-origen", caso)
            # consulta de estado: las dos primeras peticiones fallan; la interfaz debe reintentar y terminar igualmente
            fallos = {"quedan": 2, "vistos": 0}

            def cortar_algunas(ruta):
                if fallos["quedan"] > 0:
                    fallos["quedan"] -= 1
                    fallos["vistos"] += 1
                    return ruta.abort()
                return ruta.continue_()

            pag.route("**/api/trabajo/*", cortar_algunas)
            pag.click("#btn-rapido")
            expect(pag.locator("#rap-estado")).to_contain_text("revisión de píxeles pendiente", timeout=180000)
            expect(pag.locator("#rap-pasos .paso-fila")).to_have_count(6)
            expect(pag.locator("#rap-pasos")).to_contain_text("COINCIDE")
            expect(pag.locator("#rap-pasos")).to_contain_text("IDENTICO")
            expect(pag.locator("#rap-pasos .insignia.error")).to_have_count(0)
            expect(pag.locator("#rap-revision")).not_to_have_class(re.compile(r"\boculto\b"))
            ok(True, "modo rápido: seis pasos sin fallos; el zip nace pendiente y los controles de revisión quedan a la vista")
            ok(fallos["vistos"] == 2, "modo rápido: dos consultas de estado fallidas no dejan la pantalla colgada")
            pag.unroute("**/api/trabajo/*")
            # el visor de la corrida: exactamente los archivos del paquete, en el orden de sus nombres
            expect(pag.locator("#rap-visor")).not_to_have_class(re.compile(r"\boculto\b"), timeout=30000)
            expect(pag.locator("#rap-series button")).to_have_count(2)
            expect(pag.locator("#rap-etiqueta")).to_contain_text("corte 1 de 8 · corte_000001_")
            pag.wait_for_function("document.querySelector('#rap-corte').complete && document.querySelector('#rap-corte').naturalWidth > 0", timeout=30000)
            pag.eval_on_selector("#rap-deslizador", "el => { el.value = 2; el.dispatchEvent(new Event('input', {bubbles: true})); }")
            expect(pag.locator("#rap-etiqueta")).to_contain_text("corte 2 de 8 · corte_000002_")
            pag.wait_for_function("document.querySelector('#rap-corte').src.includes('/corte/0/2?') && document.querySelector('#rap-corte').naturalWidth > 0", timeout=30000)
            pag.locator("#rap-series button").nth(1).click()
            expect(pag.locator("#rap-etiqueta")).to_contain_text("corte 1 de 8 · corte_000001_")
            expect(pag.locator("#rap-orden")).to_contain_text("Cortes ordenados por InstanceNumber")
            expect(pag.locator("#rap-etiqueta-estudio")).to_have_text("ANON-0001 · O · 63 años en el estudio")
            ok(True, "modo rápido: el visor de la corrida muestra las dos series, con el nombre exportado de cada corte")
            expect(pag.locator("#btn-rap-revision")).to_be_disabled()   # todavía no se han mostrado todos los cuadros
            expect(pag.locator("#rap-progreso")).to_contain_text("de 16 cuadros de la corrida")
            for n_ in range(2):                                     # se recorren las dos series de la corrida
                pag.locator("#rap-series button").nth(n_).click()
                pag.click("#btn-rap-recorrer")
                expect(pag.locator("#rap-progreso")).to_contain_text("8 de 8 en esta serie", timeout=60000)
            expect(pag.locator("#rap-progreso")).to_contain_text("16 de 16 cuadros de la corrida")
            expect(pag.locator("#btn-rap-revision")).to_be_enabled()
            ok(True, "modo rápido: el botón de la revisión se habilita solo cuando los 16 cuadros han estado en pantalla")
            pag.click("#btn-rap-revision")                      # sin nombre: no registra
            expect(pag.locator("#rap-estado")).to_contain_text("Indica quién revisa")
            pag.fill("#rap-revisor", "prueba")
            pag.click("#btn-rap-revision")
            expect(pag.locator("#rap-estado")).to_contain_text("Zip revisado y guardado en el destino", timeout=60000)
            expect(pag.locator("#rap-estado .mensaje")).to_have_class(re.compile(r"\bok\b"))
            expect(pag.locator("#rap-revision")).to_have_class(re.compile(r"\boculto\b"))
            ok(True, "modo rápido: el sello de revisión deja el zip en el destino y oculta los controles")

            # retirar la revisión de la corrida desde Registro: con nombre y motivo; el original se conserva
            pag.click("nav.pestanas button[data-tab=registro]")
            fila = lambda alcance: pag.locator("#tabla-revisiones tbody tr").filter(has=pag.locator("td:first-child", has_text=alcance))
            fila_corrida = fila("Corrida ·")
            expect(fila_corrida).to_have_count(1)
            fila_corrida.locator("button", has_text="Retirar").click()
            pag.click("#btn-retirar")                               # sin nombre ni motivo: no retira
            expect(pag.locator("#retirada-resultado")).to_contain_text("Indica quién retira la revisión y por qué")
            pag.fill("#retirar-quien", "prueba")
            pag.fill("#retirar-motivo", "registrada en una prueba automática")
            pag.click("#btn-retirar")
            expect(pag.locator("#retirada-resultado")).to_contain_text("Revisión retirada", timeout=30000)
            expect(pag.locator("#retirada-resultado")).to_contain_text("ya no deben compartirse")
            expect(pag.locator("#retirada-resultado")).to_contain_text("Retirar no cambia los zips ya exportados")
            expect(fila("Corrida ·")).to_contain_text("retirada el")
            expect(fila("Paciente ·")).to_contain_text("revisión retirada")
            ok(True, "Registro: retirar la revisión de la corrida deja constancia de quién y por qué, y señala el paquete afectado")
            pag.click("nav.pestanas button[data-tab=rapido]")
            pag.screenshot(path=str(destino / f"{a.etiqueta}-0-rapido.png"), full_page=True)

            # consulta de estado que falla sin parar: mensaje claro, botón principal disponible y recuperación a petición
            cortado = {"si": True}
            pag.route("**/api/trabajo/*", lambda ruta: ruta.abort() if cortado["si"] else ruta.continue_())
            pag.click("#btn-rapido")
            expect(pag.locator("#rap-estado")).to_contain_text("No se pudo consultar el estado de la corrida", timeout=60000)
            expect(pag.locator("#btn-rapido")).to_be_enabled()
            cortado["si"] = False
            pag.click("#rap-estado button.reintentar")
            expect(pag.locator("#rap-estado")).to_contain_text("revisión de píxeles pendiente", timeout=180000)
            expect(pag.locator("#rap-pasos .paso-fila")).to_have_count(6)
            ok(True, "modo rápido: si la consulta de estado falla sin parar, se dice, el botón vuelve y 'Volver a consultar' recupera la corrida")
            pag.unroute("**/api/trabajo/*")
            nav.close()
        print(f"capturas en {destino}")
        if fallos:
            print("FALLOS:", fallos)
            return 1
        return 0
    finally:
        proc.terminate()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
