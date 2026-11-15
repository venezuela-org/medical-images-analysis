# Lenguaje visual de PET/CT local

Un lenguaje sereno y preciso para una familia de herramientas de imagen médica que acompañan el juicio de expertos. Fondos neutros, texto oscuro legible, un solo color de acento, iconos simples y una identidad de texto. La imagen médica es el centro visual; la interfaz se mantiene a su alrededor sin competir con ella. El nombre de la aplicación sigue siendo "PET/CT local"; no hay marca paraguas por ahora.

## Tokens

Viven en `herramientas/estilo.css` como variables CSS y se reutilizan tal cual en futuras aplicaciones. En la siguiente tabla están los grupos y su intención.

| Grupo | Tokens | Intención |
|---|---|---|
| Neutros | `--fondo`, `--panel`, `--panel-2`, `--borde`, `--borde-fuerte`, `--tinta`, `--tinta-2`, `--tinta-3` | Página, tarjetas, superficies secundarias, bordes y tres niveles de texto. Modo oscuro por `prefers-color-scheme`. |
| Imagen | `--imagen-fondo` | Las imágenes médicas siempre sobre negro, sin bordes de color. |
| Acento | `--acento`, `--acento-tinta`, `--acento-suave` | Un azul contenido para acciones primarias, selección y foco. Único color de marca. |
| Estados | `--ok`, `--aviso`, `--error`, `--info` y sus variantes `-suave` | Siempre acompañados de texto o símbolo (✓, !, ×, i). El color refuerza, nunca sustituye. |
| Tipografía | `--fuente` (sistema), `--mono`, `--t-xs` a `--t-xl`, `--interlineado` | Fuente del sistema en cada plataforma; monoespaciada para rutas, salidas y versiones. |
| Espacio | `--e-1` (4 px) a `--e-8` (32 px) | Escala de 4 px para todo el ritmo vertical y horizontal. |
| Bordes | `--radio`, `--radio-s`, `--sombra`, `--ancho-max` | Esquinas suaves, sombra casi inexistente, ancho máximo cómodo en portátiles. |

## Componentes

| Componente | Clase | Regla |
|---|---|---|
| Identidad | `.marca` con `.icono`, `.nombre`, `.lema`, `.version` | Cabecera fija: icono de dos cuadros superpuestos (dos modalidades sobre la misma rejilla), nombre, lema corto, versión en monoespaciada. |
| Navegación | `nav.pestanas` con `.paso` | Pestañas numeradas en el orden del flujo (1 Entrada, 2 Estudios, 3 Enviar; Registro sin número). |
| Botones | `button`, `button.primario` | Un solo botón primario por pantalla, el de la acción principal del paso. Los demás, neutros. |
| Controles | `input[type=text]`, `.corto`, `select`, `input[type=range]` | Altura mínima de 36 px; foco visible con el acento. |
| Tarjetas | `.tarjeta`, `.rejilla` | Etiqueta pequeña en mayúsculas y valor debajo; rejilla adaptable. |
| Mensajes | `.mensaje` + `.ok`, `.aviso`, `.error`, `.info`, `.neutro` | Una frase completa que dice qué pasó y qué sigue; símbolo al inicio. |
| Insignias | `.insignia` + estado | Estado breve junto a un elemento de lista o celda; siempre con texto. |
| Tablas | `table.datos`, `.num`, `.tabla-marco` | Cabecera fija, números alineados a la derecha. Fila seleccionada: fondo de acento suave, barra izquierda y marcador ▸; distinta del hover, que solo oscurece el fondo. |
| Visor | `.visor`, `.lienzo`, `.par` | Imagen a la izquierda sobre negro con el deslizador debajo; metadatos y acciones a la derecha. El par antes/después mantiene la misma ventana en ambos lados. |
| Salidas | `pre.salida` | Texto de las herramientas, monoespaciado, desplazable, sin colorear. |

## Semántica de estados

Tres cosas distintas que nunca deben confundirse, y cada una tiene su propia frase:

1. Verificación de metadatos (la compuerta): "metadatos en regla". Un ✓ aquí dice que las etiquetas cumplen la política. No dice nada de los píxeles.
2. Revisión de píxeles: "sin hallazgos, vigente" / "con hallazgos" / "pendiente". La hace una persona y se liga a los hashes de los archivos.
3. Listo para compartir: solo cuando la revisión de píxeles está vigente y sin hallazgos. Un paquete puede existir localmente sin estar listo.

Por eso los mensajes de la compuerta terminan con "esto no es la revisión de píxeles", y los paquetes con revisión pendiente llevan un aviso en la interfaz y en el manifiesto.

## La pestaña Rápido (v0.10.0)

Una columna y tres apartados en orden: procesamiento, revisión de píxeles y paquete. Lo que termina bien en uno no se presenta como éxito de los otros.

- **Franja de estados** (`.franja`): tres celdas con qué es, cómo está y un detalle. El borde izquierdo toma el color del estado; el texto y el icono lo dicen sin depender del color. Con `.caduco` indica que el estado no se pudo volver a comprobar.
- **Libro de pasos** (`.libro`, `.paso-fila`): una fila por paso, hecha con `details`, que se abre con el ratón o con Intro y Espacio. Número, nombre, estado, frase, cifra y detalle.
- **Caja de aviso** (`.caja-aviso`): para avisos y bloqueos. Siempre responde a tres preguntas: qué pasó, a qué afecta y qué puedes hacer. Solo ofrece acciones que la aplicación tiene.
- **Resumen** (`.resumen`): tres columnas con lo que consta de la corrida. El texto que se copia sale de una lista cerrada de campos.
- **Barra** (`.barra`): solo para cantidades que la aplicación mide, como los cuadros mostrados. El proceso no lleva barra ni porcentaje.

El verde describe el resultado concreto junto al que aparece; el ámbar, un aviso o algo pendiente; el rojo, un fallo o un bloqueo. Sin animaciones. Por debajo de 980 px todo pasa a una columna.

## Para las próximas herramientas

Etiquetado y anotación de imágenes, segmentación con corrección experta y resaltado de regiones candidatas usarán los mismos tokens, y dos reglas más:

- Lo sugerido por un modelo y lo confirmado por un experto se ven distinto en forma, no solo en color: contorno discontinuo y etiqueta "sugerido" frente a contorno continuo y etiqueta "confirmado por …". Un cambio de estado siempre es una acción explícita de la persona, registrada con su nombre y fecha.
- Las superposiciones sobre la imagen son finas y se pueden apagar con una tecla; la imagen sin superposiciones está siempre a un gesto de distancia, y la ventana de visualización nunca cambia sola.

Las decisiones de este documento caben en una hoja de estilo de doscientas líneas y no requieren ningún marco de interfaz: HTML, CSS y JavaScript sin dependencias, como hasta ahora.
