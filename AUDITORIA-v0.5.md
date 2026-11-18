# Auditoría de integridad y seguridad — Cloudy 0.5

Fecha: 9/11/2026

## Resultado
| Revisión | Resultado |
|---|---|
| Pruebas automáticas (`pytest`) | **26/26 OK** (18 previas + 8 nuevas de salud, respaldo, registros y disco) |
| Pruebas de estrés (`stress/run_stress.py`) | **22/22 OK** (red, datos, contraseña, nombres maliciosos, kill -9, disco lleno, 30.000 elementos…) |
| Análisis estático de seguridad (`bandit`, severidad media o alta) | **0 hallazgos** |
| Vulnerabilidades conocidas en dependencias (`pip-audit`) | **0** |

## Qué se corrigió en esta auditoría
1. **Programas del sistema con ruta absoluta.** Abrir carpeta / mostrar en carpeta usaba `explorer`, `open` o
   `xdg-open` por nombre: en Windows un archivo plantado con ese nombre en la carpeta actual podía ejecutarse.
   Ahora se usa `%SystemRoot%\explorer.exe`, `/usr/bin/open`, etc.
2. **Nunca se abre un ejecutable.** «Abrir» desde la Fototeca solo abre fotos, videos, carpetas y registros.
   Cualquier otro tipo de archivo se muestra en la carpeta en vez de ejecutarse.
3. **Catálogo cerrado al salir.** Un temporizador podía consultar el catálogo después de cerrarlo (error en
   consola al cerrar la ventana). Ahora se detienen los temporizadores antes de cerrarlo.
4. **Consultas SQL revisadas.** Los nombres de tabla/columna de la base de Fotos se validan con una lista
   blanca (`[A-Za-z0-9_]`); los valores siempre van como parámetros.

## Integridad (nuevo)
- **Verificar todo**: relee cada archivo del disco y compara su SHA-256 con el calculado al copiarlo. Detecta
  archivos borrados, con otro tamaño o con un solo bit cambiado (probado).
- **Reparar**: los dañados se renombran a `.danado` (no se borran) y se vuelven a copiar en la próxima copia.
- **Respaldo del catálogo**: automático tras cada copia y verificación (últimos 7, en `.vault/backups`, dentro
  de tu carpeta). Si el catálogo se daña, se restaura solo al abrir (probado dañándolo a propósito).
- **FAT32**: los archivos de más de 4 GB no se intentan copiar en un disco FAT32 (se avisa antes).
- **Sin suspensión**: la PC no se duerme mientras copia o verifica.

## Privacidad (sin cambios en lo que sale de la PC)
- Nada sale de la PC por cable: bloqueo de red activo en todo el proceso (prueba de estrés de red OK).
- El registro en disco (`%APPDATA%\iPhoneVault\logs`) y el historial de alertas enmascaran correos, códigos de
  6 dígitos y tokens. La contraseña de Apple nunca se escribe (prueba de estrés OK).
- Respaldos del catálogo y registros quedan en tu PC: nada en servidores de terceros.
