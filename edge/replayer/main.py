import paho.mqtt.client as mqtt
import pandas as pd
import time
import json
from pathlib import Path
import os

# Configuration
device_ids_env = os.getenv("DEVICE_IDS")

if not device_ids_env:
    raise ValueError("DEVICE_IDS doit être configurée")

device_ids = device_ids_env.split(",")

replay_speed_env = os.getenv("REPLAY_SPEED")

if not replay_speed_env:
    raise ValueError("REPLAY_SPEED doit être configurée")

replay_speed = int(replay_speed_env)

if replay_speed <= 0:
    raise ValueError("REPLAY_SPEED doit être supérieur à 0")

delay = 900 / replay_speed

replay_start_env = os.getenv("REPLAY_START")
replay_end_env = os.getenv("REPLAY_END")

if not replay_start_env or not replay_end_env:
    raise ValueError("REPLAY_START et REPLAY_END doivent être configurées")

replay_start = pd.Timestamp(replay_start_env)
replay_end = pd.Timestamp(replay_end_env)

# Configuration MQTT Broker

mqtt_host = os.getenv("MQTT_HOST", "localhost")
mqtt_port = int(os.getenv("MQTT_PORT", "1883"))

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.connect(mqtt_host, mqtt_port, 60)
client.loop_start()

# Chemin des données
data_dir = Path(os.getenv(
    "DATA_DIR",
    r"C:\Users\crafa\TSM_CLOUDSYS\cloudsys-p4-students - Copie\data\meters"
))

# Chargement des données pour chaque compteur
dfs = {}

for device in device_ids:
    filename = f"meter_{device}.parquet"
    file_path = data_dir / filename

    df = pd.read_parquet(file_path)

    df = df[(df['timestamp'] >= replay_start) & (df['timestamp'] < replay_end)]
    dfs[device] = df.sort_values(by='timestamp').reset_index(drop=True)

# Replay des données pour chaque compteur

iterators = [dfs[device].iterrows() for device in device_ids]

for records in zip(*iterators):

    rows = [row for index, row in records]

    clocktopic = f"replay/clock"
    
    clock_info =client.publish(clocktopic, json.dumps({
        "timestamp": rows[0]["timestamp"].isoformat(),
        "timestamp_utc": rows[0]['timestamp_utc'].isoformat()  if pd.notna(rows[0]["timestamp_utc"]) else None
    }))
    
    for device, row in zip(device_ids, rows):
            topic = f"meters/{device}/readings"

            client.publish(topic, json.dumps({
                            "device_id": device,
                            "timestamp": row['timestamp'].isoformat(),
                            "timestamp_utc":  row["timestamp_utc"].isoformat() if pd.notna(row["timestamp_utc"]) else None,
                            "ap_kw": float(row["ap_kw"]) if pd.notna(row["ap_kw"]) else None,
                            "ap_kw_origin": int(row['ap_kw_origin'])
                        }, allow_nan=False))

    time.sleep(delay)

clock_info.wait_for_publish()

# MQTT Cleanup
client.disconnect()
client.loop_stop()

