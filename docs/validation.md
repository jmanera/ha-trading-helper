# Validación de la estrategia (fase 4)

Ejecución del 10 de noviembre de 2026: `ha-trading-helper validate --years 2`, universo
automático de 20 pares `-USDT` de OKX, velas de 4 h, comisión 0,10 % por lado y deslizamiento
0,05 % por lado, máximo 3 posiciones simultáneas. Resultados en múltiplos de R (riesgo inicial
de cada operación), netos de costes.

## Muestra completa por umbral de entrada

| Umbral | Operaciones | Aciertos | Esperanza | Factor de beneficio | Máx. drawdown |
|---:|---:|---:|---:|---:|---:|
| 60 | 415 | 36 % | +0,078R | 1,17 | 21,3R |
| 70 | 276 | 31 % | −0,069R | 0,87 | 32,6R |
| 80 | 166 | 37 % | +0,028R | 1,05 | 20,8R |

## Walk-forward (umbral elegido con datos anteriores, evaluado en la ventana siguiente)

| Ventana de test hasta | Umbral | Operaciones | Aciertos | Esperanza | FB |
|---|---:|---:|---:|---:|---:|
| 2025-08-04 | 60 | 102 | 32 % | +0,046R | 1,09 |
| 2026-01-05 | 60 | 49 | 53 % | +0,342R | 1,92 |
| 2026-06-09 | 60 | 79 | 32 % | −0,075R | 0,86 |
| 2026-11-10 | 60 | 125 | 30 % | −0,002R | 1,00 |
| **Total fuera de muestra** | | **355** | **34 %** | **+0,043R** | **1,09** |

## Conclusión

La puerta de la fase 4 **no se supera con claridad**. La esperanza fuera de muestra es
positiva pero mínima (+0,04R por operación), el factor de beneficio (1,09) queda por debajo
del mínimo exigido (1,1) y las dos últimas ventanas son planas o negativas. Además:

- el umbral 70 del plan es el peor de los tres en la muestra completa;
- el universo se elige con el volumen actual, lo que introduce sesgo de supervivencia (las
  monedas que hoy son líquidas son las que salieron bien), así que el resultado real sería
  probablemente algo peor.

Por eso la app arranca con `require_validation: true`: las señales ENTRAR se muestran y
explican, pero se marcan como **informativas** (`alerta: false`) y el binary_sensor de
entrada sigue reflejando la acción, no una recomendación validada. La revalidación semanal
actualizará este veredicto automáticamente.

## Siguientes pasos propuestos

1. Reducir grados de libertad: probar la tendencia + ruptura como señal base y el resto como
   desempate, en lugar de puntos sumados.
2. Validar con un universo histórico (pares líquidos en cada fecha) para eliminar el sesgo
   de supervivencia.
3. Añadir un régimen más estricto (BTC sobre EMA 200 **y** EMA 200 con pendiente positiva),
   ya que buena parte de las pérdidas se concentra en 2026, con BTC bajo su EMA 200.
4. Mantener la fase 7 (registro de señales en `signals.jsonl`) durante 4 semanas antes de
   cualquier cambio a ejecución real.

## Revalidación con universo histórico (12 de noviembre de 2026)

`validate` reconstruye ahora el universo vela a vela: de los 294 pares `-USDT` que OKX lista
hoy, en cada vela solo pueden entrar los 20 con más volumen medio de los 30 días anteriores
(mínimo 5 M USDT al día). 128 pares estuvieron en algún momento en ese universo; 111 de ellos
no están en el universo de hoy. Mismas reglas (con la tendencia como filtro obligatorio),
costes y walk-forward que arriba.

| Universo | Operaciones | Aciertos | Esperanza | Factor de beneficio | Máx. drawdown |
|---|---:|---:|---:|---:|---:|
| Pares líquidos hoy (18) | 125 | 38 % | +0,024R | 1,04 | 15,8R |
| **Histórico (128)** | **177** | **29 %** | **−0,149R** | **0,74** | **34,6R** |

Sin el sesgo de supervivencia la estrategia **pierde** fuera de muestra (unas 1,7 veces su
error estándar por debajo de cero). La validación anterior era optimista.

Exploración de salidas con el mismo universo histórico (no implementada): sin venta parcial,
sin objetivo fijo y sin salida por EMA 50, solo trailing desde el máximo, da +0,14R
(trailing 3,5 × ATR, FB 1,22) a +0,29R (4 × ATR, FB 1,42) por operación, con un error
estándar de ±0,21 a ±0,27R: mejor que las salidas actuales, pero no distinguible de cero y
negativo sin las cinco mejores operaciones.
