# Identificador anónimo, sexo registrado y edad en el estudio

Cada estudio se presenta con una etiqueta de tres partes, siempre en este orden y siempre separadas:

```
ANON-0001 · F · 54 años en el estudio
```

La primera es el identificador anónimo del paciente; la segunda, el sexo tal como consta en el DICOM; la tercera, la edad cumplida en la fecha del estudio. Aparece en la pestaña Estudios, en el visor de cada corrida del modo rápido y, con su detalle, en el manifiesto de cada paquete.

## Identificador

Es el seudónimo que ya asignaba la aplicación, `ANON-0001`, y sale del mismo mapa privado y persistente. Un paciente conserva su identificador en todas las corridas, carpetas y paquetes, y hay una sola numeración para todos los pacientes. El sexo y la edad no forman parte del identificador ni de ninguna ruta: la carpeta del paciente sigue llamándose `ANON-0001`, y corregir un dato demográfico no cambia la identidad ni mueve archivos.

## Sexo

Se lee de `PatientSex` y se muestra tal como consta: `F`, `M` u `O`. No se deduce del nombre, de la imagen ni de ningún otro dato.

| Lo que traen los archivos del estudio | Lo que se muestra |
|---|---|
| Un mismo valor, `F`, `M` u `O` | Ese valor |
| Ningún valor, o un valor que no es uno de los tres | "sexo no registrado" |
| Valores distintos dentro del estudio | "sexo en conflicto", con los valores encontrados en el aviso; no se elige ninguno |

Una letra visible sobre una imagen puede ser una superposición del visor de otro programa o texto grabado en los píxeles. La aplicación no modifica los píxeles ni saca conclusiones de ellos, y esa letra no sustituye a la revisión de píxeles.

## Edad en la fecha del estudio

La edad es la del paciente el día del estudio, en años cumplidos. Se calcula durante la limpieza, con la fecha de nacimiento y la fecha del estudio originales, antes de que la primera se vacíe y la segunda se desplace. De ese cálculo solo se guarda la edad o el motivo por el que no se pudo obtener; ninguna fecha. La fecha de hoy no interviene.

- Quien nació un 29 de febrero cumple el 1 de marzo en los años no bisiestos.
- Por debajo de un año se conserva la precisión: meses cumplidos; por debajo de un mes, semanas; por debajo de una semana, días.
- A partir de 90 años se muestra "90 años o más", la misma agrupación que la política ya aplica a `PatientAge`.
- La edad pertenece al estudio. El mismo paciente puede tener 53 años en un estudio y 56 en otro; el mapa de seudónimos no guarda ninguna edad.

Cuando no se puede calcular, la etiqueta dice "edad no disponible" y el aviso da el motivo: falta la fecha de nacimiento o la del estudio, alguna no es válida, la del estudio es anterior a la de nacimiento, el resultado no es plausible, o los archivos del estudio dan edades distintas.

Si no hay edad calculada pero los archivos traen `PatientAge`, se muestra esa edad con su nombre: "54 años (edad informada por el equipo)". Es un dato que escribió el equipo, no un cálculo de esta aplicación. Si hay edad calculada y la informada no coincide, se muestra la calculada y un aviso.

## Qué cambia en la política de anonimización

Nada en los archivos limpios. La política `pet-ct-local/0.4` ya conservaba el sexo, el peso, la talla y la edad informada (opción *Retain Patient Characteristics*), vaciaba la fecha de nacimiento y desplazaba las fechas. Eso sigue igual: los archivos limpios no llevan ningún dato nuevo, sus bytes no cambian y las revisiones de píxeles registradas siguen vigentes.

Lo nuevo es un registro local, `datos-pet/limpios_PRIVADO/demografia.json`, que la limpieza rellena con lo que cada archivo original decía de la edad y el sexo. Contiene valores derivados, sin fechas y sin identidad, y no sale de la computadora; al manifiesto de un paquete solo pasa el resumen de los estudios de ese paquete.

## Estudios importados con una versión anterior

Los estudios que ya estaban importados no tienen ese registro. Para ellos:

- El sexo se muestra igual, porque se conserva en el archivo limpio.
- La edad calculada no existe, porque las fechas originales ya no están en la copia limpia. Si el archivo trae `PatientAge`, se muestra como edad informada; si no, "edad no disponible", con el aviso de que el estudio se importó con una versión anterior.
- Para obtener la edad calculada hay que **volver a importar la carpeta original**. La limpieza reconoce los archivos como duplicados verificados, no reescribe ninguno y solo rellena el registro; las revisiones de píxeles ya registradas no se invalidan.

## Dónde se decide y dónde se congela

Como el orden de los cortes, la demografía se resume siempre sobre un conjunto exacto de archivos. La pestaña Estudios resume todo lo guardado de cada estudio. Una corrida del modo rápido resume sus propios archivos y congela ese resumen: el visor de la corrida, el zip pendiente y el zip revisado muestran lo mismo aunque después se importe más de ese paciente. El manifiesto de un paquete lleva solo los pacientes y estudios presentes en él.

## En el manifiesto

```json
"demografia": {
  "nota": "…",
  "estudios": [
    {
      "paciente": "ANON-0001",
      "estudio": "estudio_3f1c9a7e52",
      "etiqueta": "ANON-0001 · F · 54 años en el estudio",
      "sexo": {"estado": "registrado", "valor": "F", "fuente": "PatientSex del DICOM; no se infiere", "avisos": []},
      "edad_en_el_estudio": {"estado": "calculada", "valor": 54, "unidad": "años", "agrupada": false, "texto": "54 años",
                             "fuente": "calculada con la fecha de nacimiento y la fecha del estudio originales, antes de anonimizar las fechas",
                             "motivo": null, "avisos": []},
      "archivos": 16
    }
  ]
}
```

`estado` vale `registrado`, `no_registrado` o `conflicto` para el sexo, y `calculada`, `informada` o `no_disponible` para la edad.
