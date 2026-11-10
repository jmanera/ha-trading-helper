# HA Trading Helper

Asesor de señales cripto **explicables** para Home Assistant. Al cerrar cada vela de 4 h
puntúa los pares spot más líquidos de OKX con indicadores técnicos clásicos, publica el
resultado en Home Assistant por MQTT Discovery y explica punto a punto en qué se basa cada
señal.

> **Solo genera alertas. Nunca ejecuta órdenes ni necesita claves de OKX.**
> No es asesoramiento financiero: las criptomonedas pueden perder gran parte de su valor y
> los resultados pasados no garantizan los futuros.

```text
SOL-USDT · ENTRAR · puntuación 85/100 · vela 2026-11-10 16:00 UTC
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

1. **Universo**: pares `-USDT` de OKX con volumen 24 h ≥ 5 M USDT y spread ≤ 0,2 %, sin
   stablecoins ni acciones tokenizadas (o una lista fija de pares).
2. **Datos**: velas de 4 h de los endpoints públicos de OKX v5, solo velas cerradas
   (`confirm=1`), caché incremental en Parquet.
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

Resultados actuales: [docs/validation.md](docs/validation.md). Decisiones de diseño:
[docs/adr/](docs/adr/).

## Entidades en Home Assistant

Todas bajo el dispositivo **HA Trading Helper**:

| Entidad | Estado | Atributos |
|---|---|---|
| `sensor.ha_trading_helper_mejor_oportunidad` | par mejor situado | acción, puntuación, motivos, niveles, ranking top 10 |
| `sensor.ha_trading_helper_<par>_puntuacion` | 0-100 | `accion`, `motivos`, `niveles`, `validacion`, `alerta`, `vigilar` |
| `binary_sensor.ha_trading_helper_<par>_entrada` | on si ENTRAR | |
| `sensor.ha_trading_helper_estado` | ok / degradada / error | última vela, errores, validación |

El panel **Cripto 4H** ([dashboards/ha_trading_helper.yaml](dashboards/ha_trading_helper.yaml))
usa solo tarjetas estándar, y hay una automatización de ejemplo que avisa al móvil cuando un
par pasa a ENTRAR o SALIR ([dashboards/automations/](dashboards/automations/)).

## Uso en local

Requisitos: [uv](https://docs.astral.sh/uv/) (instala Python 3.12 por ti) y Docker para el
stack completo.

```bash
uv sync                                   # dependencias
make check                                # lint + tipos + tests + auditoría
uv run ha-trading-helper scan             # ranking con motivos (sin Home Assistant)
uv run ha-trading-helper backtest --pair SOL-USDT --years 2 --trades
uv run ha-trading-helper validate --years 2   # walk-forward -> .data/validation.json
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

1. Copia `dist/ha_trading_helper/` a la carpeta `/addons/` de Home Assistant (Samba o SSH).
2. *Ajustes → Apps → Tienda de apps → ⋮ → Buscar actualizaciones*; aparece en *Apps locales*.
3. Instala, revisa las opciones y arranca. Necesita la app **Mosquitto broker** y la
   integración MQTT; las credenciales se obtienen solas del Supervisor.
4. Copia `dashboards/ha_trading_helper.yaml` como panel nuevo (modo YAML o pegándolo en el
   editor en bruto) y adapta `notify.notify` en la automatización de ejemplo.

Al arrancar hace un ciclo, lo repite tras cada cierre de vela de 4 h (00, 04, 08, 12, 16 y
20 UTC + 1 min) y recalcula la validación cada domingo (o al arrancar si falta o tiene más de
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
