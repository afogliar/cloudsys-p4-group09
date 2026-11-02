from unittest.mock import Mock

import psycopg
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi.testclient import TestClient

from app import main
from app.database import BatchConflictError


@pytest.fixture
def client():
    with TestClient(main.app) as client:
        yield client


def headers():
    return {"Authorization": "Bearer " + "t" * 64}


@pytest.mark.parametrize("created,status,label", [
    (True, 201, "created"), (False, 200, "already_stored"),
])
def test_success_status(client, monkeypatch, payload, created, status, label):
    save = Mock(return_value=created)
    monkeypatch.setattr(main, "save_low_performance_batch", save)
    response = client.post("/v1/lowperf", json=payload, headers=headers())
    assert response.status_code == status
    assert response.json() == {
        "status": label, "stored": True, "batch_id": payload["batch_id"],
    }
    save.assert_called_once()


@pytest.mark.parametrize("request_headers", [{}, {"Authorization": "Bearer wrong"}])
def test_unauthorized_never_writes(client, monkeypatch, payload, request_headers):
    save = Mock()
    monkeypatch.setattr(main, "save_low_performance_batch", save)
    assert client.post("/v1/lowperf", json=payload, headers=request_headers).status_code == 401
    save.assert_not_called()


def test_other_meter_never_writes(client, monkeypatch, payload):
    save = Mock()
    monkeypatch.setattr(main, "save_low_performance_batch", save)
    payload.update(device_id="5c3492e5", category="pros")
    assert client.post("/v1/lowperf", json=payload, headers=headers()).status_code == 403
    save.assert_not_called()


def test_invalid_never_writes(client, monkeypatch, payload):
    save = Mock()
    monkeypatch.setattr(main, "save_low_performance_batch", save)
    payload["forecast_kw"].pop()
    assert client.post("/v1/lowperf", json=payload, headers=headers()).status_code == 422
    save.assert_not_called()


def test_conflict(client, monkeypatch, payload):
    monkeypatch.setattr(main, "save_low_performance_batch", Mock(side_effect=BatchConflictError("conflict")))
    assert client.post("/v1/lowperf", json=payload, headers=headers()).status_code == 409


@pytest.mark.parametrize("error", [
    psycopg.OperationalError("private database diagnostic"),
    EndpointConnectionError(endpoint_url="http://private-minio"),
    ClientError({"Error": {"Code": "AccessDenied", "Message": "private secret"}}, "PutObject"),
])
def test_storage_failure_is_retryable(client, monkeypatch, payload, error):
    monkeypatch.setattr(main, "save_low_performance_batch", Mock(side_effect=error))
    response = client.post("/v1/lowperf", json=payload, headers=headers())
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert "private" not in response.text
    assert "stored" not in response.json()
