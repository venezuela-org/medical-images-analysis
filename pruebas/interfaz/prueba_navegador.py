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
            modalidad = lambda m: filas.filter(has=pag.locator("td:nth-child(4)", has_text=re.compile(rf"^{m}$")))
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
            expect(pag.locator("#rev-sin")).to_be_enabled()
            expect(pag.locator("#rev-con")).to_be_disabled()
            ok(True, "botones de revisión: deshabilitados sin revisor; con un hallazgo solo 'con hallazgos'; sin hallazgos solo 'sin hallazgos'")
            pag.click("#rev-sin")
            expect(pag.locator("#rev-estado")).to_contain_text("Lista para compartir", timeout=30000)
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
