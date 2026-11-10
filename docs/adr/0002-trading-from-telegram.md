# ADR 0002: Compra y venta confirmadas desde Telegram

Fecha: 2026-11-10 · Estado: aceptada

## Contexto

La v1 solo avisaba (ADR 0001). Se pide poder abrir una posición desde el aviso de ENTRAR
indicando el importe por Telegram, y cerrarla desde el aviso de SALIR.

## Decisiones

1. **Desactivado por defecto.** Sin `trading_enabled` y las tres claves de OKX la app sigue
   siendo solo de avisos; cualquier botón responde que la operativa está desactivada.
2. **Home Assistant es la pasarela de Telegram.** El bot ya está en HA (polling, solo el chat
   permitido). Las automatizaciones reenvían botones y textos por MQTT
   (`<base>/telegram/in`) y envían las respuestas de la app (`<base>/telegram/out`). La app no
   necesita el token del bot y no hay dos procesos leyendo el mismo bot.
3. **La conversación vive en la app** (`trading/telegram.py`), con tests: pedir importe,
   validar contra `max_order_amount`, pedir **Confirmar**, ejecutar. Los tokens de
   confirmación son de un solo uso y caducan a los 10 minutos.
4. **Órdenes a mercado sin reintentos automáticos.** Un reintento tras un timeout podría
   comprar dos veces; cada orden lleva `clOrdId` para rastrearla.
5. **Stop-loss en el exchange** (orden *conditional* de OKX) al nivel de la señal (entrada −
   2×ATR, anclado al precio real de compra), para que proteja aunque HA o la app estén
   caídos. Los objetivos no se ponen en OKX: vender sigue siendo decisión del usuario.
6. **Posiciones propias** en `/data/positions.json` con la cantidad neta de comisión; se
   añaden a las `positions` de la configuración para que la estrategia emita SALIR.
7. **Demo primero.** `okx_demo: true` envía `x-simulated-trading: 1`; las claves deben ser de
   Demo trading. Clave recomendada: solo permiso Trade y vinculada a una IP.

## Consecuencias

- Si el stop salta en OKX, la app no lo sabe hasta que pulsas Vender: entonces ve que no hay
  saldo y da la posición por cerrada.
- Una compra hecha fuera de Telegram no está en `positions.json`; para recibir SALIR hay que
  apuntarla en `positions`.
