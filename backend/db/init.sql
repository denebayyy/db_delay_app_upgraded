
CREATE TABLE IF NOT EXISTS train_data (
    id VARCHAR(50),
    query_time TIMESTAMP,
    planned_arrival TIMESTAMP,
    actual_arrival TIMESTAMP,
    planned_destination TIMESTAMP,
    actual_destination TIMESTAMP,
    train VARCHAR(255),
    cancellation BOOLEAN,
    trip_information VARCHAR(255)
);

CREATE TABLE IF NOT EXISTS jobs_config (
    id VARCHAR(50) PRIMARY KEY,
    from_station VARCHAR(255) NOT NULL,
    to_station VARCHAR(255) NOT NULL,
    interval FLOAT NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    max_retries INT DEFAULT 3,
    timeout FLOAT DEFAULT 120.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);