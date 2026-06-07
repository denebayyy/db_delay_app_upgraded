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
            INSERT INTO train_data (id, query_time, planned_arrival, actual_arrival, planned_destination, actual_destination, train, cancellation)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """)
        cur.execute(insert_query, (
            data['id'],
            data['query_time'],
            data['planned_arrival'],
            data['actual_arrival'],
            data['planned_destination'],
            data['actual_destination'],
            data['train'],
            data['cancellation']
        ))
        conn.commit()