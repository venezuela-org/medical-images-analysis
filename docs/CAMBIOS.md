# Cambios 0.2 / Changes 0.2

Respuesta a la revisión independiente del 7 de noviembre de 2026. Cada hallazgo se cerró con un cambio y con una prueba que lo reproduce primero y lo comprueba después. Las pruebas usaron copias de IM136 e IM0 a IM9 y variantes sintéticas con valores `ZZ`; ningún original se modificó ni salió de la máquina.

Response to the independent review of 7 November 2026. Each finding was closed with a change and with a test that reproduced it first and checked it afterwards.

En la siguiente tabla se ve el hallazgo, el cambio y cómo se probó (finding, change, test).

| # | Hallazgo / Finding | Cambio / Change | Prueba / Test |
|---|---|---|---|
| 1 | `..` como paciente alcanzaba el mapa privado | Solo seudónimos `^[A-Z0-9]{2,12}-\d{4}$`, resueltos y contenidos en `limpios/`, sin enlaces simbólicos; cada identificador se valida antes de tocar nada | `..`, `../PRIVADO`, `ANON-0001/..`, `PRIVADO`, `anon-0001` → 400; enlace simbólico como paciente → 400 |
| 2 | Archivos no revisados entraban al zip | `verificar` revisa todos los archivos, de cualquier extensión; el empaquetado rechaza cualquier archivo que no sea `.dcm` o `serie.json`; manifiesto SHA-256 de los bytes exactos, zip temporal releído y comparado antes de publicar | `notes.txt` dentro del paciente → 409, nada escrito; `crudo.dcm` (DICOM sin limpiar) → la compuerta lista 20 problemas |
| 3 | Identidad sobrevivía en campos no listados y en secuencias | Lista cerrada de texto permitido; todo otro LO, LT, ST, UT, SH, UC y PN se elimina en cualquier nivel; etiquetas públicas desconocidas eliminadas; objetos no soportados (SR, SC, documentos…) en cuarentena; el verificador aplica la misma política pero de forma independiente | `TextValue`, `PatientTransportArrangements`, `RequestedProcedureLocation`, `FillerOrderNumberImagingServiceRequest`, `ClinicalTrialSubjectID`, `CommentsOnThePerformedProcedureStep` y `TextValue` anidado: todos ausentes tras limpiar; búsqueda binaria de cada cadena `ZZ` en los archivos limpios: ninguna |
| 4 | Preámbulo y metadatos de archivo conservaban identidad | Preámbulo a cero; metadatos reconstruidos desde cero (sin títulos AE ni información privada); el verificador lee los 128 bytes y los metadatos | Preámbulo con `ZZ IDENTITY` y `SourceApplicationEntityTitle` → ambos ausentes; preámbulo alterado a mano en un archivo limpio → la compuerta lo detecta |
| 5 | `BurnedInAnnotation=YES` solo avisaba | Cuarentena; el verificador lo trata como problema | Archivo con `YES` → cuarentena, no copiado |
| 6 | Pacientes de distintos emisores se fusionaban | Clave emisor + ID; aviso si el mismo ID trae nombre o fecha de nacimiento distintos | Mismo ID con dos emisores → dos seudónimos |
| 7 | Series distintas se mezclaban | Carpetas derivadas del `SeriesInstanceUID` nuevo; etiqueta de número solo como sufijo | Dos series con número 7 → dos carpetas |
| 8 | Reimportar duplicaba instancias | Nombre de archivo derivado del `SOPInstanceUID` nuevo; mismo contenido se omite; contenido distinto con el mismo UID va a cuarentena | Copia repetida → 1 duplicado omitido; reimportar todo por la app → 0 escritos, 11 duplicados |
| 9 | Una corrida fallida perdía el mapa | Mapa escrito de forma atómica al aparecer cada paciente, antes del primer archivo; permisos 0600/0700; cada archivo en `try`, cuarentena y la corrida sigue; cerrojo `.corriendo` | Archivo roto en medio de la corrida → cuarentena, mapa presente, 5 archivos limpios; un elemento privado GE mal formado en los PET reales ya no detiene la corrida |
| 10 | Se reescribían UID semánticos | Solo se reescriben los identificadores de instancia (lista cerrada) bajo una raíz de proyecto; el verificador exige esa raíz | `CodingSchemeUID` y `ContextGroupVersion` intactos; `ReferencedSOPInstanceUID` reescrito |
| 11 | Enlaces simbólicos y HTML en la página | Sin seguir enlaces en ninguna lectura; `textContent` en toda la interfaz; CSP sin código en línea; títulos de diálogo fijos | `<img onerror>` en `SeriesDescription` queda como texto sin `<>`; cabecera CSP presente |
| otros | Prefijo distinto entre limpiar y verificar; zips del mismo minuto se pisaban; `/` y `/docs` sin token; rutas en reportes | Prefijo guardado en `.politica.json`; nombre de zip con segundos y sufijo aleatorio, publicación atómica; token en toda ruta y `/docs` desactivado; reportes sin rutas ni valores | Dos paquetes en el mismo segundo → dos zips; `/`, `/static/app.js` sin token → 401; `/docs` → 404 |
| PET | La afirmación sobre el SUV no estaba probada | Factor SUV de peso corporal calculado antes y después (BQML, convención QIBA); si cambia, cuarentena; lista permitida de factores privados Philips | 10 cortes PET GE Discovery LS: factor idéntico en 10/10. La comparación numérica sobre un volumen completo y emparejado queda para el paso 3 |

Lo que sigue abierto: inspección de píxeles (texto grabado sin etiqueta, rostros), detección de carpetas sincronizadas solo por nombre, vista previa sin geometría, factores privados de otros fabricantes.

Still open: pixel inspection (unflagged burned-in text, faces), sync detection by path name only, non-geometric preview, other vendors' private quantification factors.


# Cambios 0.3 / Changes 0.3

Respuesta a la segunda ronda de revisión (8 de noviembre de 2026). Las comprobaciones de 0.2 se repitieron sin regresiones.

| # | Hallazgo / Finding | Cambio / Change | Prueba / Test |
|---|---|---|---|
| 1 | La página no podía cargar su JavaScript ni su CSS (los recursos estáticos exigían el token) | `app.js` y `app.css` se sirven sin token: son código, sin datos. La página y la API siguen exigiéndolo | `/static/app.js` y `/static/app.css` → 200 sin token; `/` y `/api/estado` → 401; `/docs` → 404 |
| 2 | `serie.json` se aceptaba por su nombre y entraba al zip sin inspección | `serie.json` desaparece. `verificar` solo acepta DICOM, más `.politica.json` validado contra su esquema en la raíz. El empaquetado solo copia DICOM; los reportes viven en `limpios_REPORTES/` y no se copian | `serie.json` con identidad plantado en un paciente → 409, nada escrito |
| 3 | Identidad sobrevivía en `EncapsulatedDocument`, `RetrieveURL`, `CodeMeaning`, un factor Philips con texto y un creador privado huérfano | URI eliminadas; binarios eliminados salvo píxeles y paletas; `CodeMeaning` reemplazado por `designador valor`; factores privados solo si son numéricos; creadores privados eliminados salvo los permitidos con algún factor presente. El verificador comprueba cada regla | Fixture con los cinco casos: ninguna cadena sintética sobrevive; el verificador lista los cinco hallazgos sobre el fixture sin limpiar |
| 4 | Un factor SUV cambiado se reportaba pero el archivo se escribía igual | Cuarentena y `continue` antes de escribir; rangos de cordura para peso, dosis, vida media e intervalo | Calculadora forzada a resultados distintos sobre los 10 PET → 0 escritos, 10 en cuarentena, 0 archivos en disco |
| 5 | Verificación y hash miraban momentos distintos | Cada archivo se lee una sola vez; esos bytes se verifican, se hashean y se escriben al zip; el zip se relee y se compara antes de publicarse | DICOM crudo plantado como `.dcm` en un paciente limpio → 409 con la lista de etiquetas, nada publicado |

Residuales declarados: CS y numéricos sin inspección de contenido; clases Enhanced y Segmentation no validadas con muestras reales; píxeles sin revisar; detección de iCloud "Escritorio y Documentos" fuera del alcance de la heurística.


# Cambios 0.3.1 / Changes 0.3.1

Respuesta a la tercera ronda (8 de noviembre de 2026).

| # | Hallazgo / Finding | Cambio / Change | Prueba / Test |
|---|---|---|---|
| 1 | El verificador se detenía en `PixelData` y no veía `DataSetTrailingPadding` ni nada posterior | Lectura completa en `verificar` y en el empaquetado; cualquier elemento después de los píxeles o en los grupos FFFA+ es un problema; el limpiador los elimina | Relleno con identidad después de los píxeles: eliminado al limpiar; plantado en un archivo limpio, la compuerta lo lista y rechaza |
| 2 | `emparejar` declaraba par coherente con datos ausentes, inconsistentes, desplazados o duplicados | Reescrito: tres veredictos (candidato compatible, inconsistente, información insuficiente); cada imagen validada contra su serie; identificadores completos para comparar; solape tridimensional de los campos de visión; duplicados y posiciones ausentes cambian el veredicto; etiquetas con sal por corrida; sin nombres de carpeta ni horas | Los siete casos del revisor: coherente → compatible; sin posiciones → insuficiente; sin ID ni estudio → insuficiente; una imagen de otro paciente → inconsistente; CT desplazado 1000 mm → inconsistente; un PET sin posición → insuficiente; posición PET duplicada → inconsistente |
| 3 | Dos resultados verdes antes y después no completan el paso 3 | Nuevo `cotejar.py`: original contra limpio por píxeles idénticos, geometría y cuantificación campo a campo, biyección de series, estudios, marcos y pacientes, desplazamiento de fechas uniforme | IM136 + 10 PET: 11 de 11 coinciden; con una imagen limpia alterada y una copia extra: diferencias detectadas |

Residuales declarados: la validación numérica del SUV sobre un volumen completo sigue pendiente de un caso real emparejado; CS y numéricos sin inspección de contenido; píxeles sin revisar.


# Cambios 0.3.2 / Changes 0.3.2

Respuesta a la cuarta ronda (8 de noviembre de 2026).

| # | Hallazgo / Finding | Cambio / Change | Prueba / Test |
|---|---|---|---|
| 1 | `cotejar` emparejaba por hash de píxeles: dos instancias distintas con píxeles iguales se confundían; cambios de interpretación fotométrica o de fecha-hora pasaban; faltantes se daban por cuarentena sin registro | Reescrito: correspondencia por el mapa privado (cada original se vuelve a limpiar en memoria y se compara byte a byte con el limpio en su ruta derivada); cada original sin limpio debe tener registro de cuarentena en el reporte; omitidos cotejados contra el reporte; limpios sin original son diferencia | Dos originales con SOP distintos y píxeles idénticos, uno borrado → DIFERENCIAS; `PhotometricInterpretation` cambiada → DIFERENCIAS; `AcquisitionDateTime` cambiada → DIFERENCIAS; limpio borrado sin registro → DIFERENCIAS; limpio extra → DIFERENCIAS; base de 11 y de 13 imágenes → COINCIDE |
| 2 | Siete bytes mal formados al final de un archivo limpio pasaban la compuerta y el empaquetado | `verificar` y el empaquetado exigen que la serialización canónica reproduzca exactamente los bytes leídos | Siete bytes añadidos a un limpio → la compuerta lo descalifica y el paquete se rechaza con 409; los 11 limpios normales siguen pasando |
| 3 | El seudónimo no es siempre `ANON-0001` cuando se reutiliza un mapa | `cotejar` imprime los seudónimos del caso; la secuencia documentada los toma de ahí | Salida del cotejo: `seudónimos del caso: ANON-0001, ANON-0002` |

Residuales declarados: validación numérica de SUV sobre un volumen completo pendiente de un caso real; CS y numéricos sin inspección de contenido; píxeles sin revisar.


# Cambios 0.4 / Changes 0.4

Paso 4 de la revisión: cuantificación PET validada contra referencias externas, y una corrección de política derivada de ella.

| Qué / What | Detalle / Detail | Prueba / Test |
|---|---|---|
| `suv.py` nuevo | SUVbw según el manual IBSI-SUV de OncoRay, cuadro por cuadro, con todas las ramas de unidades, corrección de decaimiento, hora de inicio del escaneo, hora de administración y Enhanced PET | 58 de 58 objetos de referencia digitales correctos (43 valores exactos, 15 errores esperados); 16 series reales de TCIA calculadas o rechazadas por regla, con los valores guardados en `validacion_ibsi/` |
| Política `pet-ct-local/0.4` | Se conservan, desplazadas, las fechas-hora privadas de referencia de decaimiento de GE (`0009,xx0D`) y Siemens (`0071,xx22`), porque son la primera fuente de la hora de inicio del escaneo y la copia limpia 0.3 las perdía | En los 10 cortes PET GE: factor y estrategia idénticos entre original y copia limpia (`privada GE (0009,100D)`); la fecha se desplaza igual que `SeriesDate` y la hora se conserva; `verificar`, `cotejar` y todos los fixtures anteriores siguen pasando |
| `cotejar` 0.2.1 | El conteo de archivos sobrantes se limita a las carpetas de serie del caso; otros casos del mismo mapa no cuentan | Diez casos reales en una misma raíz: diez COINCIDE |

Al pasar de la política 0.3 a la 0.4 hay que borrar `limpios/` y volver a limpiar; el mapa privado se conserva, así que los seudónimos, fechas y UID se mantienen.


# Cambios 0.4.1 / Changes 0.4.1

| Qué / What | Detalle / Detail | Prueba / Test |
|---|---|---|
| Lanzadores de doble clic | `PET-CT local.command` (macOS) y `PET-CT local.bat` (Windows): instalan `uv` si falta, arrancan la aplicación y abren el navegador; carpeta de datos `datos-pet` en la carpeta del usuario, configurable con `PETCT_DATOS` | El `.command` probado bajo bash: instala dependencias, arranca y sirve la página con token. El `.bat` no se ha probado en Windows todavía |
| Consola UTF-8 | Todos los scripts fuerzan salida UTF-8, para que los acentos y símbolos se vean en la consola de Windows | Sintaxis verificada; sin prueba en Windows todavía |


# Cambios 0.5 / Changes 0.5

| Qué / What | Detalle / Detail | Prueba / Test |
|---|---|---|
| Panel "Antes y después" en Estudios | Imagen original y limpia lado a lado con la misma ventana; comprobación SHA-256 de píxeles; tarjetas con lo que se hizo (nombre e ID → seudónimo, fecha de nacimiento vacía, fechas desplazadas, UID reescritos, etiquetas eliminadas, sin cambio) y lista completa por acción. Solo nombres de etiqueta; los valores originales nunca salen del servidor local | Sobre IM136 y un corte PET: píxeles idénticos, 140 y 312 privadas eliminadas, 6 y 5 fechas desplazadas, 5 UID reescritos; búsqueda de los valores originales (nombre, ID, nacimiento, institución, operador, fecha) en la respuesta: ninguno presente |
| Índice privado limpio → original | `anonimizar.py limpiar` escribe `PRIVADO/indice_origen.jsonl`, una línea por archivo, permisos 0600; es lo que permite al panel encontrar el original | 11 líneas para 11 archivos; `cotejar` sigue en COINCIDE |


# Cambios 0.5.1 / Changes 0.5.1

Respuesta a la revisión del panel antes/después.

| # | Hallazgo / Finding | Cambio / Change | Prueba / Test |
|---|---|---|---|
| 1 | La app leía el índice en `PRIVADO` y la línea de comandos lo escribía junto a su mapa (`limpios_PRIVADO`) | La app usa `limpios_PRIVADO`, la misma carpeta que la línea de comandos, y lee el índice de ambas por compatibilidad; el índice guarda la ruta limpia relativa a `limpios/`, así sobrevive a mover la carpeta | Datos limpiados por línea de comandos y copiados a otra carpeta de trabajo: el panel y `cotejar` desde la app funcionan sin reimportar |
| 2 | Reimportar archivos idénticos no creaba índice | El duplicado verificado también se indexa; las líneas no se repiten | Índice borrado, reimportación: 0 imágenes escritas, 11 duplicados, índice reconstruido con 11 líneas; una tercera corrida no añade líneas |
| 3 | Respuestas fuera de orden al navegar rápido mezclaban series | Número de secuencia por petición; la respuesta se descarta si cambió la serie, el corte o la ventana; al abrir otra serie el panel se vacía | Lógica revisada; la condición compara serie, corte y ventana antes de pintar |
| 4 | Redacción: el hash compara los bytes de PixelData, no el archivo completo | Tarjeta y README dicen "bytes de PixelData idénticos, SHA-256" | Texto revisado |
| + | Botones de flujo en Estudios | Emparejar, Cotejar con el original (con la carpeta de entrada registrada en el índice) y SUV de la serie, con su salida en pantalla | Los tres probados sobre datos limpiados por línea de comandos: emparejar (sin par, correcto), cotejar COINCIDE, suv con factor y máximo |
| + | `ventana.py` | Ventana nativa opcional con pywebview, experimental | Sin ejecutar en Mac ni Windows todavía |


# Cambios 0.5.2 / Changes 0.5.2

Respuesta a la revisión de migración.

| # | Hallazgo / Finding | Cambio / Change | Prueba / Test |
|---|---|---|---|
| 1 | Reimportar archivos limpiados por una versión anterior los enviaba a cuarentena por el nombre de versión de los metadatos | El nombre de versión pasa a ser estable y ligado a la política (`PETCTLOCAL_0.4`); en la deduplicación, una diferencia limitada a ese nombre se resuelve actualizando el archivo en el sitio tras comprobar que la serialización canónica coincide; `cotejar` tolera y señala la misma diferencia | 11 archivos con `ANONIMIZAR_0.4.2` simulado: cotejar COINCIDE con aviso; reimportación: 0 escritos, 11 actualizados, 0 cuarentena; tercera corrida: 11 duplicados; un cambio real de contenido sigue en cuarentena |
| 2 | Mover el espacio de trabajo con sus originales dejaba el panel sin originales | El índice guarda el original relativo a su carpeta de entrada y la carpeta de entrada relativa a la carpeta de trabajo cuando está dentro; la app resuelve ambas; botón para reubicar la carpeta de originales (`raices.json`) | Espacio de trabajo movido completo: panel y cotejo funcionan; originales movidos aparte: el panel lo detecta, la reubicación los recupera y cotejo vuelve a COINCIDE |

Residual: `ventana.py` sigue sin ejecutarse en Mac ni en Windows.


# Cambios 0.5.3 / Changes 0.5.3

| # | Hallazgo / Finding | Cambio / Change | Prueba / Test |
|---|---|---|---|
| 1 | La excepción de versión reserializaba el archivo existente antes de comparar y por eso descartaba bytes sobrantes: siete bytes añadidos pasaban como "versión" en `cotejar` y la reimportación los borraba contándolos como actualización | `solo_cambia_version` exige primero que la serialización canónica del existente reproduzca sus bytes exactos, luego que el nombre de versión sea distinto del actual, y solo entonces iguala ese nombre y compara | Siete bytes añadidos: `cotejar` DIFERENCIAS y reimportación → cuarentena por contenido distinto; versión antigua simulada en 11 archivos: 11 actualizados, COINCIDE; misma versión con `PhotometricInterpretation` cambiada: cuarentena; suites anteriores sin regresión |


# Cambios 0.6 / Changes 0.6

Respuesta a la lista de mejoras priorizada (8 de noviembre de 2026). Los números remiten a la tabla del revisor.

| Prioridad | Mejora | Estado | Prueba |
|---|---|---|---|
| 1 | Impedir importar el árbol de salida | Hecho: la línea de comandos y la app rechazan entradas que se solapan con la copia limpia, la carpeta privada, los reportes o los paquetes, con rutas resueltas | Salida dentro de la entrada → código 2; desde la app → 400 |
| 1 | Detectar entradas ya anonimizadas | Hecho: se cuentan aparte como "ya anonimizados" y no reciben seudónimo ni se copian | La copia limpia como entrada: 11 ya anonimizados, 0 escritos, mapa con 0 pacientes |
| 1 | Registrar los ajustes reales de la importación | Hecho: `limpios_PRIVADO/corridas.jsonl`, una línea inmutable por corrida con entrada, salida, mapa, política, prefijo, opción y conteos | Registro presente tras cada corrida |
| 1 | Actualizaciones de política explícitas | Parcial: la actualización de etiqueta de versión en el sitio existe (0.5.3); la reconstrucción por cambio de política sigue pendiente de diseño | |
| 1 | Comparar SUV original / limpio | Hecho: `suv.py comparar` y botón en Estudios; correspondencia por el índice privado, geometría y orden, cálculo por separado, valores finitos, diferencia máxima, vóxeles distintos, factores e intervalos, reporte JSON | 10 cortes: 10/10 exactos, 0 vóxeles distintos; un vóxel alterado → DIFERENCIAS |
| 1 | Flujo de revisión de píxeles | Primera versión: registro por paciente (sin hallazgos / con hallazgos: texto grabado, rostro, otro; revisor; notas) ligado a los hashes de todos sus archivos; se invalida si cambian | Registro, vigencia, invalidación tras cambiar un archivo |
| 1 | Separar paquete creado de revisión completada | Hecho: estado por paciente en Enviar, aviso en la respuesta y `revision_pixeles` en el manifiesto | Paquete con un paciente sin revisar: creado y marcado pendiente |
| 2 | Resaltar fila seleccionada; alcance explícito; refrescar tras importar | Hecho | Lógica revisada en la interfaz |
| 2 | Progreso, selector de carpeta de trabajo, tabla de todos los pacientes, almacenamiento | Pendientes, como issues | |
| 3 | Lanzamiento nativo y Windows | Pendientes de prueba en plataforma | |

Además: repositorio `venezuela-org/medical-images-analysis` con pruebas sintéticas (8 casos, sin datos reales) y validación IBSI-SUV en integración continua.


# Cambios 0.6.1 / Changes 0.6.1

Respuesta a la revisión de 0.6. Plataforma de Miguel: Windows 10.

| # | Hallazgo / Finding | Cambio / Change | Prueba / Test |
|---|---|---|---|
| 1 | `suv.py comparar` solo enumeraba los limpios presentes: un corte borrado desaparecía del denominador; el orden no se validaba | Correspondencia en ambos sentidos antes de comparar: serie original completa (todos los archivos de las carpetas de origen con el mismo SeriesInstanceUID), índice privado y carpeta limpia; faltantes, sobrantes, duplicados y mal mapeados son problemas; orden físico sin duplicados y números de instancia coherentes y iguales entre original y limpio; el veredicto exige correspondencia completa | Corte limpio borrado → "1 faltante", DIFERENCIAS; InstanceNumber cambiado → "1 instancia distinta", DIFERENCIAS; original no importado → faltante; vóxel alterado → detectado; 10 cortes reales → IDENTICO; 5 pruebas sintéticas en CI |
| 2 | La revisión de píxeles aceptaba "sin hallazgos" con un hallazgo marcado y sin revisor | Regla única en API e interfaz: sin hallazgos exige lista vacía y revisor; con hallazgos exige al menos uno; cualquier hallazgo registrado deja al paciente pendiente para compartir, diga lo que diga la etiqueta; un registro contradictorio en disco se lee como pendiente | 400 en los tres casos inválidos; con hallazgos → pendiente en el paquete; registro contradictorio escrito a mano → pendiente; 5 pruebas sintéticas en CI |
| 3 | La respuesta de la revisión podía pintarse tras cambiar de paciente | Secuencia por petición y comprobación de la selección; el formulario se vacía al abrir otra serie; botones deshabilitados cuando la combinación es contradictoria | Lógica revisada |
| 4 | Sin pruebas de las funciones nuevas | 11 pruebas nuevas (comparación SUV, reglas de revisión, ausencia de DICOM en el repositorio) además de las 8 de la política; CI en Linux y Windows | 19 pruebas pasan |
| + | El `.gitignore` no detiene archivos DICOM sin extensión | Gancho de pre-commit que inspecciona los bytes de cada archivo preparado y prueba de CI que recorre el repositorio | Prueba incluida |


# Cambios 0.6.2 / Changes 0.6.2

| # | Hallazgo / Finding | Cambio / Change | Prueba / Test |
|---|---|---|---|
| 1 | El gancho de pre-commit leía la copia de trabajo y rutas separadas por espacios: un DICOM con espacio en el nombre, o preparado y luego sustituido en disco por texto, pasaban | Rutas separadas por NUL (`-z`) y bytes del índice de git (`git cat-file blob :ruta`); limitación documentada: busca el marcador DICM del byte 128 | 3 pruebas: DICOM con espacio en el nombre → rechazado; DICOM en el índice con copia de trabajo sustituida → rechazado; archivos normales → aceptados |
| 2 | El orden físico no afectaba al veredicto: el mismo InstanceNumber alterado en original y limpio daba IDENTICO | El veredicto exige correspondencia completa, orden y geometría correctos y píxeles idénticos; los tres se informan por separado; geometría ausente es un problema | Pruebas: orden incoherente en ambos lados → DIFERENCIAS con píxeles idénticos; geometría ausente → DIFERENCIAS; los 10 cortes reales siguen en IDENTICO |
| 3 | El guardado de la revisión solo comprobaba la selección en el éxito y no bloqueaba envíos solapados | Secuencia y selección comprobadas en éxito, error y cierre; envíos solapados bloqueados; cambiar de serie invalida respuestas pendientes | Lógica revisada |
| + | Redacción | Un runner `windows-latest` no es una prueba del Windows 10 de Miguel; el README lo dice así | |


# Cambios 0.7.0 / Changes 0.7.0

| Qué / What | Detalle / Detail | Prueba / Test |
|---|---|---|
| Punto de entrada único | `lanzador.py`: abre la aplicación o ejecuta una herramienta como subcomando; `app.py` invoca las herramientas a través del propio ejecutable cuando va congelado | Flujo completo (demo, limpiar, cotejar, emparejar, suv comparar, versión) desde el código fuente y desde un ejecutable PyInstaller construido en Linux; servidor congelado probado por HTTP, importación por subproceso congelado, comparación SUV y empaquetado |
| Caso sintético de demostración | `demo.py` y botón "Crear caso de demostración": CT y PET inventados con identidad ficticia, misma rejilla y marco, lesión caliente, atributos de dosis | El caso recorre todo el flujo: COINCIDE, CANDIDATO COMPATIBLE, IDENTICO |
| Flujo de publicación | `publicar.yml`: pruebas, paquetes Windows y macOS, prueba de humo del ejecutable en cada runner, caso sintético, comprobación de adjuntos sin archivos privados, borrador de Release con notas e instrucciones | Pendiente de su primera ejecución en GitHub; el constructor (`empaquetar.sh`) probado en Linux |
| Documentación | `docs/instalacion.md` (español, para la Release), `docs/lanzamientos.md` (proceso), `docs/notas-de-version/v0.7.0.md` | |
| Consola | Salida con búfer por línea en todas las herramientas, para que el ejecutable muestre la dirección y el token de inmediato | Visto en el ejecutable congelado |


# Cambios 0.7.1 / Changes 0.7.1

| # | Hallazgo / Finding | Cambio / Change | Prueba / Test |
|---|---|---|---|
| 1 | La prueba de orden elegía un corte cualquiera; si tocaba un extremo, el orden seguía válido y la prueba fallaba sin motivo | La prueba elige el corte del medio por posición física y rompe el orden a propósito | Suite ejecutada tres veces: 25 de 25 cada vez |
| 2 | La geometría solo se exigía en conjunto: un par sin ImagePositionPatient pasaba si otros la traían | Cada par debe traer posición, orientación y espaciado en ambos lados; el conteo `geometria_incompleta` forma parte del resultado de orden y geometría | Un solo corte sin posición → DIFERENCIAS con correspondencia completa; todos sin posición → DIFERENCIAS |
| 3 | Un problema de orden también marcaba como incompleta la correspondencia | Tres resultados independientes: correspondencia (inventario), orden y geometría, igualdad numérica; cada problema se imprime bajo su apartado y el veredicto exige los tres | Orden incoherente en ambos lados: correspondencia sí, orden NO, píxeles sí, DIFERENCIAS |

## Revisión del PR #1 (Copilot) y runner de Windows

| # | Hallazgo / Finding | Cambio / Change | Prueba / Test |
|---|---|---|---|
| 1 | El gancho llegaba con CRLF al runner de Windows (20 retornos de carro); la prueba nueva lo mostró | `.gitattributes` añadido a la rama (`.githooks/* text eol=lf`); la prueba de CR sigue vigilándolo | `test_el_gancho_no_tiene_retornos_de_carro` |
| 2 | "Lista para compartir" solo acreditaba la revisión de píxeles | El mensaje dice "sin hallazgos, vigente" y que la compuerta se vuelve a comprobar al empaquetar | Prueba de navegador actualizada |
| 3 | El patrón de archivos privados no detectaba `PRIVADO/` en la raíz del zip ni separadores de Windows | Límites de componente en el patrón | Zip con `PRIVADO/secreto.json` en la raíz, rechazado |
| 4 | La identidad de la demo se buscaba en los bytes; creadores privados solo al nivel superior | Valores exactos de PatientName y Manufacturer y recorrido completo del dataset | DICOM ajeno colado en la demo, rechazado |
| 5 | Sin pydicom el comprobador aceptaba en silencio | pydicom obligatorio: sin él, rechaza con código 2 | Simulado sin pydicom: código 2 |
| 6 | El manifiesto podía omitir DICOM del zip | La prueba de humo exige que las rutas del manifiesto sean exactamente los DICOM del zip, sin repetidos | Prueba de humo, 39 comprobaciones |
| 7 | Contraste de `--tinta-3` bajo el mínimo en claro y en oscuro | `#6b6b66` (claro) y `#9a9a93` (oscuro) | |

## Revisión del PR #1, segunda ronda

| # | Hallazgo / Finding | Cambio / Change | Prueba / Test |
|---|---|---|---|
| 1 | En Windows el exportador escribía rutas con `\` en el manifiesto y el zip las guardaba con `/`: la relectura fallaba | Rutas POSIX en el zip y en el manifiesto | Prueba de humo contra el ejecutable: el manifiesto enumera exactamente las entradas del zip |
| 2 | Conteo de retornos de carro dentro de un f-string: error de sintaxis en Python 3.10 y 3.11, y contaba los bytes equivocados | Conteo fuera del f-string con el byte correcto | Sin f-strings con barras en el archivo |
| 3 | Dos entradas del zip con el mismo nombre: la segunda ocultaba a la primera | Lectura por `ZipInfo` y rechazo de entradas repetidas, en el comprobador y en la prueba de humo | `test_entrada_repetida_se_rechaza` |
| 4 | Solo se validaban los creadores privados de la demo | Todas las etiquetas privadas del dataset deben ser las dos que escribe `demo.py` | `test_demo_con_etiqueta_privada_ajena_se_rechaza` |
| + | Pruebas de los adjuntos | `pruebas/test_adjuntos.py`, seis zips sintéticos: demo limpia aceptada; DICOM en el paquete, carpeta privada en la raíz o con `\`, entrada repetida, etiqueta privada ajena e identidad distinta rechazados | 33 pruebas en la suite |

# Cambios 0.7.2 / Changes 0.7.2

| Qué / What | Detalle / Detail | Prueba / Test |
|---|---|---|
| macOS: "Python.framework está dañado" en un Mac real | La descarga lleva la marca de cuarentena y Gatekeeper bloquea las bibliotecas del paquete. El lanzador `Abrir PET-CT local.command` quita la cuarentena de su carpeta antes de arrancar (`xattr -dr com.apple.quarantine .`) y el flujo firma ad hoc cada binario (`.dylib`, `.so`, frameworks y ejecutable). Instrucciones con el remedio manual por Terminal | Pendiente de prueba en el Mac de Arturo con el paquete v0.7.2 |
| Python fijo en los paquetes | `empaquetar.sh` usa Python 3.12 en vez del más reciente del runner (macOS había construido con 3.15) | Construcción en Linux con 3.12 y prueba de humo por API en verde |

# Cambios 0.7.3 / Changes 0.7.3

| Qué / What | Detalle / Detail | Prueba / Test |
|---|---|---|
| SUV solo en PET | "SUV de esta serie" y "Comparar SUV original / limpio" deshabilitados en series que no son PET, con nota | Prueba en navegador en verde |
| Insignias de Enviar | La insignia de revisión se busca en la lista actual al llegar la respuesta; si la lista se repintó, se actualiza igual | Prueba en navegador en verde |

# Cambios 0.7.4 / Changes 0.7.4

| Qué / What | Detalle / Detail |
|---|---|
| Botones de descarga en el README | Windows, Mac y datos de demostración, hacia `releases/latest/download/<nombre>` |
| Adjuntos con nombre fijo | `PET-CT-local-windows.zip`, `PET-CT-local-macos.zip`, `datos-demo-sinteticos.zip`; `instalacion.md` actualizado |

# Cambios 0.7.5 / Changes 0.7.5

| Qué / What | Detalle / Detail |
|---|---|
| Acentos, toda la cadena | Salida de las herramientas decodificada como UTF-8 (antes cp1252 en Windows: "polÃ­tica"); selector de carpetas en UTF-8 para rutas con acentos; app.js y app.css con charset; consola de Windows en UTF-8; todas las lecturas y escrituras de texto con encoding explícito |

# Cambios 0.9.5 / Changes 0.9.5

Las versiones 0.8.0 a 0.9.4 no se publicaron; sus cambios y los hallazgos de sus ocho rondas de revisión están aquí.

| # | Hallazgo o cambio / Finding or change | Qué se hizo / What was done | Prueba / Test |
|---|---|---|---|
| 1 | Al descomprimir un paquete, los archivos de una serie aparecían desordenados (nombres internos por hash) | Rutas exportadas `corte_000001_<código>.dcm` por serie, desde una correspondencia explícita interna → exportada; los archivos limpios y sus bytes no cambian | `test_nombres_y_orden_de_entrada_mezclados_salen_numerados_por_corte`, `test_bytes_hashes_cantidad_y_manifiesto_coinciden` |
| 2 | Criterio de orden | `ordenar_serie()`, compartida por el visor y la exportación: `InstanceNumber` válido y único; posición y orientación si la pila es coherente; desempate determinista con aviso. Documentado en `docs/orden-de-cortes.md` | `test_instance_number_valido_ordena_numericamente`, `test_sin_instance_number_…`, `test_numero_repetido_…`, `test_varias_fases_…`, `test_el_visor_y_la_exportacion_usan_el_mismo_orden` |
| 3 | Modo rápido | Pestaña Rápido: limpieza, compuerta, cotejo, emparejamiento, SUV y paquete con un botón | `pruebas/test_rapido.py` |
| 4 | Un zip sin revisar podía escribirse en una carpeta sincronizada | El zip pendiente se queda en local; el destino solo recibe el zip revisado | `test_el_zip_nace_pendiente_y_en_la_carpeta_de_trabajo` |
| 5 | El zip incluía todo lo guardado del paciente, o lo que alguna vez salió de la carpeta | La limpieza escribe la lista exacta de salidas de la invocación (`--lista-salida`); el modo rápido usa solo esa lista | `test_solo_entran_los_archivos_de_la_carpeta_elegida`, `test_misma_carpeta_con_una_serie_retirada_…`, `test_serie_retirada_de_la_carpeta_no_entra_…` |
| 6 | La revisión se aplicaba a los archivos actuales, sin candado; los hashes se comparaban después de escribir | Hashes congelados en la corrida; candado; comparación sobre los bytes que se empaquetan, antes de escribir | `test_el_sello_se_rechaza_si_un_archivo_cambio_desde_la_corrida`, `test_cambio_entre_la_comprobacion_y_el_empaquetado_no_escribe_en_el_destino`, `test_sello_con_el_candado_tomado_se_rechaza` |
| 7 | El zip llegaba al destino antes que su manifiesto externo y el registro de la revisión | Publicación como punto de compromiso: evidencias `.preparada` antes, confirmadas por renombrado después; tras publicar, solo avisos | `test_la_evidencia_preparada_existe_antes_de_publicar_y_se_confirma_despues`, `test_si_no_se_puede_guardar_el_registro_de_revision_nada_se_publica`, `test_fallo_al_borrar_el_temporal_tras_publicar_es_un_aviso_y_conserva_la_ruta` |
| 8 | Los fallos al retirar archivos intermedios se ignoraban | Lo que no se puede retirar vuelve en el error con su ruta (`restos`) | `test_copia_fallida_y_registro_que_no_se_deja_borrar_se_informa_con_su_ruta` |
| 9 | La pantalla se ponía verde siempre tras registrar la revisión; SUV no calculable igual que discrepancia | Se pinta el estado devuelto; discrepancia real de SUV bloquea, "no se puede calcular" avisa | Prueba de navegador; `test_suv_no_calculable_es_aviso_y_no_discrepancia` |
| 10 | Los nombres exportados del modo rápido debían ser los mismos en el zip pendiente y en el revisado | La correspondencia se congela con la corrida y se reutiliza al registrar la revisión | `test_pendiente_y_revisado_llevan_los_mismos_nombres_aunque_se_importe_mas` |
| 11 | Tras retirar parte de una serie de la carpeta, el modo rápido fallaba en el cotejo ("archivos limpios sin original") aunque los archivos de la corrida coincidían | El cotejo, el emparejamiento y el SUV del modo rápido se limitan al conjunto de la corrida (`--solo-lista`), con el cotejo estricto en los dos sentidos sobre ese conjunto; el cotejo general no cambia | `test_retirar_parte_de_una_serie_no_hace_fallar_el_cotejo_del_modo_rapido`, `test_el_cotejo_general_sigue_siendo_estricto_con_la_serie_entera`, `test_el_cotejo_de_un_conjunto_es_estricto_en_los_dos_sentidos` |
| 12 | El visor de Estudios y el paquete podían ordenarse con criterios distintos, porque ordenaban conjuntos distintos (InstanceNumber 2,2,1,1 con posiciones 0,1,2,3) | El orden del paquete se congela con la corrida y se revisa en el visor de la corrida, que muestra exactamente sus archivos en ese orden y se conserva tras otras importaciones | `test_el_criterio_se_evalua_sobre_el_conjunto_recibido`, `test_el_visor_de_la_corrida_muestra_sus_archivos_en_el_orden_congelado_del_paquete`, `test_el_visor_de_la_corrida_no_muestra_un_archivo_que_cambio` |
| 13 | El visor de la corrida comprobaba el hash con una lectura y dibujaba con otra: un cambio entre ambas mostraba la imagen modificada sin error | Una sola lectura: el hash se calcula sobre esos bytes y la imagen se dibuja desde esos mismos bytes (`render_bytes`); la ruta no se vuelve a abrir | `test_el_visor_muestra_los_bytes_verificados_aunque_el_archivo_cambie_antes_de_dibujar`, `test_el_visor_de_la_corrida_lee_cada_archivo_una_sola_vez`, `test_el_visor_de_la_corrida_no_muestra_un_archivo_que_cambio` |
| 14 | Faltaban la edad y el sexo junto al identificador anónimo, y un nombre legible por paciente | Etiqueta por estudio `ANON-0001 · F · 54 años en el estudio`: seudónimo persistente, `PatientSex` tal como consta y edad en la fecha del estudio, calculada antes de anonimizar las fechas. En Estudios, en el visor de la corrida y en el manifiesto. `docs/demografia.md` | `pruebas/test_demografia.py` (33) |
| 15 | La edad no debe exponer fechas ni depender de hoy; es del estudio, no del paciente | Solo se guarda la edad o el motivo; 90 años o más agrupados; menores de un año en meses, semanas o días; `PatientAge` como edad informada, distinguida | `test_ni_la_fecha_de_nacimiento_ni_la_del_estudio_aparecen_en_lo_nuevo`, `test_el_mismo_paciente_tiene_edades_distintas_en_dos_estudios`, `test_la_edad_no_depende_de_la_fecha_de_hoy` |
| 16 | La demografía de un paquete no debe cambiar con importaciones posteriores ni tocar archivos ya revisados | Resumen sobre los archivos exactos, congelado con la corrida; los archivos limpios no cambian y los estudios ya importados se completan reimportando la carpeta original | `test_la_demografia_de_una_corrida_no_cambia_con_importaciones_posteriores`, `test_los_archivos_limpios_y_sus_pixeles_no_cambian`, `test_estudios_importados_con_una_version_anterior` |
| 17 | El visor de la corrida no podía mostrar archivos con varios cuadros (error al dibujar) y la revisión se podía registrar igualmente | `render_bytes()` dibuja el cuadro pedido; segundo deslizador en el visor; si una imagen no se puede dibujar, la corrida lo anota y su revisión se rechaza; Estudios avisa y no admite "sin hallazgos" para pacientes con archivos de varios cuadros | `test_un_archivo_con_varios_cuadros_se_puede_recorrer_en_el_visor_de_la_corrida`, `test_una_imagen_que_el_visor_no_puede_dibujar_bloquea_la_revision`, `test_un_fallo_al_dibujar_durante_la_revision_tambien_la_bloquea`, `test_archivos_con_varios_cuadros_por_la_api` |
| 18 | Dos originales que dan el mismo archivo limpio podían pisarse la edad: quedaba la del último | Se conservan todas las observaciones distintas de la invocación, también la de un original en cuarentena por contenido distinto; el estudio muestra el conflicto | `test_dos_copias_de_una_instancia_con_fechas_distintas_dan_conflicto_y_no_la_ultima_edad`, `test_dos_copias_con_sexos_distintos_no_se_resuelven_en_silencio` |
| 19 | `--solo-lista` limitaba solo el lado limpio de la comparación de SUV; un original en cuarentena contaba como faltante | Con lista, la serie original son los originales de los archivos listados; sin lista, sin cambios | `test_un_pet_en_cuarentena_no_cuenta_como_faltante_en_el_suv_de_la_corrida` |
| 20 | Una consulta de estado fallida dejaba el modo rápido colgado, con el botón deshabilitado | `seguirTrabajo()`: reintentos, mensaje, botón principal disponible y "Volver a consultar"; también en la limpieza de Entrada | Prueba de navegador: dos fallos seguidos y fallo sin parar |
| 21 | La dirección de la aplicación se imprimía antes de que el servidor escuchara | Se imprime al terminar el índice de estudios | |
| 22 | Una revisión registrada no se podía retirar sin borrar archivos a mano | "Retirar revisión" en Registro: quién, cuándo y por qué; el original se conserva (historial o en su sitio) y la retirada lo sustituye; el estado deja de valer; se enumeran los paquetes revisados afectados; los zips exportados no cambian | `test_retirar_la_revision_de_un_paciente_conserva_el_original_y_lo_sustituye`, `test_retirar_la_revision_de_una_corrida_no_toca_su_registro_ni_su_zip`, `test_retirar_exige_quien_y_por_que_y_algo_que_retirar`, `test_un_zip_que_ya_no_esta_o_que_cambio_se_dice` |
| 23 | Se podía declarar "revisé todos los cortes" sin que los cortes hubieran estado en pantalla | Recuento de cuadros mostrados en el navegador, cada cuadro de cada archivo; las comprobaciones automáticas no cuentan; "sin hallazgos" exige el recuento completo y lo guarda junto a la declaración; se llama mostrado, no revisado | `test_las_comprobaciones_automaticas_no_cuentan_como_mostrado`, `test_solo_cuenta_lo_que_se_sirvio_al_visor_y_cada_cuadro_una_vez`, `test_sin_haber_mostrado_todo_no_se_puede_declarar_la_revision`, `test_cada_cuadro_de_un_archivo_con_varios_cuenta`, `test_revision_clasica_por_la_api_exige_mostrar_todos_los_cortes` |
| 24 | Un registro de revisión se sobrescribía al registrar otro | Copia intacta en `historial_revisiones/` antes de cualquier sustitución | `test_una_revision_nueva_tras_una_retirada_conserva_todo_el_historial` |
