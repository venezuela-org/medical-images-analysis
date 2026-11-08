#!/bin/bash
# Crea los issues iniciales a partir de la lista de mejoras de la revisión (8 nov 2026).
# Requiere gh autenticado con acceso al repositorio. Ejecutar una sola vez desde la raíz del repo.
set -e
R="venezuela-org/medical-images-analysis"
gh label create P1 --color B60205 --description "Antes de compartir casos" -R $R 2>/dev/null || true
gh label create P2 --color D93F0B --description "Experiencia de uso" -R $R 2>/dev/null || true
gh label create P3 --color FBCA04 --description "Empaquetado y plataformas" -R $R 2>/dev/null || true
gh label create hecho-en-0.6 --color 0E8A16 --description "Implementado, pendiente de prueba en Mac" -R $R 2>/dev/null || true

mk() { gh issue create -R $R --title "$1" --label "$2" --body "$3"; }

mk "Impedir importar el árbol de salida" "P1,hecho-en-0.6" "## Criterio de aceptación
La línea de comandos y la app rechazan rutas de entrada y salida que se solapan (incluidas la privada y la de reportes), antes de crear o cambiar archivos, resolviendo alias de ruta.

## Estado
Implementado en anonimizar 0.4.6 y app 0.6.0; probado con DICOM sintéticos. Falta la prueba en el Mac."
mk "Detectar entradas ya anonimizadas" "P1,hecho-en-0.6" "## Criterio de aceptación
Una importación normal las reporta aparte, sin asignar nuevos seudónimos ni copiar; nada de ANON a ANON en silencio.

## Estado
Implementado en anonimizar 0.4.6 (contador 'ya anonimizados'); prueba sintética en pruebas/."
mk "Registrar los ajustes reales de cada importación" "P1,hecho-en-0.6" "## Criterio de aceptación
Cada corrida terminada deja un registro inmutable con entrada, salida, política, prefijo, opción de descripciones y conteos.

## Estado
Implementado: limpios_PRIVADO/corridas.jsonl (privado, porque las rutas pueden llevar nombres). Falta mostrarlo en la app."
mk "Actualizaciones de política explícitas" "P1" "## Criterio de aceptación
Distinguir la actualización de la etiqueta de versión (en el sitio, ya implementada) de una reconstrucción por cambio de política: conservar el mapa y ofrecer un flujo recuperable, documentado en la app.

## Estado
Pendiente de diseño."
mk "Comparar SUV original / limpio" "P1,hecho-en-0.6" "## Criterio de aceptación
Comparación vóxel a vóxel del volumen PET completo seleccionado, emparejado por el índice privado: correspondencia uno a uno, geometría y orden, cálculo por separado, valores finitos, diferencia máxima, vóxeles distintos, factores e intervalos; reporte numérico guardado. Igualdad exacta esperada.

## Estado
Implementado: suv.py comparar y botón en Estudios; probado en 10 cortes (10/10 exactos) y con un vóxel alterado (detectado). Falta correr sobre la serie completa de S/P4 en el Mac."
mk "Flujo de revisión de píxeles" "P1,hecho-en-0.6" "## Criterio de aceptación
La revisión (texto grabado, rostro, otros identificadores) se registra contra los hashes exactos de los archivos revisados y se invalida si cambian. Se revisa la serie completa, no cortes de muestra. Cualquier recorte o defacing produce un derivado aparte, conserva los originales y exige validación nueva; la decisión sobre la cabeza es de Miguel.

## Estado
Primera versión implementada en app 0.6.0 (registro por paciente, invalidación por hash). Falta la herramienta de recorrido de toda la serie con marcadores, y el derivado recortado o con defacing."
mk "Separar paquete creado de revisión completada" "P1,hecho-en-0.6" "## Criterio de aceptación
La app y el manifiesto distinguen 'paquete creado localmente' de 'revisión completada'; la revisión pendiente se muestra de forma prominente.

## Estado
Implementado en app 0.6.0: estado por paciente en Enviar, aviso en la respuesta y en manifiesto.json."
mk "Resaltar la fila seleccionada" "P2,hecho-en-0.6" "## Criterio de aceptación
La fila de la serie mostrada queda resaltada de forma persistente, también tras refrescar la lista."
mk "Alcance explícito de las comprobaciones" "P2,hecho-en-0.6" "## Criterio de aceptación
La interfaz distingue serie, paciente e importación completa; la bitácora registra el alcance real de cada comprobación."
mk "Refrescar tras importar" "P2,hecho-en-0.6" "## Criterio de aceptación
Al terminar una importación se reconstruye el inventario y se limpian selecciones e imágenes obsoletas; nunca 'serie desconocida' por una selección vieja."
mk "Mejorar progreso y resultados" "P2" "## Criterio de aceptación
Procesados/total, tiempo transcurrido, resúmenes legibles y detalles técnicos desplegables en cada operación larga."
mk "Selector de carpeta de trabajo" "P2" "## Criterio de aceptación
Elegir y recordar la carpeta de trabajo desde la app, sin variables de entorno ni mover carpetas a mano; rechazar carpetas sincronizadas."
mk "Validación de todos los pacientes en una tabla" "P2" "## Criterio de aceptación
Emparejamiento, cotejo y SUV por paciente en una tabla; los fallos permanecen visibles."
mk "Gestión de almacenamiento" "P2" "## Criterio de aceptación
Espacio usado por originales, copias limpias, paquetes y respaldos; limpieza solo de archivos reproducibles o reemplazados, elegidos uno a uno."
mk "Lanzamiento de escritorio y ciclo de vida" "P3" "## Criterio de aceptación
Lanzamiento nativo por clic (ventana.py), errores de arranque claros, manejo de puerto ocupado, apagado limpio y reapertura correcta. Prueba en Mac real."
mk "Prueba en Windows y publicación de versiones" "P3" "## Criterio de aceptación
Repetir el flujo completo en Windows; publicar versiones probadas (zip con lanzadores, y más adelante ejecutables construidos por GitHub Actions) con instrucciones de instalación."
echo "issues creados"
