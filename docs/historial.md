# Comprobar el historial de git antes de hacer público el repositorio

Un archivo borrado en un commit posterior sigue dentro del historial. Estas dos comprobaciones recorren todos los objetos de todas las ramas y etiquetas; se ejecutan en la raíz del repositorio, y la primera tarda unos segundos.

Ningún archivo DICOM en ningún commit (marcador `DICM` en el byte 128):

```
git rev-list --objects --all | awk '{print $1}' | git cat-file --batch-check='%(objectname) %(objecttype) %(objectsize)' \
  | awk '$2=="blob" && $3>132 {print $1}' | while read b; do git cat-file blob "$b" | head -c 132 | tail -c 4 | grep -q DICM && echo "DICOM en el historial: $b"; done; echo "historial revisado: sin DICOM si no hay líneas arriba"
```

Ningún archivo privado del proyecto en ningún commit (el nombre de la prueba `test_revision_pixeles.py` aparece y es código, no un registro):

```
git log --all --name-only --pretty=format: | sort -u | grep -Ei "mapa_seudonimos|indice_origen|raices\.json|corridas\.jsonl|revision_pixeles_ANON|registro\.log" || echo "sin archivos privados en el historial"
```

Ambas se ejecutaron el 9 de noviembre de 2026 antes de hacer público el repositorio, con resultado limpio.
