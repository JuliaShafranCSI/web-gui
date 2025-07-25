import psycopg
import os
from User_Authentication import load_env
from psycopg_pool import ConnectionPool
from datetime import datetime, timezone # Import datetime and timezone
import time

# Global connection pool
pool = None

def get_connection_pool():
    global pool
    if pool is None:
        min_conn = int(os.getenv("PG_MIN_CONNECTIONS", "1"))
        max_conn = int(os.getenv("PG_MAX_CONNECTIONS", "10"))
        try:
            pool = ConnectionPool(
                min_size=min_conn,
                max_size=max_conn,
                kwargs={
                    "dbname": os.getenv("PG_DBNAME", "your_db"),
                    "user": os.getenv("PG_USER", "your_user"),
                    "password": os.getenv("PG_PASSWORD", "your_password"),
                    "host": os.getenv("PG_HOST", "localhost"),
                    "port": os.getenv("PG_PORT", "5432")
                }
            )
        except Exception as e:
            print(f"Failed to create connection pool: {e}")
            pool = None # Ensure pool is None if creation fails
    return pool

def close_connection_pool():
    global pool
    if pool:
        pool.close()
        pool = None

def get_utc_now():
    """Returns the current timestamp in UTC."""
    return datetime.now(timezone.utc)

def insert_stalls_in_stalls_table(lot_id, stall_number, stall_type, current_status, is_operational):
    if not isinstance(lot_id, int):
        print("Error: lot_id must be an integer.")
        return 1
    if not isinstance(stall_number, int):
        print("Error: stall_number must be an integer.")
        return 1
    
    valid_stall_types = ['Regular', 'Accessible', 'EV Charging', 'Motorcycle']
    if stall_type not in valid_stall_types:
        print(f"Error: stall_type must be one of {valid_stall_types}.")
        return 1

    valid_current_statuses = ['Vacant', 'Occupied']
    if current_status not in valid_current_statuses:
        print(f"Error: current_status must be one of {valid_current_statuses}.")
        return 1

    if not isinstance(is_operational, bool):
        print("Error: is_operational must be a boolean.")
        return 1
    
    pool = get_connection_pool()
    if pool is None:
        print("Error: Database connection pool not initialized.")
        return 1

    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO public.stalls (lot_id, stall_number, stall_type, current_status, is_operational)
            VALUES (%s, %s, %s, %s, %s);
        """, (lot_id, stall_number, stall_type, current_status, is_operational))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"SQL command execution error: {e}")
        return 1
    finally:
        if cur:
            cur.close()
        if conn:
            pool.putconn(conn)

    return 0

def reset_all_stalls():
    pool = get_connection_pool()
    if pool is None:
        print("Error: Database connection pool not initialized.")
        return 1

    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute("""
            UPDATE public.stalls
            SET current_status = %s, is_operational = %s;
        """, ("Vacant", True))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"Database update error: {e}")
        return 1
    finally:
        if cur:
            cur.close()
        if conn:
            pool.putconn(conn)
    
    return 0

def get_all_vacant_stall_number_from_db(lot_id):
    pool = get_connection_pool()
    if pool is None:
        print("Error: Database connection pool not initialized.")
        return []

    conn = None
    cur = None
    try:
        conn = pool.getconn()
        conn.autocommit = True  # Set autocommit to True for read-only operation
        cur = conn.cursor()
        cur.execute("""
            SELECT stall_number FROM public.stalls
             WHERE current_status = %s AND lot_id = %s
             ORDER BY stall_number;
        """, ("Vacant", lot_id))
        
        rows = cur.fetchall()
        vacant_stalls = [int(row[0]) for row in rows]
        return vacant_stalls
    except Exception as e:
        print(f"SQL command execution error: {e}")
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            pool.putconn(conn)

def get_stall_id_using_stall_number_and_lot_id(stall_number, lot_id):
    if not isinstance(stall_number, str):
        print("Error: stall_number must be a string.")
        return []
    if not isinstance(lot_id, int):
        print("Error: lot_id must be an integer.")
        return []
    pool = get_connection_pool()
    if pool is None:
        print("Error: Database connection pool not initialized.")
        return []

    conn = None
    cur = None
    try:
        conn = pool.getconn()
        conn.autocommit = True  # Set autocommit to True for read-only operation
        cur = conn.cursor()

        cur.execute("SELECT stall_id FROM public.stalls WHERE stall_number = %s AND lot_id = %s;", (stall_number, lot_id))
        stall_id = cur.fetchone()[0]
        return stall_id

    except Exception as e:
        print(f"SQL command execution error: {e}")
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            pool.putconn(conn)



def start_session(db_stall_id, timestamp_now,vehicle_identifier="default"):
    global pool
    pool = get_connection_pool()
    if pool is None:
        print("Error: Database connection pool not initialized.")
        return 1
    conn = None
    cur = None
    try:
        conn = pool.getconn()            
        cur = conn.cursor()

        cur.execute("""INSERT INTO public.parkingsessions 
                    (stall_id, entry_timestamp, vehicle_identifier) 
                    VALUES (%s,%s,%s);""", 
                    (db_stall_id, timestamp_now, vehicle_identifier))
        conn.commit()
        return 0
    except Exception as e:
        print(f"SQL command execution error: {e}")
        return 1
    finally:
        if cur:
            cur.close()
        if conn:
            pool.putconn(conn)

def end_session(db_stall_id, timestamp_now):
    global pool
    pool = get_connection_pool()
    if pool is None:
        print("Error: Database connection pool not initialized.")
        return 1
    conn = None
    cur = None
    try:
        conn = pool.getconn()
        cur = conn.cursor()
        cur.execute("""UPDATE public.parkingsessions
                    SET exit_timestamp = %s
                    WHERE session_id = (
                        SELECT session_id
                        FROM public.parkingsessions
                        WHERE stall_id = %s
                        AND exit_timestamp IS NULL
                        ORDER BY entry_timestamp DESC
                        LIMIT 1
                    );""", 
                    (timestamp_now, db_stall_id))
        conn.commit() # Corrected from conn.commit
        return 0
    except Exception as e:
        print(f"SQL command execution error: {e}")
        return 1
    finally:
        if cur:
            cur.close()
        if conn:
            pool.putconn(conn)

def update_stall_status(lot_id, db_stall_id, status):
    if not isinstance(lot_id, int):
        print("The first arguement must be an integer")
        return 1
    if not isinstance(db_stall_id, int):
        print("The second arguement must be an integer")
        return 1
    acceptable_status = ["Vacant", "Occupied"]
    if status not in acceptable_status:
        print("Status must be 'Vacant' or 'Occupied'")
        return 1
    global pool
    pool = get_connection_pool()
    if pool is None:
        print("Error: Database connection pool not initialized.")
        return 1
    conn = None
    cur = None
    try:
        conn = pool.getconn()            
        cur = conn.cursor()
        cur.execute("""UPDATE public.stalls SET current_status = %s 
                    WHERE lot_id = %s and stall_id = %s;""", (status, lot_id, db_stall_id))
        conn.commit()
        return 0
    except Exception as e:
        print(f"SQL command execution error: {e}")
        return 1
    finally:
        if cur:
            cur.close()
        if conn:
            pool.putconn(conn)


    


if __name__=='__main__':
    load_env("gui/.env")
    print(f"PG_DBNAME: {os.getenv('PG_DBNAME')}")
    print(f"PG_USER: {os.getenv('PG_USER')}")
    print(f"PG_PASSWORD: {os.getenv('PG_PASSWORD')}")
    print(f"PG_HOST: {os.getenv('PG_HOST')}")
    print(f"PG_PORT: {os.getenv('PG_PORT')}")
    
    result = reset_all_stalls()
    if result == 0:
        print("Reset successful.")

    # vacant_list = get_all_vacant_stall_number_from_db(1)
    # print(vacant_list)
    
    # print(get_stall_id_using_stall_number_and_lot_id("0",1))

    # start_session(1, datetime.now(timezone.utc),vehicle_identifier="default")
    # print("Session started")
    # time.sleep(3)
    # end_session(1, datetime.now(timezone.utc))
    # print("Session stopped")

    # result=update_stall_status(1,1,"Occupied")
    # if result ==0:
    #     print("Status updated")



    close_connection_pool()


        
