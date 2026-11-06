# Tasky

Panel de consulta para tareas vencidas o con fecha límite de hoy en Todoist y Trello, pensado para la pantalla táctil Freenove ESP32-S3 de 4 pulgadas.

Tasky no crea ni edita tareas. Permite completar tareas de Todoist y archivar tarjetas de Trello. La interfaz pide confirmación antes de cualquiera de estas acciones.

## Estado de la primera entrega

- El proyecto no tenía archivos ni instrucciones locales antes de empezar.
- Freenove identifica la variante de 4 pulgadas como `FNK0104S`, con pantalla 320x480 y controlador `ST7796`. En orientación horizontal, el firmware usa 480x320. El README del fabricante etiqueta esa variante como `ST7789`, pero su tabla de hardware y el ejemplo Arduino específico seleccionan `ST7796`; el proyecto sigue estos últimos.
- El ejemplo táctil de Freenove identifica `FT6336U`, con SDA GPIO 16, SCL GPIO 15, reset GPIO 18 e interrupción GPIO 17. El fabricante documenta Wi-Fi de 2,4 GHz.
- El esquema oficial consultado identifica el ESP32-S3, pero no da una capacidad verificable de flash/PSRAM ni un número de revisión física. Comprueba el texto impreso en tu módulo antes de cargar el firmware; no se ha supuesto un módulo de otra placa CYD.
- El firmware y el servicio aún no se han probado en la placa ni contra cuentas reales. Este entorno no tiene Arduino CLI ni PlatformIO instalados.

## Arquitectura

```text
Todoist API ─┐
             ├─ servidor local Python ── HTTP en la LAN ── firmware Arduino / TFT táctil
Trello API ──┘      tokens, filtros, caché y acciones limitadas
```

Se recomienda el intermediario local. El ESP32-S3 puede hacer Wi-Fi y TLS, así que las API remotas son técnicamente accesibles de forma directa; sin embargo, el dispositivo tendría que guardar tokens de Todoist y Trello con permisos de escritura. En este diseño, los tokens de proveedor solo se configuran en el servidor. El firmware recibe un token local independiente.

El firmware consulta como máximo cinco tareas por página; conserva esa página en RAM y hace las peticiones de red en una tarea FreeRTOS distinta a la interfaz. El servidor mantiene en memoria la última respuesta válida de cada proveedor, actualiza ambos proveedores en paralelo y comunica los fallos por separado. Una actualización manual fuerza una actualización; el intervalo predeterminado es cinco minutos y se configura con `TASKY_REFRESH_SECONDS` (mínimo 60 segundos). El firmware recibe ese mismo intervalo desde el servicio.

## APIs y selección de tareas

Todoist usa la API REST v1 actual: `GET /api/v1/tasks/filter` con `query=today | overdue` y `lang=en`, paginación por cursor, `GET /api/v1/projects` para los nombres de proyecto y `POST /api/v1/tasks/{task_id}/close` para completar. Se descartan objetos sin fecha o posteriores al día local.

Trello usa `GET /1/members/me/boards?filter=open`, y por cada tablero consulta tarjetas abiertas y nombres de listas. Incluye tarjetas con `due` vencido o de hoy, excluye `dueComplete` y cerradas, y muestra tablero/lista cuando están disponibles. Archivar llama `PUT /1/cards/{id}` con `closed=true`. La API también ofrece eliminar una tarjeta, pero esta versión elige archivar para que la acción sea reversible.

Trello documenta límites de 300 solicitudes por clave cada 10 segundos y 100 por token cada 10 segundos, además del límite especial de 100 solicitudes a `/1/members` cada 900 segundos. La implementación usa el recurso anidado `/members/me/boards`, no consulta `/1/members`, y cachea los datos cinco minutos. Todoist documenta un tiempo de procesamiento de 15 segundos para una solicitud estándar; su límite publicado de sincronización aplica a `/sync`, que Tasky no usa. Las respuestas HTTP 429 y errores transitorios se reintentan una vez en lecturas.

## Servicio local

Requiere Python 3.10 o posterior. El servicio usa solo la biblioteca estándar.

1. Crea un token local para el dispositivo en una terminal privada:

   ```sh
   python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
   ```

2. En esa misma terminal, configura el token generado, tu token personal de Todoist, la clave de API y token de Trello, y tu zona horaria IANA. No pegues secretos en este repositorio ni en conversaciones.

   ```sh
   export TASKY_DEVICE_TOKEN='token-local-generado'
   export TODOIST_API_TOKEN='token-personal-de-Todoist'
   export TRELLO_API_KEY='clave-de-la-aplicacion-Trello'
   export TRELLO_API_TOKEN='token-de-Trello'
   export TASKY_TIMEZONE='Europe/Madrid'
   export TASKY_REFRESH_SECONDS='300'
   ```

   Las variables de Todoist y Trello pueden omitirse si todavía no quieres conectar ese proveedor; la interfaz mostrará su estado como no configurado. No se implementa OAuth dentro de la pantalla. Crea los tokens desde las páginas oficiales de desarrollador de cada servicio. El token de Trello es secreto y puede autorizar acceso a tu cuenta; limita y revoca credenciales según las opciones actuales del proveedor.

3. Arranca el servicio:

   ```sh
   PYTHONPATH=server python3 -m tasky_service
   ```

   Por defecto escucha en el puerto `8787`, en todas las interfaces, para que la placa pueda acceder desde la LAN. El endpoint de salud es `http://<IP-del-servidor>:8787/health`.

4. En una instalación persistente, guarda estas variables en un archivo de entorno fuera del repo, con permisos solo para tu usuario (`chmod 600`), y arranca el servicio con systemd. Reserva la IP del servidor en el router o configura el nombre/IP que pondrás en la placa.

### Seguridad y límites

El ESP32-S3 puede usar TLS, pero la guía de Freenove solo confirma conectividad Wi-Fi de 2,4 GHz; no certifica el acceso HTTPS de este proyecto ni las condiciones de tu router. El servicio local llama a Todoist y Trello mediante HTTPS con la validación TLS de Python. La comunicación placa-servidor es HTTP en esta primera versión; usa solo una red Wi-Fi privada y de confianza con WPA2/WPA3. Sin la placa y una red de prueba no se ha verificado el enlace de extremo a extremo.

El token del dispositivo y la contraseña Wi-Fi se compilan en `secrets.h` y quedan en flash; alguien con acceso físico o al firmware puede extraerlos. No son equivalentes a los tokens de los proveedores, que permanecen en el servidor. El token personal de Todoist no es un secreto de solo lectura y puede dar acceso a datos de la cuenta; Trello también requiere un token privado además de la clave de API.

Para una LAN no confiable, no expongas el puerto directamente: coloca el servicio detrás de HTTPS con un certificado validado por el ESP32 y actualiza el cliente de red del firmware para validar esa CA. Esta versión no desactiva verificaciones TLS ni admite certificados arbitrarios. Protege el puerto del servidor con el firewall y no lo publiques en Internet.

## Firmware Freenove

El flujo de compilación sigue la guía oficial de Freenove: Arduino IDE, paquete de placas Espressif ESP32 3.2.0 y placa `ESP32S3 Dev Module`. Instala desde los ZIP oficiales de `Libraries/FNK0104S`:

- `TFT_eSPI_v2.5.43.zip`
- `TFT_eSPI_Setups_v1.2.zip`
- `FT6336U_v1.0.2.zip`
- ArduinoJson 7 desde el Library Manager de Arduino

En el setup de TFT_eSPI selecciona el perfil `FNK0104S_4P0_320x480_ST7796`. Guarda una copia de [secrets.example.h](firmware/Tasky/secrets.example.h) llamada `secrets.h` en el mismo directorio y rellena SSID, contraseña, URL del servidor y token local. `secrets.h` está excluido por `.gitignore`.

Abre `firmware/Tasky/Tasky.ino`, selecciona el puerto de la placa y habilita **USB CDC On Boot**. El AP debe ofrecer 2,4 GHz y permitir que el dispositivo alcance la IP y puerto del servidor. Compila y carga desde Arduino IDE. El perfil de 4 pulgadas debe verificarse con el modelo impreso en tu propia placa; otras variantes Freenove requieren otra configuración de pantalla/touch.

## Verificaciones locales

```sh
PYTHONPATH=server python3 -m unittest discover -s server/tests -v
```

Las pruebas usan clientes simulados y comprueban caché, fallo parcial, datos conservados y paginación. No llaman a las cuentas reales ni prueban el controlador LCD/touch. Para una prueba de integración, configura las credenciales solo localmente, arranca el servicio y consulta el endpoint con el token local:

```sh
curl -H "Authorization: Bearer $TASKY_DEVICE_TOKEN" \
  "http://127.0.0.1:8787/api/v1/today?source=todoist&limit=5"
```

## Material y datos necesarios

- Placa Freenove de 4 pulgadas, cable USB-C de datos y ordenador para Arduino IDE.
- Una red Wi-Fi de 2,4 GHz y un equipo siempre encendido en la misma LAN para el servicio.
- Confirmación de que la serigrafía de tu unidad dice `FNK0104S`; comparte el número/revisión que aparezca en la placa o módulo si es distinto.
- Zona horaria IANA correcta, por ejemplo `Europe/Madrid`.
- Tokens de Todoist/Trello y datos de Wi-Fi, introducidos solo en los archivos/variables locales indicados, nunca en un issue o commit público.

## Fuentes oficiales consultadas

- [Freenove FNK0104: modelo, tamaño, resolución y controlador](https://docs.freenove.com/projects/fnk0104/en/latest/fnk0104/codes/MAIN/Freenove_ESP32S3_Display.html)
- [Freenove: touch, pines y ejemplo de pantalla](https://docs.freenove.com/projects/fnk0104/en/latest/fnk0104/codes/MAIN/11_TFT_Touch.html)
- [Freenove: Wi-Fi 2,4 GHz y Arduino IDE](https://docs.freenove.com/projects/fnk0104/en/latest/fnk0104/codes/MAIN/9_WIFI_Web_Server.html)
- [Repositorio oficial Freenove, README y esquemas](https://github.com/Freenove/Freenove_ESP32_S3_Display)
- [Todoist API v1](https://developer.todoist.com/api/v1/)
- [Trello API: tarjetas](https://developer.atlassian.com/cloud/trello/rest/api-group-cards/)
- [Trello API: tableros del miembro](https://developer.atlassian.com/cloud/trello/rest/api-group-members/)
- [Trello API: autenticación y claves](https://developer.atlassian.com/cloud/trello/guides/rest-api/api-introduction/)
- [Trello API: límites de solicitudes](https://developer.atlassian.com/cloud/trello/guides/rest-api/rate-limits/)