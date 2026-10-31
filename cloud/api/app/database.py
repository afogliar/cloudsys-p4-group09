import os

import psycopg
from psycopg.types.json import Jsonb

from app.schemas import DailyMetrics


def connect():
    return psycopg.connect(
        host="postgres",
        port=5432,
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        connect_timeout=5,
    )


def initialize_database():
    with connect() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS daily_metrics (
                device_id TEXT NOT NULL,
                day DATE NOT NULL,
                category TEXT NOT NULL
                    CHECK (category IN ('cons', 'pros')),
                model_version INTEGER NOT NULL,
                payload JSONB NOT NULL,
                received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (device_id, day)
            )
        """)


def save_daily_metrics(metrics: DailyMetrics):
    payload = metrics.model_dump(mode="json")

    with connect() as connection:
        connection.execute(
            """
            INSERT INTO daily_metrics (
                device_id,
                day,
                category,
                model_version,
                payload
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (device_id, day) DO NOTHING
            """,
            (
                metrics.device_id,
                metrics.day,
                metrics.category,
                metrics.model_version,
                Jsonb(payload),
            ),
        )


if __name__ == "__main__":
    initialize_database()
    print("Table daily_metrics prête")