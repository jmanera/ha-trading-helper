# Monitor intradía

Vigila los pares más líquidos de OKX en velas de 5 y 15 minutos, avisa cuando pasa algo
fuera de lo normal y, a la vez, **mide en papel** dos reglas intradía fijas frente a un
control al azar. **No compra ni vende nada** y no necesita claves de OKX. No es
asesoramiento financiero.

## Requisitos

- App **Mosquitto broker** e integración **MQTT**. Las credenciales se toman del Supervisor.

## Alertas (ajustables cuando quieras)

Cada 5 minutos, al cerrar la vela, la app puede avisar de:

- **Volumen**: una vela de 5 min con `volume_spike` veces el volumen normal.
- **Movimiento**: una vela de 15 min cuyo cuerpo supera `move_atr` veces su rango habitual.
- **Máximo / mínimo de 24 h**: una vela de 5 min que cierra fuera del rango del último día.
- **Cambio de mercado**: BTC cruza su media de 50 horas.

Como mucho una alerta del mismo tipo y par cada `cooldown_minutes`. Cambiar estas opciones
no afecta a la medición.

## Medición (no se toca durante la prueba)

Las reglas A (ruptura con impulso, 15 min) y B (rebote tras caída, 5 min) están fijadas en el
código desde el primer día; cada una tiene un **control al azar** que entra a la vez en un par
elegido al azar con las mismas salidas. Las operaciones se simulan con costes realistas
(0,10 % de comisión y 0,05 % de deslizamiento por lado). Detalle y criterio:
`docs/research/intraday.md` del repositorio.

Una regla **cumple** si, tras al menos 28 días y 100 operaciones, gana de media tras costes y
supera a su control con un 95 % de confianza. Hasta entonces verás «Midiendo».

No tienes que hacer nada: solo dejar la app en marcha. Si Home Assistant se reinicia o se
corta la conexión, al volver la app descarga las velas que faltan, las procesa en orden y
apunta el corte en `sensor.intraday_monitor_estado`.

## Entidades

- `event.intraday_monitor_alerta`: un evento por alerta (tipo, par, texto, precio).
- `sensor.intraday_monitor_alertas_24h`: número de alertas del último día y las últimas 10.
- `sensor.intraday_monitor_regla_a`, `..._regla_a_azar`, `..._regla_b`, `..._regla_b_azar`:
  operaciones cerradas; en los atributos, aciertos, media tras costes, intervalo, veredicto y
  las últimas operaciones.
- `sensor.intraday_monitor_estado`: salud, pares vigilados, versión de las reglas y cortes.

Panel y avisos de ejemplo: `dashboards/intraday_monitor.yaml` y
`dashboards/automations/intraday_monitor.yaml` en el repositorio.

## Datos

En `/data`: `intraday.db` (SQLite) con las operaciones en papel, el progreso de cada serie de
velas, las alertas y los cortes.
