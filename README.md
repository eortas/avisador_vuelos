# Avisador de vuelos y stock

Consulta Google Flights para todas las salidas entre 30 y 60 dias desde la fecha
de cada ejecucion, guarda el historico en SQLite y avisa por Telegram cuando:

- empieza el seguimiento;
- aparece un nuevo minimo historico;
- el minimo supera durante dos consultas el minimo de las ultimas 24 horas en
  al menos un 10 % y 5 EUR;
- el minimo sube bruscamente al menos un 20 % y 10 EUR desde la consulta anterior.

Las subidas normales tienen un periodo de silencio de 12 horas. Los nuevos
minimos y las subidas bruscas se notifican inmediatamente. Para evitar falsas
alertas al mover la ventana de 30 a 60 dias, solo se comparan fechas presentes
en ambas consultas. Cada aviso incluye las cinco fechas de salida mas baratas.

El proyecto usa `flights`, una libreria no oficial que consulta la API interna de
Google Flights. No necesita una API de pago, pero puede dejar de funcionar si
Google cambia ese servicio.

## Stock de Nintendo Switch 2 en MediaMarkt y GAME

El archivo `stock_tracker.py` comprueba las fichas de la Switch 2 Edicion Zelda
40 Aniversario de MediaMarkt y GAME. Guarda cada estado en una base SQLite
separada y envia un mensaje por Telegram cuando detecta que vuelve a estar
disponible. La primera consulta solo avisa si ya hay stock. Los estados ambiguos
o de "Disponible proximamente" no generan alertas.

Las URL y los nombres se pueden cambiar en `.env` con las variables
`STOCK_PRODUCT_URL`, `STOCK_PRODUCT_NAME`, `GAME_PRODUCT_URL` y
`GAME_PRODUCT_NAME`. Para comprobarlo en local sin mandar mensajes:

```powershell
python stock_tracker.py --no-notify
```

Para recibir el estado actual aunque no haya habido cambio, ejecuta:

```powershell
python stock_tracker.py --force-notify
```

El workflow `answer-telegram.yml` que ya dispara `cron-job.org` ahora lanza
tambien esta comprobacion de stock en paralelo. No hay que cambiar su URL,
frecuencia ni cuerpo JSON: el stock se revisara con la misma frecuencia que ese
trabajo (cada minuto con la configuracion actual).

El workflow comparte los secretos `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` con
el avisador de vuelos. Su base de datos se conserva mediante la cache de
Actions, igual que el historico de vuelos.

## Uso local

Se necesita Python 3.11 o posterior.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python flight_tracker.py --no-notify
```

Edita `.env` para cambiar los aeropuertos o los limites de la ventana movil.
Cada ejecucion consulta 31 fechas y tarda unos minutos por la pausa entre
peticiones.

## Telegram

1. Abre `@BotFather` en Telegram, crea un bot con `/newbot` y guarda el token.
2. Envia cualquier mensaje al bot nuevo.
3. Abre `https://api.telegram.org/bot<TOKEN>/getUpdates` y busca el valor `chat.id`.
4. Completa `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` en `.env`.
5. Ejecuta `python flight_tracker.py --test-telegram` para comprobar el bot.
6. Ejecuta `python flight_tracker.py` para consultar y enviar alertas.

Los secretos no se guardan en Git porque `.env` esta ignorado.

## Preguntas por Telegram con Mistral

El bot tambien puede responder preguntas en lenguaje natural sobre las fechas y
precios guardados de la ruta configurada. No consulta otras rutas y solo acepta
mensajes del `TELEGRAM_CHAT_ID` definido en `.env`.

1. Anade la clave de Mistral a `.env`:

```text
MISTRAL_API_KEY=tu_clave
```

2. Asegurate de haber ejecutado al menos una consulta de vuelos.
3. Inicia el bot:

```powershell
python telegram_assistant.py
```

Mientras el proceso este abierto puedes escribir preguntas como `Que fechas hay
por debajo de 50 EUR?`, `Cual es el sabado mas barato?` o `Ha subido el minimo?`.
El comando `/estado` muestra el minimo actual sin consumir la API de Mistral.

El trabajo horario de `cron-job.org` actualiza precios, pero no mantiene procesos
abiertos. Puedes dejar `telegram_assistant.py` ejecutandose en un ordenador o
servidor, o usar el workflow `answer-telegram.yml` descrito mas abajo.

## Ejecucion con cron-job.org y GitHub Actions

1. Crea un repositorio en GitHub y sube estos archivos.
2. Ve a **Settings > Secrets and variables > Actions**.
3. Crea los secretos `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.
4. Ajusta la ventana de dias en `.github/workflows/check-prices.yml` si lo necesitas.
5. Abre **Actions > Comprobar precios de vuelos > Run workflow** para probarlo.

Las ejecuciones manuales envian siempre el estado actual. Las ejecuciones
iniciadas por `cron-job.org` solo avisan cuando detectan alguno de los cambios
configurados.

Para activar el workflow desde `cron-job.org`:

1. Crea en GitHub un token personal de granularidad fina limitado a este
   repositorio y con permiso **Actions: Read and write**.
2. Crea un trabajo en `cron-job.org` con frecuencia de una hora.
3. Usa el metodo `POST` y esta URL:

```text
https://api.github.com/repos/eortas/avisador_vuelos/actions/workflows/check-prices.yml/dispatches
```

4. Anade estas cabeceras, sustituyendo `<TOKEN_GITHUB>` por el token anterior:

```text
Accept: application/vnd.github+json
Authorization: Bearer <TOKEN_GITHUB>
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

5. Usa este cuerpo JSON:

```json
{"ref":"main","inputs":{"force_notify":"false"}}
```

El token de GitHub se guarda solamente en `cron-job.org`; no debe anadirse al
repositorio ni confundirse con `TELEGRAM_BOT_TOKEN`.

### Trabajo de cron para responder preguntas

Crea un segundo trabajo en `cron-job.org`, por ejemplo cada 5 minutos, con las
mismas cabeceras y esta URL:

```text
https://api.github.com/repos/eortas/avisador_vuelos/actions/workflows/answer-telegram.yml/dispatches
```

Usa este cuerpo, sin `inputs`:

```json
{"ref":"main"}
```

Antes de activarlo, crea tambien el secreto `MISTRAL_API_KEY` en **Settings >
Secrets and variables > Actions**. Este segundo workflow no busca vuelos: lee la
ultima base guardada y responde los mensajes pendientes. La respuesta puede
tardar hasta la frecuencia configurada en cron-job.org.

La carpeta `data` se conserva mediante la cache de Actions, sin publicar la base
SQLite en el repositorio. La cache no es una copia de seguridad permanente;
GitHub puede eliminarla segun sus politicas de retencion.

En repositorios publicos, los runners estandar de GitHub Actions son gratuitos.
En repositorios privados se consume la cuota mensual incluida en el plan.

## Pruebas

```powershell
python -m unittest discover -s tests -v
```
