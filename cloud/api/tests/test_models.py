import hashlib
import os
from pathlib import Path
from uuid import uuid4
from unittest.mock import Mock

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import main, models, storage
from app.database import connect


@pytest.fixture
def client():
    with TestClient(main.app) as client:
        yield client


def headers():
    return {"Authorization": "Bearer " + "t" * 64}


@pytest.mark.parametrize("path", ["/v1/models/cons/latest", "/v1/models/pros/1"])
def test_requires_token(client, monkeypatch, path):
    lookup = Mock()
    monkeypatch.setattr(main, "model_metadata", lookup)
    assert client.get(path).status_code == 401
    lookup.assert_not_called()


@pytest.mark.parametrize("path", ["/v1/models/unknown/latest", "/v1/models/cons/0", "/v1/models/cons/abc"])
def test_invalid_model_path(client, monkeypatch, path):
    lookup = Mock()
    monkeypatch.setattr(main, "model_metadata", lookup)
    assert client.get(path, headers=headers()).status_code == 422
    lookup.assert_not_called()


@pytest.mark.parametrize("error,status", [
    (models.ModelNotFoundError(), 404),
    (psycopg.OperationalError("private secret diagnostic"), 503),
])
def test_lookup_errors(client, monkeypatch, error, status):
    monkeypatch.setattr(main, "model_metadata", Mock(side_effect=error))
    response = client.get("/v1/models/cons/latest", headers=headers())
    assert response.status_code == status
    assert "private" not in response.text


def test_corruption_returns_no_model(client, monkeypatch):
    monkeypatch.setattr(main, "model_metadata", Mock(return_value={}))
    monkeypatch.setattr(main, "read_model", Mock(side_effect=models.ModelIntegrityError()))
    response = client.get("/v1/models/cons/1", headers=headers())
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"


integration = pytest.mark.skipif(
    os.environ.get("RUN_MODEL_INTEGRATION") != "1",
    reason="Requires model test stack and read-only kit mount",
)


@pytest.fixture
def ready_models():
    assert os.environ["POSTGRES_DB"] == "p4_model_tests"
    assert storage.get_bucket_name() == "p4-model-tests"
    models.bootstrap(Path("/kit"))


@integration
@pytest.mark.parametrize("category", ["cons", "pros"])
def test_real_initial_download(client, ready_models, category):
    expected = Path(f"/kit/models/model_{category}_v0001.txt").read_bytes()
    latest = client.get(f"/v1/models/{category}/latest", headers=headers())
    assert latest.status_code == 200
    assert latest.json()["version"] == 1
    assert latest.json()["sha256"] == hashlib.sha256(expected).hexdigest()
    response = client.get(f"/v1/models/{category}/1", headers=headers())
    assert response.status_code == 200
    assert response.content == expected
    assert response.headers["x-model-sha256"] == latest.json()["sha256"]
    assert "attachment" in response.headers["content-disposition"]
    assert client.get(f"/v1/models/{category}/2147483647", headers=headers()).status_code == 404


@integration
def test_real_repeat_import_and_conflict(ready_models):
    models.bootstrap(Path("/kit"))
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_versions WHERE version=1").fetchone()[0] == 2
    with pytest.raises(models.ModelIntegrityError):
        models.import_initial_model("cons", b"changed", "0" * 64)


@integration
@pytest.mark.parametrize("state", ["pending", "rejected"])
def test_real_unreleased_not_downloadable(client, ready_models, state):
    version = 1000000 + uuid4().int % 1000000000
    with connect() as conn:
        conn.execute("""
            INSERT INTO model_versions
                (category, version, object_key, sha256, size_bytes, status)
            VALUES ('cons', %s, %s, %s, 1, %s)
        """, (version, f"tests/{uuid4()}", "0" * 64, state))
    assert client.get(f"/v1/models/cons/{version}", headers=headers()).status_code == 404
    assert client.get("/v1/models/cons/latest", headers=headers()).json()["version"] == 1
