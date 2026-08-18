# Avisador de precios KRK - BIO de solo ida

Consulta Google Flights para todas las salidas entre 30 y 60 dias desde la fecha
de cada ejecucion, guarda el historico en SQLite y avisa por Telegram cuando:

- empieza el seguimiento;
- aparece un nuevo minimo historico;
- el minimo sube al menos el porcentaje configurado;
- el precio sube durante varias consultas consecutivas.

El proyecto usa `flights`, una libreria no oficial que consulta la API interna de
Google Flights. No necesita una API de pago, pero puede dejar de funcionar si
Google cambia ese servicio.

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

## Ejecucion gratuita en GitHub Actions

1. Crea un repositorio en GitHub y sube estos archivos.
2. Ve a **Settings > Secrets and variables > Actions**.
3. Crea los secretos `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.
4. Ajusta la ventana de dias en `.github/workflows/check-prices.yml` si lo necesitas.
5. Abre **Actions > Comprobar precios de vuelos > Run workflow** para probarlo.

El workflow se ejecuta cada seis horas. La carpeta `data` se conserva mediante la
cache de Actions, sin publicar la base SQLite en el repositorio. La cache no es
una copia de seguridad permanente; para un proyecto personal es suficiente, pero
GitHub puede eliminarla segun sus politicas de retencion.

En repositorios publicos, los runners estandar de GitHub Actions son gratuitos.
En repositorios privados se consume la cuota mensual incluida en el plan.

## Pruebas

```powershell
python -m unittest discover -s tests -v
```
