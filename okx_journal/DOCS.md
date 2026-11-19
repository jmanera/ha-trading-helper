# Bitácora OKX

Panel para llevar un diario de tus inversiones en la cuenta **spot** de OKX: qué tienes, cuánto
te ha costado con comisiones, cuánto ganas o pierdes en cualquier periodo sin contar el dinero
que metes o sacas, y qué te ha aportado cada valor. Se abre desde la barra lateral de Home
Assistant. **Solo lee**: no compra, no vende ni mueve nada. No es asesoramiento financiero.

## Requisitos

1. **Una clave de API de OKX de solo lectura.** En OKX: *Perfil → API → Crear clave*, permiso
   **Read** únicamente. Apunta la clave, el secreto y la contraseña (passphrase).
2. **El dominio de tu cuenta.** Las cuentas de España y del resto del EEE están en
   `eea.okx.com` (valor por defecto). Si OKX responde «API key doesn't exist», el dominio no es
   el tuyo.
3. **La app MariaDB de Home Assistant** (recomendado). En sus opciones añade la base y el
   usuario, por ejemplo:

   ```yaml
   databases:
     - homeassistant
     - okx_journal
   logins:
     - username: okx_journal
       password: una-contraseña-larga
   rights:
     - username: okx_journal
       database: okx_journal
   ```

   y pon `db_user` y `db_password` en esta app. Las tablas se crean solas al arrancar. Sin
   MariaDB (`db_host` vacío) los datos se guardan en SQLite dentro de la app.
4. Opcional: la app **Mosquitto broker** para los sensores.

## Por qué hace falta una base de datos

OKX solo deja consultar por API las operaciones y movimientos de los **últimos 3 meses** y no
guarda el valor de tu cuenta en el pasado. La app lo va guardando todo: si la dejas en marcha,
el histórico crece sin límite. La primera vez trae esos 3 meses y reconstruye el valor de cada
día con los movimientos y el precio de cierre diario.

## Pantallas

- **Hoy**: valor de la cuenta, resultado del día, resultado desde el inicio, comisiones y
  reparto por activo.
- **Posiciones**: cada activo con su cantidad, coste medio, invertido, valor, comisiones
  pagadas al comprar, la que pagarías al vender (tu nivel de taker) y el resultado neto. Al
  desplegar, cada compra que sigue abierta (maker o taker, % pagado) y las órdenes abiertas.
  También los bots grid con su resultado.
- **Órdenes**: las abiertas (límite, solo maker, stops, OCO…) con su precio, el de mercado,
  la distancia entre ambos, lo que ya se ha ejecutado, el importe y la comisión estimada (maker
  si espera en el libro, taker si es de mercado o se activa con un disparo). Debajo, el
  historial con filtros por activo, estado (ejecutadas o canceladas) y fechas, con precio
  medio, comisión real y resultado de cada venta. OKX solo conserva unas horas las órdenes
  canceladas sin ejecutar, así que la bitácora guarda las que llega a ver.
- **Rendimiento**: periodos de 7, 30 y 90 días, este mes, este año, 12 meses, todo o un rango
  de fechas. Resultado neto, rentabilidad ponderada en el tiempo, caída máxima, comisiones
  (maker/taker), curva del valor frente al dinero aportado, resultado de cada día y tabla
  mensual.
- **Activos**: cada valor en el que has invertido, comprado, vendido, beneficio cerrado y
  abierto, comisiones, aciertos y tiempo medio en posición. Con una nota por activo.
- **Bitácora**: todas las órdenes y movimientos con filtros y notas y etiquetas en cada uno.

## Cómo calcula

- **Todo en USDC**, con el último precio de OKX. Los días se cuentan en UTC.
- **FIFO**: cada venta cierra las compras más antiguas primero, el criterio que usa Hacienda
  para las criptomonedas. El coste de una compra incluye su comisión; lo que da una venta ya
  descuenta la suya.
- **Transferencias**: el dinero que llega desde la cuenta de financiación cuenta como
  aportación y el que sale, como retirada. Si llega una criptomoneda, entra al precio de ese
  momento (coste marcado con ≈).
- **Rentabilidad ponderada en el tiempo (TWR)**: encadena el rendimiento de cada día
  descontando el dinero aportado ese día, para que meter dinero no parezca ganancia.
- **Bots**: sus operaciones no son fills de tu cuenta; se muestra el resultado que da OKX.
  En los días reconstruidos se cuentan por lo invertido.

Los valores con **≈** son estimados.

## Entidades (MQTT)

Dispositivo **Bitácora OKX**: `sensor.okx_journal_valor` (USDC, con todo el resumen como
atributos), `..._resultado_hoy`, `..._resultado_hoy_pct`, `..._resultado_30d`,
`..._rentabilidad_30d`, `..._comisiones_30d` y `..._posiciones`.

## Límites

- Solo la cuenta de trading spot. Lo que pasa a financiación o Earn cuenta como salida.
- Sin margen ni futuros.
- Operaciones de hace más de 3 meses: no están en la API; el histórico empieza 3 meses antes
  de instalar la app.
