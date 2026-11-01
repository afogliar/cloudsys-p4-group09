from typing import Annotated, Literal
from fastapi import Depends, FastAPI, HTTPException, Response, Path
from botocore.exceptions import BotoCoreError, ClientError
import psycopg
from app.security import authenticate_device
from app.schemas import LowPerformanceBatch, DailyMetrics
from app.database import (
    BatchConflictError, save_daily_metrics, save_low_performance_batch,
)
from app.models import ModelNotFoundError, ModelIntegrityError, model_metadata, read_model

app = FastAPI(
    title="CloudSys Group 09 API",
    version="0.2.0",
)


def model_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ModelNotFoundError):
        return HTTPException(status_code=404, detail="Modèle publié introuvable")
    return HTTPException(
        status_code=503, detail="Modèle temporairement indisponible",
        headers={"Retry-After": "5"},
    )


# Static /latest must precede the version path.
@app.get("/v1/models/{category}/latest")
def latest_model(
    category: Literal["cons", "pros"],
    device_id: Annotated[str, Depends(authenticate_device)],
):
    try:
        metadata = model_metadata(category)
    except (ModelNotFoundError, psycopg.Error) as exc:
        raise model_error(exc) from exc
    return {key: metadata[key] for key in ("category", "version", "sha256", "size_bytes")}


@app.get("/v1/models/{category}/{version}")
def download_model(
    category: Literal["cons", "pros"],
    version: Annotated[int, Path(ge=1)],
    device_id: Annotated[str, Depends(authenticate_device)],
):
    try:
        metadata = model_metadata(category, version)
        content = read_model(metadata)
    except (ModelNotFoundError, ModelIntegrityError, psycopg.Error,
            BotoCoreError, ClientError) as exc:
        raise model_error(exc) from exc
    return Response(
        content=content, media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="model_{category}_v{version:04d}.txt"',
            "ETag": f'"{metadata["sha256"]}"',
            "X-Model-SHA256": metadata["sha256"],
            "X-Model-Version": str(version),
        },
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
