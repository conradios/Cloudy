@echo off
chcp 65001 >nul
title Cloudy - Diagnostico
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\iPhoneVault\venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Primero ejecuta "Iniciar Cloudy.bat" una vez para instalar todo.
  pause
  exit /b 1
)
set "PYTHONPATH=%~dp0src"
echo Conecta el iPhone por cable y desbloquealo...
"%PY%" -m iphone_vault diagnose --out "%~dp0diagnostico_cloudy.json"
echo.
echo Se genero diagnostico_cloudy.json ^(solo cantidades, sin nombres ni fotos^).
pause
