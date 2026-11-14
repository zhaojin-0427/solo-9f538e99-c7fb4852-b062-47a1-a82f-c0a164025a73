from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import daily, obs, upload


def test_validation_rejects_naive_datetime(client):
    response = upload(client, obs("O1", "2026-11-01T00:00:00", 1.0))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_validation_rejects_negative_cumulative(client):
    response = upload(client, obs("O1", "2026-11-01T00:00:00Z", -1.0))
    assert response.status_code == 422


def test_reset_flag_is_permitted_on_retracted_observation(client):
    # Retraction wins; withdrawing a reset reading must be expressible.
    response = upload(
        client, obs("R1", "2026-11-01T00:00:00Z", 1.0, is_reset=True, is_retracted=True)
    )
    assert response.status_code == 200, response.text


def test_validation_rejects_empty_batch(client):
    response = client.post("/observations", json={"observations": []})
    assert response.status_code == 422


def test_validation_rejects_unknown_fields(client):
    payload = obs("O1", "2026-11-01T00:00:00Z", 1.0)
    payload["extra"] = "nope"
    response = client.post("/observations", json={"observations": [payload]})
    assert response.status_code == 422


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_openapi_is_available(client):
    assert client.get("/openapi.json").status_code == 200


def test_large_batch_is_processed(client):
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    observations = [
        obs(
            f"O{i}",
            (start + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"),
            float(i),
        )
        for i in range(500)
    ]
    response = upload(client, *observations)
    assert response.status_code == 200
    # 499 differences total; all credited to 2026-11-01.
    day = daily(client, day="2026-11-01").json()
    assert day["rainfall_mm"] == 499.0
    assert len(day["observation_ids"]) == 499


def test_bare_list_payload_is_accepted(client):
    response = client.post(
        "/observations",
        json=[
            {
                "observation_id": "O1",
                "station_id": "S1",
                "revision": 1,
                "observed_at": "2026-11-01T00:00:00Z",
                "cumulative_mm": 1.0,
            }
        ],
    )
    assert response.status_code == 200
    assert response.json()["created"] == 1


def test_camel_case_and_short_aliases_are_accepted(client):
    response = client.post(
        "/observations",
        json={
            "observations": [
                {
                    "id": "O1",
                    "station": "S1",
                    "rev": 1,
                    "timestamp": "2026-11-01T00:00:00Z",
                    "cumulative": 1.0,
                    "isReset": False,
                },
                {
                    "observationId": "O2",
                    "stationId": "S1",
                    "revision": 1,
                    "observedAt": "2026-11-01T06:00:00Z",
                    "cumulativeMm": 3.0,
                },
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert daily(client, day="2026-11-01").json()["rainfall_mm"] == 2.0


def test_query_style_daily_endpoint(client):
    upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 0.0),
        obs("O2", "2026-11-01T12:00:00Z", 2.0),
    )
    response = client.get("/daily", params={"station_id": "S1", "date": "2026-11-01"})
    assert response.status_code == 200
    assert response.json()["rainfall_mm"] == 2.0
    assert response.json()["observation_ids"] == ["O2"]


def test_validation_error_details_are_json_serialisable(client):
    response = upload(client, obs("O1", "2026-11-01T00:00:00", 1.0))
    assert response.status_code == 422
    # A successful round-trip proves the error body is valid JSON.
    assert response.json()["error"]["code"] == "request_validation_error"
