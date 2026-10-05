@echo off
setlocal
title Analizador de rivales
cd /d "%~dp0"

echo.
echo  ===== Analizador de rivales =====
echo.

rem ---------- 1. Python ----------
set "PY="
python --version >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
    py -3 --version >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY goto nopython

rem ---------- 2. Entorno virtual (solo la primera vez) ----------
set "VPY=venv\Scripts\python.exe"
if not exist "%VPY%" (
    echo  Creando el entorno virtual por primera vez, puede tardar un minuto...
    %PY% -m venv venv
    if errorlevel 1 goto fallo
)

rem ---------- 3. Dependencias ----------
echo  Comprobando dependencias...
"%VPY%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto pipfail
echo  Buscando actualizacion de yt-dlp, porque YouTube cambia seguido...
"%VPY%" -m pip install --disable-pip-version-check -q -U yt-dlp
if errorlevel 1 echo  No se pudo actualizar yt-dlp; se sigue con la version instalada.

rem ---------- 4. Archivo .env con la API key ----------
if not exist ".env" (
    if exist ".env.example" copy ".env.example" ".env" >nul
    echo.
    echo  Falta tu archivo .env: lo cree a partir de .env.example.
    echo  Se abrira el Bloc de notas. Pega tu API key en ANTHROPIC_API_KEY, guarda y cierralo.
    echo.
    start /wait notepad ".env"
)
findstr /b /c:"ANTHROPIC_API_KEY=pega-aqui" ".env" >nul 2>nul
if not errorlevel 1 goto nokey

rem ---------- 5. Avisos utiles ----------
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo.
    echo  AVISO: no encuentro ffmpeg, sin el no se puede descargar el video.
    echo  Instalalo con:  winget install Gyan.FFmpeg   y vuelve a abrir este archivo.
)
where node >nul 2>nul
if errorlevel 1 (
    echo.
    echo  AVISO: no encuentro Node.js, YouTube puede fallar sin el.
    echo  Descargalo desde https://nodejs.org y vuelve a abrir este archivo.
)

rem ---------- 6. Arrancar ----------
echo.
echo  Iniciando. Se abrira el navegador en http://127.0.0.1:5000
echo  Para cerrar la aplicacion, cierra esta ventana o presiona Ctrl+C.
echo.
"%VPY%" app.py
echo.
echo  La aplicacion se detuvo.
pause
exit /b 0

:nopython
echo  No encontre Python en este equipo.
echo  Instalalo desde https://www.python.org/downloads/ y marca "Add python.exe to PATH".
echo.
pause
exit /b 1

:fallo
echo.
echo  No se pudo crear el entorno virtual. Revisa el mensaje de arriba.
echo.
pause
exit /b 1

:pipfail
echo.
echo  No se pudieron instalar las dependencias. Revisa el mensaje de arriba.
echo  Si menciona una directiva de Control de aplicaciones, avisame con el texto completo.
echo.
pause
exit /b 1

:nokey
echo.
echo  Todavia no pusiste tu API key. Abre el archivo .env, reemplaza "pega-aqui-tu-clave"
echo  por tu clave real de Anthropic, guarda y vuelve a abrir este archivo.
echo.
pause
exit /b 1
