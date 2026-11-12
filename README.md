# HA Trading Helper

Asesor de señales cripto **explicables** para Home Assistant. Al cerrar cada vela (4 h por
defecto) puntúa los pares spot más líquidos de OKX con indicadores técnicos clásicos, publica el
resultado en Home Assistant por MQTT Discovery y explica punto a punto en qué se basa cada
señal.

> **Por defecto solo genera alertas y no necesita claves de OKX.** Opcionalmente puede comprar
> y vender desde los avisos de Telegram, siempre con tu confirmación (ver
> [Operar desde Telegram](#operar-desde-telegram)).
> No es asesoramiento financiero: las criptomonedas pueden perder gran parte de su valor y
> los resultados pasados no garantizan los futuros.

> **Estado (noviembre de 2026): las señales son informativas.** Validada sin sesgo de
> supervivencia, la estrategia pierde fuera de muestra (−0,15R por operación), y ninguna de las
> 26 variantes probadas, en 4 h ni en diario, muestra una ventaja demostrable
> ([docs/validation.md](docs/validation.md), [docs/research/trials.md](docs/research/trials.md)).
> La app sirve para ver el estado del mercado y por qué puntúa cada par, no para decidir
> compras. No se recomienda operar con dinero real a partir de sus señales.

```text
SOL-USDC · ENTRAR · puntuación 85/100 · vela 2026-11-10 16:00 UTC
Motivos:
  +25  Tendencia: alcista, cierre 188,4 > EMA50 181,2 > EMA200 160,7
  +20  MACD: histograma pasa de -0,31 a 0,12
  +15  RSI(14): sube de 38 a 47 tras zona de retroceso
  + 0  Ruptura: cierre no supera el máximo de 20 velas (192,0)
  +10  Volumen: 1,8x la media de 20 velas (≥ 1,5x)
  +15  Fuerza relativa: +9,4 % vs BTC en 30 velas
Mercado: BTC sobre su EMA200 (filtro OK)
Niveles: entrada 188,4 · stop 179,9 (2,0×ATR) · objetivos 201,1 / 213,9
Validación: Fuera de muestra: 34 % aciertos, esperanza +0,04R tras costes (n=355)
```

## Cómo funciona

1. **Universo**: pares de OKX en la moneda de cotización elegida (USDC por defecto; también
   USDT o EUR) con volumen 24 h ≥ 5 M USD y spread ≤ 0,2 %, sin stablecoins ni acciones
   tokenizadas (o una lista fija de pares). BTC en esa moneda es la referencia de mercado.
2. **Datos**: velas de los endpoints públicos de OKX v5 (4 h por defecto; configurable entre
   1 h y 1 día, siempre alineadas a UTC), solo velas cerradas (`confirm=1`), caché incremental
   en Parquet.
3. **Puntuación 0-100** sumando evidencias (pesos configurables):

   | Evidencia | Condición | Puntos |
   |---|---|---:|
   | Tendencia | Cierre > EMA 50 > EMA 200 | 25 |
   | Momentum | Histograma MACD cruza a positivo (últimas 3 velas) | 20 |
   | RSI | RSI(14) sale hacia arriba de la zona 30-45 | 15 |
   | Ruptura | Cierre supera el máximo de las 20 velas anteriores | 15 |
   | Volumen | Volumen ≥ 1,5 × media de 20 velas | 10 |
   | Fuerza relativa | Rendimiento 30 velas > el de BTC | 15 |

4. **Acción**: `ENTRAR` si puntuación ≥ 70 y BTC está sobre su EMA 200; `SALIR` si marcaste
   una posición y el cierre pierde el trailing stop (2,5 × ATR) o la EMA 50; si no, `ESPERAR`.
5. **Riesgo**: stop a 2 × ATR, objetivos a 1,5R y 3R, tamaño para arriesgar el 1 % del capital
   simulado (máximo 3 posiciones).
6. **Validación**: un backtest walk-forward con las **mismas reglas**, comisiones y
   deslizamiento. Un `ENTRAR` solo cuenta como *alerta* si la estrategia fue rentable fuera de
   muestra (n ≥ 30, esperanza > 0, factor de beneficio ≥ 1,1). Si no, la señal es informativa.
   Con el universo automático, la validación lo reconstruye **vela a vela**: descarga todos
   los pares de la moneda de cotización y en cada vela solo deja entrar a los `max_pairs` con
   más volumen medio de los 30 días anteriores (y al menos `min_volume_24h_usdt` al día). Así
   no se valida solo con las monedas que hoy son líquidas porque subieron (sesgo de
   supervivencia). Los pares que OKX ya retiró no se pueden descargar, así que queda algo de
   sesgo. Con `pairs` fijos se validan esos pares tal cual.

Resultados actuales: [docs/validation.md](docs/validation.md). Decisiones de diseño:
[docs/adr/](docs/adr/).

## Entidades en Home Assistant

Todas bajo el dispositivo **HA Trading Helper**:

| Entidad | Estado | Atributos |
|---|---|---|
| `sensor.ha_trading_helper_mejor_oportunidad` | par mejor situado | acción, puntuación, motivos, explicación, niveles, ranking top 10 |
| `image.ha_trading_helper_mejor_oportunidad_grafico` | gráfico de velas del par mejor situado | |
| `sensor.ha_trading_helper_<par>_puntuacion` | 0-100 | `accion`, `motivos`, `explicacion`, `niveles`, `validacion`, `alerta`, `vigilar` |
| `binary_sensor.ha_trading_helper_<par>_entrada` | on si ENTRAR | |
| `sensor.ha_trading_helper_estado` | ok / degradada / error | última vela, errores, validación |

El panel **Señales cripto** ([dashboards/ha_trading_helper.yaml](dashboards/ha_trading_helper.yaml))
usa solo tarjetas estándar, y hay una automatización de ejemplo que avisa al móvil cuando un
par pasa a ENTRAR o SALIR ([dashboards/automations/](dashboards/automations/)).

**Explicación para no expertos.** Cada señal trae en `explicacion` una lista de ideas
(`i` icono, `t` título, `d` texto) que cuenta en lenguaje llano por qué se decidió así: si el
precio va hacia arriba, si ha roto un máximo, si hay mucho volumen… y siempre termina con lo
que dice la validación. Se genera a partir de las mismas reglas que deciden la señal, sin IA,
así que no puede afirmar nada que los indicadores no vean.

**Gráfico.** En cada vela la app dibuja las últimas 120 velas del par mejor situado con todo lo
que miran las reglas: medias de 50 y 200 velas, máximo de 20 velas, velas con ENTRAR, stop y
objetivos, volumen, RSI y MACD, cada panel con una leyenda corta. Se publica como entidad
`image` y el panel lo muestra junto a la explicación; los avisos de ejemplo lo adjuntan cuando
tratan del par mejor situado.

## Operar desde Telegram

Opcional y desactivado por defecto (`trading_enabled: false`). Con la integración de
Telegram en Home Assistant y las automatizaciones de
[dashboards/automations/ha_trading_helper_telegram.yaml](dashboards/automations/ha_trading_helper_telegram.yaml):

1. El aviso de **ENTRAR** trae un botón **Comprar**: el bot pregunta el importe, lo escribes
   (p. ej. `150`) y pide **Confirmar** antes de enviar una orden a mercado.
2. Tras comprar deja en OKX un **stop-loss** al nivel de la señal (2×ATR), que protege aunque
   Home Assistant esté caído, y registra la posición para que el aviso de **SALIR** funcione.
3. El aviso de **SALIR** trae un botón **Vender** (también con confirmación). `/posiciones`
   lista lo abierto.

Requisitos de OKX: una clave API con permiso **solo de Trade** (nunca *Withdraw*), vinculada a
la IP de tu casa; empieza con claves de **Demo trading** (`okx_demo: true`, dinero ficticio).
Hay un importe máximo por orden (`max_order_amount`). Diseño: [ADR 0002](docs/adr/0002-trading-from-telegram.md).

**Compras bloqueadas sin validación positiva.** Con `require_validation: true` (por defecto)
el bot rechaza cualquier compra mientras el walk-forward no sea positivo, que es la situación
actual; vender una posición abierta sigue funcionando siempre. Se comprueba al pulsar
**Comprar** y otra vez al **Confirmar**. Para probar el flujo en Demo trading puedes poner
`require_validation: false`, que también convierte en alertas los ENTRAR no validados.

## Monitor intradía (segunda app)

[intraday_monitor/](intraday_monitor/) es una segunda app del mismo repositorio. Cada 5 minutos
consulta a OKX los pares más líquidos (por defecto 20 en USDT) y:

- **Avisa** de lo que se sale de lo normal: volumen anómalo, movimientos bruscos en 15 min,
  máximos o mínimos de 24 h y cambios de mercado (BTC cruzando su media de 50 horas). Los
  umbrales son opciones y se pueden ajustar cuando quieras.
- **Mide en papel** dos reglas intradía fijadas antes de empezar, cada una frente a un control
  que entra al azar, con costes realistas. Tras al menos 28 días y 100 operaciones dice si
  alguna tiene ventaja de verdad (B llega antes; A necesitará unas 6 semanas). Reglas y criterio: [docs/research/intraday.md](docs/research/intraday.md).

No opera ni necesita claves. Si se corta, al volver recupera de OKX las velas que faltan y las
procesa en orden. `ha-trading-helper intraday status` resume la medición desde la base de datos
local. Panel y aviso de ejemplo: [dashboards/intraday_monitor.yaml](dashboards/intraday_monitor.yaml)
y [dashboards/automations/intraday_monitor.yaml](dashboards/automations/intraday_monitor.yaml).

## Uso en local

Requisitos: [uv](https://docs.astral.sh/uv/) (instala Python 3.12 por ti) y Docker para el
stack completo.

```bash
uv sync                                   # dependencias
make check                                # lint + tipos + tests + auditoría
uv run ha-trading-helper scan             # ranking con motivos (sin Home Assistant)
uv run ha-trading-helper backtest --pair SOL-USDC --years 2 --trades
uv run ha-trading-helper validate --years 2   # walk-forward -> .data/validation-usdc-4h.json
```

Stack completo (Mosquitto + Home Assistant + la app):

```bash
cp .env.example .env
make up                                   # docker compose up --build -d
# http://localhost:8123 -> onboarding -> añadir integración MQTT con broker "mosquitto"
```

Toda opción se puede fijar con variables `HTH_<NOMBRE>` (ver `.env.example`) o, dentro de
Home Assistant, desde la pestaña de configuración de la app.

## Instalación en Home Assistant (app local)

```bash
make app-bundle          # crea dist/ha_trading_helper/
```

1. Copia `dist/ha_trading_helper/` a la carpeta de apps locales de Home Assistant: `/addons/` (Samba) o `/local_apps/` por SSH en versiones recientes.
2. *Ajustes → Apps → Tienda de apps → ⋮ → Buscar actualizaciones*; aparece en *Apps locales*.
3. Instala, revisa las opciones y arranca. Necesita la app **Mosquitto broker** y la
   integración MQTT; las credenciales se obtienen solas del Supervisor.
4. Copia `dashboards/ha_trading_helper.yaml` como panel nuevo (modo YAML o pegándolo en el
   editor en bruto) y adapta `notify.notify` en la automatización de ejemplo.

Al arrancar hace un ciclo, lo repite tras cada cierre de vela (con 4 h: 00, 04, 08, 12, 16
y 20 UTC + 1 min) y recalcula la validación cada domingo (o al arrancar si falta o tiene más de
7 días; la primera descarga de 2 años de histórico tarda unos minutos).

## Desarrollo

```text
src/ha_trading_helper/
  okx/          cliente async (httpx), modelos, limitador de ritmo
  data/         caché Parquet, filtro de universo
  indicators/   EMA, RSI, MACD, ATR, Donchian, volumen relativo (funciones puras)
  strategy/     reglas, puntuación, riesgo, explicación, señal
  backtest/     motor, métricas, walk-forward
  publish/      payloads MQTT Discovery, publicador con last-will
  service.py    ciclo de análisis      scheduler.py  modo servicio      cli.py
trading_helper/  app de Home Assistant (config.yaml, Dockerfile, traducciones)
```

`indicators/` y `strategy/` no tocan red ni reloj y tienen cobertura del 100 %. La CI
(GitHub Actions) ejecuta lint, mypy estricto, tests (incluida integración con Mosquitto real),
auditoría de dependencias, construye la imagen para amd64 y arm64, y cada lunes repite la
validación con datos reales de OKX.
