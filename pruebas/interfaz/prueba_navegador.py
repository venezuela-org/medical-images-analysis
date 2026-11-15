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
        fallos: list[str] = []          # comprobaciones fallidas; decide el código de salida. Ningún otro uso de este nombre.

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
            # la casilla de conservar descripciones: se explica, y volver a limpiar con ella un estudio ya guardado
            # dice qué pasó en lugar de dejar solo "terminó con cuarentena"
            pag.click("nav.pestanas button[data-tab=entrada]")
            pag.locator("#ayuda-conservar > summary").click()
            expect(pag.locator("#ayuda-conservar")).to_contain_text("No conserva ningún otro dato del paciente")
            expect(pag.locator("#ayuda-conservar")).to_contain_text("todavía no están guardados en esta carpeta de trabajo")
            pag.check("#conservar")
            pag.click("#btn-anonimizar")
            expect(pag.locator("#estado-anon")).to_contain_text("Terminó con cuarentena o avisos", timeout=120000)
            explicacion = pag.locator("#explicacion-anon")
            expect(explicacion).to_contain_text("16 imágenes ya estaban guardadas con la otra opción de descripciones")
            expect(explicacion).to_contain_text("limpiadas sin la casilla de conservar descripciones")
            expect(explicacion).to_contain_text("La copia guardada no cambió")
            expect(explicacion).to_contain_text("DICOM en cuarentena")
            # Una limpieza rechazada por el candado externo no hereda el reporte anterior.
            candado_externo = trabajo / "limpios" / ".corriendo"
            candado_externo.write_text("prueba", encoding="utf-8")
            try:
                pag.click("#btn-anonimizar")
                expect(pag.locator("#estado-anon")).to_contain_text("Terminó con error", timeout=60000)
                expect(explicacion).to_be_empty()
                expect(pag.locator("#det-reporte")).to_be_hidden()
                expect(pag.locator("#salida-anon")).to_contain_text(".corriendo")
            finally:
                candado_externo.unlink(missing_ok=True)
            ok(True, "Entrada: una limpieza fallida no muestra las cifras ni el reporte de la anterior")
            pag.uncheck("#conservar")
            pag.click("nav.pestanas button[data-tab=estudios]")
            expect(pag.locator("#tabla-series tbody tr.fila-serie")).to_have_count(2)
            expect(pag.locator("#tabla-series tbody tr.fila-serie td:nth-child(3)").first).to_have_text("Estudio 1")
            ok(True, "Entrada: la casilla de descripciones se explica, y limpiar de nuevo con ella un estudio guardado dice qué pasó y que nada cambió")
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
            consultas_cortadas = {"quedan": 2, "vistos": 0}

            def cortar_algunas(ruta):
                if consultas_cortadas["quedan"] > 0:
                    consultas_cortadas["quedan"] -= 1
                    consultas_cortadas["vistos"] += 1
                    return ruta.abort()
                return ruta.continue_()

            pag.route("**/api/trabajo/*", cortar_algunas)
            pag.click("#btn-rapido")
            # tres estados separados: el proceso terminó, la revisión está pendiente y el paquete no ha salido
            expect(pag.locator("#rap-f-paquete")).to_contain_text("Creado, sin publicar", timeout=180000)
            expect(pag.locator("#rap-f-proceso")).to_contain_text("Terminado")
            expect(pag.locator("#rap-f-proceso")).to_contain_text("6 de 6 comprobaciones correctas")
            expect(pag.locator("#rap-f-revision")).to_contain_text("Pendiente")
            expect(pag.locator("#rap-f-revision")).to_contain_text("Nadie ha declarado la revisión")
            expect(pag.locator("#rap-f-revision")).to_have_class(re.compile(r"\baviso\b"))      # el proceso en verde no pone en verde la revisión
            expect(pag.locator("#rap-pasos .paso-fila")).to_have_count(6)
            expect(pag.locator("#rap-pasos .paso-fila[data-estado=ok]")).to_have_count(6)
            expect(pag.locator("#rap-pasos .paso-fila[data-estado=error]")).to_have_count(0)
            fila_limpieza = pag.locator("#rap-pasos .paso-fila[data-clave=limpieza]")
            expect(fila_limpieza.locator(".paso-estado")).to_have_text("Correcto")
            expect(fila_limpieza.locator(".paso-resumen")).to_contain_text("16 archivos anonimizados, de 1 paciente")
            expect(fila_limpieza.locator(".paso-cifra")).to_contain_text("16")
            expect(pag.locator("#rap-pasos .paso-fila[data-clave=cotejo] .paso-resumen")).to_contain_text("los 16 coinciden byte a byte")
            expect(pag.locator("#rap-pasos .paso-fila[data-clave=suv] .paso-resumen")).to_contain_text("1 serie PET con valores idénticos")
            fila_limpieza.locator("> summary").click()                 # cada paso se abre: qué comprueba, sus cifras y la salida
            expect(fila_limpieza.locator(".paso-detalle")).to_contain_text("Omitidos por no ser imágenes DICOM")
            expect(fila_limpieza.locator(".paso-detalle")).to_contain_text("DICOM apartados en cuarentena")
            fila_limpieza.locator("> summary").click()
            expect(pag.locator("#rap-revision")).not_to_have_class(re.compile(r"\boculto\b"))
            expect(pag.locator("#rap-resumen")).to_contain_text("Pendiente: nadie la ha declarado")
            expect(pag.locator("#rap-resumen")).to_contain_text("PET-CT local")
            expect(pag.locator("#rap-paquete-ficha")).to_contain_text("Creado, sin publicar")
            expect(pag.locator("#rap-paquete-ficha")).to_contain_text("Adónde irá")
            ok(True, "modo rápido: seis pasos sin fallos; el zip nace pendiente y los controles de revisión quedan a la vista")
            ok(True, "modo rápido: procesamiento, revisión y paquete se muestran por separado, cada uno con su estado")
            ok(consultas_cortadas["vistos"] == 2, "modo rápido: dos consultas de estado fallidas no dejan la pantalla colgada")
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
            expect(pag.locator("#rap-porque")).to_contain_text("Escribe tu nombre")
            pag.fill("#rap-revisor", "prueba")
            pag.press("#rap-revisor", "Enter")                  # Intro en el nombre no declara nada: hay que pulsar el botón
            pag.wait_for_timeout(400)
            expect(pag.locator("#rap-f-revision")).to_contain_text("Pendiente")
            pag.click("#btn-rap-revision")
            expect(pag.locator("#rap-f-paquete")).to_contain_text("Publicado en el destino", timeout=60000)
            expect(pag.locator("#rap-f-paquete")).to_have_class(re.compile(r"\bok\b"))
            expect(pag.locator("#rap-f-revision")).to_contain_text("Registrada")
            expect(pag.locator("#rap-f-revision")).to_contain_text("Declarada por prueba")
            expect(pag.locator("#rap-revision")).to_have_class(re.compile(r"\boculto\b"))
            expect(pag.locator("#rap-resumen")).to_contain_text("Registrada y vigente")
            expect(pag.locator("#rap-resumen")).to_contain_text("16 de 16, en el navegador")
            expect(pag.locator("#rap-resumen")).to_contain_text("Ninguna")
            ok(True, "modo rápido: el sello de revisión deja el zip en el destino y oculta los controles")
            # el resumen que se copia: estados, cifras y versiones; ni rutas, ni nombres de archivo, ni quién revisó
            copiado = pag.evaluate("textoResumen(ULTIMA, ULTIMA.situacion)")
            ok("Revisión de píxeles: Registrada" in copiado and "Paquete: Publicado en el destino" in copiado and "PET-CT local" in copiado
               and "prueba" not in copiado and not re.search(r"[\\/][A-Za-z_.~]", copiado.replace("pet-ct-local/0", "").replace("original / limpio", ""))
               and ".dcm" not in copiado and ".zip" not in copiado and "ANON-" not in copiado and "datos-pet" not in copiado,
               "modo rápido: el resumen copiado lleva estados y cifras, sin rutas, nombres de archivo, etiqueta del estudio ni revisor")

            # retirar la revisión de la corrida desde Registro: con nombre y motivo; el original se conserva
            pag.click("nav.pestanas button[data-tab=registro]")
            fila = lambda alcance: pag.locator("#tabla-revisiones tbody tr").filter(has=pag.locator("td:first-child", has_text=alcance))
            fila_corrida = fila("Corrida ·")
            expect(fila_corrida).to_have_count(1)
            fila_corrida.locator("button", has_text="Retirar").click()
            pag.click("#btn-retirar")                               # sin nombre ni motivo: no retira
            expect(pag.locator("#retirada-resultado")).to_contain_text("Indica quién retira la revisión y por qué")
            # Un zip huérfano deja incertidumbre persistente también en la fila de corrida.
            (trabajo / "paquetes" / "paquete_sin_manifiesto.zip").write_bytes(b"zip ilegible de prueba")
            pag.fill("#retirar-quien", "prueba")
            pag.fill("#retirar-motivo", "registrada en una prueba automática")
            pag.click("#btn-retirar")
            expect(pag.locator("#retirada-resultado")).to_contain_text("Revisión retirada", timeout=30000)
            expect(pag.locator("#retirada-resultado")).to_contain_text("ya no deben compartirse")
            expect(pag.locator("#retirada-resultado")).to_contain_text("Retirar no cambia los zips ya exportados")
            expect(fila("Corrida ·")).to_contain_text("retirada el")
            pag.click("nav.pestanas button[data-tab=rapido]")
            pag.click("nav.pestanas button[data-tab=registro]")
            expect(fila("Corrida ·")).to_contain_text("lista de paquetes puede estar incompleta")
            expect(fila("Corrida ·")).to_contain_text("paquete_sin_manifiesto.zip")
            # Diarios antiguos o dañados: la incertidumbre sigue visible al reabrir Registro.
            diario = next((trabajo / "limpios_REPORTES" / "retiradas").glob("retirada_*.json"))
            guardado = json.loads(diario.read_text(encoding="utf-8"))
            for inventario in (None, {"completo": True, "problemas": 1, "sin_determinar": []}):
                datos = dict(guardado)
                if inventario is None:
                    datos.pop("inventario_de_paquetes", None)
                else:
                    datos["inventario_de_paquetes"] = inventario
                diario.write_text(json.dumps(datos), encoding="utf-8")
                pag.click("nav.pestanas button[data-tab=rapido]")
                pag.click("nav.pestanas button[data-tab=registro]")
                expect(fila("Corrida ·")).to_contain_text("no se sabe si la lista de paquetes está completa")
            diario.write_text(json.dumps(guardado), encoding="utf-8")
            expect(fila("Paciente ·")).to_contain_text("revisión retirada")
            ok(True, "Registro: retirar la revisión de la corrida deja constancia de quién y por qué, y señala el paquete afectado")
            pag.click("nav.pestanas button[data-tab=rapido]")
            # al volver a Rápido el estado se pide de nuevo: la revisión retirada no se sigue mostrando como registrada
            expect(pag.locator("#rap-f-revision")).to_contain_text("Retirada", timeout=30000)
            expect(pag.locator("#rap-f-paquete")).to_contain_text("revisión retirada")
            expect(pag.locator("#rap-revision-aviso")).to_contain_text("La revisión de esta corrida se retiró")
            expect(pag.locator("#rap-revision-aviso")).to_contain_text("ya no debe compartirse")
            ok(True, "modo rápido: una revisión retirada en Registro se ve retirada al volver, con qué pasó, a qué afecta y qué hacer")
            # el estado de las retiradas deja de poder verificarse: no se da nada por vigente y se dice qué diario es
            bueno = diario.read_bytes()
            diario.write_bytes(bueno[:25])
            pag.click("#btn-rap-comprobar")
            expect(pag.locator("#rap-f-revision")).to_contain_text("No verificable", timeout=30000)
            expect(pag.locator("#rap-f-revision")).to_have_class(re.compile(r"\berror\b"))
            expect(pag.locator("#rap-revision-aviso")).to_contain_text("No se puede verificar el estado de las retiradas")
            expect(pag.locator("#rap-revision-aviso")).to_contain_text(diario.name)
            expect(pag.locator("#rap-f-paquete")).to_contain_text("revisión sin verificar")
            expect(pag.locator("#rap-f-proceso")).to_contain_text("Terminado")          # el procesamiento sigue siendo lo que fue
            diario.write_bytes(bueno)
            pag.locator("#rap-revision-aviso button", has_text="Volver a comprobar").click()
            expect(pag.locator("#rap-f-revision")).to_contain_text("Retirada", timeout=30000)
            ok(True, "modo rápido: con un diario de retirada ilegible la revisión pasa a no verificable, y vuelve al leerse el diario")
            pag.screenshot(path=str(destino / f"{a.etiqueta}-0-rapido.png"), full_page=True)

            # consulta de estado que falla sin parar: mensaje claro, botón principal disponible y recuperación a petición
            cortado = {"si": True}
            pag.route("**/api/trabajo/*", lambda ruta: ruta.abort() if cortado["si"] else ruta.continue_())
            pag.click("#btn-rapido")
            expect(pag.locator("#rap-estado")).to_contain_text("No se pudo consultar el estado de la corrida", timeout=60000)
            expect(pag.locator("#btn-rapido")).to_be_enabled()
            cortado["si"] = False
            pag.click("#rap-estado button.reintentar")
            expect(pag.locator("#rap-f-paquete")).to_contain_text("Creado, sin publicar", timeout=180000)
            expect(pag.locator("#rap-f-revision")).to_contain_text("Pendiente")
            expect(pag.locator("#rap-pasos .paso-fila")).to_have_count(6)
            ok(True, "modo rápido: si la consulta de estado falla sin parar, se dice, el botón vuelve y 'Volver a consultar' recupera la corrida")
            pag.unroute("**/api/trabajo/*")
            nav.close()
        print(f"capturas en {destino}")
        assert isinstance(fallos, list), "la lista de comprobaciones fallidas se ha sobrescrito"
        if fallos:
            print("FALLOS:", fallos)
            return 1
        return 0
    finally:
        proc.terminate()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
