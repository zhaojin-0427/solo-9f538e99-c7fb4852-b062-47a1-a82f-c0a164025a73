from __future__ import annotations

from conftest import daily, obs, upload


def test_difference_between_consecutive_readings_is_credited_to_later_day(client):
    response = upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 10.0),
        obs("O2", "2026-11-01T12:00:00+08:00", 14.5),  # 04:00 UTC, same day
        obs("O3", "2026-11-02T00:00:00Z", 20.0),
    )
    assert response.status_code == 200, response.text

    first = daily(client, day="2026-11-01").json()
    assert first["rainfall_mm"] == 4.5
    assert first["observation_ids"] == ["O2"]

    second = daily(client, day="2026-11-02").json()
    assert second["rainfall_mm"] == 5.5
    assert second["observation_ids"] == ["O3"]


def test_first_reading_only_establishes_baseline(client):
    assert upload(client, obs("O1", "2026-11-01T00:00:00Z", 42.0)).status_code == 200
    result = daily(client, day="2026-11-01").json()
    assert result["rainfall_mm"] == 0.0
    assert result["observation_ids"] == []


def test_reset_breaks_segment_and_supplies_new_baseline(client):
    response = upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 100.0),
        obs("O2", "2026-11-01T06:00:00Z", 105.0),
        obs("R1", "2026-11-01T12:00:00Z", 2.0, is_reset=True),
        obs("O3", "2026-11-01T18:00:00Z", 5.0),
    )
    assert response.status_code == 200, response.text
    day = daily(client, day="2026-11-01").json()
    # 5 (O2) + 3 (O3); reset itself contributes nothing.
    assert day["rainfall_mm"] == 8.0
    assert day["observation_ids"] == ["O2", "O3"]


def test_reset_with_no_predecessor_contributes_nothing(client):
    response = upload(
        client,
        obs("R1", "2026-11-01T00:00:00Z", 50.0, is_reset=True),
        obs("O2", "2026-11-01T06:00:00Z", 52.5),
    )
    assert response.status_code == 200, response.text
    assert daily(client, day="2026-11-01").json()["rainfall_mm"] == 2.5


def test_zero_increment_is_omitted_from_contributors(client):
    response = upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 10.0),
        obs("O2", "2026-11-01T06:00:00Z", 10.0),
        obs("O3", "2026-11-01T12:00:00Z", 11.0),
    )
    assert response.status_code == 200, response.text
    day = daily(client, day="2026-11-01").json()
    assert day["rainfall_mm"] == 1.0
    assert day["observation_ids"] == ["O3"]


def test_fractional_accumulator_arithmetic_is_exact(client):
    response = upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 0.0),
        obs("O2", "2026-11-01T01:00:00Z", 0.1),
        obs("O3", "2026-11-01T02:00:00Z", 0.3),
    )
    assert response.status_code == 200, response.text
    assert daily(client, day="2026-11-01").json()["rainfall_mm"] == 0.3


def test_unknown_day_reports_zero_not_error(client):
    response = daily(client, station_id="NOPE", day="2026-11-01")
    assert response.status_code == 200
    assert response.json()["rainfall_mm"] == 0.0
    assert response.json()["observation_ids"] == []


def test_daily_query_requires_iso_date(client):
    assert client.get("/stations/S1/daily/not-a-date").status_code == 422
