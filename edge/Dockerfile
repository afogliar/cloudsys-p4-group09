FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=materials kit/ /opt/kit/
COPY --from=materials data/ /opt/data/
RUN pip install --no-cache-dir -r /opt/kit/requirements.txt \
    && pip install --no-cache-dir -c /opt/kit/requirements.txt \
       paho-mqtt==2.1.0 requests==2.32.5

COPY edge/ /opt/app/edge/
WORKDIR /opt/kit
RUN python -m p4kit.verify --vectors test_vectors/

ENV PYTHONPATH=/opt/app:/opt/kit \
    DATA_DIR=/opt/data/meters \
    WEATHER_PATH=/opt/data/weather_fribourg_15min.parquet \
    METERS_JSON=/opt/kit/meters.json \
    MODELS_DIR=/state/models \
    STATE_PATH=/state/forecaster_state.json \
    OUTBOX_DIR=/state/outbox \
    REPLAY_END=2026-08-17 \
    MQTT_HOST=data-gateway \
    MQTT_PORT=1883

ENTRYPOINT ["python"]
