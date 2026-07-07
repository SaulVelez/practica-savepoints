import mysql.connector

try:
    conn = mysql.connector.connect(
        host="localhost",
        port=3306,
        database="ejercicio_login",
        user="admin",
        password="Saulito123.0"
    )

    conn.autocommit = False
    cur = conn.cursor()

    print("Iniciando transacción principal...")

    # Operación 1
    cur.execute("""
        UPDATE cuentas
        SET saldo = saldo + 100
        WHERE nombre = 'Juan'
    """)

    # Crear checkpoint (SAVEPOINT)
    cur.execute("SAVEPOINT sp_transferencia")

    try:
        print("Realizando segunda operación...")

        # Operación 2
        cur.execute("""
            UPDATE cuentas
            SET saldo = saldo + 100
            WHERE nombre = 'Maria'
        """)

        # Simular error
        raise Exception("Error durante la transferencia")

        cur.execute("RELEASE SAVEPOINT sp_transferencia")

    except Exception as e:
        print(f"Error detectado: {e}")

        # Regresar al checkpoint
        cur.execute("ROLLBACK TO SAVEPOINT sp_transferencia")

        print("Se revirtió únicamente la segunda operación")

    # Continuar con otras operaciones
    cur.execute("""
        INSERT INTO cuentas(nombre, saldo)
        VALUES ('John', 400)
    """)

    # Confirmar toda la transacción
    conn.commit()
    print("Transacción principal confirmada")

except Exception as e:
    conn.rollback()
    print(f"Error fatal: {e}")

finally:
    cur.close()
    conn.close()