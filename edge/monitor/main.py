import os
import json
from collections import defaultdict
import paho.mqtt.client as mqtt
import numpy as np
import pandas as pd
from p4kit.metrics import low_performance, daily_mae
from datetime import datetime, timezone
from pathlib import Path
import requests
import uuid
import threading
import time

mqtt_host = os.getenv("MQTT_HOST", "localhost")
mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
device_ids = os.getenv("DEVICE_IDS", "").split(",")

api_url = os.getenv("API_URL", "http://localhost:8000").rstrip("/")
device_tokens = json.loads(os.getenv("DEVICE_TOKENS_JSON", "{}"))

outbox_dir = Path(os.getenv("OUTBOX_DIR", "outbox"))
outbox_dir.mkdir(parents=True, exist_ok=True)
outbox_lock = threading.Lock()

meters_json_path = Path(os.getenv("METERS_JSON", "kit/meters.json"))

with open(meters_json_path, encoding="utf-8") as f:
    meters_data = json.load(f)

meter_categories = {
    meter["meter_id"]: meter["category"]
    for meter in meters_data["meters"]
}

# Mesures réelles reçues pour chaque compteur
readings = defaultdict(dict)

# Prévisions reçues pour chaque compteur
forecasts = defaultdict(dict)


def on_connect(client, userdata, flags, reason_code, properties):
    print("Monitor connecté à MQTT :", reason_code)

    for device in device_ids:
        client.subscribe(f"meters/{device}/readings")
        client.subscribe(f"meters/{device}/forecast")

    client.subscribe("replay/clock")

def send_to_cloud(endpoint, payload):
    device = payload["device_id"]

    token = device_tokens.get(device)

    if not token:
        print(f"{device} : token manquant")
        return False

    headers = {
        "Authorization": f"Bearer {token}"
    }
    
    try:
        response = requests.post(
            f"{api_url}/v1/{endpoint}",
            json=payload,
            headers=headers,
            timeout=10
        )

        response.raise_for_status()

        print(
            f"{device} : {endpoint} envoyé au Cloud "
            f"({response.status_code})"
        )
        return True

    except requests.RequestException as e:
        print(f"{device} : échec envoi {endpoint} : {e}")
        return False

def save_to_outbox(metrics):
    device = metrics["device_id"]
    day = metrics["day"]

    filename = f"metrics_{device}_{day}.json"
    filepath = outbox_dir / filename

    temp_path = filepath.with_suffix(".tmp")
    
    with outbox_lock:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(metrics, f, allow_nan=False)

        os.replace(temp_path, filepath)

    print(f"{device} : métriques sauvegardées dans {filepath}")

def retry_outbox():
    for filepath in sorted(outbox_dir.glob("*.json")):
        try:
            with open(filepath, encoding="utf-8") as f:
                payload = json.load(f)

            endpoint = (
                "lowperf"
                if filepath.name.startswith("lowperf_")
                else "metrics"
            )

            if send_to_cloud(endpoint, payload):
                filepath.unlink()
                print(f"Outbox : {filepath.name} envoyé et supprimé")
            else:
                print(f"Outbox : {filepath.name} conservé")

        except (OSError, ValueError) as e:
            print(f"Outbox : erreur avec {filepath.name} : {e}")

def outbox_worker():
    while True:
        retry_outbox()
        time.sleep(5)

def evaluate_day(client, device, day):
    day_start = pd.Timestamp(day)
    lag7_start = day_start - pd.Timedelta(days=7)

    forecast = forecasts[device].get(str(day_start.date()))

    if forecast is None:
        print(f"{device} : aucune prévision pour {day}")
        return

    actual_kw = []
    actual_origin = []
    lag7_kw = []
    lag7_origin = []

    for i in range(96):
        timestamp = day_start + pd.Timedelta(minutes=15 * i)
        lag7_timestamp = lag7_start + pd.Timedelta(minutes=15 * i)

        actual = readings[device].get(timestamp.isoformat())
        previous = readings[device].get(lag7_timestamp.isoformat())

        actual_kw.append(actual["ap_kw"] if actual else np.nan)
        actual_origin.append(actual["ap_kw_origin"] if actual else 91)

        lag7_kw.append(previous["ap_kw"] if previous else np.nan)
        lag7_origin.append(previous["ap_kw_origin"] if previous else 91)

    result = low_performance(
        actual_kw=np.array(actual_kw, dtype=float),
        actual_origin=np.array(actual_origin),
        forecast_kw=np.array(forecast["forecast_kw"], dtype=float),
        lag7_kw=np.array(lag7_kw, dtype=float),
        lag7_origin=np.array(lag7_origin),
        margin=0.0
    )

    mae_kw, n_valid_slots = daily_mae(
        actual_kw=np.array(actual_kw, dtype=float),
        forecast_kw=np.array(forecast["forecast_kw"], dtype=float),
        origin=np.array(actual_origin)
    )

    print(f"{device} - {day} : {result}")

    daily_metrics = {
        "device_id": device,
        "category": meter_categories[device],
        "day": str(day),
        "model_version": forecast["model_version"],
        "n_valid_slots": int(n_valid_slots),
        "n_comparison_slots": int(result["n_slots"]),
        "mae_kw": float(mae_kw) if np.isfinite(mae_kw) else None,
        "mae_comparison_kw": (
            float(result["mae_model_kw"]) if result["evaluated"] else None
        ),
        "mae_persistence_kw": (
            float(result["mae_persistence_kw"]) if result["evaluated"] else None
        ),
        "low_performance": (
            bool(result["flagged"]) if result["evaluated"] else None
        ),
        "sent_at": datetime.now(timezone.utc).isoformat()
    }

    if result["evaluated"] and result["flagged"]:

        history_kw = []
        history_origin = []

        history_start = day_start - pd.Timedelta(days=7)

        for i in range(672):
            timestamp = history_start + pd.Timedelta(minutes=15 * i)
            reading = readings[device].get(timestamp.isoformat())

            history_kw.append(
                reading["ap_kw"] if reading else None
            )
            history_origin.append(
                reading["ap_kw_origin"] if reading else 91
            )

        lowperf_payload = {
            "batch_id": str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{device}:{day}:{forecast['model_version']}"
                )
            ),
            "device_id": device,
            "category": meter_categories[device],
            "model_version": forecast["model_version"],
            "day": str(day),
            "mae_kw": float(result["mae_model_kw"]),
            "mae_persistence_kw": float(result["mae_persistence_kw"]),
            "forecast_kw": [
                float(value) for value in forecast["forecast_kw"]
            ],
            "actual_kw": [
                float(value) if np.isfinite(value) else None
                for value in actual_kw
            ],
            "actual_origin": [
                int(value) for value in actual_origin
            ],
            "history_kw": [
                float(value) if value is not None and np.isfinite(value)
                else None
                for value in history_kw
            ],
            "history_origin": [
                int(value) for value in history_origin
            ],
            "sent_at": datetime.now(timezone.utc).isoformat()
        }

        filename = f"lowperf_{device}_{day}.json"
        filepath = outbox_dir / filename
        temp_path = filepath.with_suffix(".tmp")

        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(lowperf_payload, f, allow_nan=False)

        os.replace(temp_path, filepath)

        print(f"{device} : rapport lowperf sauvegardé")   

    topic = f"meters/{device}/daily"

    info = client.publish(
        topic,
        json.dumps(daily_metrics, allow_nan=False),
        qos=1
    )

    if info.rc == mqtt.MQTT_ERR_SUCCESS:
        print(f"{device} : métriques publiées sur {topic}")
    else:
        print(f"{device} : erreur de publication MQTT")

    save_to_outbox(daily_metrics)

def on_message(client, userdata, msg):
    payload = json.loads(msg.payload.decode("utf-8"))

    if msg.topic.endswith("/readings"):
        device = payload["device_id"]
        timestamp = payload["timestamp"]

        readings[device][timestamp] = {
            "ap_kw": payload["ap_kw"],
            "ap_kw_origin": payload["ap_kw_origin"]
        }

    elif msg.topic.endswith("/forecast"):
        device = payload["meter_id"]
        day = payload["day"]

        forecasts[device][day] = payload
        print(f"{device} : prévisions reçues pour {day}")

    elif msg.topic == "replay/clock":
        timestamp = pd.Timestamp(payload["timestamp"])

        if timestamp.hour == 0 and timestamp.minute == 0:
            previous_day = timestamp - pd.Timedelta(days=1)

            if previous_day >= pd.Timestamp("2026-04-01"):
                for device in device_ids:
                    evaluate_day(client, device, previous_day.date())

outbox_thread = threading.Thread(
    target=outbox_worker,
    daemon=True
)
outbox_thread.start()

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message

client.connect(mqtt_host, mqtt_port, 60)
client.loop_forever()
