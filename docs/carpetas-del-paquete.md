# Las carpetas del paquete

Dentro del zip, cada archivo está en `PACIENTE/ESTUDIO/SERIE/corte_NNNNNN_<código>.dcm`. Desde la v0.10.0 las carpetas del paciente y del estudio se pueden leer:

```
ANON-0002_F/
  estudio_01_37a/
    serie_b946f81dcd_MR_901/
      corte_000001_….dcm
      corte_000002_….dcm
manifiesto.json
```

Antes eran `ANON-0002/estudio_7b7191db84/…`. Lo pidió quien probó la aplicación con un estudio real: en Estudios veía "ANON-0002 · F · 37 años en el estudio" y en el zip solo `ANON-0002` y un código.

## Cómo se forman

En la siguiente tabla se ve cada carpeta y de dónde sale su nombre.

| Carpeta | Nombre | De dónde sale |
|---|---|---|
| Paciente | `ANON-0002_F` | El identificador anónimo y el sexo registrado en `PatientSex` |
| Estudio | `estudio_01_37a` | El número del estudio dentro del paciente y la edad en ese estudio |
| Serie | `serie_b946f81dcd_MR_901` | Sin cambios: código, modalidad y número de serie |

**El sexo va en la carpeta del paciente** cuando todos sus estudios del paquete registran el mismo. Si no está registrado, o hay conflicto, la carpeta lleva solo el identificador: `ANON-0002`.

**La edad va en la carpeta del estudio**, porque es del estudio y no del paciente. El mismo paciente puede tener un estudio a los 37 años y otro a los 39:

```
ANON-0002_F/estudio_01_37a/
ANON-0002_F/estudio_02_39a/
```

La edad se escribe con su unidad: `37a` (años), `8m` (meses), `3sem` (semanas), `12d` (días). A partir de 90 años se agrupa, como en la etiqueta: `90a-o-mas`. Una edad informada por el equipo, y no calculada, lo dice: `54a-informada`. Sin edad disponible, la carpeta lleva solo el número: `estudio_01`.

**El número del estudio** es su orden dentro del paciente: `estudio_01`, `estudio_02`. Se asigna la primera vez que la aplicación ve el estudio y no cambia después, así que el mismo estudio lleva el mismo número en todos los paquetes hechos con esa carpeta de trabajo. El orden es el de llegada a la aplicación, que puede no coincidir con el de las fechas. La pestaña Estudios muestra el mismo número: "Estudio 1".

## Qué no cambia

- Los archivos DICOM: ni un byte. El nombre de una carpeta no modifica ningún atributo.
- Los archivos limpios guardados en `datos-pet/limpios`, que conservan sus nombres internos.
- Las revisiones de píxeles ya registradas, que se refieren a esos archivos y a sus hashes.
- Lo que el paquete dice del paciente: el sexo y la edad ya estaban en su manifiesto y en la etiqueta del estudio. Ahora se ven también al listar el zip.

## Dónde queda escrito

- El manifiesto del zip lleva el bloque `carpetas`, con la correspondencia entre los nombres internos y los del paquete, y cada estudio de `demografia` lleva su `carpeta` y su `numero`.
- Los números de estudio se guardan en `datos-pet/limpios_PRIVADO/estudios.json`, que no sale de la computadora. Si ese registro no se pudiera leer, la aplicación no lo sobrescribe: el paquete se crea con el nombre interno del estudio y el manifiesto lo anota con `numero` vacío.
- En el modo rápido los nombres se fijan con la corrida: el zip pendiente y el revisado llevan las mismas carpetas.
