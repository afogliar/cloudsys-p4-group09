from fastapi import FastAPI

app = FastAPI(
    title="CloudSys Group 09 API",
    version="0.1.0",
)


@app.get("/v1/health")
def health():
    return {"status": "ok"}