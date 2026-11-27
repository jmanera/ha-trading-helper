# Análisis fundamental

Panel en la barra lateral de Home Assistant con todos los pares **cripto/USDC** de OKX:
cuánto ha cambiado el precio y cuánto se ha negociado en las **últimas 4 horas**. Cada par
enlaza a un **análisis fundamental** de la moneda. **Solo lee** datos públicos: no necesita tu
cuenta de OKX y no opera. No es asesoramiento financiero.

## Requisitos

Funciona sin configurar nada, pero con dos claves gratuitas los análisis son rápidos y
completos:

1. **Clave Demo de CoinGecko** (`coingecko_api_key`). Regístrate en
   [coingecko.com/es/api/pricing](https://www.coingecko.com/es/api/pricing) con el plan
   *Demo* (gratis, 30 consultas por minuto). Sin clave, CoinGecko admite unas pocas consultas
   por minuto y cada análisis tarda o falla.
2. **Token de GitHub** (`github_token`). En *GitHub → Settings → Developer settings →
   Personal access tokens* crea uno **sin ningún permiso**. Sin token, GitHub admite 60
   consultas por hora para toda tu IP y la puntuación de desarrollo puede quedar sin datos.

## Pantallas

- **Pares USDC**: precio, cambio de 4 h (las últimas 48 velas de 5 minutos) y de 24 h, y
  volumen de 4 h y de 24 h, con buscador. Ordenada por defecto por el cambio de 4 h; también
  por cambio de 24 h, volúmenes o puntuación fundamental. Sin monedas estables ni acciones
  tokenizadas. Las monedas ya analizadas muestran su puntuación y su perspectiva.
- **Analizar todas**: analiza en segundo plano, una tras otra y de la más negociada a la
  menos, las monedas sin un análisis reciente. Muestra el progreso, se puede detener y sigue
  aunque cierres la página.
- **Ficha de cada moneda**: puntuación fundamental (0-100), perspectiva (alcista, neutral o
  bajista), riesgo de dilución y riesgo de liquidez, las cuatro puntuaciones parciales y el
  detalle de cada métrica con su valor, sus puntos y por qué importa. Debajo, la descripción
  del proyecto y el estado de cada fuente.

## Fuentes

| Fuente | Qué aporta | Clave |
|---|---|---|
| OKX | Pares, velas de 5 min, libro de órdenes (profundidad y diferencial) | no |
| CoinGecko | Capitalización, FDV, suministro, puesto, seguidores, máximo histórico, serie de 1 año | Demo, opcional |
| DefiLlama | TVL de protocolos y cadenas, comisiones e ingresos de 30 días | no |
| GitHub | Repositorios activos, último cambio, estrellas | token, opcional |

Messari y Token Terminal tienen más datos (desbloqueos, ingresos detallados), pero su API es
de pago. CoinGecko dejó de dar datos de desarrollo en el plan gratuito el 28/08/2026; por eso
se leen de GitHub.

## Cómo puntúa

Cada métrica da de 0 a 100 puntos en una rampa recta entre dos umbrales. Cada apartado es la
media ponderada de sus métricas con datos.

- **Tokenomics**: suministro en circulación, emisión del último año (calculada con la
  capitalización entre el precio de cada día), tope de suministro y FDV / capitalización.
- **Adopción**: puesto por capitalización, seguidores en CoinGecko, TVL y su cambio en 30
  días, antigüedad.
- **Finanzas**: comisiones e ingresos anualizados frente a la capitalización, comisiones
  frente al mes anterior, volumen / capitalización, distancia al máximo histórico y tamaño.
- **Desarrollo**: repositorios con cambios en 30 días, días desde el último cambio y estrellas.
- **Riesgo de dilución**: suministro por emitir, emisión anual y FDV / capitalización.
- **Riesgo de liquidez**: profundidad del libro de OKX a ±2 %, diferencial, volumen global y
  rotación. Bajo por debajo de 35, alto desde 65.

La **puntuación global** es la media de los cuatro apartados, menos 10 puntos por cada riesgo
alto. La **perspectiva** es *alcista* con 65 o más, ningún riesgo alto y sin caída de uso de
más del 20 % en 30 días; *bajista* por debajo de 40 o con los dos riesgos altos; *neutral* en
el resto y cuando hay datos de menos del 40 % de las métricas.

Si una fuente responde pero no tiene nada (una moneda sin comisiones en DefiLlama o sin código
público), la métrica cuenta como 0. Si una fuente falla, queda «sin datos» y no cuenta.

Los umbrales son criterios razonables, **no ajustados a precios pasados**: la puntuación sirve
para comparar proyectos, no predice la rentabilidad.

## Límites de las claves al analizar todas

Cada moneda nueva gasta **2 consultas de CoinGecko** (ficha y serie de un año) y 1 de GitHub
por proyecto (guardada 24 h). DefiLlama y OKX no tienen claves ni cuotas que importen aquí.

| Clave | Límite | Una pasada completa (unos 270 pares) |
|---|---|---|
| CoinGecko Demo | 30 consultas/min y **10.000 al mes** | unas 540 consultas, ~22 min |
| Sin clave de CoinGecko | unas 5/min y variable | horas, con muchas fallidas |
| GitHub con token | 5.000/hora | sin problema |
| GitHub sin token | 60/hora por IP | la mayoría sin desarrollo |

La cuota mensual de CoinGecko es la que manda: caben unas **18 pasadas completas al mes**,
así que conviene no lanzar más de una cada dos días. La pasada se salta las monedas con un
análisis de menos de `cache_hours` (12 h): repetirla enseguida no gasta nada. El panel muestra
cuántas consultas lleva el mes (contadas por la app, reintentos incluidos) y avisa si la pasada
superaría las 10.000. Al llegar al límite, CoinGecko responde con error hasta el mes siguiente
y las monedas quedan «sin datos» en sus métricas de mercado.

## Opciones

- `refresh_minutes`: cada cuánto se lee la lista de 4 h (5 por defecto).
- `cache_hours`: cuánto se reutiliza un análisis (12 h). En cada ficha puedes volver a leer
  las fuentes.
- `coin_ids`: si un símbolo apunta a otra moneda con el mismo símbolo. Se elige la de mayor
  capitalización entre las 1.250 primeras de CoinGecko; para cambiarla, añade
  `SIMBOLO=id-de-coingecko` (el id es la última parte de la dirección de la moneda en
  CoinGecko).

## Límites

- Sin datos de desbloqueos programados ni de concentración de tenedores: no hay API gratuita.
- Monedas fuera de las 1.250 mayores de CoinGecko: solo liquidez, salvo que añadas su id en
  `coin_ids`.
- La edad se toma de la fecha de génesis de CoinGecko; si no la tiene, de la primera vez que
  OKX listó la moneda (marcada con ≥).
