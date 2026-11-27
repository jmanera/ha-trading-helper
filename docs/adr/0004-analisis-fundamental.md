# ADR 0004: Análisis fundamental de los pares USDC

Fecha: 2026-11-27 · Estado: propuesta

## Contexto

Queremos un panel con todos los pares cripto/USDC de OKX, su cambio y volumen en 4 h, y un
enlace por par a un análisis fundamental: puntuación global (0-100), tokenomics, adopción,
finanzas, desarrollo, riesgo de dilución, riesgo de liquidez y perspectiva.

Probamos las APIs el 27/11/2026:

- **CoinGecko** (Demo, gratis): mercado y suministro; la serie diaria solo llega a 365 días.
  Desde el 28/08/2026 ya no devuelve `developer_data` ni `community_data` en el plan Demo.
- **DefiLlama** (gratis, sin clave): TVL, comisiones e ingresos. Los protocolos con versiones
  solo llevan el id de CoinGecko en la versión o en el padre; un protocolo desconocido es un
  400. Algunos puentes llevan el id de CoinGecko de su cadena (el de Solana, `solana`).
- **GitHub**: 60 consultas por hora sin token.
- **Messari**: el plan gratuito de la API está retirado o recortado; Token Terminal y
  Tokenomist (desbloqueos) son de pago.

## Decisiones

1. **Cuarta app, `crypto_fundamentals`**, con el código en `src/ha_trading_helper/fundamentals/`.
   Solo datos públicos: sin claves de OKX, sin MQTT y sin base de datos externa.
2. **Fuentes gratuitas**: OKX, CoinGecko, DefiLlama y GitHub. Las claves de CoinGecko y
   GitHub son opcionales y recomendadas.
3. **4 h = 48 velas de 5 min** por par, pedidas en paralelo bajo el límite de OKX (unos 20 s
   para unos 270 pares), cada 5 min. Se excluyen las monedas estables y las acciones
   tokenizadas (`instCategory` ≠ 1).
4. **Análisis bajo demanda** al abrir la ficha, con caché SQLite de 12 h en `/data`. Se guardan
   los hechos, no las puntuaciones: un cambio en el cálculo se ve al momento.
5. **Puntuación explicable** (`scoring.py`, funciones puras): rampas lineales entre umbrales,
   media ponderada de las métricas con datos, −10 por riesgo alto. Los umbrales son criterio,
   no ajuste: no hay validación con precios, igual que la puntuación no es una señal.
6. **Ausencia frente a fallo**: si la fuente responde y no tiene nada, la métrica vale 0 (un
   memecoin sin comisiones ni código no puede puntuar alto por falta de datos); si la fuente
   falla, la métrica no cuenta. Con menos del 40 % de métricas, perspectiva neutral.
7. **Emisión anual = capitalización / precio** de la serie de CoinGecko: la historia del
   suministro es de pago y así sale de la serie gratuita.
8. **Símbolo → moneda**: la de mayor capitalización con ese símbolo entre las 1.250 primeras;
   la opción `coin_ids` corrige los casos ambiguos. Para DefiLlama, primero la cadena de la que
   la moneda es el token y después el protocolo.

## Consecuencias

- Sin claves, los análisis pueden tardar más de un minuto o quedar incompletos (el límite
  público de CoinGecko varía con la carga).
- Faltan desbloqueos programados y concentración de tenedores; el riesgo de dilución se apoya
  en el suministro por emitir y en la emisión real del último año.
