# Registro de pruebas

Cada variante que se evalúa queda aquí **antes** de ver su resultado. Cuantas más variantes se
prueban, más probable es que alguna salga bien por azar (Harvey, Liu y Zhu, 2016; Bailey y
López de Prado, 2014), así que la ganadora debe juzgarse sabiendo cuántas hubo.

## Periodo reservado

Del **2026-05-12 al 2026-11-12** (últimos 6 meses): no se usa para elegir nada. Solo se evalúa,
una vez, la candidata final.

## Pruebas anteriores (velas de 4 h, 2 años)

| # | Fecha | Variante | Universo | Resultado fuera de muestra |
|---:|---|---|---|---|
| 1-5 | 2026-11-12 | Entradas: base, tendencia obligatoria, estado + disparo, régimen con pendiente, combinadas | hoy | Ninguna mejora; −0,03R a +0,02R |
| 6-12 | 2026-11-12 | Salidas: actual, sin EMA 50, trailing 3,5, sin tope (E3), E3 + breakeven, sin parcial | hoy | E3 +0,80R (inflado por supervivencia) |
| 13-17 | 2026-11-12 | E0 y E3 con trailing 2,5 / 3,5 / 4 / 5 | histórico | E0 −0,15R; E3 de +0,03R a +0,29R |

Sobre los datos del periodo reservado ya se han hecho las pruebas 1-17 (usaban los 2 últimos
años completos); el periodo reservado vale solo para lo que se pruebe desde aquí.

## Momentum diario en cartera (velas de 1 día, 5 años)

Universo histórico: en cada día, los 20 pares `-USDT` con más volumen medio de 30 días (mínimo
5 M USDT/día). Revisión semanal, ejecución al cierre del día siguiente, costes 0,15 % por lado
sobre la rotación. Sin apalancamiento (exposición total ≤ 100 %).

| # | Señal | Ventana | Tamaño |
|---:|---|---:|---|
| 18 | Serie temporal: todo par del universo con rendimiento > 0 en la ventana; BTC también > 0 | 28 d | igual (1/20 cada uno) |
| 19 | igual | 28 d | por volatilidad (objetivo 40 % anual) |
| 20 | igual | 56 d | igual |
| 21 | igual | 56 d | por volatilidad |
| 22 | Transversal: los 5 de más rendimiento en la ventana, si > 0; BTC también > 0 | 28 d | igual (1/5) |
| 23 | igual | 28 d | por volatilidad |
| 24 | igual | 56 d | igual |
| 25 | igual | 56 d | por volatilidad |
| 26 | Solo BTC: comprado si su rendimiento en la ventana > 0 (misma regla que 18-25, sin altcoins) | 56 d | por volatilidad |

Referencias: comprar y mantener BTC; cartera equiponderada del universo histórico, revisada
cada semana.

La prueba 26 se añadió tras ver 18-25, para saber si la selección de altcoins aporta algo
frente a aplicar el mismo filtro y tamaño solo a BTC.

## Resultados 18-26 (desarrollo: 2021-06-01 a 2026-05-11)

158 pares estuvieron alguna vez en el universo. Sharpe anualizado con tipo de interés 0; su
error estándar en 4,9 años es de ±0,5 aproximadamente.

| # | CAGR | Volatilidad | Sharpe | Máx. caída |
|---|---:|---:|---:|---:|
| Ref. BTC comprar y mantener | +17,0 % | 54 % | 0,56 | −77 % |
| Ref. equiponderada del universo | −35,9 % | 79 % | −0,16 | −94 % |
| 18 TS 28 d igual | +7,3 % | 42 % | 0,38 | −53 % |
| 19 TS 28 d vol | +7,8 % | 17 % | 0,52 | −25 % |
| 20 TS 56 d igual | +10,4 % | 44 % | 0,45 | −53 % |
| 21 TS 56 d vol | +9,3 % | 17 % | 0,60 | −25 % |
| 22 XS 28 d igual | +6,8 % | 72 % | 0,45 | −78 % |
| 23 XS 28 d vol | +6,7 % | 20 % | 0,42 | −35 % |
| 24 XS 56 d igual | +8,5 % | 73 % | 0,47 | −72 % |
| 25 XS 56 d vol | +12,0 % | 22 % | 0,63 | −28 % |
| 26 Solo BTC 56 d vol | +20,9 % | 30 % | 0,79 | −49 % |

- Comprar altcoins y mantenerlas destruyó valor (−36 % anual). El momentum lo evita en buena
  parte, pero no supera a BTC.
- El tamaño por volatilidad reduce la caída máxima a la mitad o menos en todas las variantes,
  con un Sharpe igual o mejor (coherente con Moreira y Muir, 2017).
- Seleccionar altcoins resta frente a aplicar la misma regla solo a BTC: −9,7 % anual (25 − 26,
  t = −1,0) y −12,9 % (21 − 26, t = −1,6). No es significativo, pero la dirección es la misma
  en todas.
- Ninguna variante tiene un Sharpe distinguible de cero ni de BTC con 5 años de datos.
- Los pares retirados no están en los datos: las altcoins que se hundieron hasta desaparecer
  faltan, así que la realidad de 18-25 y de la equiponderada sería algo peor.


## Evaluación única de la prueba 26 en el periodo reservado (2026-05-12 a 2026-11-11)

| | Rentabilidad | Volatilidad | Sharpe | Máx. caída |
|---|---:|---:|---:|---:|
| 26 Solo BTC 56 d vol | −12,5 % | 28 % | −0,81 | −22,8 % |
| BTC comprar y mantener | −9,4 % | 38 % | −0,33 | −27,7 % |

- La prueba 26 **no se confirma**: pierde más que mantener BTC, aunque con menos volatilidad y
  una caída máxima algo menor.
- El resultado cae dentro de lo habitual en desarrollo para ventanas de 184 días (percentil 10:
  −22 %, mediana +10 %, peor −33 %), así que tampoco la descarta: 6 meses no bastan para
  confirmar ni refutar.
- Perdió por entradas y salidas en un mercado lateral: fuera en junio y julio, entra y sale en
  agosto, dentro en octubre y fuera en noviembre.
- El periodo reservado queda **gastado**: no se puede usar para evaluar otras variantes.
