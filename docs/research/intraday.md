# Medición intradía: reglas fijadas antes de empezar

Reglas **v1 · 2026-11-12** (`src/ha_trading_helper/intraday/rules.py`). Se fijan antes de ver
ningún resultado y **no se cambian durante la medición**: ajustarlas viendo cómo van sería
volver a ajustar el ruido. Una idea nueva es una regla nueva, con nombre propio, medida desde
su propio inicio.

## Cómo se mide

- Solo hacia delante: la medición empieza con la primera vela cerrada tras instalar la app.
- Universo: los pares más líquidos de OKX (por defecto 20 en USDT, mínimo 5 M USD al día),
  revisado cada hora; un par que sale sigue gestionando su operación abierta.
- Señal al cierre de la vela t, entrada a la apertura de t+1 más deslizamiento. Dentro de una
  vela se comprueba primero el stop (peor caso); las salidas por cierre se ejecutan en la
  apertura siguiente. Costes: 0,10 % de comisión y 0,05 % de deslizamiento por lado.
- Una operación abierta por regla y par a la vez. Resultados netos, en % y en R.
- Tras un corte, las velas que faltan se descargan de OKX y se procesan en orden; el corte se
  registra igualmente.

## Reglas

| Regla | Velas | Entrada | Salida |
|---|---|---|---|
| A · Ruptura con impulso | 15 min | Cierre > máximo de las 20 velas anteriores, volumen ≥ 1,5 × la media de 20, cierre > EMA 50, y BTC con cierre de 1 h > su EMA 50 | Stop inicial a 3 ATR(14), que sube como stop móvil a 3 ATR del máximo; o a las 24 velas (6 h) |
| B · Rebote tras caída | 5 min | RSI(14) < 25 y cierre < banda de Bollinger inferior (20, 2σ), con el par en cierre de 1 h > su EMA 50 | Cierre ≥ banda media; stop a 1,5 ATR(14); o a las 12 velas (1 h) |
| A · Control al azar | 15 min | Cada vez que A entra, entra a la vez en un par elegido al azar entre los vigilados sin operación de control abierta | Las de A |
| B · Control al azar | 5 min | Igual, para B | Las de B |

El sorteo es reproducible: depende solo de la semilla 20261112, la regla, el par y la hora.

## Criterio de éxito (fijado hoy)

Una regla **cumple** cuando, tras al menos **28 días** y **100 operaciones cerradas**:

1. su resultado medio tras costes es **positivo**, y
2. supera a su control al azar con un **95 % de confianza** (diferencia de medias menos
   1,96 errores estándar > 0).

Si no, la regla no tiene ventaja demostrada y la app se queda como monitor. Ritmo esperado,
de una prueba sobre los 3 días anteriores al arranque con los 17 pares USDT que pasaban el
filtro: B unas 130 operaciones en 4 semanas; A unas 65, así que A necesitará unas 6 semanas
para llegar a 100. El veredicto espera a que se cumplan los dos mínimos.
