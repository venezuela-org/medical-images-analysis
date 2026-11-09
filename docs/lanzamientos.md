# Cómo se publica una versión

El repositorio es la única fuente: no hay zips mantenidos aparte. Cada versión sale de una etiqueta `vX.Y.Z` sobre `main`.

1. Pruebas en verde en `main` (flujo `comprobaciones`).
2. Escribe `docs/notas-de-version/vX.Y.Z.md` con tres apartados: qué cambió, qué se probó, limitaciones conocidas. El flujo de publicación falla si falta.
3. Actualiza `VERSION` en `herramientas/app.py` y `docs/CAMBIOS.md`.
4. Etiqueta y empuja: `git tag vX.Y.Z && git push origin vX.Y.Z`.
5. El flujo `publicar` construye los paquetes de Windows y macOS, les hace la prueba de humo por la API (importación, comprobaciones, revisión, exportación y verificación del paquete), genera el caso sintético, comprueba los adjuntos por contenido (ningún DICOM fuera de la demo, ningún archivo privado conocido; los datos de prueba de las dependencias se podan del paquete) y crea un **borrador** de Release con todo. La comprobación de adjuntos demuestra lo que inspecciona, no la ausencia de datos de pacientes en cualquier forma imaginable: por eso el paso siguiente es manual.
6. Descarga los paquetes del borrador y pruébalos a mano en Windows 10 y macOS: instalación, "Crear caso de demostración", Anonimizar, Estudios, Enviar. Anota el resultado en las notas si hace falta.
7. Publica el borrador. Solo entonces funcionan los botones de descarga del README (`releases/latest` ignora los borradores) y solo entonces lo descargan los colegas. La guía rápida en PDF (`docs/guia-rapida.pdf`) se adjunta sola; si cambia, se regenera desde el lienzo de diseño y se sustituye en `docs/` antes de etiquetar. El repositorio es público por ahora: cualquiera puede descargar los paquetes, y por eso nunca entra en él ni en sus adjuntos nada que no sea software o datos sintéticos.

Nunca entran al repositorio ni a los adjuntos: imágenes de pacientes, mapas de seudónimos, índices de originales, registros de revisión de píxeles ni paquetes de exportación. Solo software y datos sintéticos o públicos aprobados. Antes de cambiar la visibilidad del repositorio, o tras cualquier duda, se revisa todo el historial de git con las dos comprobaciones de `docs/historial.md`.
