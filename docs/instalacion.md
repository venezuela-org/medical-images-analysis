# PET-CT local: instalación y uso

Aplicación local para anonimizar estudios PET/CT, comprobar que la copia es exacta, emparejar CT y PET, calcular el SUV y preparar paquetes verificados. Todo ocurre en tu computadora; nada identificable sale de ella. Uso de investigación y aprendizaje.

## Instalar

1. Descarga el paquete de tu sistema desde la versión publicada: `PET-CT-local-vX.Y.Z-windows.zip` o `PET-CT-local-vX.Y.Z-macos.zip`.
2. Descomprímelo en una carpeta fuera de OneDrive, iCloud Drive, Dropbox o Google Drive, por ejemplo `C:\PET-CT local` o `~/Aplicaciones/PET-CT local`.
3. Windows: doble clic en `PET-CT local.exe`. Si SmartScreen avisa, "Más información" y "Ejecutar de todas formas"; el programa no está firmado.
   macOS: doble clic en `Abrir PET-CT local.command`. La primera vez macOS dice que no puede abrirlo porque no puede comprobar al desarrollador: cierra el aviso, ve a Ajustes del Sistema, Privacidad y seguridad, baja hasta el mensaje sobre `Abrir PET-CT local.command` y pulsa "Abrir de todos modos"; vuelve a hacer doble clic. Ese lanzador quita la marca de cuarentena del paquete y arranca la aplicación. Si en vez de eso aparece "Python.framework está dañado", el paquete sigue en cuarentena: abre la Terminal, escribe `xattr -dr com.apple.quarantine ` con un espacio al final, arrastra la carpeta `PET-CT local` a la ventana y pulsa Intro; luego vuelve a hacer doble clic en el lanzador.
4. Se abre una ventana de terminal (es la aplicación corriendo) y el navegador con la aplicación. Para apagarla, cierra la ventana de terminal.

La carpeta de datos se crea en `datos-pet` dentro de tu carpeta de usuario. Los originales que vayas a limpiar pueden estar en cualquier sitio fuera de la nube; la aplicación solo los lee.

## Probar sin datos de pacientes

En la pestaña Entrada, pulsa "Crear caso de demostración": crea un CT y un PET inventados con identidad ficticia. Pulsa Anonimizar y recorre el resto: Estudios (ver cortes, Antes y después, Emparejar, Cotejar, SUV, Comparar SUV) y Enviar.

## Uso con estudios reales

1. Entrada: "Elegir carpeta…" hasta la carpeta del caso, Anonimizar. Lee el reporte: archivos limpios, cuarentena, comprobación PET.
2. Estudios: selecciona una serie, recorre los cortes, mira "Antes y después", pulsa Emparejar, Cotejar y Comparar SUV. Registra la revisión de píxeles del paciente cuando hayas recorrido la serie completa.
3. Enviar: marca los pacientes y pulsa "Verificar y empaquetar". El zip queda en `datos-pet/paquetes`. Si la revisión de píxeles está pendiente, la aplicación lo dice y lo escribe en el manifiesto: el paquete existe, pero no está listo para compartirse.

## Lo que nunca debes compartir

La carpeta `limpios_PRIVADO` (mapa de seudónimos e índice de originales), la bitácora y los originales. Solo los zips de `paquetes` con revisión de píxeles vigente y sin hallazgos.
