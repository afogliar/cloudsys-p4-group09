import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from app import database, models, storage
from app.schemas import LowPerformanceBatch
from p4kit.metrics import low_performance
from trainer import worker

LOOP = dict(trigger_min_days=8, trigger_min_meters=2, fallback_days=14)


@pytest.mark.parametrize("count,meters,elapsed,expected", [
    (0, 0, 30, None), (1, 1, 13, None), (1, 1, 14, "fallback"),
    (7, 2, 1, None), (8, 1, 1, None), (8, 2, 1, "threshold"),
    (8, 1, 14, "fallback"), (8, 2, 14, "threshold"),
])
def test_trigger_boundaries(count, meters, elapsed, expected):
    batches = [dict(device_id=str(i % max(meters, 1))) for i in range(count)]
    start = date(2026, 4, 1)
    assert worker.trigger_reason(batches, start + timedelta(days=elapsed), start, LOOP) == expected


@pytest.fixture
def environment():
    import os
    assert os.environ["POSTGRES_DB"] == "p4_trainer_tests"
    assert storage.get_bucket_name() == "p4-trainer-tests"
    worker.initialize_training()
    with database.connect() as conn:
        conn.execute("TRUNCATE training_runs, lowperf_batches, daily_metrics, model_releases, model_versions")
    models.bootstrap(Path("/opt/kit"))


def seed(category, count=8, offset=0):
    """Real measurements/history; intentionally bad forecasts ONLY for test triggering."""
    config = worker.settings()
    ids = config["categories"][category]["meters"][:2]
    batches = []
    for meter in ids:
        frame = pd.read_parquet(f"/data/meters/meter_{meter}.parquet")
        days = frame["timestamp"].dt.normalize()
        candidates = sorted(days[days >= pd.Timestamp("2026-04-01")].unique())
        for day in candidates[offset:]:
            actual = frame.loc[days == day, "ap_kw"].to_numpy()
            origins = frame.loc[days == day, "ap_kw_origin"].to_numpy()
            history = frame.loc[(days < day) & (days >= day - pd.Timedelta(days=7))]
            hist = history["ap_kw"].to_numpy()
            hist_origin = history["ap_kw_origin"].to_numpy()
            forecast = np.nan_to_num(actual, nan=0.0) + 100.0
            result = low_performance(actual, origins, forecast, hist[:96], hist_origin[:96], 0)
            if not result["evaluated"] or not result["flagged"]:
                continue
            clean = lambda a: [None if not np.isfinite(v) else float(v) for v in a]
            payload = dict(
                batch_id=str(uuid4()), device_id=meter, category=category,
                model_version=1, day=pd.Timestamp(day).date().isoformat(),
                mae_kw=result["mae_model_kw"], mae_persistence_kw=result["mae_persistence_kw"],
                forecast_kw=forecast.tolist(), actual_kw=clean(actual),
                actual_origin=origins.tolist(), history_kw=clean(hist),
                history_origin=hist_origin.tolist(), sent_at="2026-11-01T12:00:00Z",
            )
            batch = LowPerformanceBatch(**payload)
            database.save_low_performance_batch(batch)
            batches.append(batch)
            if len(batches) % 4 == 0 or len(batches) == count:
                break
        if len(batches) == count:
            return batches
    raise AssertionError("Could not construct test batches")


def fake_execution(*args, **kwargs):
    return {"release": {"released": False, "reason": "test rejected candidate"}}


@pytest.mark.parametrize("category", ["cons", "pros"])
def test_real_training_both_categories(environment, tmp_path, category):
    seed(category)
    result = worker.process_category(category, tmp_path)
    assert result["status"] == "completed" and result["trigger"] == "threshold"
    assert result["candidate_version"] == 2
    assert isinstance(result["release"]["released"], bool)
    folder = tmp_path / "runs" / result["run_id"] / "output"
    record = json.loads((folder / "metrics.json").read_text())
    assert record["seed"] == 42
    assert record["num_boost_round"] == worker.settings()["categories"][category]["num_boost_round"]
    assert len(record["lineage"]["batches_used"]) == 6
    assert len(record["lineage"]["batches_held_out"]) == 2
    key = f"models/{category}/v0002/"
    client = storage.create_s3_client()
    for name in ("model.txt", "metrics.json", "manifest.json"):
        assert client.head_object(Bucket=storage.get_bucket_name(), Key=key + name)["ContentLength"] > 0
    assert hashlib.sha256((folder / "model.txt").read_bytes()).hexdigest() == record["model_sha256"]
    assert models.model_metadata(category)["version"] == 1  # No publication in this block.
    again = worker.process_category(category, tmp_path)
    assert again["status"] == "idle" and again["new_days"] == 0


def test_empty_and_out_of_replay_metrics_do_not_trigger(environment, tmp_path):
    assert worker.process_category("cons", tmp_path)["status"] == "idle"
    with database.connect() as conn:
        conn.execute("""INSERT INTO daily_metrics
            (device_id,day,category,model_version,payload)
            VALUES ('27afc1ed','2099-01-01','cons',1,'{}')""")
    assert worker.observed_clock(worker.settings()) == date(2026, 4, 1)


def test_fallback_uses_simulated_days(environment, tmp_path, monkeypatch):
    seed("cons", count=1)
    monkeypatch.setattr(worker, "execute_run", fake_execution)
    assert worker.process_category("cons", tmp_path)["status"] == "idle"
    with database.connect() as conn:
        conn.execute("""INSERT INTO daily_metrics
            (device_id,day,category,model_version,payload)
            VALUES ('27afc1ed','2026-04-14','cons',1,'{}')""")
    result = worker.process_category("cons", tmp_path)
    assert result["status"] == "completed" and result["trigger"] == "fallback"


def test_failed_run_does_not_consume_and_version_is_not_reused(environment, tmp_path, monkeypatch):
    seed("cons")
    def fail(*args):
        raise RuntimeError("injected training failure")
    monkeypatch.setattr(worker, "execute_run", fail)
    with pytest.raises(RuntimeError):
        worker.process_category("cons", tmp_path)
    monkeypatch.setattr(worker, "execute_run", fake_execution)
    result = worker.process_category("cons", tmp_path)
    assert result["candidate_version"] == 3 and result["status"] == "completed"
    with database.connect() as conn:
        assert [r[0] for r in conn.execute("SELECT status FROM training_runs ORDER BY candidate_version")] == ["failed", "completed"]
    # A technically successful but rejected candidate consumes the snapshot.
    assert worker.process_category("cons", tmp_path)["status"] == "idle"


def test_category_lock_prevents_concurrent_training(environment, tmp_path, monkeypatch):
    seed("cons")
    entered, finish = Event(), Event()
    def slow(*args):
        entered.set()
        assert finish.wait(15)
        return fake_execution()
    monkeypatch.setattr(worker, "execute_run", slow)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(worker.process_category, "cons", tmp_path)
        try:
            assert entered.wait(15)
            assert worker.process_category("cons", tmp_path)["status"] == "locked"
            # Other category has its own lock and is not blocked.
            assert worker.process_category("pros", tmp_path)["status"] == "idle"
        finally:
            finish.set()
        assert first.result()["status"] == "completed"


def test_snapshot_does_not_consume_batches_arriving_during_training(environment, tmp_path, monkeypatch):
    seed("cons")
    def receive_during_training(*args):
        seed("cons", count=1, offset=10)
        return fake_execution()
    monkeypatch.setattr(worker, "execute_run", receive_during_training)
    assert worker.process_category("cons", tmp_path)["status"] == "completed"
    result = worker.process_category("cons", tmp_path)
    assert result["status"] == "idle" and result["new_days"] == 1


def test_dead_worker_recovered(environment, tmp_path, monkeypatch):
    seed("cons")
    monkeypatch.setattr(worker, "execute_run", fake_execution)
    assert worker.process_category("cons", tmp_path)["status"] == "completed"
    with database.connect() as conn:
        conn.execute("UPDATE training_runs SET status='running', finished_at=NULL")
    result = worker.process_category("cons", tmp_path)
    assert result["candidate_version"] == 3
    with database.connect() as conn:
        assert conn.execute("SELECT status FROM training_runs WHERE candidate_version=2").fetchone()[0] == "interrupted"


def test_corrupt_input_rejected(environment, tmp_path):
    batch = seed("cons")[0]
    with database.connect() as conn:
        key = conn.execute("SELECT object_key FROM lowperf_batches WHERE batch_id=%s", (batch.batch_id,)).fetchone()[0]
    storage.create_s3_client().put_object(Bucket=storage.get_bucket_name(), Key=key, Body=b"corrupt")
    with pytest.raises(ValueError, match="integrity"):
        worker.process_category("cons", tmp_path)
    with database.connect() as conn:
        assert conn.execute("SELECT status FROM training_runs").fetchone()[0] == "failed"


def test_duplicate_days_and_pending_batches_do_not_inflate_threshold(environment, tmp_path):
    batches = seed("cons")
    duplicate = batches[0].model_copy(update={"batch_id": uuid4()})
    database.save_low_performance_batch(duplicate)
    with database.connect() as conn:
        conn.execute("UPDATE lowperf_batches SET status='pending', stored_at=NULL WHERE batch_id=%s",
                     (batches[-1].batch_id,))
    result = worker.process_category("cons", tmp_path)
    assert result["status"] == "idle" and result["new_days"] == 7


def test_subprocess_timeout(environment, tmp_path):
    with database.connect() as conn:
        with pytest.raises(TimeoutError):
            worker.run_kit([sys.executable, "-c", "import time; time.sleep(30)"],
                           tmp_path / "timeout.log", conn, 0.1)
