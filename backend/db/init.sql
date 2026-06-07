
CREATE TABLE IF NOT EXISTS train_data (
    id SERIAL PRIMARY KEY,
    query_time TIMESTAMP,
    planned_arrival TIMESTAMP,
    actual_arrival TIMESTAMP,
    planned_destination TIMESTAMP,
    actual_destination TIMESTAMP,
    train VARCHAR(255),
    cancellation BOOLEAN
);