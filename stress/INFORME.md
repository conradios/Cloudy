# Informe de pruebas de estrés — iPhone Vault 0.3

Resultado: **22/22 pruebas de estrés OK** y **13/13 pruebas funcionales OK**.
Para repetirlas: `python stress/run_stress.py` (tarda unos 4 minutos y genera `stress/results.json`).

Como acá no hay un iPhone real, las pruebas usan un **iPhone sintético** (`stress/synthetic.py`). Genera
fototecas enormes con contenido válido (HEIC, MOV, JPEG, PNG) sin ocupar disco del lado del origen y permite
inyectar latencia, límite de velocidad, cortes, errores, datos truncados u orígenes hostiles. La parte de
iCloud se probó contra los servidores reales de Apple (inicio de sesión) y con un servidor simulado (descargas).

## Resultados

| | Categoría | Prueba | Tiempo |
|---|---|---|---|
| ✅ | seguridad | Red: solo conexiones locales durante todo el flujo + intentos de fuga bloqueados | 0.7 s |
| ✅ | seguridad | Datos: no quedan archivos fuera del destino (normal y tras kill -9 en pleno escaneo) | 4.1 s |
| ✅ | seguridad | Contraseña de Apple: nunca se escribe en disco ni en el registro (login real contra Apple) | 1.0 s |
| ✅ | seguridad | Nombres maliciosos: path traversal, nombres reservados de Windows, symlinks | 2.1 s |
| ✅ | seguridad | Base de Fotos hostil o corrupta: no rompe el escaneo ni ejecuta SQL | 0.0 s |
| ✅ | seguridad | Origen hostil: stream infinito o truncado no llena el disco ni deja archivos | 4.1 s |
| ✅ | seguridad | Dos instancias sobre la misma carpeta: la segunda se rechaza | 0.1 s |
| ✅ | rendimiento | Escala: escaneo y plan de 30.000 elementos (fototeca grande) | 2.9 s |
| ✅ | rendimiento | Velocidad máxima del motor con un video de 2 GB (con y sin relectura) + memoria | 10.1 s |
| ✅ | rendimiento | Muchos archivos chicos: 3.000 archivos de 64 KB (costo fijo por archivo) | 6.5 s |
| ✅ | rendimiento | Velocidad real simulada (cable a 40 MB/s): el motor no frena y el tiempo restante es fiable | 10.7 s |
| ✅ | rendimiento | Interfaz: refresco con 30.000 elementos y costo por actualización en vivo | 2.1 s |
| ✅ | fallas | Cortes de cable aleatorios: 12+ desconexiones durante 500 MB | 41.8 s |
| ✅ | fallas | Lecturas con errores al azar (3 %): reintenta y nunca guarda basura | 5.0 s |
| ✅ | fallas | Disco lleno a mitad de copia: no deja basura y la siguiente corrida completa | 0.8 s |
| ✅ | fallas | Archivo bloqueado por el antivirus al renombrar (típico de Windows): reintenta | 5.1 s |
| ✅ | fallas | kill -9 del proceso 8 veces en momentos al azar: retoma y todo queda íntegro | 19.7 s |
| ✅ | fallas | El iPhone no vuelve nunca: se rinde a tiempo, conserva lo parcial y avisa | 6.1 s |
| ✅ | fallas | Cancelar mientras espera la reconexión responde al instante | 1.5 s |
| ✅ | fallas | iCloud: enlace vencido (403) + servidor que ignora Range + corte de red a mitad | 0.1 s |
| ✅ | fallas | Nombres repetidos y archivos tuyos ya existentes: nunca se pisa nada | 0.0 s |
| ✅ | fallas | Interfaz: un error inesperado en segundo plano se muestra y la app sigue usable | 0.9 s |

## Números clave

| Medición | Resultado |
|---|---|
| Fototeca de 30.000 elementos (~264 GB): escaneo | 0.46 s (estimado con USB real: ~1.7 s) |
| Plan de copia, catálogo vacío / con 30.000 ya copiados | 0.13 s / 0.56 s |
| Velocidad máxima del motor (video de 2 GB) con relectura SHA-256 | 454 MB/s |
| Ídem sin relectura | 670 MB/s |
| Memoria de Python durante una copia de 2 GB | 9.0 MB (no crece con el tamaño del archivo) |
| Costo fijo por archivo (3.000 archivos de 64 KB) | 2.0 ms (509 archivos/s) |
| Cable simulado a 40 MB/s: velocidad lograda | 35.6 MB/s |
| Error medio del «tiempo restante» | 0.3 % |
| Interfaz con 30.000 filas: refresco completo | 0.72 s |
| Cortes de cable durante 500 MB | 13 cortes, 13 reconexiones, 7 archivos retomados a mitad, 0 diferencias |
| kill -9 en momentos al azar | 8 veces; 39 archivos idénticos al original; catálogo «ok» |

**En la práctica:** el motor (más de 400 MB/s) es mucho más rápido que cualquier cable de iPhone, así que el
límite lo pone el teléfono. Como referencia, una fototeca de 285 GB por un cable Lightning (USB 2, unos 30–35 MB/s)
tardaría alrededor de 2,5 horas. Con USB-C de alta velocidad debería ser bastante menos, pero eso hay que
medirlo con tu iPhone.

## Problemas que encontraron las pruebas (y que ya están corregidos en la 0.3)

1. **Fuga de red por proxy local** (grave): `requests` respetaba el proxy del sistema, así que una conexión
   a 127.0.0.1 podía salir a internet. Ahora se ignoran los proxies y el loopback solo se permite hacia
   usbmuxd (el servicio USB de Apple) o hacia puertos que abrió la propia app.
2. **UDP y sockets crudos** (grave): `sendto` hacia 8.8.8.8 y el módulo `_socket` esquivaban el bloqueo.
   Ahora hay además un *audit hook* de Python, que intercepta en C toda conexión, envío y resolución DNS.
3. **Path traversal** (grave): un nombre de archivo malicioso de iCloud (`../../../x`) escribía fuera de la carpeta
   destino. Ahora los nombres se limpian (sin separadores, sin nombres reservados de Windows como CON o NUL,
   con largo acotado) y se verifica que la ruta final no salga del destino, tampoco a través de enlaces simbólicos.
4. **Origen hostil con datos infinitos** (grave): un servidor que no paraba de mandar datos llenaba el disco
   (llegó a 16 GB en un minuto). Ahora se corta en cuanto supera el tamaño anunciado.
5. **Metadatos sensibles en temporales**: si el proceso moría durante el escaneo, la copia de `Photos.sqlite`
   (con fechas y ubicaciones GPS de toda la fototeca) quedaba en la carpeta temporal del sistema. Ahora va a una
   carpeta privada de la app que se vacía al terminar y cada vez que la app arranca.
6. **Dos ventanas sobre la misma carpeta** podían pisarse. Ahora hay un bloqueo entre procesos.
7. **Pérdida silenciosa por nombres repetidos**: dos fotos distintas con el mismo nombre y el mismo segundo de
   captura se tomaban por la misma y se copiaba una sola. La huella ahora incluye el tamaño del original.
8. **Duplicado tras un corte** entre «renombrar» y «anotar en el catálogo»: ahora el archivo huérfano se
   reconoce por SHA-256 y se reutiliza.
9. **Disco lleno**: antes reintentaba archivo por archivo. Ahora se detiene enseguida con un mensaje claro y la
   siguiente corrida completa sin problemas.
10. **Base de Fotos con una fila rara**: antes se descartaba toda la base. Ahora se ignora solo esa fila, y los
    nombres de tabla no confiables nunca se usan en SQL (`trusted_schema=OFF`).
11. **Interfaz lenta con fototecas grandes**: el refresco con 30.000 filas bajó de 4,4 s a 0.72 s
    (orden y textos precalculados).
12. **Velocidad**: la lectura del origen ahora va por adelantado (*prefetch*), así que el cable no queda
    ocioso mientras se escribe y se calcula el SHA-256.

## Qué NO se pudo probar acá (y cómo lo probamos con tu equipo)

- **iPhone real por USB**: latencia de AFC, velocidad real del cable, diálogo «Confiar» y cambios del esquema de
  Fotos según la versión de iOS. → Ejecutá `Diagnostico.bat` y una copia de 10 elementos.
- **Descargas reales de iCloud** con tu cuenta (solo se probó el inicio de sesión contra Apple con una cuenta inexistente).
- **Windows en sí**: rutas de más de 260 caracteres, bloqueos del antivirus (simulados), el driver de Apple.
  Los nombres se acotan a 100 caracteres, pero conviene que la carpeta destino tenga una ruta corta (por ejemplo, `D:\Fotos`).
- **Cortes de luz reales**: `kill -9` simula un cierre abrupto del proceso, no una caída del sistema
  operativo con escrituras sin volcar. Igual, cada archivo se sincroniza al disco (fsync) antes de darse por copiado.
