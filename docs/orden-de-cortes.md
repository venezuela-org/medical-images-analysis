# Orden de los cortes y nombres de los archivos exportados

Al descomprimir un paquete, los archivos de cada serie aparecen en el orden de los cortes. Este documento explica cómo se decide ese orden y qué significa el número del nombre.

## Nombres

Dentro de la carpeta de trabajo, cada archivo limpio conserva su nombre interno: un código de 16 caracteres derivado de su identificador de instancia ya reescrito, por ejemplo `6a1511f09c538af7.dcm`. Esos nombres no cambian, y con ellos tampoco cambian las revisiones de píxeles ya registradas.

En el zip, y en su manifiesto, cada archivo aparece con su ruta exportada:

```
ANON-0001/estudio_cb168a224a/serie_98b54957b2_PT_002/corte_000002_6a1511f09c538af7.dcm
```

`corte_000002` es la posición del archivo dentro de su serie; el código que sigue es el nombre interno. Paciente, estudio y serie siguen en carpetas separadas, la numeración empieza en 1 en cada serie y lleva seis cifras, así el Finder y el Explorador de Windows ordenan bien también la serie de mil cortes. Ningún nombre original entra en la ruta.

El número es un índice de presentación. No se escribe en `InstanceNumber` ni en ningún otro atributo: los bytes de cada DICOM son los mismos antes y después de exportar, y el manifiesto guarda su hash.

La correspondencia entre ruta exportada y ruta interna queda en el manifiesto local del paquete, en `datos-pet/paquetes`, bajo `correspondencia_interna`. El manifiesto que viaja dentro del zip solo lleva rutas exportadas.

## Criterio de orden

El visor de la pestaña Estudios y la exportación usan la misma función, `ordenar_serie()` en `herramientas/app.py`, de modo que el corte que el visor muestra en la posición 7 es `corte_000007` en el zip. Los criterios se prueban en este orden, serie por serie:

| Criterio | Cuándo se usa | Qué dice el manifiesto |
|---|---|---|
| `InstanceNumber` | Todos los archivos de la serie lo tienen, es un entero y no se repite | `"criterio": "InstanceNumber"` |
| Posición y orientación | Falta o se repite `InstanceNumber`, y los archivos forman una pila coherente: todos con `ImagePositionPatient` e `ImageOrientationPatient`, una sola orientación, posiciones distintas a lo largo de la normal del plano y ninguna señal de varias fases temporales. Orden creciente a lo largo de la normal | `"criterio": "posición y orientación"`, con el motivo en `aviso` |
| Desempate determinista | Nada de lo anterior permite ordenar: número ausente o repetido y posiciones ausentes, repetidas, con varias orientaciones o con varias fases (`TemporalPositionIdentifier`, `NumberOfTemporalPositions` o `NumberOfTimeSlices` mayores que uno). Se ordena por número si lo hay y después por nombre interno | `"criterio": "desempate determinista"` y un aviso que dice que no es un orden anatómico verificado |

El desempate da siempre el mismo resultado para los mismos archivos, y se anuncia: en el visor, en el paso "Paquete" del modo rápido (que queda en aviso), en el mensaje de la pestaña Enviar y en el campo `orden_de_cortes` del manifiesto.

Una serie con varias fases temporales o varios fotogramas se ordena por `InstanceNumber` cuando este es válido y único; la posición sola nunca se usa para mezclar fases.

## El criterio se decide sobre un conjunto concreto

`ordenar_serie()` ordena los archivos que recibe, y el criterio depende de ellos. Una serie de cuatro cortes con `InstanceNumber` 2, 2, 1, 1 y posiciones crecientes se ordena por posición, porque el número se repite. Si un paquete lleva solo el primero y el tercero, sus números son 2 y 1, no se repiten, y ese paquete se ordena por `InstanceNumber`: el tercero queda delante del primero. Los dos resultados son correctos para su conjunto, y son distintos.

Por eso el orden de un paquete no se deduce cada vez de la serie guardada: se calcula una vez, sobre los archivos exactos del paquete, y se congela.

## En el modo rápido

La correspondencia de nombres se calcula una vez, sobre los archivos exactos de la corrida, y se congela junto con sus hashes. El zip pendiente y el zip revisado llevan los mismos nombres y el mismo orden. Una importación posterior del mismo paciente no añade archivos a ese paquete ni cambia su numeración.

La revisión de píxeles de una corrida se hace en el visor de la propia pestaña Rápido, "Los archivos de este paquete, en su orden". Ese visor muestra exactamente los archivos de la corrida, serie por serie, en el orden congelado, con el nombre exportado de cada corte; y se niega a mostrar un archivo que haya cambiado desde la corrida. Para ello lee cada archivo una sola vez, comprueba el hash de esos bytes y dibuja la imagen desde esos mismos bytes. La pestaña Estudios muestra todo lo guardado del paciente, que puede ser más y, como en el ejemplo anterior, puede ordenarse con otro criterio: sirve para explorar, y el visor de la corrida es el que corresponde al paquete.

Un archivo Enhanced puede llevar varios cuadros. En el visor de la corrida cada archivo sigue siendo un corte de la lista, y un segundo deslizador recorre sus cuadros. Si alguna imagen no se puede dibujar, la corrida lo indica y su revisión de píxeles no se puede registrar. La pestaña Estudios muestra el primer cuadro de cada archivo y lo avisa.

En la pestaña Enviar, el paquete lleva las series guardadas enteras, el mismo conjunto que muestra Estudios, de modo que ahí los dos órdenes coinciden.

## Qué no garantiza

El orden sigue los metadatos del estudio. Si el equipo numeró los cortes de la cabeza a los pies, así saldrán; si lo hizo al revés, también. La aplicación no decide cuál es el sentido anatómico correcto.
