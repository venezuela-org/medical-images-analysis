# La casilla "conservar descripciones"

Está en la pestaña Entrada, junto al botón Anonimizar. Este documento dice qué hace, qué no hace, y qué pasó cuando se probó con un estudio ya guardado.

## Qué hace

Tres textos del DICOM los escribe una persona en el equipo: la descripción del estudio, la descripción de la serie y el nombre del protocolo. Por ejemplo, "T1_3D_SAG". También entran aquí dos etiquetas de las segmentaciones.

| | Sin la casilla (por defecto) | Con la casilla |
|---|---|---|
| Descripción del estudio, de la serie y nombre del protocolo | Se eliminan | Se conservan |
| Si contienen el nombre o el ID del paciente | No aplica | Esa parte se sustituye por `[REDACTADO]` y la limpieza lo avisa |
| Nombre, ID, fechas y demás identidad del paciente | Se quitan | Se quitan igual |

Por eso, sin la casilla, la pestaña Estudios muestra "Descripción —" y "Protocolo —".

## Qué no hace

- **No conserva ningún otro dato del paciente.** La casilla solo decide sobre esos textos técnicos.
- **No garantiza que esos textos estén limpios.** La aplicación busca en ellos el nombre y el ID del paciente. No puede reconocer otras cosas que alguien haya escrito ahí, como el nombre de un familiar o de un médico. Si marcas la casilla, lee las descripciones en la pestaña Estudios antes de compartir. Por eso viene desmarcada.
- **No cambia un estudio ya guardado.** Se aplica a los estudios que todavía no están en la carpeta de trabajo.
- **No existe en el modo rápido**, que limpia siempre sin ella.

## Qué pasó en la prueba

Quien probó la aplicación limpió una serie de resonancia, marcó después la casilla y volvió a limpiar la misma carpeta. Vio "Terminó con cuarentena o avisos" y "0 archivos limpios", y preguntó si había pasado algo.

Con el caso sintético se reproduce así:

| Paso | Resultado |
|---|---|
| Limpiar una carpeta, sin la casilla | Todos los archivos limpios; descripciones eliminadas |
| Limpiar la misma carpeta, con la casilla | 0 archivos limpios; todos en cuarentena |
| Limpiar esa carpeta en una carpeta de trabajo nueva, con la casilla | Todos limpios; descripciones conservadas |

En el segundo paso la limpieza produce, para imágenes que ya están guardadas, un resultado distinto del guardado. La aplicación no sustituye una imagen guardada por otra distinta: la aparta y deja la guardada como estaba. No se pierde nada y no se duplica nada, pero la casilla no tiene efecto.

Esto reproduce la secuencia que se describió y da el mismo mensaje. No se ha visto el reporte de aquella corrida, así que es la explicación más probable, no una comprobada. Desde esta versión la propia aplicación lo dice.

## Qué cambió en la v0.10.0

- Bajo la casilla hay una explicación, "Qué hace esa casilla".
- La limpieza distingue dos motivos que antes compartían mensaje: la misma imagen, guardada antes con la otra opción de descripciones; y otra copia de la imagen con contenido distinto.
- Al terminar, la pestaña Entrada muestra las cifras de la limpieza y, por cada motivo de cuarentena, qué pasó, a qué afecta y qué se puede hacer.
- Si una carpeta se guardó con la casilla y después se pasa por el modo rápido, el modo rápido dice que esas imágenes ya están guardadas con descripciones y que siguen en Estudios. Antes preguntaba si los archivos eran CT, PET o MR.

## Lo que todavía no se puede hacer

Cambiar la opción en un estudio ya guardado. Hacerlo significa sustituir los archivos limpios, y con ellos cambian sus hashes: las revisiones de píxeles de ese paciente volverían a estar pendientes y los paquetes ya hechos dejarían de corresponder a lo guardado. Es una decisión de quienes llevan el proyecto, y no está tomada.
