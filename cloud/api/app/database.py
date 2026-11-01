import hashlib
import json
import os

import psycopg
from psycopg.types.json import Jsonb

from app.schemas import DailyMetrics, LowPerformanceBatch
from app.storage import put_batch_json


def connect():
    return psycopg.connect(
        host=os.environ.get("POSTGRES_HOST", "postgres"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
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
        connection.execute("""
            CREATE TABLE IF NOT EXISTS lowperf_batches (
                batch_id UUID PRIMARY KEY,
                device_id TEXT NOT NULL,
                category TEXT NOT NULL CHECK (category IN ('cons', 'pros')),
                day DATE NOT NULL,
                model_version INTEGER NOT NULL CHECK (model_version >= 1),
                object_key TEXT NOT NULL UNIQUE,
                payload_sha256 TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'stored')),
                received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                stored_at TIMESTAMPTZ,
                CHECK ((status = 'stored') = (stored_at IS NOT NULL))
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS lowperf_stored_category_idx
            ON lowperf_batches (category, stored_at)
            WHERE status = 'stored'
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


class BatchConflictError(Exception):
    """A batch ID is immutable once reserved, even if its upload is pending."""


def save_low_performance_batch(batch: LowPerformanceBatch) -> bool:
    """Return True when finalized now, False for an already stored retry.

    Reserve the identity durably, then serialize upload/finalization with a row
    lock. Failures leave a pending row for an identical edge outbox retry.
    """
    content = json.dumps(
        batch.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    payload_hash = hashlib.sha256(content).hexdigest()
    object_key = (
        f"lowperf/category={batch.category}/device={batch.device_id}/"
        f"day={batch.day.isoformat()}/{batch.batch_id}.json"
    )

    # Commit this reservation separately so a failed S3 upload cannot release
    # the ID for different content. No payload or credentials are logged.
    with connect() as connection:
        connection.execute("SET LOCAL lock_timeout = '5s'")
        connection.execute(
            """
            INSERT INTO lowperf_batches (
                batch_id, device_id, category, day, model_version,
                object_key, payload_sha256
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (batch_id) DO NOTHING
            """,
            (batch.batch_id, batch.device_id, batch.category, batch.day,
             batch.model_version, object_key, payload_hash),
        )

    with connect() as connection:
        connection.execute("SET LOCAL lock_timeout = '5s'")
        existing = connection.execute(
            """
            SELECT payload_sha256, status FROM lowperf_batches
            WHERE batch_id = %s FOR UPDATE
            """,
            (batch.batch_id,),
        ).fetchone()
        if existing is None:
            raise RuntimeError("Batch reservation disappeared")
        if existing[0] != payload_hash:
            raise BatchConflictError("Ce batch_id existe avec un contenu différent")
        if existing[1] == "stored":
            return False

        put_batch_json(object_key, content)
        connection.execute(
            """
            UPDATE lowperf_batches SET status = 'stored', stored_at = NOW()
            WHERE batch_id = %s
            """,
            (batch.batch_id,),
        )
    return True


if __name__ == "__main__":
    initialize_database()
    print("Tables daily_metrics et lowperf_batches prêtes")
