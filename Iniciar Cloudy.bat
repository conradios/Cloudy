@echo off
chcp 65001 >nul
setlocal EnableExtensions
title Cloudy
cd /d "%~dp0"

rem El entorno de Python va FUERA de la carpeta del proyecto (que puede estar en OneDrive):
rem OneDrive bloquea archivos mientras sincroniza y hace fallar la instalacion.
set "APPDIR=%LOCALAPPDATA%\iPhoneVault"
set "VENV=%APPDIR%\venv"
set "PY=%VENV%\Scripts\python.exe"
set "MARK=%VENV%\instalado-v5.txt"
set "LOG=%APPDIR%\instalacion.log"
if not exist "%APPDIR%" mkdir "%APPDIR%"

rem ---------------------------------------------------------------- buscar Python
set "BASEPY="
where py >nul 2>nul
if not errorlevel 1 set "BASEPY=py -3"
if not defined BASEPY (
  where python >nul 2>nul
  if not errorlevel 1 set "BASEPY=python"
)
if not defined BASEPY goto :nopython

set "BASEEXE="
for /f "delims=" %%i in ('%BASEPY% -c "import sys;print(sys.executable)" 2^>nul') do set "BASEEXE=%%i"
if not defined BASEEXE goto :nopython
echo %BASEEXE% | find /i "WindowsApps" >nul
if not errorlevel 1 goto :storepython

%BASEPY% -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)"
if errorlevel 1 goto :oldpython
for /f "delims=" %%v in ('%BASEPY% -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%v"
echo Python %PYVER% en %BASEEXE%

rem ---------------------------------------------------------------- crear entorno
if exist "%PY%" goto :havevenv
echo Creando el entorno de Python en %VENV% ^(solo la primera vez^)...
%BASEPY% -m venv "%VENV%" > "%LOG%" 2>&1
if not errorlevel 1 goto :havevenv
echo   El metodo normal fallo ^(ensurepip^). Pruebo el metodo alternativo...
rmdir /s /q "%VENV%" 2>nul
%BASEPY% -m venv --without-pip "%VENV%" >> "%LOG%" 2>&1
if errorlevel 1 goto :error

:havevenv
"%PY%" -m pip --version >nul 2>&1
if not errorlevel 1 goto :haspip
echo Instalando pip dentro del entorno...
%BASEPY% -m pip --python "%PY%" install --disable-pip-version-check pip >> "%LOG%" 2>&1
if errorlevel 1 goto :error

:haspip
rem Revisa SIEMPRE que esten todas las librerias (si se agrego una nueva, se instala sola)
"%PY%" -c "import PySide6, PySide6.QtSvg, pymobiledevice3, pyicloud, pillow_heif, imageio_ffmpeg, psutil, comtypes" >nul 2>&1
if not errorlevel 1 if exist "%MARK%" goto :run
echo Instalando o completando dependencias ^(puede tardar unos minutos^)...
"%PY%" -m pip install --disable-pip-version-check --upgrade pip >> "%LOG%" 2>&1
"%PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :error
echo ok> "%MARK%"

:run
call :ensure_apple
call :ensure_codecs
set "PYTHONPATH=%~dp0src"
"%PY%" -m iphone_vault
if errorlevel 1 goto :error
exit /b 0

rem ---------------------------------------------------------------- driver USB de Apple (iTunes)
rem Windows necesita el servicio "Apple Mobile Device Service" (viene con iTunes) para ver el iPhone
rem por cable. Si no esta, se instala iTunes: primero con winget y, si no hay winget, con el
rem instalador oficial de Apple. Si falla, la app abre igual (el modo demo funciona sin el driver).
:ensure_apple
sc query "Apple Mobile Device Service" >nul 2>&1
if not errorlevel 1 exit /b 0
netstat -an 2>nul | find "127.0.0.1:27015" >nul
if not errorlevel 1 exit /b 0
echo.
echo Falta el driver USB de Apple, que viene con iTunes. Lo instalo ahora ^(solo esta vez^)...
echo Si Windows pregunta si permitis cambios, toca "Si".
where winget >nul 2>nul
if errorlevel 1 goto :apple_direct
winget install -e --id Apple.iTunes --source winget --accept-package-agreements --accept-source-agreements
goto :apple_check
:apple_direct
echo No hay winget en esta PC: descargo el instalador oficial de iTunes desde apple.com...
curl.exe -L --fail -o "%TEMP%\iTunes64Setup.exe" "https://www.apple.com/itunes/download/win64"
if errorlevel 1 goto :apple_fail
echo Se abre el instalador de iTunes: segui los pasos y cuando termine volves aca.
start "" /wait "%TEMP%\iTunes64Setup.exe"
:apple_check
sc query "Apple Mobile Device Service" >nul 2>&1
if errorlevel 1 goto :apple_fail
echo Driver de Apple instalado. Si la app no ve el iPhone, reinicia la PC una vez.
exit /b 0
:apple_fail
echo.
echo No pude instalar iTunes automaticamente. Instalalo a mano desde
echo   https://www.apple.com/itunes/   ^(version para Windows^)
echo y volve a abrir este archivo. La app abre igual: podes usar el modo demo mientras tanto.
pause
exit /b 1

rem ---------------------------------------------------------------- codecs para ver HEIC / HEVC
rem Gratis y oficiales de Microsoft: "HEIF Image Extensions" (9PMMSR1CGPWG) y
rem "HEVC Video Extensions from Device Manufacturer" (9N4WGH0Z6VHQ). Con eso Windows abre las fotos
rem HEIC y los videos HEVC ORIGINALES, sin convertir nada y sin perder calidad.
rem Si la Microsoft Store no esta disponible (por ejemplo en AtlasOS), se instalan dos visores gratuitos
rem que traen sus propios codecs: ImageGlass (fotos HEIC) y VLC (videos).
:ensure_codecs
if exist "%APPDIR%\codecs-v1.txt" exit /b 0
call :codecs_ok
if not errorlevel 1 goto :codecs_done
echo.
echo Instalo los codecs gratuitos de Microsoft para ver fotos HEIC y videos HEVC ^(solo esta vez^)...
where winget >nul 2>nul
if errorlevel 1 goto :codecs_manual
winget install --id 9PMMSR1CGPWG --source msstore --accept-package-agreements --accept-source-agreements
winget install --id 9N4WGH0Z6VHQ --source msstore --accept-package-agreements --accept-source-agreements
call :codecs_ok
if not errorlevel 1 goto :codecs_done
echo.
echo La Microsoft Store no esta disponible en esta PC. Instalo visores gratuitos que abren
echo los originales sin codecs extra: ImageGlass ^(fotos HEIC^) y VLC ^(videos^)...
winget install -e --id DuongDieuPhap.ImageGlass --source winget --accept-package-agreements --accept-source-agreements
winget install -e --id VideoLAN.VLC --source winget --accept-package-agreements --accept-source-agreements
goto :codecs_done
:codecs_manual
echo No hay winget en esta PC. Instala a mano, gratis:
echo   Fotos HEIC:  https://apps.microsoft.com/detail/9PMMSR1CGPWG
echo   Videos HEVC: https://apps.microsoft.com/detail/9N4WGH0Z6VHQ
echo   o los visores ImageGlass ^(imageglass.org^) y VLC ^(videolan.org^).
echo Tambien podes activar en la app: Ajustes ^> "Copia JPG / MP4".
:codecs_done
echo ok> "%APPDIR%\codecs-v1.txt"
exit /b 0

:codecs_ok
powershell -NoProfile -Command "if ((Get-AppxPackage -Name '*HEIFImageExtension*') -and (Get-AppxPackage -Name '*HEVCVideoExtension*')) { exit 0 } else { exit 1 }"
exit /b %errorlevel%

rem ---------------------------------------------------------------- errores
:nopython
echo.
echo No encontre Python. Instala Python 3.12 desde https://www.python.org/downloads/
echo y marca "Add python.exe to PATH" durante la instalacion. Despues volve a abrir este archivo.
pause
exit /b 1

:storepython
echo.
echo Tenes el Python de la Microsoft Store ^(%BASEEXE%^).
echo Esa version tiene restricciones que suelen romper los entornos virtuales.
echo Instala Python 3.12 desde https://www.python.org/downloads/ ^(marcando "Add python.exe to PATH"^)
echo y volve a abrir este archivo.
pause
exit /b 1

:oldpython
echo.
echo Tu Python es muy viejo. Hace falta 3.10 o superior ^(recomendado 3.12^): https://www.python.org/downloads/
pause
exit /b 1

:error
echo.
echo Hubo un error. Detalle guardado en: %LOG%
echo ---- ultimas lineas ----
powershell -NoProfile -Command "if (Test-Path '%LOG%') { Get-Content -Tail 25 '%LOG%' }"
echo ------------------------
echo Copia el texto de arriba y compartilo para revisarlo.
pause
exit /b 1
