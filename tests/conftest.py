from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    app = create_app(db)
    # TestClient runs lifespan on context entry, creating the schema.
    with TestClient(app) as test_client:
        yield test_client


def obs(
    observation_id: str,
    observed_at: str,
    cumulative_mm: float,
    *,
    station_id: str = "S1",
    revision: int = 1,
    is_reset: bool = False,
    is_retracted: bool = False,
) -> dict:
    return {
        "observation_id": observation_id,
        "station_id": station_id,
        "revision": revision,
        "observed_at": observed_at,
        "cumulative_mm": cumulative_mm,
        "is_reset": is_reset,
        "is_retracted": is_retracted,
    }


def upload(client, *observations: dict):
    return client.post("/observations", json={"observations": list(observations)})


def daily(client, station_id: str = "S1", day: str = "2026-11-01"):
    return client.get(f"/stations/{station_id}/daily/{day}")
