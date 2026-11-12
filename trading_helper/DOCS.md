# HA Trading Helper

Señales cripto explicables de OKX (spot, velas de 4 h por defecto; de 1 h a 1 día) publicadas como entidades de Home
Assistant. **Solo alertas: no ejecuta órdenes ni necesita claves de OKX.** No es
asesoramiento financiero.

## Requisitos

- App **Mosquitto broker** e integración **MQTT** configuradas. La app obtiene las
  credenciales del Supervisor; solo rellena las opciones `mqtt_*` para otro broker.

## Qué hace

- Al arrancar y tras cada cierre de vela (con 4 h: 00, 04, 08, 12, 16, 20 UTC + 1 min) analiza
  los pares más líquidos y publica:
  - `sensor.ha_trading_helper_mejor_oportunidad`
  - `sensor.ha_trading_helper_<par>_puntuacion` (motivos en los atributos)
  - `binary_sensor.ha_trading_helper_<par>_entrada`
  - `sensor.ha_trading_helper_estado`
- Cada domingo recalcula la validación walk-forward con 2 años de histórico. Mientras la
  estrategia no sea rentable fuera de muestra tras costes, los ENTRAR son informativos
  (`alerta: false`).
- Hoy la validación **no es positiva**: la estrategia pierde fuera de muestra, así que las
  señales son solo informativas. No se recomienda operar con dinero real a partir de ellas.
- Con el universo automático, la validación usa el universo que habría habido en cada fecha
  (los pares más líquidos de entonces, no los de hoy). Para eso descarga el histórico de todos
  los pares de la moneda de cotización: la primera vez tarda unos 30 minutos y ocupa unas
  decenas de MB en `/data`; después solo descarga las velas nuevas.

## Opciones

| Opción | Por defecto | Descripción |
|---|---|---|
| `quote_currency` | USDC | Moneda de cotización: USDC, USDT o EUR. BTC en esa moneda es la referencia de mercado |
| `bar` | 4H | Temporalidad de vela: 1H, 2H, 4H, 6H, 12H o 1D (velas alineadas a UTC). Los indicadores cuentan velas, así que cambia el horizonte de la estrategia |
| `pairs` | `[]` | Lista fija de pares; vacío = universo automático |
| `max_pairs` | 20 | Tamaño del universo automático |
| `min_volume_24h_usdt` | 5 000 000 | Filtro de liquidez, en USD equivalentes. En USDC y EUR hay menos pares líquidos que en USDT: baja este valor si salen pocos |
| `enter_threshold` | 70 | Puntuación mínima para ENTRAR |
| `wait_threshold` | 40 | Por encima, una moneda en ESPERAR se marca para vigilar |
| `require_validation` | true | ENTRAR solo es alerta, y Telegram solo compra, si la estrategia está validada |
| `capital` | 10 000 | Capital simulado para el tamaño sugerido |
| `risk_per_trade_pct` | 1 | Riesgo por operación |
| `max_positions` | 3 | Posiciones simultáneas |
| `fee_pct` / `slippage_pct` | 0,10 / 0,05 | Costes por lado (verifica tu nivel de comisiones en OKX) |
| `positions` | `[]` | Posiciones abiertas fuera de Telegram, para avisar de SALIR |
| `trading_enabled` | false | Permite comprar y vender desde Telegram, siempre con confirmación |
| `okx_demo` | true | Opera en Demo trading de OKX (dinero ficticio). Necesita claves creadas en modo demo |
| `okx_api_key` / `okx_api_secret` / `okx_passphrase` | | Clave API de OKX con permiso solo de Trade |
| `max_order_amount` | 500 | Importe máximo por compra, en la moneda de cotización |
| `stop_loss_on_exchange` | true | Deja en OKX un stop-loss al nivel de la señal tras cada compra |

Ejemplo de posición:

```yaml
positions:
  - pair: SOL-USDC
    entry_price: 180.5
    entry_time: "2026-11-01T12:00:00Z"
```

## Operar desde Telegram

1. Crea en OKX una clave API con permiso **solo de Trade** (nunca *Withdraw*) y vincúlala a
   la IP pública de tu casa. Para probar, créala en **Demo trading** y deja `okx_demo: true`.
2. Rellena las tres opciones `okx_*`, activa `trading_enabled` y reinicia la app.
3. Copia las automatizaciones de `dashboards/automations/ha_trading_helper_telegram.yaml` en
   Home Assistant (sustituyen al aviso sin botones).

Al pulsar **Comprar**, el bot pide el importe; tras **Confirmar** compra a mercado, pone el
stop en OKX y registra la posición. **Vender** cancela ese stop y vende la cantidad comprada.
Las peticiones caducan a los 10 minutos. `/posiciones` lista las abiertas.

Mientras la validación no sea positiva (hoy no lo es), el bot **no compra**: responde que las
señales son informativas. Vender sigue funcionando. Con `require_validation: false` se
desactiva este bloqueo; úsalo solo con Demo trading.

## Datos

En `/data` de la app: caché de velas (Parquet), `validation-<moneda>-<vela>.json` (último walk-forward, uno por moneda y temporalidad) y
`signals.jsonl` (cada cambio de acción, para comparar en vivo contra el backtest) y
`positions.json` (posiciones abiertas desde Telegram).
