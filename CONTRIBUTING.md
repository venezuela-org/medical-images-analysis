# Cómo contribuir / How we work

## El ciclo de una tarea

1. Abre un *issue* con la plantilla: objetivo, qué se aprende, criterio de aceptación, recursos.
2. Crea una rama desde `main` con el número del *issue* en el nombre.
3. Cambios pequeños y commits atómicos con mensajes descriptivos.
4. Abre un *pull request*; las comprobaciones automáticas deben estar en verde.
5. La otra persona revisa y aprueba; quien revisa escribe o actualiza la página de documentación de esa tarea.
6. Se mezcla en `main` y se cierra el *issue* con un comentario de una línea sobre lo que cambió.

## Publicar una versión

Sigue `docs/lanzamientos.md`: notas de la versión escritas, etiqueta `vX.Y.Z`, borrador de Release generado por el flujo, prueba manual en Windows 10 y macOS, y solo entonces publicar.

## Lo que nunca entra al repositorio

Imágenes DICOM o NIfTI, zips de pacientes, mapas de seudónimos, índices privados, registros de revisión de píxeles, bitácoras con rutas de originales, claves o tokens. Tampoco en los adjuntos de una Release: el flujo de publicación lo comprueba, y la única excepción son los datos sintéticos de `demo.py`. Si dudas, no lo subas y pregunta en el *issue*.

## Sin terminal

Todo el ciclo se puede hacer desde el navegador: editar un archivo en GitHub, abrir el *pull request* desde la pantalla de comparación, comentar y aprobar. Codespaces da un entorno listo con un clic cuando hace falta ejecutar algo.

---

One issue per task; a small branch and pull request; green checks; the other person reviews; the reviewer documents; merge and close. Never commit images, patient zips, pseudonym maps, private indexes, logs with original paths, or secrets. Everything can be done from the browser; Codespaces provides a ready environment when something must run.
