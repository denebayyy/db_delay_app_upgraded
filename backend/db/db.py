from psycopg2 import sql
import psycopg2
import os

conn = psycopg2.connect(
    host=os.environ["DB_HOST"],
    dbname=os.environ["DB_NAME"],
    user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
)

def insert_data(data):
    with conn.cursor() as cur:
        insert_query = sql.SQL("""
            INSERT INTO train_data (id, query_time, planned_arrival, actual_arrival, planned_destination, actual_destination, train, cancellation, trip_information)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """)
        cur.execute(insert_query, (
            data['id'],
            data['query_time'],
            data['planned_arrival'],
            data['actual_arrival'],
            data['planned_destination'],
            data['actual_destination'],
            data['train'],
            data['cancellation'],
            data['trip_information']
        ))
        conn.commit()

def remove_data():
    # Dummy function to remove all the stored data.
    # Truncate resets the table, Delete only row-by-row
    with conn.cursor() as cur:
        truncate_query = sql.SQL("TRUNCATE TABLE train_data RESTART IDENTITY")
        cur.execute(truncate_query)
        conn.commit()

def get_data_debug():
    with conn.cursor() as cur:
        # get all the data
        get_query = sql.SQL("""
        SELECT *
        FROM train_data
        """)
        data = cur.execute(get_query)
        data = cur.fetchall()
        return data

def save_job(job_config):
    """Insert or update a job configuration."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO jobs_config (id, from_station, to_station, interval, enabled, max_retries, timeout)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                from_station = EXCLUDED.from_station,
                to_station = EXCLUDED.to_station,
                interval = EXCLUDED.interval,
                enabled = EXCLUDED.enabled,
                max_retries = EXCLUDED.max_retries,
                timeout = EXCLUDED.timeout
        """, (job_config.id, job_config.from_station, job_config.to_station, 
              job_config.interval, job_config.enabled, job_config.max_retries, job_config.timeout))
        conn.commit()

def load_all_jobs():
    """Load all saved job configurations from database."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, from_station, to_station, interval, enabled, max_retries, timeout
            FROM jobs_config
        """)
        rows = cur.fetchall()
        return rows

def delete_job_config(job_id):
    """Delete a job configuration from database."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM jobs_config WHERE id = %s", (job_id,))
        conn.commit()