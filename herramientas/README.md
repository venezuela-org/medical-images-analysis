# PET/CT local 0.7.1

Herramientas locales para el proyecto de segmentación PET/CT: un anonimizador DICOM con política versionada (`anonimizar.py`) y una aplicación que corre en tu propia computadora para limpiar, revisar y empaquetar estudios (`app.py`). Nada identificable sale de la máquina donde están los originales.

Local tools for the PET/CT segmentation project: a DICOM anonymizer with a versioned policy (`anonimizar.py`) and an app that runs on your own computer to clean, review and package studies (`app.py`). Nothing identifiable leaves the machine that holds the originals. English version below.

Estado: prototipo de investigación. La política limpia etiquetas; los píxeles no se revisan. / Status: research prototype. The policy cleans tags; pixels are not inspected.

---

## Español

### Instalación en un minuto

Solo hace falta `uv`, que instala Python y las dependencias por su cuenta.

macOS o Linux:

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows (PowerShell):

```
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Descarga `app.py` y `anonimizar.py` a la misma carpeta. No hay más pasos.

### Abrir con doble clic, sin comandos

`PET-CT local.command` (macOS) y `PET-CT local.bat` (Windows) abren la aplicación con un doble clic: instalan `uv` la primera vez si hace falta, arrancan el servidor local y abren el navegador. La ventana de terminal que aparece es la aplicación corriendo; al cerrarla se apaga. La carpeta de datos por defecto es `datos-pet` dentro de la carpeta del usuario (fuera de Documentos y de OneDrive); se cambia con la variable de entorno `PETCT_DATOS`.

Primer doble clic en macOS: si Gatekeeper dice que el archivo viene de un desarrollador no identificado, clic derecho, Abrir, y confirmar una sola vez. En Windows, si SmartScreen avisa, "Más información" y "Ejecutar de todas formas". Es lo esperable para un script que no está firmado.

### Migrar sin borrar nada

Al cambiar de versión de la herramienta no hace falta borrar la copia limpia. Reimportar la misma carpeta original sobre el mismo mapa vuelve a limpiar cada archivo en memoria y lo compara con el que ya existe: si es idéntico, lo cuenta como duplicado; si el existente se reserializa exactamente a sus propios bytes, su nombre de versión es distinto del actual y al igualar solo ese nombre coincide con la limpieza recalculada, lo actualiza en el sitio y lo cuenta como "actualizado"; en cualquier otro caso (bytes sobrantes, misma versión con otro contenido, cualquier otra diferencia) va a cuarentena. Desde 0.4.4 ese nombre de versión es estable (`PETCTLOCAL_0.4`, ligado a la política), así que futuras versiones no producen diferencias. `cotejar` tolera la diferencia de versión y la señala para que se reimporte.

El índice privado guarda la ruta limpia relativa a `limpios/`, la original relativa a su carpeta de entrada, y la carpeta de entrada relativa a la carpeta de trabajo cuando está dentro de ella. Así se puede mover el espacio de trabajo completo. Si los originales se mueven por separado, el panel ofrece un botón para indicar dónde están ahora; la nueva ubicación queda en `limpios_PRIVADO/raices.json`.

### Ventana propia (experimental)

`ventana.py` abre la misma aplicación en una ventana nativa en vez de una pestaña del navegador, con `pywebview` (WebKit en macOS, WebView2 en Windows). Todavía no se ha ejecutado en un Mac ni en un Windows reales; `app.py` y los lanzadores son la ruta probada.

```
uv run ventana.py --trabajo ~/datos-pet
```

### Uso rápido

```
uv run app.py
```

La terminal imprime una dirección con un token (`http://127.0.0.1:8765/?t=...`) y abre el navegador. La página y la API exigen el token; los dos archivos estáticos de código (`app.js`, `app.css`) no llevan datos y se sirven sin token para que el navegador pueda cargarlos. Las peticiones que no vienen de `127.0.0.1` reciben 403. No existe `/docs`.

Desde la línea de comandos, sin la aplicación:

```
uv run anonimizar.py limpiar   /ruta/Originales  /ruta/Limpios  [--prefijo ANON] [--conservar-descripciones]
uv run anonimizar.py verificar /ruta/Limpios
```

### Política `pet-ct-local/0.4`

Cada archivo limpio lleva la versión de la política en `DeidentificationMethod`, y `verificar` la exige. En la siguiente tabla se resume qué hace la limpieza.

| Aspecto | Regla |
|---|---|
| Objetos aceptados | CT, PET, MR y Segmentation (clásicos y Enhanced). SR, capturas secundarias, documentos, estados de presentación, RTSTRUCT y clases desconocidas van a cuarentena y no se copian. |
| Nombre e ID | Seudónimo `ANON-0001`. La clave de paciente es emisor + ID; sin ID, nombre + fecha de nacimiento. Dos emisores con el mismo ID son dos pacientes. |
| Fecha de nacimiento, número de acceso, médico remitente | Vacíos. |
| Características del paciente | Sexo, edad (agrupada como `090Y` a partir de 90), peso y talla se conservan porque el SUV los necesita. |
| Fechas | Desplazadas un mismo número de días por paciente, entre uno y tres años atrás. Las horas se conservan. Las fechas semánticas (versiones de grupos de contexto) no se tocan. |
| Texto libre | Lista cerrada de campos técnicos (fabricante, modelo, kernel, métodos de corrección, radiofármaco, códigos). Cualquier otro LO, LT, ST, UT, SH, UC o nombre de persona se elimina, también dentro de secuencias. Las descripciones de estudio y serie y el protocolo se eliminan por defecto; con `--conservar-descripciones` se mantienen, revisadas contra el nombre y el ID originales y sin marcas HTML ni caracteres de control. |
| `CodeMeaning` | Se reemplaza por `designador valor` del mismo código (por ejemplo `SRT C-B1031`), así no puede llevar texto ajeno. |
| URI y binarios | Todo campo URI se elimina. Todo binario (OB, OW, OF, OD, OL, OV, UN) se elimina salvo píxeles y paletas. Documentos encapsulados nunca sobreviven. |
| UID | Solo se reescriben los identificadores de instancia (estudio, serie, SOP, marco de referencia y sus referencias) bajo la raíz `1.2.826.0.1.3680043.8.498.`, de forma determinista con un secreto por mapa. Clases SOP, sintaxis de transferencia y UID semánticos (esquemas de codificación) no se tocan. |
| Etiquetas privadas | Eliminadas, salvo una lista cerrada para cuantificación PET: los factores numéricos de Philips (`7053,xx00`, `7053,xx09`), conservados solo si son numéricos, y las fechas-hora de referencia de decaimiento de GE (`0009,xx0D`, creador `GEMS_PETD_01`) y Siemens (`0071,xx22`), conservadas solo si son fechas-hora válidas y desplazadas con las demás fechas del paciente. Su creador privado se conserva solo mientras quede algún elemento permitido; cualquier otro creador se elimina. Etiquetas públicas fuera del diccionario también se eliminan. |
| Overlays y curvas | Eliminados. |
| Preámbulo y metadatos de archivo | Preámbulo puesto a cero; metadatos de archivo reconstruidos (sin títulos de aplicación ni información privada). |
| Píxeles | Sin cambios. Archivos con `BurnedInAnnotation=YES` van a cuarentena. El rostro en series de cabeza requiere un paso aparte. |
| PET | Se calcula el factor SUV de peso corporal (unidades BQML, convención QIBA, con rangos de cordura para peso, dosis, vida media e intervalo) antes y después de limpiar; si cambia, el archivo va a cuarentena y no se escribe. Que el factor sea idéntico demuestra que la limpieza no lo altera, no que el cálculo sea clínicamente correcto. |

### Lo que produce `limpiar`

| Resultado | Dónde queda | ¿Se comparte? |
|---|---|---|
| Copia limpia organizada por seudónimo, estudio y serie, con carpetas y nombres derivados de los UID nuevos (dos series distintas nunca se mezclan; una instancia repetida se omite). Dentro solo hay DICOM | `Limpios/ANON-0001/estudio_xxxx/serie_xxxx_CT_002/xxxx.dcm` | Sí |
| Reportes por corrida (`reporte_anonimizacion_FECHA.md` y `.json`, más `ultimo_reporte.*`): conteos, acciones por etiqueta, cuarentena, comprobación PET; sin valores ni rutas | `Limpios_REPORTES/` | Sí |
| `.politica.json` con la versión de la política y el prefijo, validado por `verificar` | raíz de `Limpios/` | Sí |
| Mapa de seudónimos (identidad real, desplazamiento de fechas, secreto de UID), guardado de forma atómica antes de escribir el primer archivo de cada paciente, e índice limpio → original para el panel antes/después | `Limpios_PRIVADO/mapa_seudonimos.json` y `indice_origen.jsonl` (o `--mapa`); la aplicación usa la misma carpeta | Nunca |

Si un archivo falla, va a cuarentena y la corrida continúa; el mapa ya está en disco. Un segundo `limpiar` sobre el mismo mapa da los mismos seudónimos, fechas y UID. Un archivo `.corriendo` impide dos limpiezas simultáneas sobre la misma salida.

### `verificar`, la compuerta

Revisa todos los archivos de la carpeta, no solo los que terminan en `.dcm`: cada uno debe ser un DICOM que cumple la política. La única excepción es `.politica.json` en la raíz, que se valida contra su esquema; ningún otro archivo se acepta por su nombre. Lee cada archivo completo y exige que su serialización canónica reproduzca exactamente los bytes leídos, así que bytes sobrantes o mal formados al final, aunque no sean un elemento DICOM, lo descalifican. Enlaces simbólicos, archivos que no son DICOM, preámbulo con contenido, elementos después de los píxeles, metadatos de archivo con títulos de aplicación, privadas no permitidas, texto fuera de la lista, UID sin reescribir y objetos no aceptados son problemas. Termina con código 0 si todo está limpio y 1 si no. El prefijo se lee de `.politica.json`.

### `emparejar.py`, la clasificación de un caso

Antes de procesar nada, clasifica cada posible par CT + PET de una carpeta de caso (por ejemplo `Casos S/P4`, con `SE0` y `SE1` dentro, o un paciente limpio) en uno de tres veredictos: candidato compatible, inconsistente o información insuficiente. Valida cada imagen contra su serie (paciente, estudio, marco de referencia, modalidad, matriz, espaciado, orientación, posición), señala posiciones duplicadas, huecos y números de instancia faltantes, y compara CT y PET por identificadores completos, orientación, posición de cada corte PET y solape tridimensional de los campos de visión. Un candidato compatible es apto para la validación de volumen; no es permiso para compartir ni prueba de que el examen esté completo.

Imprime conteos, geometría y etiquetas cortas con sal aleatoria de cada corrida; nunca valores de identidad, nombres de carpeta ni horas de adquisición. Se puede correr sobre originales en tu computadora.

```
uv run emparejar.py "Casos S/P4"
uv run emparejar.py ~/datos-pet/limpios/ANON-0003 --json par.json
```

Código 0 candidato compatible, 1 inconsistente, 3 información insuficiente.

### `cotejar.py`, el cotejo exacto original contra limpio

Usa el mapa privado para establecer la correspondencia exacta: cada original aceptado se vuelve a limpiar en memoria con el mismo seudónimo, desplazamiento y secreto, se serializa y se compara byte a byte con el archivo limpio que debe existir en la ruta derivada de los UID reescritos. Todo original sin archivo limpio debe figurar en la cuarentena del reporte de la corrida; los archivos que no son DICOM deben coincidir con los omitidos del reporte; ningún archivo limpio puede existir sin original. Un cambio de un solo byte en la copia (píxeles, interpretación fotométrica, una fecha, bytes sobrantes) es una diferencia. Imprime conteos y los seudónimos del caso, que son los nombres de carpeta que necesitas para los pasos siguientes.

```
uv run cotejar.py "Casos S/P4" ~/datos-pet/limpios
```

La carpeta original debe ser la misma que se pasó a `limpiar`. Código 0 si coincide, 1 si hay diferencias.
### `suv.py`, SUV de peso corporal según IBSI-SUV

Calcula el SUVbw de una serie PET siguiendo el manual IBSI-SUV de OncoRay, cuadro por cuadro: unidades BQML, GML con sus tipos de SUV, CM2ML y CNTS de Philips; corrección de decaimiento ADMIN, START y NONE; hora de inicio del escaneo por etiquetas privadas de GE y Siemens, por adquisición igual a serie, o por retrocálculo según el fabricante; hora de administración con y sin fecha, cruce de medianoche y radionúclidos de vida larga; objetos Enhanced PET. Cuando falta un atributo requerido se niega a calcular y dice por qué.

```
uv run suv.py calcular ~/datos-pet/limpios/ANON-0001/estudio_xxxx/serie_xxxx_PT_000 [--mascara roi.nii.gz] [--nifti suv.nii.gz]
uv run suv.py validar  ruta/al/repositorio/suv_computation/DRO
```

Validación incluida en `validacion_ibsi/`: los 58 objetos de referencia digitales del repositorio `oncoray/suv_computation` dan el resultado esperado (43 calculables con mín, mediana y máx exactos; 15 casos de error rechazados por la razón correcta), y las 16 series reales de TCIA del conjunto de validación dan los valores listados en `validacion_series_reales.csv` para comparar con otra implementación, por ejemplo el módulo PET SUV de 3D Slicer.

Esta política conserva las fechas-hora privadas de referencia de GE y Siemens precisamente porque `suv.py` las usa como primera fuente de la hora de inicio del escaneo, como recomienda el manual.


### La aplicación

| Pestaña | Qué hace |
|---|---|
| 1. Entrada | Selector nativo de carpeta (título fijo). Corre `limpiar` en segundo plano; muestra la salida y el reporte, incluida la cuarentena. Casilla para conservar descripciones (por defecto se eliminan). Se niega a correr si la carpeta de trabajo está sincronizada con la nube. |
| 2. Estudios | Series limpias por seudónimo, estudio y serie, con la política que lleva cada archivo. Vista corte por corte, ordenada por número de instancia, con ventanas CT y una ventana automática para PET que es solo de vista: aquí no hay SUV. Debajo, el panel "Antes y después": la imagen original y la limpia con la misma ventana, la comprobación de que los píxeles son idénticos (SHA-256), y qué se hizo con cada etiqueta (seudónimo, vaciada, desplazada, reescrita, eliminada), siempre por nombre de etiqueta y nunca con el valor original. El panel usa el índice privado `limpios_PRIVADO/indice_origen.jsonl` que escribe `limpiar` (también para duplicados verificados, así que reimportar una carpeta reconstruye el índice sin volver a escribir imágenes) y solo funciona en la máquina donde están los originales. Debajo de los metadatos, cuatro botones corren `emparejar`, `cotejar`, `suv` y la comparación vóxel a vóxel de SUV original contra limpio sobre el paciente o la serie en pantalla, y muestran su salida. Más abajo, la revisión de píxeles del paciente: se registra sin hallazgos o con hallazgos (texto grabado, rostro, otro) contra los hashes exactos de sus archivos y pasa a pendiente si cambian. |
| 3. Enviar | Muestra junto a cada paciente si su revisión de píxeles está vigente o pendiente, y el manifiesto lo registra: un zip puede existir localmente sin estar listo para compartirse. Elige pacientes uno por uno (ninguno marcado por defecto). Solo acepta seudónimos del inventario, contenidos en `limpios/`, sin enlaces simbólicos. Cada archivo se lee una sola vez: esos mismos bytes se verifican contra la política, se hashean en el manifiesto y se escriben al zip, que se relee y comprueba antes de publicarse con nombre único. Solo entran DICOM limpios; ningún otro archivo se copia, ni reportes. El mapa privado no puede entrar. |
| Registro | Bitácora privada en la carpeta de trabajo; rutas y seudónimos, nunca valores. |

Seguridad de la página: `Content-Security-Policy` con scripts y estilos solo desde la propia aplicación, sin código en línea; los metadatos se insertan como texto, nunca como HTML.

Carpeta de trabajo, por defecto `~/datos-pet` (se cambia con `--trabajo`), con permisos solo para tu usuario:

```
~/datos-pet/limpios/            copia anonimizada            se comparte
~/datos-pet/limpios_REPORTES/   reportes por corrida         se comparte
~/datos-pet/paquetes/           zips verificados + manifiesto  se comparte
~/datos-pet/PRIVADO/    mapa de seudónimos           nunca sale
~/datos-pet/registro.log
```

### Límites conocidos

- Los píxeles no se inspeccionan: texto grabado sin la etiqueta `BurnedInAnnotation` y rostros no se detectan.
- La detección de carpetas sincronizadas es por nombre de ruta; es un aviso, no una prueba.
- La vista previa no es cuantitativa ni geométrica (sin marcadores de orientación ni corrección de aspecto).
- Factores de cuantificación en etiquetas privadas de otros fabricantes requieren añadirse a la lista permitida con evidencia.
- Los campos de código (CS) y numéricos se conservan tal cual por ser estructurales; un valor malicioso cabría en ellos. La verificación afirma que las etiquetas cumplen la política; no afirma que el archivo sea anónimo en todos los sentidos.
- Las clases Enhanced CT/PET/MR y Segmentation están permitidas pero no validadas con muestras reales.
- La detección de carpetas sincronizadas no ve la opción "Escritorio y Documentos" de iCloud, que sincroniza `~/Documents` sin cambiar su ruta. Revisa esa opción en Ajustes del Sistema antes de guardar originales ahí.

### Orden de trabajo

1. Ingesta e identidad (esta versión). 2. Exportación (esta versión). 3. Un caso PET/CT completo: `emparejar` sobre el original, `limpiar`, `cotejar` original contra limpio (que imprime el seudónimo asignado), `emparejar` sobre la carpeta de ese seudónimo (hecho en los diez casos), y `suv.py` sobre la serie PET original y la limpia con el mismo factor y la misma estrategia. Compartir sigue exigiendo la revisión de píxeles aparte. 4. Revisión experta con máscaras editables e historial. 5. Modelos y radiómica, uno a la vez, contra anotaciones de referencia.

---

## English

### One-minute install

Only `uv` is needed; it installs Python and the dependencies on its own.

macOS or Linux:

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows (PowerShell):

```
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Download `app.py` and `anonimizar.py` into the same folder. That is the whole setup.

### Open with a double-click, no commands

`PET-CT local.command` (macOS) and `PET-CT local.bat` (Windows) open the app with a double-click: they install `uv` the first time if needed, start the local server and open the browser. The terminal window that appears is the app running; closing it shuts it down. The default data folder is `datos-pet` inside the user's home folder (outside Documents and OneDrive); change it with the `PETCT_DATOS` environment variable.

First double-click on macOS: if Gatekeeper says the file comes from an unidentified developer, right-click, Open, and accept once. On Windows, if SmartScreen warns, "More info" then "Run anyway". That is expected for an unsigned script.

### Migrating without deleting anything

Changing tool version does not require deleting the clean copy. Re-importing the same original folder over the same map re-cleans each file in memory and compares it with the existing one: identical counts as a duplicate; if the existing file re-serializes exactly to its own bytes, its version name differs from the current one, and equalizing only that name makes it match the recomputed cleaning, it is upgraded in place and counted as "actualizado"; in any other case (trailing bytes, same version with different content, any other difference) it goes to quarantine. Since 0.4.4 that version name is stable (`PETCTLOCAL_0.4`, tied to the policy), so later versions produce no differences. `cotejar` tolerates the version difference and flags it so you re-import.

The private index stores the clean path relative to `limpios/`, the original relative to its input folder, and the input folder relative to the work folder when it sits inside it, so the whole workspace can move. If the originals move on their own, the panel offers a button to point at their new location, saved in `limpios_PRIVADO/raices.json`.

### Native window (experimental)

`ventana.py` opens the same app in a native window instead of a browser tab, through `pywebview` (WebKit on macOS, WebView2 on Windows). It has not yet run on a real Mac or Windows machine; `app.py` and the launchers are the tested path.

```
uv run ventana.py --trabajo ~/datos-pet
```

### Quick use

```
uv run app.py
```

The terminal prints an address with a token (`http://127.0.0.1:8765/?t=...`) and opens the browser. The page and the API require the token; the two static code files (`app.js`, `app.css`) carry no data and are served without it so the browser can load them. Requests not coming from `127.0.0.1` get 403. There is no `/docs`.

From the command line, without the app:

```
uv run anonimizar.py limpiar   /path/Originals  /path/Clean  [--prefijo ANON] [--conservar-descripciones]
uv run anonimizar.py verificar /path/Clean
```

### Policy `pet-ct-local/0.4`

Every clean file carries the policy version in `DeidentificationMethod`, and `verificar` requires it. The table below summarizes the cleaning rules.

| Aspect | Rule |
|---|---|
| Accepted objects | CT, PET, MR and Segmentation (classic and Enhanced). SR, secondary captures, documents, presentation states, RTSTRUCT and unknown classes are quarantined and never copied. |
| Name and ID | Pseudonym `ANON-0001`. Patient key is issuer + ID; without an ID, name + birth date. Two issuers with the same ID are two patients. |
| Birth date, accession number, referring physician | Emptied. |
| Patient characteristics | Sex, age (grouped as `090Y` from 90 up), weight and height kept, since SUV needs them. |
| Dates | Shifted by one per-patient offset, one to three years back. Times kept. Semantic dates (context group versions) untouched. |
| Free text | Closed allowlist of technical fields (manufacturer, model, kernel, correction methods, radiopharmaceutical, codes). Any other LO, LT, ST, UT, SH, UC or person name is removed, inside sequences too. Study and series descriptions and protocol name are removed by default; `--conservar-descripciones` keeps them, scanned for the original name and ID, without HTML marks or control characters. |
| `CodeMeaning` | Replaced by `designator value` of its own code (e.g. `SRT C-B1031`), so it cannot carry foreign text. |
| URIs and binaries | Every URI field is removed. Every binary (OB, OW, OF, OD, OL, OV, UN) is removed except pixels and palettes. Encapsulated documents never survive. |
| UIDs | Only instance identifiers (study, series, SOP, frame of reference and their references) are remapped under the root `1.2.826.0.1.3680043.8.498.`, deterministically with a per-map secret. SOP classes, transfer syntaxes and semantic UIDs (coding schemes) are untouched. |
| Private tags | Removed, except a closed allowlist for PET quantification: Philips numeric factors (`7053,xx00`, `7053,xx09`), kept only when numeric, and the GE (`0009,xx0D`, creator `GEMS_PETD_01`) and Siemens (`0071,xx22`) decay-reference datetimes, kept only when valid datetimes and date-shifted like every other date of the patient. Their private creator is kept only while an allowed element remains; any other creator is removed. Unknown public tags are removed too. |
| Overlays and curves | Removed. |
| Preamble and file meta | Preamble zeroed; file meta rebuilt (no application entity titles, no private information). |
| Pixels | Untouched. Files with `BurnedInAnnotation=YES` are quarantined. Faces in head series need a separate step. |
| PET | The body-weight SUV factor (BQML units, QIBA convention, with sanity ranges for weight, dose, half-life and interval) is computed before and after cleaning; if it changes, the file is quarantined and not written. An identical factor shows cleaning does not alter it, not that the calculation is clinically correct. |

### What `limpiar` produces

| Output | Location | Shareable? |
|---|---|---|
| Clean copy organized by pseudonym, study and series, with folder and file names derived from the new UIDs (distinct series never merge; a repeated instance is skipped). Nothing but DICOM inside | `Clean/ANON-0001/estudio_xxxx/serie_xxxx_CT_002/xxxx.dcm` | Yes |
| Per-run reports (`reporte_anonimizacion_DATE.md` and `.json`, plus `ultimo_reporte.*`): counts, actions per tag, quarantine, PET check; no values, no paths | `Clean_REPORTES/` | Yes |
| `.politica.json` with the policy version and prefix, validated by `verificar` | root of `Clean/` | Yes |
| Pseudonym map (real identity, date offset, UID secret), written atomically before the first file of each patient, plus the clean → original index for the before/after panel | `Clean_PRIVADO/mapa_seudonimos.json` and `indice_origen.jsonl` (or `--mapa`); the app uses the same folder | Never |

If a file fails, it is quarantined and the run continues; the map is already on disk. A second `limpiar` against the same map yields the same pseudonyms, dates and UIDs. A `.corriendo` lock file prevents two simultaneous runs on the same output.

### `verificar`, the gate

It checks every file in the folder, not only those ending in `.dcm`: each must be a DICOM that meets the policy. The only exception is `.politica.json` at the root, validated against its schema; no other file is accepted by name. It reads each file in full and requires its canonical serialization to reproduce exactly the bytes read, so trailing or malformed bytes, even ones that are not a DICOM element, disqualify it. Symlinks, non-DICOM files, a non-zero preamble, elements after the pixel data, file meta with application entity titles, private tags outside the allowlist, text outside the allowlist, unmapped UIDs and non-accepted objects are problems. Exit code 0 when clean, 1 otherwise. The prefix is read from `.politica.json`.

### `emparejar.py`, the case classification

Before processing anything, it classifies each possible CT + PET pair in a case folder (for example `Casos S/P4`, with `SE0` and `SE1` inside, or a clean patient) into one of three verdicts: compatible candidate, inconsistent, or insufficient information. It validates every image against its series (patient, study, frame of reference, modality, matrix, spacing, orientation, position), flags duplicated positions, gaps and missing instance numbers, and compares CT and PET by full identifiers, orientation, the position of every PET slice and three-dimensional overlap of the fields of view. A compatible candidate is fit for volume validation; it is neither permission to share nor proof that the examination is complete.

It prints counts, geometry and short labels salted per run; never identity values, folder names or acquisition times. It can run on originals on your computer.

```
uv run emparejar.py "Casos S/P4"
uv run emparejar.py ~/datos-pet/limpios/ANON-0003 --json par.json
```

Exit 0 compatible candidate, 1 inconsistent, 3 insufficient information.

### `cotejar.py`, exact original-versus-clean comparison

It uses the private map to establish exact correspondence: every accepted original is re-cleaned in memory with the same pseudonym, offset and secret, serialized, and compared byte for byte with the clean file that must exist at the path derived from the remapped UIDs. Every original without a clean file must appear in the run's quarantine record; non-DICOM files must match the report's skipped count; no clean file may exist without an original. A single changed byte in the copy (pixels, photometric interpretation, a date, trailing bytes) is a difference. It prints counts and the case's pseudonyms, which are the folder names you need for the next steps.

```
uv run cotejar.py "Casos S/P4" ~/datos-pet/limpios
```

The original folder must be the one given to `limpiar`. Exit 0 when everything matches, 1 on differences.
### `suv.py`, body-weight SUV per IBSI-SUV

Computes SUVbw for a PET series following OncoRay's IBSI-SUV manual, frame by frame: BQML, GML with its SUV types, CM2ML and Philips CNTS units; ADMIN, START and NONE decay correction; scan start from the GE and Siemens private datetimes, from acquisition equal to series, or by vendor-specific back-computation; administration time with and without a date, midnight crossing and long-lived radionuclides; Enhanced PET objects. When a required attribute is missing it refuses to compute and says why.

```
uv run suv.py calcular ~/datos-pet/limpios/ANON-0001/estudio_xxxx/serie_xxxx_PT_000 [--mascara roi.nii.gz] [--nifti suv.nii.gz]
uv run suv.py validar  path/to/suv_computation/DRO
```

Validation shipped in `validacion_ibsi/`: all 58 digital reference objects of the `oncoray/suv_computation` repository give the expected result (43 computable with exact min, median and max; 15 error cases refused for the right reason), and the 16 real TCIA series of its validation set give the values listed in `validacion_series_reales.csv`, ready to compare against another implementation such as 3D Slicer's PET SUV module.

This policy keeps the GE and Siemens private decay-reference datetimes precisely because `suv.py` uses them as the first source of the scan start, as the manual recommends.


### The app

| Tab | What it does |
|---|---|
| 1. Entrada | Native folder picker (fixed title). Runs `limpiar` in the background; shows the output and the report, quarantine included. Checkbox to keep descriptions (removed by default). Refuses to run if the work folder is cloud-synced. |
| 2. Estudios | Clean series by pseudonym, study and series, with the policy each file carries. Slice-by-slice view, ordered by instance number, with CT windows and an automatic PET window that is for viewing only: no SUV here. Below it, the "Antes y después" panel: original and clean image with the same window, the check that pixels are identical (SHA-256), and what was done to each tag (pseudonym, emptied, shifted, remapped, removed), always by tag name and never with the original value. It uses the private index `limpios_PRIVADO/indice_origen.jsonl` written by `limpiar` (for verified duplicates too, so re-importing a folder rebuilds the index without rewriting images) and only works on the machine that holds the originals. Under the metadata, four buttons run `emparejar`, `cotejar`, `suv` and the voxel-wise original-versus-clean SUV comparison on the patient or series on screen, and show their output. Below, the patient's pixel review: recorded as no findings or with findings (burned-in text, face, other) against the exact hashes of its files, and back to pending if they change. |
| 3. Enviar | Shows next to each patient whether the pixel review is current or pending, and the manifest records it: a zip can exist locally without being ready to share. Pick patients one by one (none selected by default). Accepts only inventory pseudonyms contained in `limpios/`, no symlinks. Each file is read once: those same bytes are verified against the policy, hashed into the manifest and written to the zip, which is re-read and checked before being published under a unique name. Only clean DICOM goes in; no other file is copied, reports included. The private map cannot get in. |
| Registro | Private log in the work folder; paths and pseudonyms, never values. |

Page security: `Content-Security-Policy` with scripts and styles only from the app itself, no inline code; metadata is inserted as text, never as HTML.

Work folder, `~/datos-pet` by default (change with `--trabajo`), with permissions for your user only:

```
~/datos-pet/limpios/            anonymized copy              shareable
~/datos-pet/limpios_REPORTES/   per-run reports              shareable
~/datos-pet/paquetes/           verified zips + manifest     shareable
~/datos-pet/PRIVADO/    pseudonym map                never leaves
~/datos-pet/registro.log
```

### Known limits

- Pixels are not inspected: burned-in text without the `BurnedInAnnotation` flag and faces are not detected.
- Synced-folder detection is by path name; it is a warning, not proof.
- The preview is neither quantitative nor geometric (no orientation markers, no aspect correction).
- Quantification factors in other vendors' private tags must be added to the allowlist with evidence.
- Code (CS) and numeric fields are kept as they are, being structural; a malicious value could fit in them. Verification states that tags meet the policy; it does not state that a file is anonymous in every sense.
- Enhanced CT/PET/MR and Segmentation classes are allowed but not validated against real samples.
- Synced-folder detection does not see iCloud's "Desktop & Documents" option, which syncs `~/Documents` without changing its path. Check that option in System Settings before keeping originals there.

### Release order

1. Ingestion and identity (this version). 2. Export (this version). 3. One complete PET/CT case: `emparejar` on the original, `limpiar`, `cotejar` original against clean (which prints the assigned pseudonym), `emparejar` on that pseudonym's folder (done for all ten cases), and `suv.py` on the original and the clean PET series with the same factor and strategy. Sharing still requires the separate pixel review. 4. Expert review with editable masks and history. 5. Models and radiomics, one at a time, against reference annotations.
