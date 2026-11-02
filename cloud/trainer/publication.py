"""Replayable MLflow tracking and publication of completed kit candidates."""
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path

from mlflow import MlflowClient
from mlflow.exceptions import MlflowException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.database import connect
from app.models import model_metadata, read_model
from app.storage import create_s3_client, get_bucket_name
from trainer.rollout import sync_rollouts


class PublicationConflictError(Exception):
    pass


def initialize_publication():
    with connect() as conn:
        for definition in (
            "mlflow_run_id TEXT", "mlflow_model_version TEXT",
            "publication_status TEXT NOT NULL DEFAULT 'pending'",
            "publication_error_type TEXT", "publication_finished_at TIMESTAMPTZ",
            "nuvla_rollout_status TEXT", "nuvla_rollout_detail TEXT",
            "nuvla_rollout_at TIMESTAMPTZ",
        ):
            conn.execute("ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS " + definition)
        for definition in ("mlflow_run_id TEXT", "mlflow_model_version TEXT", "lineage JSONB",
                           "nuvla_rollout_status TEXT"):
            conn.execute("ALTER TABLE model_versions ADD COLUMN IF NOT EXISTS " + definition)


def tracking_client():
    uri = os.environ["MLFLOW_TRACKING_URI"]
    return MlflowClient(tracking_uri=uri, registry_uri=uri)


def experiment_id(client, category):
    name = "p4-" + category
    experiment = client.get_experiment_by_name(name)
    if experiment:
        return experiment.experiment_id
    try:
        return client.create_experiment(name)
    except MlflowException as exc:
        if exc.error_code != "RESOURCE_ALREADY_EXISTS":
            raise
        return client.get_experiment_by_name(name).experiment_id


def get_or_create_run(client, category, identifier, tags):
    experiment = experiment_id(client, category)
    runs = client.search_runs([experiment],
                              filter_string=f"tags.training_run_id = '{identifier}'",
                              max_results=1)
    if runs:
        return runs[0]
    return client.create_run(experiment, tags={"training_run_id": identifier, **tags})


def get_or_register_version(client, category, run_id, project_version, digest, decision):
    name = "p4-" + category
    try:
        client.create_registered_model(name)
    except MlflowException as exc:
        if exc.error_code != "RESOURCE_ALREADY_EXISTS":
            raise
    token = None
    while True:
        versions = client.search_model_versions(f"name = '{name}'", max_results=1000,
                                               page_token=token)
        for version in versions:
            if version.run_id == run_id:
                if version.tags.get("sha256") != digest:
                    raise PublicationConflictError("MLflow model hash mismatch")
                return version.version
        token = versions.token
        if not token:
            break
    run = client.get_run(run_id)
    version = client.create_model_version(
        name, source=run.info.artifact_uri.rstrip("/") + "/model", run_id=run_id,
        tags={"project_version": str(project_version), "category": category,
              "sha256": digest, "decision": decision},
    )
    return version.version


def log_artifacts(client, run_id, model_bytes, record, manifest):
    if client.get_run(run_id).data.tags.get("artifacts_complete") == "true":
        return
    client.log_dict(run_id, record, "metrics.json")
    client.log_dict(run_id, manifest, "manifest.json")
    with tempfile.TemporaryDirectory(prefix="p4-mlflow-") as folder:
        path = Path(folder) / "model.txt"
        path.write_bytes(model_bytes)
        client.log_artifact(run_id, str(path), artifact_path="model")
    client.set_tag(run_id, "artifacts_complete", "true")


def ensure_initial_registered(client, category):
    with connect() as conn:
        mapping = conn.execute("""
            SELECT mlflow_run_id, mlflow_model_version FROM model_versions
            WHERE category=%s AND version=1
        """, (category,)).fetchone()
    if mapping and all(mapping):
        existing = client.get_model_version("p4-" + category, mapping[1])
        if existing.run_id != mapping[0]:
            raise PublicationConflictError("Initial MLflow mapping mismatch")
        return
    metadata = model_metadata(category, 1)
    content = read_model(metadata)
    run = get_or_create_run(client, category, "initial-" + category,
                            {"origin": "provided_kit", "project_version": "1",
                             "category": category, "decision": "released"})
    log_artifacts(client, run.info.run_id, content,
                  {"origin": "provided_kit", "sha256": metadata["sha256"]},
                  {"category": category, "project_version": 1, "batches": []})
    version = get_or_register_version(client, category, run.info.run_id, 1,
                                      metadata["sha256"], "released")
    client.set_terminated(run.info.run_id, status="FINISHED")
    with connect() as conn:
        current = conn.execute("SELECT version FROM model_releases WHERE category=%s", (category,)).fetchone()
    if current and current[0] == 1:
        client.set_registered_model_alias("p4-" + category, "released", str(version))
    with connect() as conn:
        conn.execute("""
            UPDATE model_versions SET mlflow_run_id=%s, mlflow_model_version=%s
            WHERE category=%s AND version=1
        """, (run.info.run_id, str(version), category))


def read_candidate(run):
    client = create_s3_client()
    objects = {}
    for name in ("model.txt", "metrics.json", "manifest.json"):
        response = client.get_object(Bucket=get_bucket_name(), Key=run["artifact_prefix"] + "/" + name)
        with response["Body"] as body:
            objects[name] = body.read()
    record = json.loads(objects["metrics.json"])
    manifest = json.loads(objects["manifest.json"])
    digest = hashlib.sha256(objects["model.txt"]).hexdigest()
    if (record["category"] != run["category"] or record["model_sha256"] != digest or
            record["release"] != run["release_decision"] or
            manifest["run_id"] != str(run["run_id"]) or
            manifest["category"] != run["category"] or
            manifest["base_version"] != run["base_version"]):
        raise PublicationConflictError("Candidate metadata mismatch")
    if type(record["release"]["released"]) is not bool:
        raise PublicationConflictError("Invalid release decision")
    lineage = record["lineage"]
    if set(lineage["batches_used"] + lineage["batches_held_out"]) != set(run["batch_ids"]):
        raise PublicationConflictError("Candidate lineage mismatch")
    if {b["batch_id"] for b in manifest["batches"]} != set(run["batch_ids"]):
        raise PublicationConflictError("Candidate manifest mismatch")
    for side in ("challenger", "current"):
        for name in ("a2_nmae", "heldout_nmae"):
            if not math.isfinite(record[side][name]):
                raise PublicationConflictError("Non-finite validation metric")
    from p4kit.train import release_decision
    if release_decision(record["challenger"], record["current"])["released"] != record["release"]["released"]:
        raise PublicationConflictError("Decision disagrees with kit rule")
    return objects["model.txt"], record, manifest, digest


def publish_completed(client, run):
    content, record, manifest, digest = read_candidate(run)
    accepted = record["release"]["released"]
    decision = "released" if accepted else "rejected"
    tracked = get_or_create_run(client, run["category"], str(run["run_id"]), {
        "category": run["category"], "project_version": str(run["candidate_version"]),
        "decision": "accepted" if accepted else "rejected",
        "simulated_at": run["simulated_at"].isoformat(),
        "trigger": run["trigger_reason"],
    })
    mlflow_id = tracked.info.run_id
    with connect() as conn:
        conn.execute("UPDATE training_runs SET mlflow_run_id=%s WHERE run_id=%s",
                     (mlflow_id, run["run_id"]))
    for key, value in {
        "category": run["category"], "project_version": run["candidate_version"],
        "base_version": run["base_version"], "seed": record["seed"],
        "num_boost_round": record["num_boost_round"],
        "batches_used": len(record["lineage"]["batches_used"]),
        "batches_held_out": len(record["lineage"]["batches_held_out"]),
    }.items():
        client.log_param(mlflow_id, key, value)
    # Reuse the same run and artifact paths on retries; no duplicate model version.
    if tracked.data.tags.get("metrics_complete") != "true":
        for side in ("challenger", "current"):
            for metric in ("a2_nmae", "heldout_nmae"):
                client.log_metric(mlflow_id, side + "." + metric, record[side][metric])
        client.log_metric(mlflow_id, "training_seconds", record["training_seconds"])
        client.set_tag(mlflow_id, "metrics_complete", "true")
    client.set_tag(mlflow_id, "release_reason", record["release"]["reason"])
    log_artifacts(client, mlflow_id, content, record, manifest)
    mlflow_version = get_or_register_version(client, run["category"], mlflow_id,
                                              run["candidate_version"], digest,
                                              "accepted" if accepted else "rejected")
    client.set_terminated(mlflow_id, status="FINISHED")
    with connect() as conn:
        current = conn.execute("SELECT version FROM model_releases WHERE category=%s FOR UPDATE",
                               (run["category"],)).fetchone()
        if accepted and (current is None or current[0] not in {run["base_version"], run["candidate_version"]}):
            raise PublicationConflictError("Published baseline changed; refusing stale candidate")
        conn.execute("""
            INSERT INTO model_versions
              (category,version,object_key,sha256,size_bytes,status,mlflow_run_id,mlflow_model_version,lineage)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (category,version) DO NOTHING
        """, (run["category"], run["candidate_version"], run["artifact_prefix"] + "/model.txt",
              digest, len(content), decision, mlflow_id, str(mlflow_version), Jsonb(record["lineage"])))
        existing = conn.execute("SELECT sha256,status FROM model_versions WHERE category=%s AND version=%s",
                                (run["category"], run["candidate_version"])).fetchone()
        if existing != (digest, decision):
            raise PublicationConflictError("Immutable model version conflict")
        if accepted:
            conn.execute("UPDATE model_releases SET version=%s WHERE category=%s",
                         (run["candidate_version"], run["category"]))
            # The accepted version now owes exactly one Nuvla rollout request,
            # performed by trainer.rollout once this transaction commits. A
            # terminal outcome (updated/skipped) is never reset by a re-publish.
            conn.execute("""
                UPDATE training_runs SET nuvla_rollout_status='pending',
                    nuvla_rollout_detail=NULL
                WHERE run_id=%s AND (nuvla_rollout_status IS NULL OR
                    nuvla_rollout_status NOT IN ('updated', 'skipped'))
            """, (run["run_id"],))
            conn.execute("""
                UPDATE model_versions SET nuvla_rollout_status='pending'
                WHERE category=%s AND version=%s AND (nuvla_rollout_status IS NULL OR
                    nuvla_rollout_status NOT IN ('updated', 'skipped'))
            """, (run["category"], run["candidate_version"]))
    if accepted:
        client.set_registered_model_alias("p4-" + run["category"], "released", str(mlflow_version))
    client.set_tag(mlflow_id, "publication", decision)
    client.set_model_version_tag("p4-" + run["category"], str(mlflow_version), "publication", decision)
    with connect() as conn:
        conn.execute("""
            UPDATE training_runs SET mlflow_model_version=%s, publication_status=%s,
                publication_error_type=NULL, publication_finished_at=NOW()
            WHERE run_id=%s
        """, (str(mlflow_version), decision, run["run_id"]))


def log_failed(client, run):
    tracked = get_or_create_run(client, run["category"], str(run["run_id"]), {
        "category": run["category"], "project_version": str(run["candidate_version"]),
        "decision": "technical_failure", "execution_status": run["status"],
        "error_type": run["error_type"] or "unknown",
    })
    client.log_dict(tracked.info.run_id, {
        "run_id": str(run["run_id"]), "batch_ids": run["batch_ids"],
        "base_version": run["base_version"], "status": run["status"],
        "error_type": run["error_type"],
    }, "failure.json")
    client.set_terminated(tracked.info.run_id, status="FAILED")
    with connect() as conn:
        conn.execute("""
            UPDATE training_runs SET mlflow_run_id=%s, publication_status='failed_logged',
                publication_error_type=NULL, publication_finished_at=NOW() WHERE run_id=%s
        """, (tracked.info.run_id, run["run_id"]))


def sync_category_locked(category):
    """Caller must hold the trainer's category advisory lock throughout."""
    client = tracking_client()
    ensure_initial_registered(client, category)
    with connect() as conn:
        with conn.cursor(row_factory=dict_row) as cursor:
            runs = cursor.execute("""
                SELECT * FROM training_runs WHERE category=%s
                  AND status IN ('completed','failed','interrupted')
                  AND publication_status='pending'
                ORDER BY candidate_version
            """, (category,)).fetchall()
    for run in runs:
        try:
            if run["status"] == "completed":
                publish_completed(client, run)
            else:
                log_failed(client, run)
        except Exception as exc:
            with connect() as conn:
                conn.execute("UPDATE training_runs SET publication_error_type=%s WHERE run_id=%s",
                             (type(exc).__name__, run["run_id"]))
            raise
    # Nuvla rollout of the released version, on every pass (fresh acceptions
    # and retries). It records its own outcome and must never fail publication.
    try:
        sync_rollouts(category)
    except Exception as exc:
        print(json.dumps({"event": "nuvla_rollout", "category": category,
                          "status": "error", "detail": type(exc).__name__}), flush=True)
    return len(runs)
