import json
import paho.mqtt.client as mqtt
import pandas as pd
from collections import deque
from pathlib import Path
from p4kit.predict import predict_day
import os
import requests
import numpy as np
from datetime import datetime

# Configuration
mqtt_host = os.getenv("MQTT_HOST", "localhost")
mqtt_port = int(os.getenv("MQTT_PORT", "1883"))

models_dir = Path(os.getenv("MODELS_DIR", "kit/models"))
meters_json_path = Path(os.getenv("METERS_JSON", "kit/meters.json"))

api_url = os.getenv("API_URL", "http://localhost:8000").rstrip("/")
device_tokens = json.loads(os.getenv("DEVICE_TOKENS_JSON", "{}"))

models_dir.mkdir(parents=True, exist_ok=True)

available_versions = {
    "cons": int(os.getenv("MODEL_VERSION_CONS", "1")),
    "pros": int(os.getenv("MODEL_VERSION_PROS", "1"))
}

device_ids = [
    device.strip()
    for device in os.getenv("DEVICE_IDS", "").split(",")
    if device.strip()
]

active_versions = {
    device: 1 for device in device_ids
}

def get_model_path(category, version):
    return models_dir / f"model_{category}_v{version:04d}.txt"

def download_model(category, version, device):
    model_path = get_model_path(category, version)

    if model_path.exists():
        return model_path

    token = device_tokens.get(device)

    if not token:
        print(f"{device} : token manquant")
        return None

    try:
        response = requests.get(
            f"{api_url}/v1/models/{category}/{version}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15
        )
        response.raise_for_status()

        temp_path = model_path.with_suffix(".tmp")
        temp_path.write_bytes(response.content)
        temp_path.replace(model_path)

        print(f"Modèle {category} v{version} téléchargé")
        return model_path

    except (requests.RequestException, OSError) as e:
        print(f"Erreur téléchargement : {e}")
        return None

def evaluate_model(device, category, old_version, new_version, timestamp):
    history = list(histories[device])

    if len(history) < 1344:
        print(f"{device} : historique insuffisant pour comparer les modèles")
        return False

    expected_times = pd.date_range(
        end=timestamp - pd.Timedelta(minutes=15),
        periods=1344,
        freq="15min"
    )

    actual_times = pd.to_datetime([row["timestamp"] for row in history])

    if not (actual_times == expected_times).all():
        print(f"{device} : historique de 14 jours mal aligné")
        return False

    old_path = get_model_path(category, old_version)
    new_path = download_model(category, new_version, device)

    if not old_path.exists() or new_path is None:
        return False

    old_errors = []
    new_errors = []

    for i in range(7):
        day_start = timestamp - pd.Timedelta(days=7 - i)
        history_start = i * 96
        history_end = history_start + 672

        past = history[history_start:history_end]
        actual = history[history_end:history_end + 96]

        if len(actual) != 96:
            return False

        weather_day_df = weather_df[
            (weather_df["timestamp"] >= day_start)
            & (weather_df["timestamp"] < day_start + pd.Timedelta(days=1))
        ].sort_values("timestamp")

        if len(weather_day_df) != 96:
            return False

        weather_day = {
            "ghi": weather_day_df["global_radiation_wm2"].tolist(),
            "temp": weather_day_df["temp_air_2m_c"].tolist(),
            "sunshine": weather_day_df["sunshine_min"].tolist()
        }

        history_kw = [row["ap_kw"] for row in past]
        history_origin = [row["ap_kw_origin"] for row in past]

        for model_path, errors in [
            (old_path, old_errors),
            (new_path, new_errors)
        ]:
            prediction = predict_day(
                model_path=model_path,
                meter_id=device,
                day=day_start.date(),
                history_kw=history_kw,
                history_origin=history_origin,
                weather_day=weather_day,
                meters_json=meters_data
            )

            for j in range(96):
                actual_kw = actual[j]["ap_kw"]
                origin = actual[j]["ap_kw_origin"]
                predicted_kw = prediction["forecast_kw"][j]

                if (
                    origin is not None
                    and origin < 20
                    and actual_kw is not None
                    and predicted_kw is not None
                    and np.isfinite(actual_kw)
                    and np.isfinite(predicted_kw)
                ):
                    errors.append(abs(actual_kw - predicted_kw))

    if not old_errors or not new_errors:
        return False

    old_mae = np.mean(old_errors)
    new_mae = np.mean(new_errors)

    print(f"{device} : MAE v{old_version} = {old_mae:.4f}")
    print(f"{device} : MAE v{new_version} = {new_mae:.4f}")

    if new_mae <= old_mae:
        active_versions[device] = new_version
        print(f"{device} : adoption du modèle v{new_version}")
        return True

    print(f"{device} : conservation du modèle v{old_version}")
    return True

with open(meters_json_path, "r", encoding="utf-8") as f:
    meters_data = json.load(f)

meter_categories = {
    meter["meter_id"]: meter["category"]
    for meter in meters_data["meters"]
}

weather_path = os.getenv("WEATHER_PATH", "data/weather_fribourg_15min.parquet")
weather_df = pd.read_parquet(weather_path)
weather_df["timestamp"] = pd.to_datetime(weather_df["timestamp"])

histories = {device: deque(maxlen=1344) for device in device_ids}

state_path = Path(os.getenv("STATE_PATH", "data/forecaster_state.json"))
state_path.parent.mkdir(parents=True, exist_ok=True)

last_evaluated_versions = {}

def save_state():
    state = {
        "histories": {
            device: list(history)
            for device, history in histories.items()
        },
        "active_versions": active_versions,
        "last_evaluated_versions": last_evaluated_versions
    }

    temp_path = state_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(state), encoding="utf-8")
    temp_path.replace(state_path)


def load_state():
    if not state_path.exists():
        return

    state = json.loads(state_path.read_text(encoding="utf-8"))

    for device in device_ids:
        histories[device].extend(state.get("histories", {}).get(device, []))
        active_versions[device] = state.get("active_versions", {}).get(device, 1)

    last_evaluated_versions.update(state.get("last_evaluated_versions", {}))


load_state()

def on_connect(client, userdata, flags, reason_code, properties):
    print("Connexion MQTT :", reason_code)

    for device in device_ids:
        client.subscribe(f"meters/{device}/readings")

    client.subscribe("replay/clock")


def on_message(client, userdata, msg):
    payload = json.loads(msg.payload.decode("utf-8"))

    if msg.topic.endswith("/readings"):
        device = payload["device_id"]

        if device in histories:
            histories[device].append({
                "timestamp": payload["timestamp"],
                "ap_kw": payload["ap_kw"],
                "ap_kw_origin": payload["ap_kw_origin"]
            })
            save_state()

    elif msg.topic == "replay/clock":
        timestamp = pd.Timestamp(payload["timestamp"])

        if timestamp.hour == 0 and timestamp.minute == 0 and timestamp >= pd.Timestamp("2026-04-01"):
            print(f"Nouvelle journée détectée : {timestamp.date()}")

            day_start = timestamp
            day_end = day_start + pd.Timedelta(days=1)

            weather_day_df = weather_df[
                (weather_df["timestamp"] >= day_start)
                & (weather_df["timestamp"] < day_end)
            ].sort_values("timestamp")

            if len(weather_day_df) != 96:
                print(
                    f"Météo incomplète pour {timestamp.date()} : "
                    f"{len(weather_day_df)}/96 mesures"
                )
                return

            weather_day = {
                "ghi": weather_day_df["global_radiation_wm2"].tolist(),
                "temp": weather_day_df["temp_air_2m_c"].tolist(),
                "sunshine": weather_day_df["sunshine_min"].tolist()
            }

            for device in device_ids:
                if len(histories[device]) >= 672:
                    recent_history = list(histories[device])[-672:]

                    expected_start = timestamp - pd.Timedelta(days=7)

                    actual_start = pd.Timestamp(recent_history[0]["timestamp"])
                    actual_end = pd.Timestamp(recent_history[-1]["timestamp"])

                    if (
                        actual_start != expected_start
                        or actual_end != timestamp - pd.Timedelta(minutes=15)
                    ):
                        print(f"{device} : historique mal aligné")
                        continue

                    history_kw = [
                        row["ap_kw"] for row in recent_history
                    ]

                    history_origin = [
                        row["ap_kw_origin"] for row in recent_history
                    ]

                    print(f"{device} : historique prêt pour la prédiction")

                    category = meter_categories[device]

                    new_version = available_versions[category]
                    old_version = active_versions[device]

                    if (
                        new_version > old_version
                        and new_version != last_evaluated_versions.get(device)
                        and len(histories[device]) == 1344
                    ):
                        evaluated = evaluate_model(
                            device, category, old_version, new_version, timestamp
                        )

                        if evaluated:
                            last_evaluated_versions[device] = new_version
                            save_state()

                    version = active_versions[device]
                    model_path = get_model_path(category, version)

                    prediction = predict_day(
                        model_path=model_path,
                        meter_id=device,
                        day=timestamp.date(),
                        history_kw=history_kw,
                        history_origin=history_origin,
                        weather_day=weather_day,
                        meters_json=meters_data
                    )

                    prediction["model_version"] = version

                    topic = f"meters/{device}/forecast"

                    info = client.publish(
                        topic,
                        json.dumps(prediction, allow_nan=False),
                        qos=1
                    )

                    if info.rc == mqtt.MQTT_ERR_SUCCESS:
                        print(
                            f"{device} : {len(prediction['forecast_kw'])} "
                            f"prévisions publiées pour {prediction['day']}"
                        )
                    else:
                        print(f"{device} : erreur de publication MQTT")

                else:
                    print(
                        f"{device} : historique incomplet "
                        f"({len(histories[device])}/672)"
                    )

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message

client.connect(mqtt_host, mqtt_port, 60)
client.loop_forever()