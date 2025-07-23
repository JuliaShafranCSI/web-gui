import psycopg
import os
import bcrypt


def create_user(username, email, raw_password):
    password_bytes = raw_password.encode()
    hashed = bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode()

    conn = psycopg.connect(
            dbname=os.getenv("PG_DBNAME", "your_db"),
            user=os.getenv("PG_USER", "your_user"),
            password=os.getenv("PG_PASSWORD", "your_password"),
            host=os.getenv("PG_HOST", "localhost"),
            port=os.getenv("PG_PORT", "5432")
        )
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO public.users (username, email, password_hash)
        VALUES (%s, %s, %s);
    """, (username, email, hashed))
    conn.commit()
    cur.close()
    conn.close()
    print("User created.")



def authenticate_user_sql(username, input_password):
    # Placeholder for SQL authentication logic
    # In a real application, you would query your database here
    # return username == "user" and password == "password"
    try:
        conn = psycopg.connect(
            dbname=os.getenv("PG_DBNAME", "your_db"),
            user=os.getenv("PG_USER", "your_user"),
            password=os.getenv("PG_PASSWORD", "your_password"),
            host=os.getenv("PG_HOST", "localhost"),
            port=os.getenv("PG_PORT", "5432")
        )
        cur = conn.cursor()
        # IMPORTANT: Use parameterized queries to prevent SQL injection
        cur.execute("SELECT password_hash FROM users WHERE username = %s;", (username,))
        result = cur.fetchone()
        cur.close()
        conn.close()

        if not result:
            print("User not found.")
            return False

        
        stored_hash = result[0].encode()
        is_valid = bcrypt.checkpw(input_password.encode(), stored_hash)

        return is_valid




    except Exception as e:
        print(f"Database connection or query error: {e}")
        return False
    
def load_env(env_path):
    """Loads environment variables from a .env file if it exists.
    Environment variables are set using os.environ.setdefault,
    meaning existing system environment variables take precedence.
    """
    

    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                # Ignore comments and empty lines, and ensure it's a key-value pair
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    # Set the environment variable only if it's not already set
                    os.environ.setdefault(key.strip(), value.strip())
        # Stop after finding and loading the first .env file
        return
    
if __name__ == "__main__":
    load_env("./.env")
    print(f"PG_DBNAME: {os.getenv('PG_DBNAME')}")
    print(f"PG_USER: {os.getenv('PG_USER')}")
    print(f"PG_PASSWORD: {os.getenv('PG_PASSWORD')}")
    print(f"PG_HOST: {os.getenv('PG_HOST')}")
    print(f"PG_PORT: {os.getenv('PG_PORT')}")


