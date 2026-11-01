from typing import Annotated
from fastapi import Depends, FastAPI, HTTPException, Response
from botocore.exceptions import BotoCoreError, ClientError
import psycopg
from app.security import authenticate_device
from app.schemas import LowPerformanceBatch, DailyMetrics
from app.database import (
    BatchConflictError, save_daily_metrics, save_low_performance_batch,
)

app = FastAPI(
    title="CloudSys Group 09 API",
    version="0.2.0",
)


@app.get("/v1/health")
def health(
    device_id: Annotated[str, Depends(authenticate_device)],
):
    return {"status": "ok"}


@app.post("/v1/lowperf", status_code=201)
def ingest_low_performance(
    batch: LowPerformanceBatch,
    response: Response,
    device_id: Annotated[str, Depends(authenticate_device)],
):
    if batch.device_id != device_id:
        raise HTTPException(
            status_code=403,
            detail="Ce token ne permet pas d'envoyer les données de ce compteur",
        )

    try:
        created = save_low_performance_batch(batch)
    except BatchConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (psycopg.Error, BotoCoreError, ClientError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Stockage indisponible ; réessayez le même batch sans le modifier",
            headers={"Retry-After": "5"},
        ) from exc

    response.status_code = 201 if created else 200
    return {
        "status": "created" if created else "already_stored",
        "stored": True,
        "batch_id": str(batch.batch_id),
    }

@app.post("/v1/metrics")
def ingest_daily_metrics(
    metrics: DailyMetrics,
    device_id: Annotated[str, Depends(authenticate_device)],
):
    if metrics.device_id != device_id:
        raise HTTPException(
            status_code=403,
            detail="Ce token ne permet pas d'envoyer les données de ce compteur",
        )

    save_daily_metrics(metrics)

    return {
        "status": "stored",
        "stored": True,
        "device_id": metrics.device_id,
        "day": metrics.day.isoformat(),
    }
