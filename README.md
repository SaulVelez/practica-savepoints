# Simulación de Transacciones Anidadas, Deadlocks y Timeouts en un Sistema de Reservas Turísticas (MySQL)

Proyecto académico que implementa, en Python + **MySQL (InnoDB)**, un sistema
de reservas de viajes (vuelo + hotel + transporte) para ilustrar conceptos
fundamentales de **transacciones en bases de datos**: transacciones anidadas
con **savepoints**, **transacciones de compensación**, **deadlocks reales**
(detectados por el propio motor) y **timeouts reales** por espera
prolongada de un recurso bloqueado.

---

## Tabla de contenidos

1. [Introducción teórica](#1-introducción-teórica)
2. [Explicación del escenario](#2-explicación-del-escenario)
3. [Explicación del código](#3-explicación-del-código)
4. [Cómo ejecutar el proyecto](#4-cómo-ejecutar-el-proyecto)
5. [Resultados obtenidos](#5-resultados-obtenidos)
6. [Preguntas de reflexión](#6-preguntas-de-reflexión)
7. [Conclusión](#7-conclusión)

---

## 1. Introducción teórica

### 1.1 Transacciones anidadas y savepoints

Una **transacción** es una unidad de trabajo que agrupa una o varias
operaciones sobre la base de datos, garantizando las propiedades **ACID**
(Atomicidad, Consistencia, Aislamiento y Durabilidad). O se ejecutan todas
las operaciones, o no se ejecuta ninguna.

Una **transacción anidada** (o más precisamente, en MySQL/InnoDB, una
transacción con **puntos de guardado o savepoints**) es un mecanismo que
permite marcar un punto intermedio dentro de una transacción activa, al
cual se puede regresar (`ROLLBACK TO SAVEPOINT`) sin deshacer la
transacción completa. Esto es muy útil cuando una transacción tiene varios
pasos y se quiere poder deshacer solo una parte si algo falla más adelante,
conservando el trabajo previo válido.

Sintaxis en MySQL (InnoDB):

```sql
START TRANSACTION;
    -- operación 1
    SAVEPOINT sp1;
        -- operación 2
    ROLLBACK TO SAVEPOINT sp1;  -- deshace solo la operación 2
    RELEASE SAVEPOINT sp1;
COMMIT;
```

Cuando el paso posterior al savepoint falla y no se puede simplemente
"deshacer" porque parte del trabajo previo ya tuvo efectos que deben
revertirse de forma explícita (por ejemplo, un vuelo que ya se compró y que
depende de reglas de negocio para cancelarse), se recurre al patrón de
**transacción de compensación**.

### 1.2 Transacciones de compensación (patrón SAGA)

En sistemas distribuidos o en flujos de negocio de varios pasos, no siempre
es posible envolver todo en una única transacción ACID clásica (por
ejemplo, cuando cada paso lo maneja un servicio distinto). El patrón
**SAGA** resuelve esto ejecutando una secuencia de transacciones locales,
donde cada paso tiene asociada una **transacción de compensación** que
revierte su efecto si un paso posterior falla.

En este proyecto simulamos una SAGA simplificada dentro de una sola base de
datos: si la reserva del vuelo ya se confirmó y luego falla la reserva del
hotel, se ejecuta una compensación explícita (`cancelar_vuelo`) que libera
el asiento, en lugar de depender únicamente del rollback automático.

### 1.3 Deadlocks (interbloqueos) — detección real en InnoDB

Un **deadlock** ocurre cuando dos (o más) transacciones se bloquean
mutuamente porque cada una espera un recurso que la otra tiene retenido,
formando una **espera circular**. Las cuatro condiciones necesarias para
que se produzca un deadlock son:

1. **Exclusión mutua**: un recurso solo puede ser usado por una transacción a la vez.
2. **Retención y espera**: una transacción retiene un recurso mientras espera otro.
3. **No apropiación (no preemption)**: un recurso no puede ser arrebatado, solo liberado voluntariamente.
4. **Espera circular**: existe un ciclo de transacciones esperándose entre sí.

**MySQL/InnoDB implementa un detector de deadlocks real.** Internamente
mantiene un grafo de espera entre transacciones (qué transacción espera el
bloqueo de cuál otra) y lo analiza continuamente. En cuanto detecta un
ciclo, elige una transacción **víctima** (típicamente la que ha modificado
menos filas, para minimizar el trabajo perdido) y la aborta
automáticamente con el **error 1213**
(`Deadlock found when trying to get lock; try restarting transaction`),
haciendo un `ROLLBACK` implícito de esa transacción y liberando sus
bloqueos para que la otra transacción pueda continuar. Esto es justamente
lo que se observa en la simulación de este proyecto (sección 5.3): no se
usó ningún timeout manual para "forzar" la detección, InnoDB lo hizo por
sí mismo, en tiempo real.

### 1.4 Timeouts por espera prolongada de un recurso

Un **timeout transaccional** es un límite de tiempo que una transacción
puede esperar para obtener un recurso bloqueado por otra transacción. Si el
recurso no se libera dentro de ese plazo, la transacción que espera falla
con un error de tipo "lock wait timeout" en lugar de quedarse esperando
indefinidamente. Esto protege al sistema de quedar "colgado" por una
transacción lenta, defectuosa o que nunca libera sus recursos, a costa de
que la transacción que esperaba deba reintentarse o reportar el error al
usuario.

En MySQL/InnoDB esto se controla con la variable de sesión
`innodb_lock_wait_timeout` (en segundos). Cuando una transacción intenta
tomar un bloqueo de fila que otra transacción ya tiene, esperará como
máximo ese tiempo antes de que el servidor le devuelva el **error 1205**
(`Lock wait timeout exceeded; try restarting transaction`). Nótese que este
mecanismo es distinto y complementario al detector de deadlocks: un
timeout ocurre incluso sin que exista una espera circular, simplemente
porque un recurso sigue ocupado más tiempo del tolerado.

---

## 2. Explicación del escenario

El sistema simula la compra de un **paquete turístico** compuesto por tres
pasos que deben confirmarse como una única transacción atómica:

| Paso | Recurso      | Acción                                  |
|------|--------------|------------------------------------------|
| 1    | Vuelo        | Descontar un asiento disponible          |
| 2    | Hotel        | Descontar una habitación disponible      |
| 3    | Transporte   | Descontar un vehículo disponible         |

**Regla de negocio central**: si el hotel no tiene cupo (paso 2), el sistema
debe:

1. Volver (`ROLLBACK TO SAVEPOINT`) al estado justo después de haber
   comprado el vuelo, deshaciendo cualquier cambio parcial del paso 2.
2. Ejecutar una **transacción de compensación** que cancele el vuelo
   (liberando el asiento que ya se había descontado), ya que el paquete
   completo no puede concretarse sin el hotel.
3. Confirmar (`COMMIT`) ese resultado: el cliente no queda con un vuelo
   "colgado" sin el resto del paquete.

Adicionalmente, se simulan dos escenarios de concurrencia que no dependen
del flujo de negocio anterior, sino del comportamiento del motor de base de
datos ante el acceso simultáneo:

- **Deadlock real**: dos transacciones concurrentes, cada una con su propia
  conexión MySQL, intentan bloquear (con `SELECT ... FOR UPDATE`) la fila
  de un vuelo y la fila de un hotel en orden inverso. InnoDB detecta la
  espera circular y aborta automáticamente una de las dos.
- **Timeout real**: una transacción "lenta" retiene un bloqueo de escritura
  sobre un vuelo por más tiempo del que otra transacción (configurada con
  un `innodb_lock_wait_timeout` corto) está dispuesta a esperar.

---

## 3. Explicación del código

El script `simulacion_transacciones.py` está organizado en las siguientes
secciones:

### 3.1 Configuración de logging

Se configura un logger (`logger = logging.getLogger("reservas")`) con dos
salidas: consola y archivo `reservas.log`. Cada línea incluye timestamp,
nivel (`INFO`/`WARNING`/`ERROR`) y el nombre del hilo (`threadName`), lo
cual es clave para poder seguir la intercalación de eventos en los
escenarios de concurrencia (deadlock y timeout).

### 3.2 Configuración y conexión a MySQL: `DB_CONFIG` / `obtener_conexion()`

```python
DB_CONFIG = {
    "host": os.environ.get("RESERVAS_DB_HOST", "localhost"),
    "port": int(os.environ.get("RESERVAS_DB_PORT", "3306")),
    "user": os.environ.get("RESERVAS_DB_USER", "reservas_app"),
    "password": os.environ.get("RESERVAS_DB_PASSWORD", "reservas_pass"),
    "database": os.environ.get("RESERVAS_DB_NAME", "reservas_db"),
    "autocommit": False,
}
```

Los valores se pueden sobreescribir con variables de entorno
(`RESERVAS_DB_HOST`, `RESERVAS_DB_USER`, etc.), útil para no dejar
credenciales "quemadas" en el código en un entorno real.

```python
def obtener_conexion(lock_wait_timeout=None):
    conn = mysql.connector.connect(**DB_CONFIG)
    if lock_wait_timeout is not None:
        cur = conn.cursor()
        cur.execute("SET SESSION innodb_lock_wait_timeout = %s", (lock_wait_timeout,))
        cur.close()
    return conn
```

- `autocommit=False` permite controlar manualmente `start_transaction()`,
  `commit()` y `rollback()` — indispensable para la lógica de savepoints y
  compensación.
- El parámetro opcional `lock_wait_timeout` configura, solo para esa
  conexión, cuántos segundos esperará antes de fallar con el error 1205 si
  otra conexión tiene un bloqueo de fila activo. Se usa específicamente en
  la simulación de timeout.

### 3.3 Creación y poblado de tablas

- `crear_tablas()`: crea `vuelos`, `hoteles` y `transportes` con
  `ENGINE=InnoDB` explícito (es el único motor de MySQL con soporte de
  transacciones, bloqueos por fila y detección de deadlocks).
- `reiniciar_base_datos()`: hace `TRUNCATE TABLE` de las tres tablas al
  inicio de cada ejecución, para que la demo parta de un estado limpio y
  reproducible (a diferencia de SQLite, aquí no existe un simple archivo
  que borrar).
- `poblar_datos()`: inserta 10 registros de prueba en cada tabla. Se fija
  `random.seed(42)` para que la demostración sea reproducible y algunos
  hoteles queden deliberadamente en 0 habitaciones.

### 3.4 Operaciones de negocio

Cada paso de la reserva es una función independiente que hace un `UPDATE`
condicionado (`WHERE disponible > 0`) y verifica `cursor.rowcount`:

```python
def reservar_hotel(conn, id_hotel):
    cur = conn.cursor()
    cur.execute(
        "UPDATE hoteles SET habitaciones_disponibles = habitaciones_disponibles - 1 "
        "WHERE id = %s AND habitaciones_disponibles > 0",
        (id_hotel,),
    )
    if cur.rowcount == 0:
        raise SinCupoException(...)
```

Si `rowcount` es 0, significa que la condición `> 0` no se cumplió (no hay
cupo), y se lanza `SinCupoException`. Este patrón evita condiciones de
carrera dentro de una misma transacción, ya que la comprobación de
disponibilidad y el descuento ocurren en una sola sentencia SQL atómica.

`cancelar_vuelo(conn, id_vuelo)` es la **transacción de compensación**:
suma nuevamente 1 al contador de asientos disponibles.

### 3.5 Transacción principal: `reservar_viaje_completo()`

Este es el núcleo del proyecto. Su flujo es:

```
START TRANSACTION
  reservar_vuelo()                       # PASO 1
  SAVEPOINT sp_despues_vuelo
  try:
      reservar_hotel()                   # PASO 2
  except SinCupoException:
      ROLLBACK TO SAVEPOINT sp_despues_vuelo
      RELEASE SAVEPOINT sp_despues_vuelo
      cancelar_vuelo()                    # COMPENSACIÓN
      COMMIT
      return False
  reservar_transporte()                  # PASO 3
COMMIT
return True
```

Puntos clave:

- El `SAVEPOINT` se crea **después** del paso 1, porque es el punto al que
  queremos poder regresar si falla el paso 2.
- El `ROLLBACK TO SAVEPOINT` deshace únicamente los efectos posteriores al
  savepoint.
- La compensación (`cancelar_vuelo`) es un paso **explícito**, no un
  rollback automático, porque conceptualmente representa una decisión de
  negocio ("como no hay hotel, cancelamos el vuelo ya confirmado") y no
  simplemente deshacer una operación SQL.
- Si el fallo ocurre en el **paso 1** o en el **paso 3** (no en el hotel),
  se hace un `ROLLBACK` completo de toda la transacción, sin necesidad de
  compensación.

### 3.6 Simulación de deadlock real: `simular_deadlock()`

Se lanzan dos hilos, cada uno con su **propia conexión MySQL** (esto es
importante: un deadlock real requiere transacciones/conexiones distintas,
no hilos compartiendo una sola conexión):

- **Transacción A** (`transaccion_a`): hace `SELECT ... FOR UPDATE` sobre
  la fila del VUELO, espera 1 segundo, luego intenta `SELECT ... FOR
  UPDATE` sobre la fila del HOTEL.
- **Transacción B** (`transaccion_b`): hace `SELECT ... FOR UPDATE` sobre
  la fila del HOTEL, espera 1 segundo, luego intenta `SELECT ... FOR
  UPDATE` sobre la fila del VUELO.

Como cada una retiene su primer recurso mientras espera el segundo (que la
otra tiene retenido), se produce una **espera circular real** a nivel de
InnoDB. El motor detecta el ciclo por sí solo (sin ningún timeout
configurado por nosotros) y aborta a una de las dos transacciones con el
error 1213, que se captura comprobando `e.errno ==
ERROR_DEADLOCK_INNODB` (1213).

### 3.7 Simulación de timeout real: `simular_timeout()`

- `transaccion_lenta()`: abre una transacción, hace un `UPDATE` sobre un
  vuelo (lo que toma un bloqueo de escritura por fila en InnoDB), y luego
  hace `time.sleep(6)` simulando una operación de larga duración antes de
  hacer `COMMIT`.
- `transaccion_impaciente()`: obtiene una conexión con
  `lock_wait_timeout=2`, es decir, con `innodb_lock_wait_timeout=2`
  configurado en esa sesión, e intenta actualizar el mismo vuelo. Como el
  recurso sigue bloqueado por la transacción lenta, MySQL lanza el error
  1205 a los ~2 segundos, que se captura comprobando `e.errno ==
  ERROR_LOCK_WAIT_TIMEOUT_INNODB` (1205).

---

## 4. Cómo ejecutar el proyecto

### 4.1 Requisitos previos

- Python 3.8+
- Un servidor **MySQL 8.x** corriendo en `localhost` (puede ser una
  instalación nativa, Docker, XAMPP/WAMP, etc.)

### 4.2 Configurar la base de datos y el usuario de aplicación

Conéctate a MySQL como administrador (por ejemplo `mysql -u root -p`) y
ejecuta:

```sql
CREATE DATABASE IF NOT EXISTS reservas_db CHARACTER SET utf8mb4;

CREATE USER IF NOT EXISTS 'reservas_app'@'localhost'
    IDENTIFIED BY 'reservas_pass';

GRANT ALL PRIVILEGES ON reservas_db.* TO 'reservas_app'@'localhost';
FLUSH PRIVILEGES;
```

> Si tu servidor MySQL usa el plugin de autenticación por defecto
> `caching_sha2_password` y tu conector Python es antiguo, puedes crear el
> usuario con
> `IDENTIFIED WITH mysql_native_password BY 'reservas_pass'` en su lugar.

### 4.3 Instalar dependencias y ejecutar

```bash
git clone <url-del-repositorio>
cd <carpeta-del-proyecto>
python3 -m venv venv        # opcional pero recomendado
source venv/bin/activate    # en Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 simulacion_transacciones.py
```

Si tu usuario, contraseña, host o nombre de base de datos son distintos a
los valores por defecto, puedes sobreescribirlos con variables de entorno
antes de ejecutar el script:

```bash
export RESERVAS_DB_HOST=localhost
export RESERVAS_DB_PORT=3306
export RESERVAS_DB_USER=reservas_app
export RESERVAS_DB_PASSWORD=reservas_pass
export RESERVAS_DB_NAME=reservas_db
python3 simulacion_transacciones.py
```

Al finalizar, revisar el archivo `reservas.log` generado en el mismo
directorio para ver el detalle completo con timestamps.

> Nota: el script hace `TRUNCATE TABLE` de `vuelos`, `hoteles` y
> `transportes` en cada ejecución (`reiniciar_base_datos()`), para que la
> demostración sea siempre reproducible desde cero. No borres manualmente
> la base de datos entre ejecuciones.

---

## 5. Resultados obtenidos

A continuación, los logs **reales** obtenidos al ejecutar
`python3 simulacion_transacciones.py` contra una instancia local de MySQL
8.0 (con la semilla fija `random.seed(42)`).

### 5.1 Caso 1: Reserva exitosa (vuelo=3, hotel=4, transporte=3)

```
=== Iniciando reserva: vuelo=3, hotel=4, transporte=3 ===
[PASO 1] Vuelo 3 reservado (asiento descontado).
SAVEPOINT 'sp_despues_vuelo' creado.
[PASO 2] Hotel 4 reservado (habitación descontada).
[PASO 3] Transporte 3 reservado (vehículo descontado).
=== Reserva completa exitosa (COMMIT) ===
Resultado caso 1 -> éxito=True | Reserva completa exitosa: vuelo, hotel y transporte confirmados.
```

Los tres pasos se ejecutaron y se confirmó la transacción completa con un
único `COMMIT`.

### 5.2 Caso 2: Hotel sin cupo → savepoint + compensación (vuelo=6, hotel=3, transporte=4)

```
=== Iniciando reserva: vuelo=6, hotel=3, transporte=4 ===
[PASO 1] Vuelo 6 reservado (asiento descontado).
SAVEPOINT 'sp_despues_vuelo' creado.
Fallo en reserva de hotel: No hay habitaciones disponibles en el hotel 3
ROLLBACK TO SAVEPOINT ejecutado.
[COMPENSACIÓN] Vuelo 6 cancelado, asiento liberado.
Transacción finalizada con COMPENSACIÓN. Vuelo cancelado, hotel no disponible.
Resultado caso 2 -> éxito=False | Sin cupo en el hotel. Vuelo cancelado mediante compensación.
```

Se observa claramente la secuencia: reserva de vuelo → savepoint → fallo en
hotel → rollback al savepoint → compensación (cancelación del vuelo) →
commit final. El inventario del vuelo 6 vuelve a su valor original (4
asientos), confirmando que la compensación funcionó correctamente.

### 5.3 Simulación de deadlock real (detectado por InnoDB)

```
########## SIMULACIÓN DE DEADLOCK (real, detectado por InnoDB) ##########
Transaccion-A: intenta bloquear fila VUELO 7 (SELECT ... FOR UPDATE)
Transaccion-B: intenta bloquear fila HOTEL 5 (SELECT ... FOR UPDATE)
Transaccion-A: bloqueó VUELO 7. Esperando antes de pedir HOTEL...
Transaccion-B: bloqueó HOTEL 5. Esperando antes de pedir VUELO...
Transaccion-A: intenta bloquear fila HOTEL 5 (SELECT ... FOR UPDATE)
Transaccion-B: intenta bloquear fila VUELO 7 (SELECT ... FOR UPDATE)
Transaccion-B: DEADLOCK DETECTADO POR INNODB (error 1213) -> Deadlock found when trying to get lock; try restarting transaction. Esta transacción fue elegida como víctima y se hizo ROLLBACK automático.
Transaccion-A: bloqueó HOTEL 5. Transacción completada con éxito (COMMIT).
########## FIN SIMULACIÓN DE DEADLOCK ##########
```

Ambas transacciones quedan mutuamente esperando (A tiene VUELO y quiere
HOTEL; B tiene HOTEL y quiere VUELO). **InnoDB detecta el ciclo por sí
mismo**, sin que el código haya definido ningún timeout manual, y aborta a
la Transacción B con el error nativo 1213. La Transacción A, al quedar
liberada de la competencia, obtiene el bloqueo de HOTEL y confirma su
`COMMIT` exitosamente.

### 5.4 Simulación de timeout real (innodb_lock_wait_timeout)

```
########## SIMULACIÓN DE TIMEOUT (real, innodb_lock_wait_timeout) ##########
Transaccion-Lenta: bloqueo de fila tomado sobre vuelo 9. Simulando operación lenta de 6s...
Transaccion-Impaciente: intentando reservar el vuelo 9 (innodb_lock_wait_timeout = 2s)...
Transaccion-Impaciente: TIMEOUT (2.00s) -> error 1205: Lock wait timeout exceeded; try restarting transaction. El recurso siguió bloqueado más tiempo del permitido.
Transaccion-Lenta: transacción lenta finalizada y confirmada (COMMIT).
########## FIN SIMULACIÓN DE TIMEOUT ##########
```

La transacción lenta retiene el bloqueo de escritura sobre la fila del
vuelo 9 durante 6 segundos. La transacción impaciente, configurada con
`innodb_lock_wait_timeout=2`, falla exactamente a los ~2.00 segundos con el
error nativo de MySQL 1205, demostrando el comportamiento real de un
timeout por espera prolongada de un recurso bloqueado a nivel de fila.

> El log completo de toda la ejecución (incluyendo el estado del
> inventario antes y después de cada caso) queda disponible en
> `reservas.log` tras correr el script.

---

## 6. Preguntas de reflexión

**1. ¿Por qué es necesario usar un savepoint en lugar de un simple rollback
completo cuando falla la reserva del hotel?**

Porque un `ROLLBACK` completo deshace *toda* la transacción, incluyendo la
reserva del vuelo que sí se pudo confirmar correctamente. Si en lugar de
eso queremos decidir explícitamente qué hacer con ese vuelo ya reservado
(cancelarlo mediante una compensación, o eventualmente ofrecerlo al cliente
por separado), necesitamos un punto de control intermedio al cual regresar
sin perder la posibilidad de inspeccionar y decidir sobre lo que ya ocurrió.
El savepoint nos da ese punto de control sin cerrar la transacción.

**2. ¿Cuál es la diferencia entre un ROLLBACK TO SAVEPOINT y una
transacción de compensación?**

El `ROLLBACK TO SAVEPOINT` es una operación puramente técnica del motor de
base de datos: deshace los cambios SQL posteriores al savepoint dentro de
la misma transacción activa. La transacción de compensación, en cambio, es
una operación de **negocio** adicional y explícita (en este caso, un nuevo
`UPDATE` que libera el asiento del vuelo) que se ejecuta *después* de
volver al savepoint, para revertir un efecto que ya estaba confirmado desde
el punto de vista lógico del negocio, aunque técnicamente siga dentro de la
misma transacción de base de datos. En sistemas distribuidos (microservicios),
la compensación suele ser la única herramienta disponible, ya que no existe
una transacción ACID global que abarque todos los servicios.

**3. ¿Cómo detecta InnoDB un deadlock y por qué elige abortar
específicamente una de las dos transacciones?**

InnoDB mantiene internamente un **grafo de espera** entre transacciones:
cada nodo es una transacción y cada arista indica "la transacción X espera
un bloqueo que retiene la transacción Y". Cuando una nueva espera crea un
ciclo en ese grafo, se confirma un deadlock. En ese momento InnoDB elige
como **víctima** a la transacción que, según sus métricas internas
(típicamente el número de filas modificadas), representa **menos trabajo
perdido** al revertirla, y le aplica un `ROLLBACK` automático devolviendo el
error 1213 a esa conexión. La otra transacción, ya sin competencia por ese
recurso, puede continuar y confirmar su trabajo con normalidad — exactamente
lo que se observó en la sección 5.3, donde la Transacción B fue la víctima y
la Transacción A pudo completar su `COMMIT`.

**4. ¿Qué riesgos existen si se configura un `innodb_lock_wait_timeout`
demasiado corto o demasiado largo en un sistema de reservas real?**

Un timeout **demasiado corto** puede provocar que transacciones legítimas
fallen innecesariamente durante picos normales de carga (por ejemplo, en
temporada alta de vuelos), generando una mala experiencia de usuario y más
reintentos de los necesarios. Un timeout **demasiado largo**, por el
contrario, puede hacer que los usuarios esperen mucho tiempo sin respuesta
ante un bloqueo real, satura los recursos del servidor (conexiones
abiertas, hilos ocupados esperando) y retrasa la detección de problemas
como transacciones colgadas o mal diseñadas. El valor adecuado depende de
medir los tiempos normales de las operaciones concurrentes esperadas y
dejar un margen razonable por encima de ellos. En MySQL, este valor por
defecto es de 50 segundos, pensado para tolerar picos de carga normales sin
sacrificar demasiado la capacidad de respuesta ante bloqueos reales.

**5. En el escenario de deadlock simulado, ¿qué estrategias podrían
usarse en un sistema real para reducir la probabilidad de que ocurra?**

La estrategia más efectiva y ampliamente usada es definir y respetar un
**orden global consistente de adquisición de recursos**: si todas las
transacciones siempre bloquean primero "vuelos" y luego "hoteles" (nunca al
revés), la espera circular se vuelve imposible porque no puede formarse un
ciclo. Otras estrategias complementarias incluyen: reducir el tiempo que
cada transacción retiene un bloqueo (transacciones más cortas y enfocadas,
evitando operaciones lentas o de I/O externo dentro de una transacción
abierta), usar niveles de aislamiento más bajos cuando el negocio lo
permita, aplicar bloqueos optimistas (verificar versión/conflicto al
momento del commit en lugar de bloquear por adelantado con `FOR UPDATE`), y
apoyarse en el detector de deadlocks automático de InnoDB combinado con
lógica de reintento en la aplicación (capturar el error 1213 y volver a
intentar la transacción completa desde cero).

---

## 7. Conclusión

Esta simulación permitió observar, de manera controlada y reproducible,
cómo los mecanismos de **savepoints** y **transacciones de compensación**
permiten construir flujos de negocio de varios pasos que necesitan poder
revertir parcialmente su trabajo sin perder la atomicidad general de la
operación. El caso del sistema de reservas turísticas es representativo de
un problema extremadamente común en el mundo real (comercio electrónico,
sistemas de aerolíneas, reservas de eventos), donde distintos recursos con
distinta disponibilidad deben confirmarse en conjunto o no confirmarse en
absoluto.

Por otro lado, al migrar la simulación de un motor embebido simple a
**MySQL/InnoDB**, se pudo observar el comportamiento **real** —no
simulado— de un motor de base de datos de nivel productivo ante la
concurrencia: un **deadlock genuino**, detectado internamente mediante un
grafo de espera y resuelto automáticamente abortando una transacción
víctima (error 1213), y un **timeout real** de bloqueo de fila, gobernado
por la variable de sesión `innodb_lock_wait_timeout` (error 1205). Esto
refuerza la comprensión de que estos mecanismos no son artificios
académicos, sino comportamientos estándar de cualquier motor transaccional
serio, con los que cualquier aplicación concurrente en producción debe
saber convivir (por ejemplo, mediante lógica de reintento ante el error
1213).

En conjunto, el proyecto refuerza la idea de que el diseño transaccional no
es solo una cuestión de sintaxis SQL, sino una decisión arquitectónica que
debe anticipar fallos parciales, condiciones de carrera y contención de
recursos desde el diseño mismo del sistema — y que elegir un motor de base
de datos con las garantías transaccionales adecuadas (como InnoDB) es parte
fundamental de esa decisión.
