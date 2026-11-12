# Revisión de píxeles: qué se registra, qué se cuenta y cómo se retira

La revisión de píxeles es la declaración de una persona: alguien miró las imágenes buscando texto grabado, un rostro identificable u otro identificador visible, y firma con su nombre lo que encontró. Ningún programa de esta aplicación la sustituye. Las comprobaciones automáticas (la compuerta de metadatos, el cotejo, la comparación de SUV) dicen cosas sobre las etiquetas y sobre los números; no dicen nada sobre lo que se ve en la imagen, y un resultado correcto en todas ellas no significa que un paquete esté listo para compartirse.

## Qué cuenta la aplicación: cuadros mostrados

Para declarar "sin hallazgos" hace falta que todos los cortes hayan estado en pantalla. La aplicación cuenta eso, y solo eso:

- Un cuadro cuenta como **mostrado** cuando el navegador avisa de que su imagen se cargó en el visor y siguió a la vista un instante, con la pestaña y la imagen visibles. Antes tiene que haberse pedido al visor.
- En un archivo con varios cuadros cuenta cada cuadro.
- Las comprobaciones que la aplicación hace por su cuenta, como dibujar una imagen de cada serie para saber si el visor puede con ella, no cuentan.
- Lo mostrado se liga al contenido. De cada corte se guarda el sha256 de los bytes que se sirvieron al visor, y al registrar la revisión se compara con el sha256 que el archivo tiene en ese momento: si un archivo se sustituyó por otro, aunque tenga el mismo tamaño y la misma fecha, sus cortes no cuentan. El contador que se ve en pantalla es más ligero (mira tamaño y fecha) y sirve de orientación; lo que autoriza es la comparación por contenido.
- Lo que se registra es lo que se recorrió. Al registrar "sin hallazgos" en Estudios, los archivos del paciente que hay en disco tienen que ser exactamente los del inventario que estaba en pantalla. Si después de cargar ese inventario llegó algún archivo, o falta alguno, la revisión se rechaza, el inventario se rehace y hay que recorrer lo que falta.

El recuento aparece como "Mostrados en este navegador: 94 de 5.968 cuadros". **Mostrado no significa revisado.** Que una imagen haya pasado por la pantalla no prueba que alguien la haya mirado con atención; el recuento solo impide declarar que se revisó todo cuando parte no llegó a verse. El botón "Recorrer la serie" pasa los cortes uno a uno por la pantalla; mirarlos sigue siendo cosa de quien revisa.

El registro de una revisión "sin hallazgos" guarda el recuento junto a la declaración: cuántos cuadros se mostraron, de cuántos, y la nota de que mostrado no es revisado. La declaración es del revisor, con su nombre y la fecha.

En el modo rápido el recuento es el de los cuadros de la corrida, en su visor. En la pestaña Estudios es el de todos los cortes guardados del paciente. Registrar "con hallazgos" no exige haberlo mostrado todo: basta haber visto el hallazgo.

## Cuándo la revisión queda pendiente

La revisión de un paciente o de una corrida está pendiente mientras nadie la haya declarado, cuando sus archivos cambiaron después, cuando se registraron hallazgos, o cuando se retiró. Si una valoración no está resuelta, por ejemplo qué hacer con los cortes de la cabeza, lo que corresponde es dejarla pendiente o registrar el hallazgo que se haya visto, no anticipar un resultado.

## Retirar una revisión

Una revisión registrada se puede retirar desde la pestaña Registro, en "Revisiones de píxeles registradas". Hay que indicar quién la retira y por qué.

Retirar no borra nada:

| Qué | Qué ocurre |
|---|---|
| Registro original de un paciente | Se guarda intacto en `limpios_REPORTES/historial_revisiones/`, con su sha256 |
| Registro original de una corrida | Se queda en su sitio, sin tocar |
| Registro de retirada | Sustituye al original: quién, cuándo, por qué, a qué registro sustituye y qué paquetes dependían de él |
| Estado en la aplicación | La revisión deja de valer: el paciente vuelve a pendiente en Estudios y en Enviar |
| Zips ya exportados | No cambian. No se pueden cambiar |

Lo último es lo importante. Un zip que ya se creó con esa revisión sigue diciendo en su manifiesto que estaba revisado, esté donde esté. La retirada los enumera, con su carpeta, si siguen allí y si siguen siendo el mismo archivo, para que no se compartan; no puede hacer más que eso. Enumera los que dependían exactamente de la revisión retirada: el zip de una corrida, para la revisión de esa corrida; y los paquetes de la pestaña Enviar cuyo manifiesto cita esa revisión del paciente, por su fecha. Un paquete revisado con otra revisión del mismo paciente no entra en la lista.

La declaración de una corrida del modo rápido es un solo acto. Por eso retirar la revisión de la corrida retira también las revisiones de paciente que esa corrida escribió, y retirar una de esas revisiones de paciente retira la corrida y las demás. Una revisión nueva después de una retirada no borra el historial: la primera revisión y su retirada siguen guardadas.

Ningún registro de revisión se sobrescribe sin dejar antes una copia en el historial, tampoco al registrar una revisión nueva sobre otra anterior.

### Retirar y empaquetar a la vez

El empaquetado de la pestaña Enviar, las retiradas, las corridas del modo rápido y el registro de revisiones usan el mismo candado: mientras uno está en marcha, los demás esperan. Además, el empaquetador vuelve a comprobar, antes de dejar evidencias y justo antes de publicar, que la revisión de cada paciente que va a salir como revisado sigue siendo el mismo registro y sigue sin retirar. Si dejó de valer, el paquete no se publica y se dice de qué paciente y por qué; al volver a empaquetar, sale con la revisión pendiente.

El manifiesto de un paquete de Enviar cita la revisión de cada paciente por el sha256 de su registro (`registro_sha256`). Con ese identificador, y no con la fecha, se reconocen los paquetes que dependen de una revisión cuando se retira. Los manifiestos anteriores a este campo se siguen reconociendo por la fecha de la revisión.

### Una retirada vale desde que se registra

Cada retirada es un diario, `limpios_REPORTES/retiradas/retirada_<fecha>_<id>.json`. Escribirlo es el punto de compromiso:

1. El diario dice quién retira, cuándo y por qué; qué registros alcanza, con el sha256 de cada uno y el motivo por el que entra; qué paquetes dependían de ellos; y qué queda por hacer en cada registro.
2. Desde que el diario existe, las revisiones que alcanza dejan de autorizar que se comparta nada. La aplicación consulta los diarios antes de mirar el registro de un paciente, de modo que da igual que la actualización de ese registro haya fallado.
3. Después se actualiza cada registro: se anota la retirada junto al registro de la corrida, se guarda en el historial el original de cada paciente y se sustituye su registro por la retirada. Cada paso se anota en el diario.

Si el diario no se puede escribir, no se ha retirado nada, y así se dice.

Si falla alguno de los pasos posteriores, la retirada queda registrada e incompleta. La respuesta enumera lo hecho y lo pendiente, y la pestaña Registro lo muestra con el botón "Terminar las retiradas pendientes". Volver a intentarlo, o arrancar de nuevo la aplicación, termina lo que falta. Terminarla no cambia quién la retiró, cuándo ni por qué: esos datos son los del primer intento, y cada reintento se anota aparte. Repetir un paso ya hecho no hace nada, y no se crean copias de más en el historial.

Si, con una retirada a medias, alguien registra una revisión nueva de ese paciente, es otro registro: la retirada no lo alcanza, y al terminarla no se toca.

Los registros se escriben de forma atómica: en disco queda el anterior entero o el nuevo entero.

### Cuando el estado de las retiradas no se puede verificar

"No hay ninguna retirada" y "no se puede saber si la hay" son situaciones distintas, y la aplicación las trata distinto. Si un diario de retirada no se puede leer, su contenido no es JSON válido, su estructura no es válida, o la carpeta `retiradas/` no se deja listar, la aplicación no puede saber a qué revisiones alcanza. En ese caso:

- Ninguna revisión autoriza que se comparta. Su estado pasa a "no verificable", con el nombre del diario que requiere atención y el motivo. Vale para todos los pacientes, también para los que nada tienen que ver con esa retirada, porque no se puede saber a quién alcanza.
- Un paquete nuevo marca a esos pacientes como pendientes, y su manifiesto lo dice.
- No se registra ninguna revisión "sin hallazgos", ni en el modo rápido ni en la pestaña Estudios. La operación se rechaza antes de escribir ninguna evidencia, con el diario que requiere atención y el motivo. Registrar un hallazgo sí se puede: no autoriza nada.
- Ningún paquete sale como revisado. La regla está en el empaquetador común, así que vale también para el modo rápido, que trae su propia revisión de corrida; y se comprueba al empezar, antes de dejar evidencias y justo antes de publicar. Al destino elegido no llega nada, tampoco un paquete con el estado rebajado.
- El zip pendiente del modo rápido se sigue creando, en la carpeta de trabajo, y su manifiesto dice en el campo `retiradas` que el estado no se pudo verificar. Todo manifiesto lleva ese campo, con `verificables` en verdadero o falso.
- La pestaña Registro muestra qué diario requiere atención. No se puede retirar ni terminar ninguna retirada hasta resolverlo.
- Al arrancar, la aplicación lo avisa en la terminal y sigue funcionando.
- Ningún registro se toca. Cuando el diario vuelve a poder leerse, todo sigue donde estaba: la retirada comprometida, lo que le faltaba por hacer y las revisiones de los demás.

Si un diario tiene algún defecto pero su alcance se lee bien, sigue retirando exactamente esos registros, aparece en Registro como diario que requiere atención y no se puede terminar hasta arreglarlo; las revisiones de los demás pacientes siguen valiendo.

Que la carpeta `retiradas/` no exista significa que no hay retiradas. Un registro de paciente que ya dice "retirada" lo sigue diciendo aunque su diario deje de poder leerse.

Lo que esto no cubre: si alguien borra el diario de una retirada que quedó a medias, la aplicación no tiene cómo saber que existió. Una retirada terminada deja además su anotación junto a la corrida y el registro de retirada de cada paciente; una interrumpida solo tiene el diario hasta que se termina.
