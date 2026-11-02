"""Real PostgreSQL + MinIO tests, only against the isolated Compose test stack."""
import copy
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import psycopg
import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi.testclient import TestClient

from app import database, main, storage
from app.schemas import LowPerformanceBatch

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BATCH_INTEGRATION") != "1",
    reason="Requires isolated PostgreSQL/MinIO test stack",
)


@pytest.fixture(scope="module", autouse=True)
def isolated_services():
    if os.environ.get("RUN_BATCH_INTEGRATION") != "1":
        return
    if os.environ.get("POSTGRES_DB") != "p4_batch_tests" or storage.get_bucket_name() != "p4-batch-tests":
        pytest.fail("Refusing to run destructive fixture outside the isolated test database/bucket")
    for attempt in range(60):
        try:
            database.initialize_database()
            storage.initialize_storage()
            break
        except (psycopg.Error, EndpointConnectionError):
            if attempt == 59:
                raise
            time.sleep(1)


@pytest.fixture(autouse=True)
def clear_test_data():
    if os.environ.get("RUN_BATCH_INTEGRATION") != "1":
        yield
        return
    with database.connect() as conn:
        conn.execute("TRUNCATE lowperf_batches")
    client = storage.create_s3_client()
    bucket = storage.get_bucket_name()
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            client.delete_object(Bucket=bucket, Key=obj["Key"])
    yield


def post(payload):
    with TestClient(main.app) as client:
        return client.post("/v1/lowperf", json=payload, headers={"Authorization": "Bearer " + "t" * 64})


def row(payload):
    with database.connect() as conn:
        return conn.execute(
            "SELECT status, object_key, stored_at FROM lowperf_batches WHERE batch_id = %s",
            (payload["batch_id"],),
        ).fetchone()


def test_first_retry_and_object_content(payload):
    assert post(payload).status_code == 201
    assert post(payload).status_code == 200
    state, key, stored_at = row(payload)
    assert state == "stored" and stored_at is not None
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM lowperf_batches").fetchone()[0] == 1
    obj = storage.create_s3_client().get_object(Bucket=storage.get_bucket_name(), Key=key)
    with obj["Body"] as body:
        assert json.loads(body.read()) == LowPerformanceBatch(**payload).model_dump(mode="json")
    objects = storage.create_s3_client().list_objects_v2(Bucket=storage.get_bucket_name())
    assert objects["KeyCount"] == 1


def test_changed_content_conflicts(payload):
    assert post(payload).status_code == 201
    changed = copy.deepcopy(payload)
    changed["forecast_kw"][0] = 2.0
    assert post(changed).status_code == 409
    assert post(payload).status_code == 200


def test_concurrent_identical_requests(payload):
    batch = LowPerformanceBatch(**payload)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: database.save_low_performance_batch(batch), range(4)))
    assert results.count(True) == 1
    assert results.count(False) == 3


@pytest.mark.parametrize("after_write", [False, True])
def test_interrupted_upload_resumes(payload, monkeypatch, after_write):
    real_put = database.put_batch_json

    def interrupted(key, content):
        if after_write:
            real_put(key, content)
        raise EndpointConnectionError(endpoint_url="http://test-interruption")

    monkeypatch.setattr(database, "put_batch_json", interrupted)
    assert post(payload).status_code == 503
    assert row(payload)[0] == "pending"
    assert row(payload)[2] is None
    changed = dict(payload, sent_at="2026-10-31T12:00:00Z")
    assert post(changed).status_code == 409
    monkeypatch.setattr(database, "put_batch_json", real_put)
    assert post(payload).status_code == 201
    assert post(payload).status_code == 200


def test_database_failure_never_uploads(payload, monkeypatch):
    upload = Mock()
    monkeypatch.setattr(database, "put_batch_json", upload)
    monkeypatch.setattr(database, "connect", Mock(side_effect=psycopg.OperationalError("test offline")))
    assert post(payload).status_code == 503
    upload.assert_not_called()


def test_failure_before_final_commit_resumes(payload, monkeypatch):
    real_connect = database.connect
    calls = 0

    class FailingCommit:
        def __init__(self):
            self.connection = real_connect()

        def __enter__(self):
            return self.connection.__enter__()

        def __exit__(self, exc_type, exc, traceback):
            if exc_type is not None:
                return self.connection.__exit__(exc_type, exc, traceback)
            failure = psycopg.OperationalError("simulated failure before commit")
            self.connection.__exit__(type(failure), failure, None)
            raise failure

    def fail_second_connection():
        nonlocal calls
        calls += 1
        return FailingCommit() if calls == 2 else real_connect()

    monkeypatch.setattr(database, "connect", fail_second_connection)
    assert post(payload).status_code == 503
    monkeypatch.setattr(database, "connect", real_connect)
    assert row(payload)[0] == "pending"
    assert row(payload)[2] is None
    assert post(payload).status_code == 201
    assert post(payload).status_code == 200


def test_invalid_batch_never_reserves(payload):
    payload["forecast_kw"].pop()
    assert post(payload).status_code == 422
    assert row(payload) is None
