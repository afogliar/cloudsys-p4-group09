from typing import Annotated

from fastapi import Depends, FastAPI

from app.security import authenticate_device

app = FastAPI(
    title="CloudSys Group 09 API",
    version="0.2.0",
)


@app.get("/v1/health")
def health(
    device_id: Annotated[str, Depends(authenticate_device)],
):
    return {"status": "ok"}