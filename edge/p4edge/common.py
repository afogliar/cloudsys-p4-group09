"""Shared helpers for the p4edge services.

Only the standard library is imported at module level, so the pure logic in
this package stays unit-testable without pandas/paho/requests/numpy/kit
(see edge/tests). Heavy dependencies are imported lazily inside functions.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("p4edge.common")

SLOT = timedelta(minutes=15)
SLOTS_PER_DAY = 96
HISTORY_DAYS = 14          # forecaster keeps 14 days of readings (1344 slots)
FORECAST_HISTORY_DAYS = 7  # kit predict_day wants 672 slots: D-7 .. D-1
ACCEPT_DAYS = 7            # local-acceptance window (last 7 complete days)
ACCEPT_ORIGIN_LIMIT = 90   # acceptance MAE slots: origin < 90 (not missing); documented choice
FORECAST_FROM = "2026-04-01"  # first forecast / evaluation day (replay split B)


# ---------------------------------------------------------------------------
# JSON / atomic files
# ---------------------------------------------------------------------------

def atomic_write_json(path: Path, obj: Any) -> None:
    """Write JSON atomically: tmp file + fsync + rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, allow_nan=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def load_json(path: Path, default: Any = None) -> Any:
    try:
        with open(Path(path), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return default


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# simulated-time helpers (simulated timestamps are naive local ISO strings)
# ---------------------------------------------------------------------------

def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(str(value)[:19])


def day_of(ts: datetime) -> str:
    return ts.date().isoformat()


def day_start(day: str) -> datetime:
    return datetime.fromisoformat(day)


def slot_times(day: str, count: int = SLOTS_PER_DAY) -> List[datetime]:
    start = day_start(day)
    return [start + SLOT * i for i in range(count)]


# ---------------------------------------------------------------------------
# environment / meters.json
# ---------------------------------------------------------------------------

def env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


def parse_device_ids(raw: str) -> List[str]:
    devices = [item.strip() for item in (raw or "").split(",") if item.strip()]
    if not devices:
        raise SystemExit(
            "DEVICE_IDS is required (comma separated meter ids, e.g. 27afc1ed,5c3492e5)"
        )
    return devices


def load_meters(path: Path) -> dict:
    with open(Path(path), "r", encoding="utf-8") as handle:
        return json.load(handle)


def meter_categories(meters: dict) -> Dict[str, str]:
    return {entry["meter_id"]: entry["category"] for entry in meters["meters"]}


def loop_params(meters: dict) -> dict:
    return meters.get("loop", {})


# ---------------------------------------------------------------------------
# process stop handling
# ---------------------------------------------------------------------------

def make_stop() -> threading.Event:
    """Event set on SIGTERM/SIGINT so containers shut down cleanly."""
    stop = threading.Event()

    def handler(signum, _frame):
        log.info("signal %s received; shutting down", signum)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except ValueError:  # not in the main thread
            pass
    return stop


# ---------------------------------------------------------------------------
# MQTT helpers (paho is imported lazily)
# ---------------------------------------------------------------------------

def connect_mqtt(
    host: str,
    port: int,
    client_id: str,
    on_connect: Optional[Any] = None,
    on_message: Optional[Any] = None,
    stop: Optional[threading.Event] = None,
    connected: Optional[threading.Event] = None,
):
    """Connect with retry; returns (client, connected_event).

    After the first successful connect, paho's network loop handles
    reconnection; on_connect fires on every (re)connect, so subscriptions
    belong there.
    """
    import paho.mqtt.client as mqtt

    stop = stop if stop is not None else threading.Event()
    connected = connected if connected is not None else threading.Event()

    def _on_connect(client, userdata, flags, reason_code, properties):
        if getattr(reason_code, "is_success", True):
            log.info("MQTT connected to %s:%s", host, port)
            connected.set()
            if on_connect:
                try:
                    on_connect(client)
                except Exception:
                    log.exception("on_connect handler failed")
        else:
            log.error("MQTT connection refused: %s", reason_code)

    def _on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
        connected.clear()
        log.warning("MQTT disconnected: %s", reason_code)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    if on_message is not None:
        client.on_message = on_message

    while not stop.is_set():
        try:
            client.connect(host, port, keepalive=60)
        except OSError as exc:
            log.warning("MQTT connect to %s:%s failed (%s); retrying", host, port, exc)
            stop.wait(2.0)
            continue
        client.loop_start()
        break
    return client, connected


def publish_qos1(
    client,
    topic: str,
    payload: str,
    connected: threading.Event,
    stop: Optional[threading.Event] = None,
    retain: bool = False,
    ack_timeout: float = 15.0,
) -> bool:
    """Publish with QoS 1 and wait for the broker ack; retries until sent.

    Returns False only when ``stop`` was set, so callers never silently drop
    a message. Downstream consumers also dedupe by (device, timestamp).
    """
    import paho.mqtt.client as mqtt

    stop = stop if stop is not None else threading.Event()
    while not stop.is_set():
        if not connected.wait(0.5):
            continue
        try:
            info = client.publish(topic, payload, qos=1, retain=retain)
        except (RuntimeError, ValueError) as exc:
            log.warning("publish %s failed: %s", topic, exc)
            connected.wait(0.5)
            continue
        if info.rc == mqtt.MQTT_ERR_SUCCESS:
            try:
                info.wait_for_publish(ack_timeout)
            except (RuntimeError, ValueError):
                pass
            if info.is_published():
                return True
        stop.wait(0.5)
    return False


def publish_qos0(client, topic: str, payload: str) -> None:
    """Fire-and-forget publish (used for replay/clock; consumers recover)."""
    try:
        client.publish(topic, payload, qos=0)
    except (RuntimeError, ValueError) as exc:
        log.warning("publish %s failed: %s", topic, exc)
