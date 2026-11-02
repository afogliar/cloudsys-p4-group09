"""Nuvla rollout requests for accepted releases; never fails a training run.

Called with the category advisory lock held, after MLflow publication. The
rollout only ever concerns the version currently pointed to by
model_releases, and only once that version's publication is complete. A
successful rollout updates the Nuvla deployment group; it does NOT mean the
edge devices already run the model: each device applies its own local
acceptance gate afterwards.
"""
import json
import os
from datetime import datetime, timezone

from psycopg.rows import dict_row

from app.database import connect

# Statuses request_model_rollout may report (plus 'error' for our own bookkeeping).
API_STATES = frozenset({"updated", "unchanged", "skipped", "disabled", "error"})
# States that end the retry loop for a given accepted version. 'skipped' means
# Nuvla already runs a higher version; 'disabled' means the rollout feature is
# unavailable in this deployment.
TERMINAL_STATES = frozenset({"updated", "unchanged", "skipped", "disabled"})
# States from which another request attempt may be made on a later pass. NULL
# is treated as pending (crash between publication and the first attempt).
RETRY_STATES = frozenset({None, "pending", "error"})


def retry_seconds():
    """Minimum real seconds between two rollout attempts after an error."""
    try:
        return max(float(os.environ.get("NUVLA_ROLLOUT_RETRY_SECONDS", "300")), 0.0)
    except ValueError:
        return 300.0


def request_model_rollout(category, version):
    """Lazy adapter over trainer.nuvla (owned separately, may be absent)."""
    try:
        from trainer.nuvla import request_model_rollout as request
    except ImportError:
        return {"status": "disabled", "category": category, "version": version,
                "detail": "nuvla module unavailable"}
    return request(category, version)


def _record(category, version, run_id, status, detail):
    with connect() as conn:
        conn.execute("""
            UPDATE training_runs SET nuvla_rollout_status=%s, nuvla_rollout_detail=%s,
                nuvla_rollout_at=NOW() WHERE run_id=%s
        """, (status, detail, run_id))
        conn.execute("""
            UPDATE model_versions SET nuvla_rollout_status=%s
            WHERE category=%s AND version=%s
        """, (status, category, version))


def _attempt(category, version, run_id):
    try:
        response = request_model_rollout(category, version)
    except Exception as exc:
        status, detail = "error", type(exc).__name__
    else:
        raw = response.get("status") if isinstance(response, dict) else None
        status = raw if raw in API_STATES else "error"
        detail = (response.get("detail") or "") if isinstance(response, dict) else "malformed rollout response"
    detail = str(detail)[:500]
    _record(category, version, run_id, status, detail)
    # One JSON line per attempt, same style as the worker; never any secret.
    print(json.dumps({"event": "nuvla_rollout", "category": category, "version": version,
                      "run_id": str(run_id), "status": status, "detail": detail}), flush=True)
    return {"category": category, "version": version, "status": status,
            "detail": detail, "requested": True}


def sync_rollouts(category):
    """Request/refresh the Nuvla rollout of the released version, idempotently.

    Caller must hold the trainer's category advisory lock. Never raises:
    failures are recorded as 'error' and retried on a later pass, spaced by
    NUVLA_ROLLOUT_RETRY_SECONDS after the last failed attempt.
    """
    try:
        with connect() as conn, conn.cursor(row_factory=dict_row) as cursor:
            release = cursor.execute(
                "SELECT version FROM model_releases WHERE category=%s", (category,)
            ).fetchone()
            if release is None:
                return None
            version = release["version"]
            run = cursor.execute("""
                SELECT run_id, nuvla_rollout_at FROM training_runs
                WHERE category=%s AND candidate_version=%s
                  AND status='completed' AND publication_status='released'
                ORDER BY candidate_version LIMIT 1
            """, (category, version)).fetchone()
            recorded = cursor.execute("""
                SELECT nuvla_rollout_status FROM model_versions
                WHERE category=%s AND version=%s
            """, (category, version)).fetchone()
        recorded = recorded["nuvla_rollout_status"] if recorded else None
        if run is None:
            # Bootstrap version or publication not finished: nothing to request.
            return {"category": category, "version": version,
                    "status": "not_eligible", "requested": False}
        if recorded in TERMINAL_STATES or recorded not in RETRY_STATES:
            # Already rolled out (updated/unchanged), remotely higher (skipped),
            # disabled, or an unknown state: never call Nuvla again for it.
            return {"category": category, "version": version, "status": recorded,
                    "requested": False}
        if recorded == "error" and run["nuvla_rollout_at"] is not None:
            age = (datetime.now(timezone.utc) - run["nuvla_rollout_at"]).total_seconds()
            if age < retry_seconds():
                return {"category": category, "version": version, "status": "error",
                        "requested": False, "detail": "retry deferred"}
        return _attempt(category, version, run["run_id"])
    except Exception as exc:
        print(json.dumps({"event": "nuvla_rollout", "category": category,
                          "status": "error", "detail": type(exc).__name__}), flush=True)
        return {"category": category, "status": "error",
                "detail": type(exc).__name__, "requested": False}
