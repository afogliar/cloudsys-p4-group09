"""Persistent, category-scoped training orchestration; never publishes a model."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from psycopg.types.json import Jsonb

from app.database import connect, initialize_database
from app.models import initialize_model_tables, model_metadata, read_model
from app.storage import create_s3_client, get_bucket_name

KIT_ROOT = Path("/opt/kit")
DATA_ROOT = Path("/data")


def settings():
    return json.loads((KIT_ROOT / "meters.json").read_text())


def initialize_training():
    initialize_database()
    initialize_model_tables()
    with connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS training_runs (
                run_id UUID PRIMARY KEY,
                category TEXT NOT NULL CHECK (category IN ('cons','pros')),
                candidate_version INTEGER NOT NULL,
                base_version INTEGER NOT NULL,
                simulated_at DATE NOT NULL,
                trigger_reason TEXT NOT NULL,
                status TEXT NOT NULL CHECK
                  (status IN ('running','completed','failed','interrupted')),
                batch_ids JSONB NOT NULL,
                new_batch_ids JSONB NOT NULL,
                artifact_prefix TEXT NOT NULL,
                release_decision JSONB,
                error_type TEXT,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at TIMESTAMPTZ,
                UNIQUE (category, candidate_version)
            )
        """)
    if os.environ.get("MLFLOW_TRACKING_URI"):
        from trainer.publication import initialize_publication
        initialize_publication()


def trigger_reason(new_batches, simulated_at, previous_at, loop):
    if (len(new_batches) >= loop["trigger_min_days"] and
            len({b["device_id"] for b in new_batches}) >= loop["trigger_min_meters"]):
        return "threshold"
    if new_batches and (simulated_at - previous_at).days >= loop["fallback_days"]:
        return "fallback"
    return None


def observed_clock(config):
    """Next midnight after the latest received completed replay day; never wall time."""
    start, end = map(date.fromisoformat, config["split"]["B"])
    with connect() as conn:
        row = conn.execute("""
            SELECT MAX(day) FROM (
                SELECT day FROM daily_metrics WHERE day BETWEEN %s AND %s
                UNION ALL
                SELECT day FROM lowperf_batches
                WHERE status = 'stored' AND day BETWEEN %s AND %s
            ) AS progress
        """, (start, end, start, end)).fetchone()
    return row[0] + timedelta(days=1) if row[0] else start


def snapshot(category, simulated_at, config):
    start = date.fromisoformat(config["split"]["B"][0])
    with connect() as conn:
        # Count days per meter, not retries or different IDs for the same day.
        rows = conn.execute("""
            SELECT DISTINCT ON (device_id, day)
                batch_id, device_id, day, object_key, payload_sha256
            FROM lowperf_batches
            WHERE category = %s AND status = 'stored' AND day >= %s AND day < %s
            ORDER BY device_id, day, stored_at, batch_id
        """, (category, start, simulated_at)).fetchall()
        completed = conn.execute("""
            SELECT simulated_at, batch_ids FROM training_runs
            WHERE category = %s AND status = 'completed'
        """, (category,)).fetchall()
    batches = [dict(batch_id=str(r[0]), device_id=r[1], day=r[2].isoformat(),
                    object_key=r[3], sha256=r[4]) for r in rows]
    consumed = {batch_id for _, ids in completed for batch_id in ids}
    previous_at = max((r[0] for r in completed), default=start)
    return batches, [b for b in batches if b["batch_id"] not in consumed], previous_at


def run_kit(command, log_path, lock_connection, timeout):
    """Stop the child if the lock session is lost or the training times out."""
    with log_path.open("wb") as log:
        process = subprocess.Popen(command, cwd=KIT_ROOT, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                lock_connection.execute("SELECT 1")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Training timeout")
                time.sleep(0.5)
            if process.returncode != 0:
                raise RuntimeError(f"Kit failed with exit code {process.returncode}; see training.log")
            lock_connection.execute("SELECT 1")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def execute_run(run_id, category, base, batches, prefix, lock_connection, state_root, timeout):
    folder = state_root / "runs" / str(run_id)
    inputs = folder / "inputs"
    inputs.mkdir(parents=True, exist_ok=False)
    client = create_s3_client()
    bucket = get_bucket_name()
    manifest = {"run_id": str(run_id), "category": category,
                "base_version": base["version"], "batches": batches}
    manifest_bytes = json.dumps(manifest, sort_keys=True, indent=2).encode()
    (folder / "manifest.json").write_bytes(manifest_bytes)
    for batch in batches:
        response = client.get_object(Bucket=bucket, Key=batch["object_key"])
        with response["Body"] as body:
            content = body.read()
        if hashlib.sha256(content).hexdigest() != batch["sha256"]:
            raise ValueError("Batch integrity mismatch")
        (inputs / (batch["batch_id"] + ".json")).write_bytes(content)
    current = folder / "current" / f"v{base['version']:04d}" / "model.txt"
    current.parent.mkdir(parents=True)
    current.write_bytes(read_model(base))
    output = folder / "output"
    command = [
        sys.executable, "-m", "p4kit.train", "--category", category,
        "--data-dir", str(DATA_ROOT),
        "--weather", str(DATA_ROOT / "weather_fribourg_15min.parquet"),
        "--meters", str(KIT_ROOT / "meters.json"),
        "--extra-days", str(inputs), "--current-model", str(current),
        "--out", str(output), "--seed", "42",
    ]
    run_kit(command, folder / "training.log", lock_connection, timeout)
    record = json.loads((output / "metrics.json").read_text())
    model_content = (output / "model.txt").read_bytes()
    if record["category"] != category or hashlib.sha256(model_content).hexdigest() != record["model_sha256"]:
        raise ValueError("Training output integrity mismatch")
    lineage = record["lineage"]
    if set(lineage["batches_used"] + lineage["batches_held_out"]) != {b["batch_id"] for b in batches}:
        raise ValueError("Training lineage mismatch")
    # Strict JSON: a candidate with non-finite validation metrics needs investigation.
    metrics_bytes = json.dumps(record, allow_nan=False, indent=2).encode()
    lock_connection.execute("SELECT 1")
    for name, content, content_type in (
        ("model.txt", model_content, "text/plain"),
        ("metrics.json", metrics_bytes, "application/json"),
        ("manifest.json", manifest_bytes, "application/json"),
    ):
        client.put_object(Bucket=bucket, Key=prefix + "/" + name,
                          Body=content, ContentType=content_type)
    return record


def process_category(category, state_root=Path("/state"), timeout=600):
    config = settings()
    if category not in config["categories"]:
        raise ValueError("Unknown category")
    with connect() as lock:
        lock.autocommit = True
        key = 1 if category == "cons" else 2
        if not lock.execute("SELECT pg_try_advisory_lock(%s, %s)", (94009, key)).fetchone()[0]:
            return {"category": category, "status": "locked"}
        # A running record with no surviving category lock belongs to a dead worker.
        with connect() as conn:
            conn.execute("""
                UPDATE training_runs SET status='interrupted', finished_at=NOW(),
                    error_type='WorkerInterrupted'
                WHERE category=%s AND status='running'
            """, (category,))
        if os.environ.get("MLFLOW_TRACKING_URI"):
            from trainer.publication import sync_category_locked
            # Finish any previous publication before selecting the next baseline.
            sync_category_locked(category)
        now = observed_clock(config)
        batches, new_batches, previous = snapshot(category, now, config)
        reason = trigger_reason(new_batches, now, previous, config["loop"])
        if reason is None:
            return {"category": category, "status": "idle", "simulated_at": now.isoformat(),
                    "new_days": len(new_batches)}
        base = model_metadata(category)
        run_id = uuid4()
        with connect() as conn:
            version = conn.execute("""
                SELECT COALESCE(MAX(version), 1) + 1 FROM (
                    SELECT version FROM model_versions WHERE category=%s
                    UNION ALL
                    SELECT candidate_version FROM training_runs WHERE category=%s
                ) versions
            """, (category, category)).fetchone()[0]
            prefix = f"models/{category}/v{version:04d}"
            conn.execute("""
                INSERT INTO training_runs
                  (run_id, category, candidate_version, base_version, simulated_at,
                   trigger_reason, status, batch_ids, new_batch_ids, artifact_prefix)
                VALUES (%s,%s,%s,%s,%s,%s,'running',%s,%s,%s)
            """, (run_id, category, version, base["version"], now, reason,
                  Jsonb([b["batch_id"] for b in batches]),
                  Jsonb([b["batch_id"] for b in new_batches]), prefix))
        try:
            record = execute_run(run_id, category, base, batches, prefix, lock,
                                 state_root, timeout)
            lock.execute("SELECT 1")
            with connect() as conn:
                conn.execute("""
                    UPDATE training_runs SET status='completed', finished_at=NOW(),
                        release_decision=%s WHERE run_id=%s
                """, (Jsonb(record["release"]), run_id))
        except Exception as exc:
            # No batches are consumed by a failed attempt. Retry gets a fresh version
            # and output folder, preserving the kit's no-overwrite guarantee.
            try:
                with connect() as conn:
                    conn.execute("""
                        UPDATE training_runs SET status='failed', finished_at=NOW(),
                            error_type=%s WHERE run_id=%s
                    """, (type(exc).__name__, run_id))
            except Exception:
                pass  # Surviving 'running' record is recovered after DB reconnect.
            raise
        result = {"category": category, "status": "completed", "run_id": str(run_id),
                  "candidate_version": version, "trigger": reason,
                  "release": record["release"]}
        # Tracking failure must never turn a completed training into a failed run.
        if os.environ.get("MLFLOW_TRACKING_URI"):
            try:
                sync_category_locked(category)
                result["publication"] = "released" if record["release"]["released"] else "rejected"
            except Exception as exc:
                result["publication"] = "pending"
                result["publication_error_type"] = type(exc).__name__
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--category", choices=("cons", "pros"))
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=600)
    args = parser.parse_args()
    if args.poll_seconds <= 0 or args.timeout_seconds <= 0:
        parser.error("Intervals must be positive")
    while True:
        try:
            initialize_training()
            for category in ([args.category] if args.category else ("cons", "pros")):
                try:
                    print(json.dumps(process_category(category, timeout=args.timeout_seconds)), flush=True)
                except Exception as exc:
                    print(json.dumps({"category": category, "status": "error",
                                      "error_type": type(exc).__name__}), flush=True)
                    if args.once:
                        return 1
            if args.once:
                return 0
        except Exception as exc:
            print(json.dumps({"status": "initialization_error", "error_type": type(exc).__name__}), flush=True)
            if args.once:
                return 1
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
