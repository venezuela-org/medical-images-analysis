@echo off
rem PET/CT local: doble clic para abrir la aplicacion (Windows)
rem Double-click to open the app. Installs uv the first time if needed.
cd /d "%~dp0"
set "UV=uv"
where uv >nul 2>nul
if errorlevel 1 (
  if exist "%USERPROFILE%\.local\bin\uv.exe" (
    set "UV=%USERPROFILE%\.local\bin\uv.exe"
  ) else (
    echo Instalando uv ^(una sola vez^)...
    powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    set "UV=%USERPROFILE%\.local\bin\uv.exe"
  )
)
if "%PETCT_DATOS%"=="" (set "DATOS=%USERPROFILE%\datos-pet") else (set "DATOS=%PETCT_DATOS%")
echo Carpeta de datos: %DATOS%   (cambiala con la variable PETCT_DATOS)
echo Se abrira el navegador. Cierra esta ventana para apagar la aplicacion.
"%UV%" run app.py --trabajo "%DATOS%"
pause
