import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

os.environ.setdefault("DEVICE_TOKENS_JSON", '{"27afc1ed":"' + "t" * 64 + '"}')
from app import database, models, storage, main
from p4kit.train import release_decision
from trainer import worker, publication

pytestmark = pytest.mark.skipif(
    os.environ.get("POSTGRES_DB") != "p4_publication_tests",
    reason="Requires isolated MLflow publication stack",
)


@pytest.fixture
def ready():
    assert storage.get_bucket_name() == "p4-publication-tests"
    worker.initialize_training()
    with database.connect() as conn:
        conn.execute("TRUNCATE training_runs, lowperf_batches, daily_metrics, model_releases, model_versions")
    models.bootstrap(Path("/opt/kit"))


def synchronize(category="cons"):
    with database.connect() as conn:
        conn.autocommit = True
        assert conn.execute("SELECT pg_try_advisory_lock(%s,%s)", (94009, 1 if category == "cons" else 2)).fetchone()[0]
        return publication.sync_category_locked(category)


def candidate(category="cons", accepted=True, version=2, base=1):
    run_id = uuid4()
    ids = [str(uuid4()), str(uuid4())]
    content = Path(f"/opt/kit/models/model_{category}_v0001.txt").read_bytes()
    challenger = dict(a2_nmae=0.9 if accepted else 1.2, heldout_nmae=0.8 if accepted else 1.1)
    current = dict(a2_nmae=1.0, heldout_nmae=1.0)
    record = dict(category=category, seed=42, num_boost_round=879 if category == "cons" else 388,
                  model_sha256=hashlib.sha256(content).hexdigest(), training_seconds=0.01,
                  challenger=challenger, current=current,
                  lineage=dict(batches_used=ids[:1], batches_held_out=ids[1:]),
                  release=release_decision(challenger, current))
    manifest = dict(run_id=str(run_id), category=category, base_version=base,
                    batches=[{"batch_id": batch_id} for batch_id in ids])
    prefix = f"models/{category}/v{version:04d}"
    client = storage.create_s3_client()
    for name, body in (("model.txt", content), ("metrics.json", json.dumps(record).encode()),
                       ("manifest.json", json.dumps(manifest).encode())):
        client.put_object(Bucket=storage.get_bucket_name(), Key=prefix + "/" + name, Body=body)
    with database.connect() as conn:
        conn.execute("""
            INSERT INTO training_runs
              (run_id, category, candidate_version, base_version, simulated_at, trigger_reason,
               status, batch_ids, new_batch_ids, artifact_prefix, release_decision, finished_at)
            VALUES (%s,%s,%s,%s,'2026-04-15','fallback','completed',%s,%s,%s,%s,NOW())
        """, (run_id, category, version, base, Jsonb(ids), Jsonb(ids), prefix, Jsonb(record["release"])))
    return run_id, content


def run_row(run_id):
    with database.connect() as conn:
        with conn.cursor(row_factory=dict_row) as cursor:
            return cursor.execute("SELECT * FROM training_runs WHERE run_id=%s", (run_id,)).fetchone()


@pytest.mark.parametrize("category", ["cons", "pros"])
def test_accepted_candidate_is_tracked_and_downloadable(ready, category):
    run_id, content = candidate(category)
    assert synchronize(category) == 1
    assert models.model_metadata(category)["version"] == 2
    row = run_row(run_id)
    assert row["publication_status"] == "released"
    client = publication.tracking_client()
    tracked = client.get_run(row["mlflow_run_id"])
    assert tracked.info.status == "FINISHED"
    assert tracked.data.metrics["challenger.heldout_nmae"] == 0.8
    assert tracked.data.params["seed"] == "42"
    alias = client.get_model_version_by_alias("p4-" + category, "released")
    assert alias.run_id == row["mlflow_run_id"]
    files = client.list_artifacts(row["mlflow_run_id"])
    assert {f.path for f in files} >= {"model", "metrics.json", "manifest.json"}
    with TestClient(main.app) as api:
        headers = {"Authorization": "Bearer " + "t" * 64}
        assert api.get(f"/v1/models/{category}/latest", headers=headers).json()["version"] == 2
        assert api.get(f"/v1/models/{category}/2", headers=headers).content == content
        assert api.get(f"/v1/models/{category}/1", headers=headers).status_code == 200


def test_rejected_candidate_is_registered_but_not_published(ready):
    run_id, _ = candidate(accepted=False)
    synchronize()
    row = run_row(run_id)
    assert row["publication_status"] == "rejected"
    assert models.model_metadata("cons")["version"] == 1
    with pytest.raises(models.ModelNotFoundError):
        models.model_metadata("cons", 2)
    version = publication.tracking_client().get_model_version("p4-cons", row["mlflow_model_version"])
    assert version.tags["decision"] == "rejected"


def test_retries_reuse_run_and_registry_version(ready):
    run_id, _ = candidate()
    synchronize()
    first = run_row(run_id)
    with database.connect() as conn:
        conn.execute("UPDATE training_runs SET publication_status='pending' WHERE run_id=%s", (run_id,))
    synchronize()
    second = run_row(run_id)
    assert (first["mlflow_run_id"], first["mlflow_model_version"]) == (second["mlflow_run_id"], second["mlflow_model_version"])
    client = publication.tracking_client()
    versions = client.search_model_versions("name = 'p4-cons'", max_results=1000)
    assert len([v for v in versions if v.run_id == second["mlflow_run_id"]]) == 1


def test_mlflow_outage_preserves_completed_training(ready, monkeypatch):
    run_id, _ = candidate()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:1")
    with pytest.raises(Exception):
        synchronize()
    row = run_row(run_id)
    assert row["status"] == "completed" and row["publication_status"] == "pending"
    assert models.model_metadata("cons")["version"] == 1
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://mlflow-publication-test:5000")
    synchronize()
    assert run_row(run_id)["publication_status"] == "released"


def test_interruption_after_database_publication_recovers(ready, monkeypatch):
    run_id, _ = candidate()
    client = publication.tracking_client()
    real_alias = client.set_registered_model_alias
    def broken_alias(*args, **kwargs):
        # Baseline alias is allowed; fail only after pointer advances to v2.
        if models.model_metadata("cons")["version"] == 2:
            raise RuntimeError("injected alias interruption")
        return real_alias(*args, **kwargs)
    monkeypatch.setattr(client, "set_registered_model_alias", broken_alias)
    monkeypatch.setattr(publication, "tracking_client", lambda: client)
    with pytest.raises(RuntimeError):
        synchronize()
    assert models.model_metadata("cons")["version"] == 2
    assert run_row(run_id)["publication_status"] == "pending"
    monkeypatch.setattr(client, "set_registered_model_alias", real_alias)
    synchronize()
    assert run_row(run_id)["publication_status"] == "released"


def test_stale_candidate_cannot_replace_newer_baseline(ready):
    candidate()
    synchronize()
    later, _ = candidate(version=3, base=1)
    with pytest.raises(publication.PublicationConflictError, match="baseline changed"):
        synchronize()
    assert models.model_metadata("cons")["version"] == 2
    assert run_row(later)["publication_status"] == "pending"


def test_corrupt_candidate_is_not_published(ready):
    run_id, _ = candidate()
    storage.create_s3_client().put_object(Bucket=storage.get_bucket_name(),
                                        Key="models/cons/v0002/model.txt", Body=b"corrupt")
    with pytest.raises(publication.PublicationConflictError):
        synchronize()
    assert models.model_metadata("cons")["version"] == 1
    assert run_row(run_id)["publication_status"] == "pending"


def test_technical_failure_is_logged_without_model_release(ready):
    run_id, _ = candidate()
    with database.connect() as conn:
        conn.execute("UPDATE training_runs SET status='failed', error_type='TimeoutError' WHERE run_id=%s", (run_id,))
    synchronize()
    row = run_row(run_id)
    assert row["publication_status"] == "failed_logged"
    assert publication.tracking_client().get_run(row["mlflow_run_id"]).info.status == "FAILED"
    assert models.model_metadata("cons")["version"] == 1


@pytest.mark.parametrize("category", ["cons", "pros"])
def test_real_training_automatically_tracks_and_applies_kit_decision(ready, tmp_path, category):
    from trainer.tests.test_worker import seed
    seed(category)
    result = worker.process_category(category, tmp_path)
    assert result["status"] == "completed"
    expected = "released" if result["release"]["released"] else "rejected"
    assert result["publication"] == expected
    row = run_row(result["run_id"])
    assert row["publication_status"] == expected
    assert models.model_metadata(category)["version"] == (2 if expected == "released" else 1)
    tracked = publication.tracking_client().get_run(row["mlflow_run_id"])
    assert tracked.info.status == "FINISHED"
    assert tracked.data.params["batches_used"] == "6"
    assert tracked.data.params["batches_held_out"] == "2"
