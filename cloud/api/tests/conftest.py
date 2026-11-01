import os
from uuid import uuid4

import pytest

# Fictitious token only; never read the developer's .env during unit tests.
os.environ.setdefault("DEVICE_TOKENS_JSON", '{"27afc1ed":"' + "t" * 64 + '"}')


@pytest.fixture
def payload():
    return {
        "batch_id": str(uuid4()), "device_id": "27afc1ed", "category": "cons",
        "model_version": 1, "day": "2026-04-01",
        "mae_kw": 0.5, "mae_persistence_kw": 0.2,
        "forecast_kw": [1.0] * 96, "actual_kw": [1.5] * 96,
        "actual_origin": [0] * 96, "history_kw": [1.3] * 672,
        "history_origin": [0] * 672, "sent_at": "2026-10-30T12:00:00Z",
    }
