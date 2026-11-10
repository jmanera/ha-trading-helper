# ADR 0001: Arquitectura y decisiones de la v1

Fecha: 2026-11-10 · Estado: aceptada

## Contexto

El plan de desarrollo pide un servicio que puntúe pares spot de OKX a 4 h, publique en
Home Assistant y explique cada señal, sin ejecutar órdenes.

## Decisiones

1. **Núcleo puro y vectorizado.** `indicators/` y `strategy/` son funciones sobre DataFrames,
   sin red ni reloj. Las reglas devuelven Series booleanas, de modo que el backtest y las
   señales en vivo evalúan exactamente el mismo código (`score_frame`).
2. **Indicadores propios** (EMA con semilla SMA, RSI/ATR con suavizado de Wilder) en lugar
   de TA-Lib o pandas-ta; se comprueban contra implementaciones de bucle de las fórmulas de
   referencia y con tests de propiedades (rango del RSI, ATR ≥ 0, sin look-ahead).
3. **Solo velas cerradas.** El cliente descarta la vela con `confirm=0`; la entrada del
   backtest se ejecuta en la apertura de la vela siguiente.
4. **Cliente OKX propio** con httpx: limitador de ventana deslizante por endpoint (por debajo
   de los límites publicados), reintentos con espera exponencial ante 429/5xx/`50011`.
5. **Universo** filtrado por `instCategory = 1` (cripto): OKX lista acciones tokenizadas en
   spot y no deben entrar.
6. **Validación honesta.** Walk-forward sobre un único parámetro libre (umbral de entrada);
   un ENTRAR solo es alerta si la validación fuera de muestra cumple n ≥ 30, esperanza > 0 y
   factor de beneficio ≥ 1,1.
7. **MQTT Discovery** con `default_entity_id` (HA ≥ 2025.10), conexión persistente y
   *last will* `offline` para que las entidades pasen a no disponibles si el servicio cae.
   Las entidades de pares que salen del universo se eliminan con configs vacías retenidas.
8. **App de HA sobre Debian** (`python:3.12-slim-bookworm`), no Alpine: pyarrow no publica
   ruedas musl. Solo amd64 y aarch64. Las credenciales MQTT se piden al Supervisor
   (`/services/mqtt`), sin bashio.
9. **Empaquetado local.** El repositorio es privado, así que la instalación es como app local
   (`make app-bundle` → `/addons`). Si se hace público, la CI puede publicar la imagen en
   GHCR y `config.yaml` apuntar a ella con `image:`.

## Consecuencias

- La imagen pesa ~475 MB por pandas/pyarrow; aceptable para un servicio que corre cada 4 h.
- La validación semanal descarga ~2 años de velas para 20 pares la primera vez (unos minutos)
  y después es incremental.
