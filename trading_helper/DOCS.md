# HA Trading Helper

Señales cripto explicables de OKX (spot, velas de 4 h) publicadas como entidades de Home
Assistant. **Solo alertas: no ejecuta órdenes ni necesita claves de OKX.** No es
asesoramiento financiero.

## Requisitos

- App **Mosquitto broker** e integración **MQTT** configuradas. La app obtiene las
  credenciales del Supervisor; solo rellena las opciones `mqtt_*` para otro broker.

## Qué hace

- Al arrancar y tras cada cierre de vela de 4 h (00, 04, 08, 12, 16, 20 UTC + 1 min) analiza
  los pares más líquidos y publica:
  - `sensor.ha_trading_helper_mejor_oportunidad`
  - `sensor.ha_trading_helper_<par>_puntuacion` (motivos en los atributos)
  - `binary_sensor.ha_trading_helper_<par>_entrada`
  - `sensor.ha_trading_helper_estado`
- Cada domingo recalcula la validación walk-forward con 2 años de histórico. Mientras la
  estrategia no sea rentable fuera de muestra tras costes, los ENTRAR son informativos
  (`alerta: false`).

## Opciones

| Opción | Por defecto | Descripción |
|---|---|---|
| `pairs` | `[]` | Lista fija de pares; vacío = universo automático |
| `max_pairs` | 20 | Tamaño del universo automático |
| `min_volume_24h_usdt` | 5 000 000 | Filtro de liquidez |
| `enter_threshold` | 70 | Puntuación mínima para ENTRAR |
| `wait_threshold` | 40 | Por encima, una moneda en ESPERAR se marca para vigilar |
| `require_validation` | true | ENTRAR solo es alerta si la estrategia está validada |
| `capital` | 10 000 | Capital simulado para el tamaño sugerido |
| `risk_per_trade_pct` | 1 | Riesgo por operación |
| `max_positions` | 3 | Posiciones simultáneas |
| `fee_pct` / `slippage_pct` | 0,10 / 0,05 | Costes por lado (verifica tu nivel de comisiones en OKX) |
| `positions` | `[]` | Posiciones abiertas para avisar de SALIR |

Ejemplo de posición:

```yaml
positions:
  - pair: SOL-USDT
    entry_price: 180.5
    entry_time: "2026-11-01T12:00:00Z"
```

## Datos

En `/data` de la app: caché de velas (Parquet), `validation.json` (último walk-forward) y
`signals.jsonl` (cada cambio de acción, para comparar en vivo contra el backtest).
