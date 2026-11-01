FROM python:3.11-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=materials kit/ /opt/kit/
COPY --from=materials data/ /data/
COPY trainer/requirements.txt /app/trainer-requirements.txt
RUN pip install --no-cache-dir -r /opt/kit/requirements.txt \
    && pip install --no-cache-dir -c /opt/kit/requirements.txt -r /app/trainer-requirements.txt
WORKDIR /opt/kit
RUN python -m p4kit.verify --vectors test_vectors/
ENV PYTHONPATH=/app:/opt/kit
ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
COPY api/app/ /app/app/
COPY trainer/ /app/trainer/
CMD ["python", "-m", "trainer.worker"]

FROM runtime AS tests
RUN pip install --no-cache-dir -c /opt/kit/requirements.txt "pytest>=8,<10" fastapi httpx
CMD ["python", "-m", "pytest", "-q", "/app/trainer/tests"]
