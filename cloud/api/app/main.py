from typing import Annotated
from fastapi import Depends, FastAPI, HTTPException
from app.security import authenticate_device
from app.schemas import LowPerformanceBatch

app = FastAPI(
    title="CloudSys Group 09 API",
    version="0.2.0",
)


@app.get("/v1/health")
def health(
    device_id: Annotated[str, Depends(authenticate_device)],
):
    return {"status": "ok"}


@app.post("/v1/lowperf")
def ingest_low_performance(
    batch: LowPerformanceBatch,
    device_id: Annotated[str, Depends(authenticate_device)],
):
    if batch.device_id != device_id:
        raise HTTPException(
            status_code=403,
            detail="Ce token ne permet pas d'envoyer les données de ce compteur",
        )

    return {
        "status": "validated",
        "stored": False,
        "batch_id": str(batch.batch_id),
    }