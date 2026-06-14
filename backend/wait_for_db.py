import os
import time
import sys
import psycopg2

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_NAME = os.environ.get("DB_NAME", "postgres")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

def wait_for_db(retries=60, delay=2):
    attempt = 0
    while True:
        try:
            conn = psycopg2.connect(
                host=DB_HOST,
                dbname=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD,
                connect_timeout=5,
            )
            conn.close()
            print("Database is available")
            return 0
        except Exception as e:
            attempt += 1
            print(f"Database unavailable (attempt {attempt}): {e}")
            if retries and attempt >= retries:
                print("Exceeded max retries, exiting with error")
                return 1
            time.sleep(delay)

if __name__ == "__main__":
    rc = wait_for_db()
    sys.exit(rc)
