# ADR 0003: Bitácora de inversiones OKX

Fecha: 2026-11-18 · Estado: aceptada

## Contexto

Queremos un panel que lea la cuenta real de OKX (solo lectura) y sirva de bitácora:
posiciones abiertas con su coste y comisiones, rendimiento diario, desglose de activos,
rendimiento neto en cualquier periodo y beneficio aportado por cada activo.

OKX no guarda un histórico del valor de la cuenta: `account/balance` solo da la foto actual, y
las operaciones (`fills-history`) y movimientos (`bills-archive`) solo se pueden pedir de los
últimos 3 meses. Para analizar a largo plazo hay que ir guardando los datos nosotros.

Decisiones del usuario: MariaDB de la app oficial de Home Assistant, panel web por ingress,
importes en USDC y solo la cuenta de trading spot.

## Decisiones

1. **Tercera app, `okx_journal`**, con el código en `src/ha_trading_helper/journal/`. No toca
   las otras dos.
2. **Clave de API de OKX de solo lectura.** El cliente (`journal/okx.py`) solo hace GET.
3. **Dominio configurable** (`okx_domain`). Las cuentas del EEE (España incluida) solo existen
   en `eea.okx.com`; en `www.okx.com` la misma clave da `50119 API key doesn't exist`.
4. **MariaDB con SQLAlchemy Core** (`pymysql`); SQLite si no hay `db_host` (desarrollo, tests).
   Sin Alembic: el esquema se crea con `create_all` y `meta.schema_version` queda para futuras
   migraciones. Las altas son *upserts* idempotentes, así que repetir una sincronización no
   duplica nada. Un test de integración lo comprueba contra MariaDB 11 en Docker.
5. **Sincronización incremental** cada 5 min (`journal/sync.py`): saldos y una foto del valor
   de la cuenta, fills, movimientos y órdenes desde el último id guardado, órdenes abiertas y
   condicionales, bots grid, comisiones de tu nivel (cada 12 h) y cierres diarios de cada
   moneda tocada. Cada paso falla por separado y queda anotado en `last_sync`.
6. **El libro de la cuenta incluye la contabilidad de los bots**: sus operaciones (órdenes que
   no están entre los fills de la cuenta) y su lado de cada transferencia, con el saldo del
   bot en `bal`. `equity.account_bills` las descarta recorriendo la cadena de saldos hacia
   atrás desde el saldo actual. Con los datos reales, el saldo FIFO de cada moneda coincide con
   el `cashBal` de OKX.
7. **FIFO con comisiones dentro** (`journal/ledger.py`), el criterio de Hacienda para las
   criptomonedas. Cada cambio de la cuenta es un evento que da una moneda y recibe otra; el
   coste de un lote es lo que se dio por él (la comisión en la moneda recibida ya reduce la
   cantidad) y lo que da una venta descuenta su comisión. Los lotes guardan maker/taker y el %.
   Conversiones (Convert, Easy convert, Simple trade) son operaciones; las transferencias a
   financiación son ventas o compras a mercado; otros abonos cuestan 0.
8. **Valor diario reconstruido** (`journal/equity.py`): saldo al cierre de cada día según el
   libro × cierre diario en USDC, más los bots por lo invertido. Una foto de la última hora del
   día sustituye a la reconstrucción; hoy es la última foto.
9. **Rendimiento separando aportaciones**: resultado = valor final − inicial − aportado neto;
   rentabilidad ponderada en el tiempo (TWR) con el dinero del día al principio del día.
10. **Panel web propio** (FastAPI + Jinja, gráficos SVG generados en el servidor, sin JS ni
    CDN) por ingress; los enlaces se construyen con `X-Ingress-Path`. Además publica siete
    sensores por MQTT (opcional).
11. **Días en UTC**, como los cierres diarios de OKX; las horas se muestran en `timezone`.

## Consecuencias

- Con la cuenta real (9 días de historia, 150 fills, 538 movimientos) la primera
  sincronización tarda unos 13 s y cada página se genera en menos de 60 ms.
- Activos que llegaron por transferencia: coste al precio de ese momento, marcado con ≈.
- Bots: su detalle no entra en el FIFO; se muestra el resultado que da OKX. En días
  reconstruidos cuentan por lo invertido.
- Operaciones de más de 3 meses: no están en la API. El histórico empieza 3 meses antes de
  instalar la app y desde ahí crece sin límite. Importar el extracto CSV de OKX queda pendiente.
