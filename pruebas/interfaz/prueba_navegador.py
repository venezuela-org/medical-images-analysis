#!/usr/bin/env python3
"""
Prueba de interfaz en navegador real (Playwright + Chromium), con el caso sintético: selección de fila, deslizador,
panel antes/después, botones de comprobación, formulario de revisión y Enviar. Guarda capturas de cada pestaña.

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
            nav = pw.chromium.launch()
            pag = nav.new_page(viewport={"width": 1366, "height": 800}, device_scale_factor=1, bypass_csp=True)  # la CSP de la app bloquea eval; Playwright lo necesita para sus propias evaluaciones
            pag.goto(f"http://127.0.0.1:{a.puerto}/?t={token}")
            pag.wait_for_selector("#trabajo")
            pag.click("#btn-demo")
            pag.wait_for_function("document.querySelector('#origen').value.length > 0", timeout=60000)
            pag.screenshot(path=str(destino / f"{a.etiqueta}-1-entrada.png"))
            pag.click("#btn-anonimizar")
            pag.wait_for_function("document.querySelector('#estado-anon').textContent.includes('Limpieza terminada')", timeout=120000)
            pag.screenshot(path=str(destino / f"{a.etiqueta}-1-entrada-terminada.png"))

            pag.click("nav.pestanas button[data-tab=estudios]")
            pag.wait_for_selector("#tabla-series tbody tr.fila-serie")
            filas = pag.query_selector_all("#tabla-series tbody tr.fila-serie")
            ok(len(filas) == 2, f"inventario con 2 series ({len(filas)})")
            # selección: la fila elegida queda marcada y distinta de las demás
            pt = next(f for f in filas if "PT" in f.inner_text())
            pt.click()
            pag.wait_for_selector("#visor:not(.oculto)")
            ok("seleccionada" in (pt.get_attribute("class") or ""), "la fila PT seleccionada lleva la clase 'seleccionada'")
            ok(sum(1 for f in pag.query_selector_all("tr.fila-serie.seleccionada")) == 1, "una sola fila seleccionada")
            # deslizador: cambia la imagen y el panel antes/después sigue a la selección
            src1 = pag.get_attribute("#corte", "src")
            pag.eval_on_selector("#deslizador", "el => { el.value = 2; el.dispatchEvent(new Event('input', {bubbles: true})); }")
            pag.wait_for_function(f"document.querySelector('#corte').src !== {json.dumps(src1)}")
            pag.wait_for_function("document.querySelector('#destacados').children.length > 0", timeout=30000)
            ok("/corte/2?" in (pag.get_attribute("#corte", "src") or ""), "el deslizador cambia el corte mostrado")
            ok("/corte_original/2?" in (pag.get_attribute("#img-original", "src") or ""), "el panel antes/después muestra el mismo corte")
            ok("idénticos" in pag.inner_text("#destacados"), "las tarjetas dicen que los bytes de PixelData son idénticos")
            # cambiar de serie limpia el panel y la selección sigue a la nueva fila
            ct = next(f for f in pag.query_selector_all("#tabla-series tbody tr.fila-serie") if "CT" in f.inner_text())
            ct.click()
            pag.wait_for_function("document.querySelector('#antes-despues').classList.contains('oculto') || document.querySelector('#destacados').children.length === 0")
            ok("seleccionada" in (ct.get_attribute("class") or ""), "al elegir la serie CT la marca de selección cambia de fila")
            # comprobaciones
            pag.click("#botones-comprobar button[data-que=emparejar]")
            pag.wait_for_function("document.querySelector('#salida-comprobar').textContent.includes('CANDIDATO')", timeout=60000)
            ok(True, "botón Emparejar muestra la salida")
            # revisión: botones deshabilitados hasta tener revisor; contradicción bloqueada
            ok(pag.is_disabled("#rev-sin") and pag.is_disabled("#rev-con"), "botones de revisión deshabilitados sin revisor")
            pag.fill("#rev-revisor", "prueba")
            pag.check("#rev-rostro")
            ok(pag.is_disabled("#rev-sin") and not pag.is_disabled("#rev-con"), "con un hallazgo marcado solo se habilita 'con hallazgos'")
            pag.uncheck("#rev-rostro")
            ok(not pag.is_disabled("#rev-sin") and pag.is_disabled("#rev-con"), "sin hallazgos marcados solo se habilita 'sin hallazgos'")
            pag.click("#rev-sin")
            pag.wait_for_function("document.querySelector('#rev-estado').textContent.includes('Lista para compartir')", timeout=30000)
            ok(True, "revisión sin hallazgos registrada y vigente")
            pag.screenshot(path=str(destino / f"{a.etiqueta}-2-estudios.png"), full_page=True)

            pag.click("nav.pestanas button[data-tab=enviar]")
            pag.wait_for_selector("#lista-pacientes label")
            pag.wait_for_function("document.querySelector('#lista-pacientes').textContent.includes('revisión de píxeles')", timeout=30000)
            pag.check("#lista-pacientes input[type=checkbox]")
            pag.click("#btn-empaquetar")
            pag.wait_for_function("document.querySelector('#estado-enviar').textContent.includes('Paquete creado')", timeout=120000)
            ok("vigente" in pag.inner_text("#estado-enviar"), "Enviar distingue el paquete con revisión vigente")
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
