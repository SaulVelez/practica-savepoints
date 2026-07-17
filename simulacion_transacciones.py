import threading
import time
import logging
import random
import os

import mysql.connector
from mysql.connector import errorcode


# Configuración de logging: se registra en consola y en archivo reservas.log
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reservas.log")

logger = logging.getLogger("reservas")
logger.setLevel(logging.DEBUG)
logger.handlers.clear()

_formato = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(threadName)-12s | %(message)s",
    datefmt="%H:%M:%S",
)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formato)
logger.addHandler(_console_handler)

_file_handler = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
_file_handler.setFormatter(_formato)
logger.addHandler(_file_handler)


 
# Configuración de conexión a MySQL
 
# Ajusta estos valores según tu instalación local de MySQL. Ver el README
# para el script SQL de creación del usuario y la base de datos.
DB_CONFIG = {
    "host": os.environ.get("RESERVAS_DB_HOST", "localhost"),
    "port": int(os.environ.get("RESERVAS_DB_PORT", "3306")),
    "user": os.environ.get("RESERVAS_DB_USER", "admin"),
    "password": os.environ.get("RESERVAS_DB_PASSWORD", "Saulito123.0"),
    "database": os.environ.get("RESERVAS_DB_NAME", "sistema_vuelo"),
    "autocommit": False,  # control manual de COMMIT/ROLLBACK
}


 
# Excepciones de negocio
class SinCupoException(Exception):
    """Se lanza cuando un recurso (vuelo, hotel o transporte) no tiene
    disponibilidad."""


class ReservaFallidaException(Exception):
    """Error genérico durante el proceso de reserva."""


 
# Conexión y utilidades de base de datos
def obtener_conexion(lock_wait_timeout=None):
    """
    Crea una conexión nueva a MySQL.

    `autocommit=False` (definido en DB_CONFIG) permite controlar
    manualmente el inicio y fin de la transacción con start_transaction(),
    commit() y rollback(), que es lo que necesitamos para implementar
    transacciones anidadas con savepoints.

    `lock_wait_timeout`, si se especifica, configura la variable de sesión
    `innodb_lock_wait_timeout` (en segundos) para ESTA conexión, es decir,
    cuánto tiempo esperará antes de fallar con el error 1205 (Lock wait
    timeout exceeded) si otra transacción mantiene un bloqueo sobre la fila
    que necesita.
    """
    conn = mysql.connector.connect(**DB_CONFIG)
    if lock_wait_timeout is not None:
        cur = conn.cursor()
        cur.execute("SET SESSION innodb_lock_wait_timeout = %s", (lock_wait_timeout,))
        cur.close()
    return conn


def crear_tablas():
    """Crea las tablas vuelos, hoteles y transportes si no existen. Se
    usa el motor InnoDB explícitamente, ya que es el único motor de MySQL
    que soporta transacciones, savepoints, bloqueos por fila y detección
    de deadlocks."""
    conn = obtener_conexion()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS vuelos (
                id INT PRIMARY KEY,
                origen VARCHAR(100) NOT NULL,
                destino VARCHAR(100) NOT NULL,
                aerolinea VARCHAR(100) NOT NULL,
                asientos_disponibles INT NOT NULL,
                precio DECIMAL(10,2) NOT NULL
            ) ENGINE=InnoDB
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hoteles (
                id INT PRIMARY KEY,
                nombre VARCHAR(100) NOT NULL,
                ciudad VARCHAR(100) NOT NULL,
                habitaciones_disponibles INT NOT NULL,
                precio_noche DECIMAL(10,2) NOT NULL
            ) ENGINE=InnoDB
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS transportes (
                id INT PRIMARY KEY,
                tipo VARCHAR(100) NOT NULL,
                empresa VARCHAR(100) NOT NULL,
                vehiculos_disponibles INT NOT NULL,
                precio DECIMAL(10,2) NOT NULL
            ) ENGINE=InnoDB
        """)
        conn.commit()
        cur.close()
        logger.info("Tablas creadas/verificadas correctamente (InnoDB).")
    finally:
        conn.close()


def reiniciar_base_datos():
    """Vacía las tablas para que cada ejecución de la demo parta de un
    estado limpio y reproducible (a diferencia de un archivo SQLite, aquí
    no se puede simplemente 'borrar el archivo', así que se hace un
    TRUNCATE de cada tabla)."""
    conn = obtener_conexion()
    try:
        cur = conn.cursor()
        for tabla in ("vuelos", "hoteles", "transportes"):
            cur.execute(f"TRUNCATE TABLE {tabla}")
        conn.commit()
        cur.close()
        logger.info("Tablas vaciadas (TRUNCATE) para reiniciar la demo.")
    except mysql.connector.Error:
        # Si las tablas todavía no existen (primera ejecución), se ignora.
        conn.rollback()
    finally:
        conn.close()


def poblar_datos():
    """Inserta 10 vuelos, 10 hoteles y 10 transportes de prueba, solo si
    las tablas están vacías."""
    conn = obtener_conexion()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM vuelos")
        (n_vuelos,) = cur.fetchone()
        if n_vuelos > 0:
            logger.info("Los datos de prueba ya existen, no se vuelven a poblar.")
            return

        # Semilla fija para que la simulación sea reproducible (los mismos
        # escenarios de éxito/fallo ocurren siempre en la misma ejecución
        # de demostración).
        random.seed(42)

        aerolineas = ["AeroSur", "SkyLine", "AndesFly", "GlobalAir", "PacificJet"]
        ciudades = [
            ("Quito", "Bogotá"), ("Guayaquil", "Lima"), ("Cuenca", "Madrid"),
            ("Quito", "Miami"), ("Guayaquil", "Ciudad de México"),
            ("Quito", "Santiago"), ("Manta", "Panamá"), ("Quito", "Buenos Aires"),
            ("Guayaquil", "Nueva York"), ("Cuenca", "Toronto"),
        ]
        for i in range(1, 11):
            origen, destino = ciudades[i - 1]
            cur.execute(
                "INSERT INTO vuelos (id, origen, destino, aerolinea, "
                "asientos_disponibles, precio) VALUES (%s, %s, %s, %s, %s, %s)",
                (i, origen, destino, random.choice(aerolineas),
                 random.randint(0, 5), round(random.uniform(150, 900), 2)),
            )

        nombres_hoteles = [
            "Hotel Central", "Plaza Suites", "Mar Azul Resort", "Torre Andina",
            "Gran Hotel", "Costa Verde", "Hotel Imperial", "Posada Real",
            "Hotel Continental", "Suites del Valle",
        ]
        # A propósito dejamos algunos hoteles SIN cupo (0 habitaciones)
        # para poder disparar el escenario de compensación.
        habitaciones = [0, 3, 0, 2, 5, 0, 1, 4, 0, 2]
        for i in range(1, 11):
            cur.execute(
                "INSERT INTO hoteles (id, nombre, ciudad, "
                "habitaciones_disponibles, precio_noche) VALUES (%s, %s, %s, %s, %s)",
                (i, nombres_hoteles[i - 1], random.choice(
                    ["Quito", "Bogotá", "Lima", "Madrid", "Miami"]),
                 habitaciones[i - 1], round(random.uniform(40, 250), 2)),
            )

        tipos_transporte = ["Van", "Auto privado", "Bus turístico", "Shuttle"]
        empresas = ["TransCity", "MoveFast", "UrbanTrans", "RideEasy"]
        for i in range(1, 11):
            cur.execute(
                "INSERT INTO transportes (id, tipo, empresa, "
                "vehiculos_disponibles, precio) VALUES (%s, %s, %s, %s, %s)",
                (i, random.choice(tipos_transporte), random.choice(empresas),
                 random.randint(0, 4), round(random.uniform(10, 80), 2)),
            )

        conn.commit()
        cur.close()
        logger.info("Datos de prueba insertados: 10 vuelos, 10 hoteles, 10 transportes.")
    finally:
        conn.close()


def mostrar_inventario():
    """Utilidad de depuración: imprime el estado actual del inventario."""
    conn = obtener_conexion()
    try:
        cur = conn.cursor()
        logger.info("----- INVENTARIO ACTUAL -----")
        cur.execute("SELECT id, origen, destino, asientos_disponibles FROM vuelos ORDER BY id")
        for row in cur.fetchall():
            logger.info(f"Vuelo {row[0]}: {row[1]}->{row[2]} | asientos={row[3]}")
        cur.execute("SELECT id, nombre, habitaciones_disponibles FROM hoteles ORDER BY id")
        for row in cur.fetchall():
            logger.info(f"Hotel {row[0]}: {row[1]} | habitaciones={row[2]}")
        cur.execute("SELECT id, tipo, vehiculos_disponibles FROM transportes ORDER BY id")
        for row in cur.fetchall():
            logger.info(f"Transporte {row[0]}: {row[1]} | vehiculos={row[2]}")
        logger.info("------------------------------")
        cur.close()
    finally:
        conn.close()


 
# Operaciones de negocio (cada una es un "paso" dentro de la transacción)
def reservar_vuelo(conn, id_vuelo):
    cur = conn.cursor()
    cur.execute(
        "UPDATE vuelos SET asientos_disponibles = asientos_disponibles - 1 "
        "WHERE id = %s AND asientos_disponibles > 0",
        (id_vuelo,),
    )
    if cur.rowcount == 0:
        cur.close()
        raise SinCupoException(f"No hay asientos disponibles en el vuelo {id_vuelo}")
    cur.close()
    logger.info(f"[PASO 1] Vuelo {id_vuelo} reservado (asiento descontado).")


def cancelar_vuelo(conn, id_vuelo):
    """Transacción de compensación: revierte la reserva del vuelo,
    devolviendo el asiento al inventario."""
    cur = conn.cursor()
    cur.execute(
        "UPDATE vuelos SET asientos_disponibles = asientos_disponibles + 1 "
        "WHERE id = %s",
        (id_vuelo,),
    )
    cur.close()
    logger.warning(f"[COMPENSACIÓN] Vuelo {id_vuelo} cancelado, asiento liberado.")


def reservar_hotel(conn, id_hotel):
    cur = conn.cursor()
    cur.execute(
        "UPDATE hoteles SET habitaciones_disponibles = habitaciones_disponibles - 1 "
        "WHERE id = %s AND habitaciones_disponibles > 0",
        (id_hotel,),
    )
    if cur.rowcount == 0:
        cur.close()
        raise SinCupoException(f"No hay habitaciones disponibles en el hotel {id_hotel}")
    cur.close()
    logger.info(f"[PASO 2] Hotel {id_hotel} reservado (habitación descontada).")


def reservar_transporte(conn, id_transporte):
    cur = conn.cursor()
    cur.execute(
        "UPDATE transportes SET vehiculos_disponibles = vehiculos_disponibles - 1 "
        "WHERE id = %s AND vehiculos_disponibles > 0",
        (id_transporte,),
    )
    if cur.rowcount == 0:
        cur.close()
        raise SinCupoException(f"No hay vehículos disponibles en el transporte {id_transporte}")
    cur.close()
    logger.info(f"[PASO 3] Transporte {id_transporte} reservado (vehículo descontado).")


 
# Transacción principal con SAVEPOINT + compensación
def reservar_viaje_completo(id_vuelo, id_hotel, id_transporte):
    """
    Ejecuta la reserva de vuelo + hotel + transporte como una transacción
    atómica sobre MySQL/InnoDB.

    Estrategia:
      START TRANSACTION
        reservar_vuelo()
        SAVEPOINT sp_despues_vuelo
        try:
            reservar_hotel()
        except SinCupoException:
            ROLLBACK TO SAVEPOINT sp_despues_vuelo
            RELEASE SAVEPOINT sp_despues_vuelo
            cancelar_vuelo()                -> COMPENSACIÓN explícita
            COMMIT                          -> se confirma que el vuelo
                                              quedó cancelado
            return False
        reservar_transporte()
      COMMIT
      return True
    """
    conn = obtener_conexion()
    try:
        conn.start_transaction()
        logger.info(f"=== Iniciando reserva: vuelo={id_vuelo}, hotel={id_hotel}, transporte={id_transporte} ===")

        # PASO 1: Vuelo
        reservar_vuelo(conn, id_vuelo)

        # Creamos un savepoint justo después de comprar el vuelo.
        cur = conn.cursor()
        cur.execute("SAVEPOINT sp_despues_vuelo")
        cur.close()
        logger.info("SAVEPOINT 'sp_despues_vuelo' creado.")

        try:
            # PASO 2: Hotel
            reservar_hotel(conn, id_hotel)
        except SinCupoException as e:
            logger.error(f"Fallo en reserva de hotel: {e}")

            # Volvemos al estado justo después del vuelo (deshace
            # cualquier cambio parcial que haya podido hacer el paso 2).
            cur = conn.cursor()
            cur.execute("ROLLBACK TO SAVEPOINT sp_despues_vuelo")
            cur.execute("RELEASE SAVEPOINT sp_despues_vuelo")
            cur.close()
            logger.warning("ROLLBACK TO SAVEPOINT ejecutado.")

            # Transacción de compensación: como el vuelo ya se había
            # confirmado dentro de la transacción, debemos revertirlo
            # explícitamente liberando el asiento.
            cancelar_vuelo(conn, id_vuelo)

            conn.commit()
            logger.warning("Transacción finalizada con COMPENSACIÓN. Vuelo cancelado, hotel no disponible.")
            return False, "Sin cupo en el hotel. Vuelo cancelado mediante compensación."

        # PASO 3: Transporte
        reservar_transporte(conn, id_transporte)

        conn.commit()
        logger.info("=== Reserva completa exitosa (COMMIT) ===")
        return True, "Reserva completa exitosa: vuelo, hotel y transporte confirmados."

    except SinCupoException as e:
        # Fallo en vuelo o transporte (no en hotel): se aborta todo.
        conn.rollback()
        logger.error(f"Reserva abortada por falta de disponibilidad: {e}")
        return False, str(e)

    except mysql.connector.Error as e:
        conn.rollback()
        logger.exception(f"Error de MySQL, se hizo ROLLBACK completo: {e}")
        return False, str(e)

    finally:
        conn.close()


 
# Simulación de DEADLOCK REAL (detectado por InnoDB)
# A diferencia de un motor embebido simple, InnoDB mantiene un grafo de
# espera entre transacciones y lo analiza periódicamente. Cuando detecta un
# ciclo (deadlock), elige una transacción "víctima" (normalmente la que ha
# hecho menos trabajo) y la aborta automáticamente con el error 1213,
# liberando sus bloqueos para que la otra transacción pueda continuar.
#
# Para provocar un deadlock real, dos transacciones deben tomar bloqueos de
# fila (SELECT ... FOR UPDATE) sobre los MISMOS DOS registros pero en
# ORDEN INVERSO.
ERROR_DEADLOCK_INNODB = 1213
ERROR_LOCK_WAIT_TIMEOUT_INNODB = 1205


def transaccion_a(id_vuelo, id_hotel):
    """Hilo A: bloquea primero la fila del VUELO y luego intenta bloquear
    la fila del HOTEL."""
    nombre = "Transaccion-A"
    threading.current_thread().name = nombre
    conn = obtener_conexion()
    cur = None
    try:
        conn.start_transaction()
        cur = conn.cursor()

        logger.info(f"{nombre}: intenta bloquear fila VUELO {id_vuelo} (SELECT ... FOR UPDATE)")
        cur.execute("SELECT asientos_disponibles FROM vuelos WHERE id = %s FOR UPDATE", (id_vuelo,))
        cur.fetchall()
        logger.info(f"{nombre}: bloqueó VUELO {id_vuelo}. Esperando antes de pedir HOTEL...")
        time.sleep(1.0)  # da tiempo a que B tome el lock de hotel

        logger.info(f"{nombre}: intenta bloquear fila HOTEL {id_hotel} (SELECT ... FOR UPDATE)")
        cur.execute("SELECT habitaciones_disponibles FROM hoteles WHERE id = %s FOR UPDATE", (id_hotel,))
        cur.fetchall()

        cur.execute(
            "UPDATE vuelos SET asientos_disponibles = asientos_disponibles - 1 WHERE id = %s",
            (id_vuelo,),
        )
        conn.commit()
        logger.info(f"{nombre}: bloqueó HOTEL {id_hotel}. Transacción completada con éxito (COMMIT).")

    except mysql.connector.Error as e:
        conn.rollback()
        if e.errno == ERROR_DEADLOCK_INNODB:
            logger.error(
                f"{nombre}: DEADLOCK DETECTADO POR INNODB (error 1213) -> {e.msg}. "
                f"Esta transacción fue elegida como víctima y se hizo ROLLBACK automático."
            )
        else:
            logger.exception(f"{nombre}: error de MySQL inesperado: {e}")
    finally:
        if cur is not None:
            cur.close()
        conn.close()


def transaccion_b(id_vuelo, id_hotel):
    """Hilo B: bloquea primero la fila del HOTEL y luego intenta bloquear
    la fila del VUELO (orden inverso a A), lo que genera la espera
    circular real que InnoDB detecta como deadlock."""
    nombre = "Transaccion-B"
    threading.current_thread().name = nombre
    conn = obtener_conexion()
    cur = None
    try:
        conn.start_transaction()
        cur = conn.cursor()

        logger.info(f"{nombre}: intenta bloquear fila HOTEL {id_hotel} (SELECT ... FOR UPDATE)")
        cur.execute("SELECT habitaciones_disponibles FROM hoteles WHERE id = %s FOR UPDATE", (id_hotel,))
        cur.fetchall()
        logger.info(f"{nombre}: bloqueó HOTEL {id_hotel}. Esperando antes de pedir VUELO...")
        time.sleep(1.0)  # da tiempo a que A tome el lock de vuelo

        logger.info(f"{nombre}: intenta bloquear fila VUELO {id_vuelo} (SELECT ... FOR UPDATE)")
        cur.execute("SELECT asientos_disponibles FROM vuelos WHERE id = %s FOR UPDATE", (id_vuelo,))
        cur.fetchall()

        cur.execute(
            "UPDATE hoteles SET habitaciones_disponibles = habitaciones_disponibles - 1 WHERE id = %s",
            (id_hotel,),
        )
        conn.commit()
        logger.info(f"{nombre}: bloqueó VUELO {id_vuelo}. Transacción completada con éxito (COMMIT).")

    except mysql.connector.Error as e:
        conn.rollback()
        if e.errno == ERROR_DEADLOCK_INNODB:
            logger.error(
                f"{nombre}: DEADLOCK DETECTADO POR INNODB (error 1213) -> {e.msg}. "
                f"Esta transacción fue elegida como víctima y se hizo ROLLBACK automático."
            )
        else:
            logger.exception(f"{nombre}: error de MySQL inesperado: {e}")
    finally:
        if cur is not None:
            cur.close()
        conn.close()


def simular_deadlock():
    """
    Lanza dos hilos, cada uno con su propia conexión MySQL, que compiten
    por los mismos dos recursos (fila de un vuelo y fila de un hotel) en
    orden inverso mediante SELECT ... FOR UPDATE. InnoDB detecta el ciclo
    de espera en tiempo real (no hace falta ningún timeout manual) y
    aborta automáticamente a una de las dos transacciones con el error
    1213, dejando que la otra continúe.
    """
    logger.info("########## SIMULACIÓN DE DEADLOCK (real, detectado por InnoDB) ##########")
    id_vuelo = 7
    id_hotel = 5

    hilo_a = threading.Thread(target=transaccion_a, args=(id_vuelo, id_hotel), name="Transaccion-A")
    hilo_b = threading.Thread(target=transaccion_b, args=(id_vuelo, id_hotel), name="Transaccion-B")

    hilo_a.start()
    hilo_b.start()

    hilo_a.join()
    hilo_b.join()
    logger.info("########## FIN SIMULACIÓN DE DEADLOCK ##########\n")


 
# Simulación de TIMEOUT REAL (innodb_lock_wait_timeout)
def transaccion_lenta(id_vuelo, segundos_de_espera):
    """
    Abre una transacción, bloquea la fila del vuelo indicado con un
    UPDATE (que toma un bloqueo de escritura por fila en InnoDB) y luego
    "duerme" simulando una operación de larga duración (por ejemplo, un
    proceso externo lento o un usuario que se demora en confirmar el
    pago) antes de hacer COMMIT.
    """
    nombre = threading.current_thread().name
    conn = obtener_conexion()
    cur = None
    try:
        conn.start_transaction()
        cur = conn.cursor()
        cur.execute(
            "UPDATE vuelos SET asientos_disponibles = asientos_disponibles "
            "WHERE id = %s", (id_vuelo,)
        )
        logger.info(f"{nombre}: bloqueo de fila tomado sobre vuelo {id_vuelo}. "
                     f"Simulando operación lenta de {segundos_de_espera}s...")
        time.sleep(segundos_de_espera)
        conn.commit()
        logger.info(f"{nombre}: transacción lenta finalizada y confirmada (COMMIT).")
    except mysql.connector.Error as e:
        conn.rollback()
        logger.exception(f"{nombre}: error en transacción lenta: {e}")
    finally:
        if cur is not None:
            cur.close()
        conn.close()


def transaccion_impaciente(id_vuelo, timeout_segundos):
    """
    Intenta actualizar el mismo vuelo que está bloqueado por la
    transacción lenta, pero con la variable de sesión
    `innodb_lock_wait_timeout` fijada en un valor bajo. Al superarse ese
    tiempo, InnoDB lanza el error 1205 (Lock wait timeout exceeded), que
    es exactamente el comportamiento de un TIMEOUT por espera prolongada
    de un recurso.
    """
    nombre = threading.current_thread().name
    conn = obtener_conexion(lock_wait_timeout=timeout_segundos)
    cur = None
    inicio = time.time()
    try:
        logger.info(f"{nombre}: intentando reservar el vuelo {id_vuelo} "
                     f"(innodb_lock_wait_timeout = {timeout_segundos}s)...")
        conn.start_transaction()
        cur = conn.cursor()
        cur.execute(
            "UPDATE vuelos SET asientos_disponibles = asientos_disponibles - 1 "
            "WHERE id = %s AND asientos_disponibles > 0",
            (id_vuelo,),
        )
        conn.commit()
        logger.info(f"{nombre}: reserva exitosa tras {time.time()-inicio:.2f}s.")
    except mysql.connector.Error as e:
        transcurrido = time.time() - inicio
        if e.errno == ERROR_LOCK_WAIT_TIMEOUT_INNODB:
            logger.error(
                f"{nombre}: TIMEOUT ({transcurrido:.2f}s) -> error 1205: {e.msg}. "
                f"El recurso siguió bloqueado más tiempo del permitido."
            )
        else:
            logger.exception(f"{nombre}: error de MySQL inesperado: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        if cur is not None:
            cur.close()
        conn.close()


def simular_timeout():
    """
    Lanza una transacción lenta que mantiene el bloqueo de escritura
    sobre el vuelo #9 durante 6 segundos, mientras otra transacción con
    `innodb_lock_wait_timeout` de solo 2 segundos intenta reservar el
    mismo vuelo. Se espera que la segunda transacción falle con el error
    1205 tras ~2 segundos.
    """
    logger.info("########## SIMULACIÓN DE TIMEOUT (real, innodb_lock_wait_timeout) ##########")
    id_vuelo_objetivo = 9

    hilo_lento = threading.Thread(
        target=transaccion_lenta, args=(id_vuelo_objetivo, 6),
        name="Transaccion-Lenta",
    )
    hilo_impaciente = threading.Thread(
        target=transaccion_impaciente, args=(id_vuelo_objetivo, 2),
        name="Transaccion-Impaciente",
    )

    hilo_lento.start()
    time.sleep(0.5)  # asegura que el hilo lento tome el lock primero
    hilo_impaciente.start()

    hilo_lento.join()
    hilo_impaciente.join()
    logger.info("########## FIN SIMULACIÓN DE TIMEOUT ##########\n")


 
# Programa principal
def main():
    try:
        crear_tablas()
    except mysql.connector.Error as e:
        if e.errno == errorcode.ER_ACCESS_DENIED_ERROR:
            logger.error("Acceso denegado. Verifica usuario/contraseña en DB_CONFIG.")
        elif e.errno == errorcode.ER_BAD_DB_ERROR:
            logger.error(f"La base de datos '{DB_CONFIG['database']}' no existe. "
                          f"Créala primero (ver README, sección de setup de MySQL).")
        else:
            logger.error(f"No se pudo conectar a MySQL: {e}")
        return

    reiniciar_base_datos()
    poblar_datos()
    mostrar_inventario()

    logger.info("\n########## CASO 1: RESERVA EXITOSA ##########")
    # Vuelo 3, hotel 4 y transporte 3 tienen cupo disponible con la semilla fija.
    exito, mensaje = reservar_viaje_completo(id_vuelo=3, id_hotel=4, id_transporte=3)
    logger.info(f"Resultado caso 1 -> éxito={exito} | {mensaje}\n")

    logger.info("########## CASO 2: HOTEL SIN CUPO (SAVEPOINT + COMPENSACIÓN) ##########")
    # El hotel 3 fue sembrado con 0 habitaciones disponibles a propósito,
    # mientras que el vuelo 6 y el transporte 4 sí tienen cupo, de modo
    # que el fallo ocurre específicamente en el PASO 2 (hotel).
    exito, mensaje = reservar_viaje_completo(id_vuelo=6, id_hotel=3, id_transporte=4)
    logger.info(f"Resultado caso 2 -> éxito={exito} | {mensaje}\n")

    mostrar_inventario()

    simular_deadlock()
    simular_timeout()

    logger.info("Simulación completa. Revise 'reservas.log' para el detalle completo.")


if __name__ == "__main__":
    main()
