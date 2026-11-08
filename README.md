# medical-images-analysis

Análisis de imágenes médicas PET/CT para investigación: anonimización en origen, emparejamiento CT + PET, cuantificación SUV validada, y después segmentación y descripción de lesiones. Colaboración entre Arturo Sánchez y el Dr. Miguel Martín Landrove (UCV).

PET/CT medical image analysis for research: anonymization at the source, CT + PET pairing, validated SUV quantification, and later lesion segmentation and description. English summary below.

## Reglas del repositorio

- Aquí viven el código, las pruebas, la documentación y los reportes de conteos. Nunca imágenes ni datos de pacientes, ni mapas de seudónimos, ni índices privados. El `.gitignore` ayuda, pero los archivos DICOM sin extensión (`IM0`, `IM1`) no se pueden excluir por nombre: por eso hay un gancho de pre-commit que inspecciona los bytes de cada archivo preparado tal como están en el índice de git (rutas separadas por NUL, así los espacios no lo esquivan) y una prueba en integración continua que recorre todo el repositorio. Ambos buscan el marcador DICM del byte 128; un DICOM sin preámbulo no se reconoce por ese medio. Los datos reales viven siempre fuera del repositorio.
- Las herramientas corren en la máquina que tiene los originales. Nada identificable sale de ella: solo copias limpias empaquetadas y verificadas, y reportes sin valores ni rutas.
- Uso de investigación y aprendizaje. Nada de esto es un producto sanitario.

## Versiones y distribución

Este repositorio es la única fuente de la aplicación: código, interfaz, herramientas, lanzadores, pruebas, documentación y flujos de publicación. Cada versión sale de una etiqueta `vX.Y.Z` y se distribuye como GitHub Release con paquetes de escritorio para Windows y macOS (doble clic, sin comandos), instrucciones en español (`docs/instalacion.md`), notas de la versión y un caso sintético de demostración. El repositorio es privado: para descargar una versión hace falta acceso de lectura. El proceso completo está en `docs/lanzamientos.md`; la primera prueba de Miguel en Windows 10 es la prueba de aceptación de un paquete que ya probamos nosotros.

## Dónde está cada cosa

| Carpeta | Contenido |
|---|---|
| `herramientas/` | `lanzador.py` (punto de entrada del paquete), `app.py`, `anonimizar.py`, `emparejar.py`, `cotejar.py`, `suv.py`, `demo.py` (caso sintético), `ventana.py`, `empaquetar.sh` (PyInstaller), lanzadores de doble clic y la validación IBSI-SUV. Su `README.md` es el manual de uso. |
| `pruebas/` | Pruebas con DICOM sintéticos que recorren la política: se ejecutan en cada cambio sin tocar datos reales. |
| `docs/` | `CAMBIOS.md`, el registro de cada hallazgo de revisión, su corrección y su prueba. |
| `.github/` | `comprobaciones.yml` (pruebas en cada cambio, Linux y Windows) y `publicar.yml` (paquetes, prueba de humo y borrador de Release al etiquetar). |

## Cómo trabajamos

Una tarea por *issue*, con objetivo, criterio de aceptación y recursos. Una rama y un *pull request* pequeños por tarea; quien no la escribió la revisa. Las comprobaciones automáticas corren en cada *pull request*: pruebas sintéticas de la política y validación del SUV contra los 58 objetos de referencia públicos. Las decisiones quedan en los *issues*, donde se pueden volver a leer meses después.

Reparto: Arturo lleva las herramientas, la seguridad y el flujo; Miguel define qué es un dato correcto (qué series, qué geometría, qué parámetros del contorno, qué umbral de acuerdo) y revisa los resultados. Sus criterios se convierten en comprobaciones automáticas.

## Empezar

```
git config core.hooksPath .githooks        # rechaza commits con DICOM o archivos privados, tengan la extensión que tengan
uv run herramientas/anonimizar.py --help
uv run herramientas/app.py
uv run --with pytest --with pydicom --with numpy --with fastapi --with uvicorn --with pillow pytest -q pruebas
```

Las comprobaciones automáticas corren las pruebas sintéticas en Linux y en un runner de Windows Server, y la validación IBSI-SUV en Linux. Eso comprueba que el código de las herramientas funciona en Windows; no es una prueba del Windows 10 de Miguel ni de sus lanzadores, selector de carpetas o ventana nativa, que requieren esa máquina.

`docs/issues-iniciales.sh` crea las dieciséis tareas iniciales con `gh` cuando se ejecuta; hasta entonces no existen en GitHub.

Desde el código fuente, el mismo punto de entrada que el paquete: `uv run herramientas/lanzador.py` abre la aplicación y `uv run herramientas/lanzador.py herramienta demo CARPETA` crea el caso sintético. Para doble clic sin terminal desde el código fuente: `herramientas/PET-CT local.command` (macOS) o `herramientas/PET-CT local.bat` (Windows); los paquetes publicados ya traen su propio ejecutable.

---

## English summary

Code, tests, documentation and count-only reports live here; patient images, pseudonym maps and private indexes never do. The tools run on the machine that holds the originals, and only verified clean packages and value-free reports leave it. Research and learning use only. See `herramientas/README.md` for the manual and `docs/CAMBIOS.md` for the review history. One issue per task, small pull requests reviewed by the other person, automatic checks on every pull request.
