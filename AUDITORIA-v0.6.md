# Auditoría v0.6 — Android, tarjetas SD y cámaras

## Qué se agregó
- **Android por MTP** (Windows, API oficial «Windows Portable Devices» vía comtypes): sin instalar nada en el celular.
- **Android por ADB** (Windows/macOS/Linux, «platform-tools» oficial de Google): con verificación SHA-256 en el propio celular.
- **Tarjetas SD, cámaras, pendrives y carpetas** (incluye RAW).
- **Detección automática** de lo enchufado (iPhone/iPad, Android por ADB o MTP, cámaras PTP) y elección si hay varios.
- **Samsung simulado** para el modo demo; diagnóstico `--source android` sin datos personales.

## Seguridad
| Riesgo | Cómo se evita |
|---|---|
| Borrar o modificar el celular | MTP se abre con `GENERIC_READ`; ADB solo ejecuta `ls/find/stat/cat/dd/sha256sum/getprop`; las carpetas se abren en modo lectura. Ningún backend tiene operaciones de escritura. La prueba con el ADB falso verifica que nunca llegue `rm`, `mv`, `push`, `install`, `tcpip`, `connect` ni redirecciones. |
| Transferencia por red / datos móviles | MTP es USB puro. ADB: solo se aceptan aparatos con `usb:` en `adb devices -l`; se descartan `ip:puerto`, mDNS (`_adb-tls-`) y emuladores; `ADB_MDNS=0`, servidor fijado a `127.0.0.1:5037`. El proceso de Cloudy sigue con el bloqueo de red (habla con adb por tuberías, no por sockets). |
| Inyección de comandos en el celular | Rutas entre comillas simples escapadas, después de `--`, sin NUL; probado con `'; touch …; '`, `$(…)`, backticks, saltos de línea y nombres que empiezan con `-`. En la compu no hay shell (lista de argumentos). |
| Salir de la carpeta / del celular | Rutas internas con prefijo `android/<serie>/` validado; se rechazan `..`, `.`, `\`, `:`; los almacenamientos son una lista cerrada (`/sdcard`, `/storage/XXXX-XXXX`). En carpetas: no se siguen enlaces simbólicos y todo se resuelve dentro de la raíz. |
| Copiar lo ya copiado (bucle) | La fuente «carpeta» nunca entra en una carpeta con `.vault/manifest.db` ni en el destino; además no se permite elegir un origen que contenga o esté dentro del destino. |
| Nombres hostiles del aparato | Cada segmento se limpia (`_safe`) también en el modo «espejo»; nombres reservados de Windows y caracteres prohibidos reemplazados. |
| Dos celulares con el mismo nombre de archivo | El catálogo usa la ruta con el número de serie (saneado). Además, la huella nombre+fecha+tamaño evita duplicados entre MTP y ADB del mismo celular. |
| Hotspot / anclaje USB de Android | Se suman las redes clásicas 192.168.42/43/44.0/24 a la detección (iCloud se bloquea). Mejor esfuerzo: Android 11+ puede elegir otra red. La copia por cable no usa red nunca. |
| Instalar ADB | Solo con consentimiento explícito, con winget/Homebrew (paquetes oficiales), fuera del proceso de Cloudy. Nunca se descarga desde la app. |

## Verificación
- Copia: tamaño exacto + SHA-256 en streaming + relectura del disco + estructura, igual que con el iPhone.
- ADB: además el celular calcula el SHA-256 y tiene que coincidir (si no, el archivo no se da por copiado). Probado con un celular que «miente».
- Cortes de cable: MTP/ADB/carpeta reintentan y retoman (ADB salta con `dd`, MTP lee y descarta).

## Herramientas
- `bandit -ll`: sin hallazgos de severidad media o alta.
- `pip-audit -r requirements.txt`: sin vulnerabilidades conocidas.
- Pruebas: 24 nuevas (fechas, almacenamiento, Samsung simulado de punta a punta, corte de cable, tarjeta de cámara,
  enlaces y destino excluido, ADB falso con nombres maliciosos, SHA del celular que no coincide, no autorizado,
  solo USB).

## Pendiente de probar con un aparato real
- MTP en Windows con un Samsung (no se puede emular acá): correr `Diagnostico.bat` o
  `python -m iphone_vault diagnose --source android-mtp` y compartir el .json (solo cantidades).
