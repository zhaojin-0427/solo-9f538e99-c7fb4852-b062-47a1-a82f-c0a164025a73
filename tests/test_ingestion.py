from __future__ import annotations

from conftest import daily, obs, upload


def test_identical_reupload_is_idempotent(client):
    payload = obs("O1", "2026-11-01T00:00:00Z", 10.0)
    payload2 = obs("O2", "2026-11-02T00:00:00Z", 12.0)

    first = upload(client, payload, payload2)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["created"] == 2

    second = upload(client, payload, payload2)
    assert second.status_code == 200
    body = second.json()
    assert body["unchanged"] == 2
    assert body["created"] == 0
    assert body["updated"] == 0
    assert body["affected_dates"] == []

    day = daily(client, day="2026-11-02").json()
    assert day["rainfall_mm"] == 2.0
    assert day["observation_ids"] == ["O2"]


def test_identical_revision_after_previous_batches_is_still_idempotent(client):
    assert upload(client, obs("O1", "2026-11-01T00:00:00Z", 1.25)).status_code == 200
    response = upload(client, obs("O1", "2026-11-01T00:00:00Z", 1.25))
    assert response.status_code == 200
    assert response.json()["unchanged"] == 1


def test_timezone_equivalent_timestamp_is_same_content(client):
    assert upload(client, obs("O1", "2026-11-01T08:00:00+08:00", 5.0)).status_code == 200
    response = upload(client, obs("O1", "2026-11-01T00:00:00Z", 5.0))
    assert response.status_code == 200
    assert response.json()["unchanged"] == 1


def test_higher_revision_replaces_older_and_updates_daily_result(client):
    upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 0.0),
        obs("O2", "2026-11-01T12:00:00Z", 5.0),
    )
    assert daily(client, day="2026-11-01").json()["rainfall_mm"] == 5.0

    correction = upload(client, obs("O2", "2026-11-01T12:00:00Z", 8.0, revision=2))
    assert correction.status_code == 200, correction.text
    assert correction.json()["updated"] == 1
    assert correction.json()["affected_dates"] == ["2026-11-01"]

    day = daily(client, day="2026-11-01").json()
    assert day["rainfall_mm"] == 8.0
    assert day["observation_ids"] == ["O2"]


def test_lower_revision_is_ignored(client):
    upload(client, obs("O1", "2026-11-01T00:00:00Z", 0.0))
    upload(client, obs("O2", "2026-11-01T12:00:00Z", 7.0, revision=3))

    stale = upload(client, obs("O2", "2026-11-01T12:00:00Z", 99.0, revision=2))
    assert stale.status_code == 200
    assert stale.json()["unchanged"] == 1
    assert daily(client, day="2026-11-01").json()["rainfall_mm"] == 7.0


def test_conflicting_content_at_same_revision_rejects_whole_batch(client):
    upload(client, obs("O1", "2026-11-01T00:00:00Z", 0.0))

    response = upload(
        client,
        obs("O2", "2026-11-01T12:00:00Z", 5.0),
        obs("O1", "2026-11-01T00:00:00Z", 1.0),  # revision 1 conflict
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "batch_rejected"
    assert body["error"]["details"][0]["code"] == "revision_conflict"

    # The O2 in the rejected batch was never stored.
    assert daily(client, day="2026-11-01").json()["rainfall_mm"] == 0.0
    observations = client.get("/stations/S1/observations").json()["observations"]
    assert [item["observation_id"] for item in observations] == ["O1"]


def test_duplicate_active_observation_at_same_instant_rejects(client):
    response = upload(
        client,
        obs("A", "2026-11-01T06:00:00Z", 1.0),
        obs("B", "2026-11-01T08:00:00+02:00", 2.0),  # same UTC instant
    )
    assert response.status_code == 409
    details = response.json()["error"]["details"]
    assert details[0]["code"] == "duplicate_active_observation"


def test_retracted_observation_does_not_count_as_duplicate(client):
    response = upload(
        client,
        obs("A", "2026-11-01T06:00:00Z", 1.0),
        obs("B", "2026-11-01T06:00:00Z", 2.0, is_retracted=True),
    )
    assert response.status_code == 200, response.text


def test_conflict_across_batches_also_rejects(client):
    upload(client, obs("A", "2026-11-01T06:00:00Z", 1.0, is_retracted=True))
    response = upload(client, obs("B", "2026-11-01T06:00:00Z", 2.0, is_retracted=True))
    assert response.status_code == 200  # retracted rows never conflict

    upload(client, obs("A", "2026-11-01T06:00:00Z", 1.0, revision=2))
    response = upload(client, obs("B", "2026-11-01T06:00:00Z", 2.0))
    assert response.status_code == 409


def test_regression_without_reset_rejects_whole_batch(client):
    response = upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 10.0),
        obs("O2", "2026-11-01T06:00:00Z", 12.0),
        obs("O3", "2026-11-01T12:00:00Z", 11.0),
    )
    assert response.status_code == 409
    details = response.json()["error"]["details"]
    assert details[0]["code"] == "cumulative_regression_without_reset"
    assert details[0]["observation_id"] == "O3"

    assert client.get("/stations/S1/observations").json()["observations"] == []


def test_regression_into_reset_is_allowed(client):
    response = upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 10.0),
        obs("R1", "2026-11-01T06:00:00Z", 1.0, is_reset=True),
        obs("O2", "2026-11-01T12:00:00Z", 3.0),
    )
    assert response.status_code == 200, response.text
    assert daily(client, day="2026-11-01").json()["rainfall_mm"] == 2.0


def test_regression_hidden_by_later_reading_is_caught(client):
    response = upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 10.0),
        obs("O2", "2026-11-01T06:00:00Z", 5.0),
        obs("O3", "2026-11-01T12:00:00Z", 11.0),
    )
    assert response.status_code == 409


def test_retraction_removes_contribution_and_reverts_baseline(client):
    upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 0.0),
        obs("O2", "2026-11-01T06:00:00Z", 5.0),
        obs("O3", "2026-11-01T12:00:00Z", 9.0),
    )
    assert daily(client, day="2026-11-01").json()["rainfall_mm"] == 9.0

    retract = upload(client, obs("O2", "2026-11-01T06:00:00Z", 5.0, revision=2, is_retracted=True))
    assert retract.status_code == 200, retract.text
    assert retract.json()["affected_dates"] == ["2026-11-01"]

    day = daily(client, day="2026-11-01").json()
    # O3 now follows O1 directly.
    assert day["rainfall_mm"] == 9.0
    assert day["observation_ids"] == ["O3"]


def test_retraction_of_all_data_clears_daily_result(client):
    upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 0.0),
        obs("O2", "2026-11-01T06:00:00Z", 5.0),
    )
    assert daily(client, day="2026-11-01").json()["rainfall_mm"] == 5.0

    upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 0.0, revision=2, is_retracted=True),
        obs("O2", "2026-11-01T06:00:00Z", 5.0, revision=2, is_retracted=True),
    )
    day = daily(client, day="2026-11-01").json()
    assert day["rainfall_mm"] == 0.0
    assert day["observation_ids"] == []


def test_late_arrival_updates_both_adjacent_days(client):
    upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 0.0),
        obs("O3", "2026-11-02T00:00:00Z", 10.0),
    )
    assert daily(client, day="2026-11-01").json()["rainfall_mm"] == 0.0
    assert daily(client, day="2026-11-02").json()["rainfall_mm"] == 10.0

    late = upload(client, obs("O2", "2026-11-01T12:00:00Z", 4.0))
    assert late.status_code == 200
    assert late.json()["affected_dates"] == ["2026-11-01", "2026-11-02"]

    assert daily(client, day="2026-11-01").json()["rainfall_mm"] == 4.0
    assert daily(client, day="2026-11-02").json()["rainfall_mm"] == 6.0


def test_correction_moving_observation_to_another_day_updates_both(client):
    upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 0.0),
        obs("O2", "2026-11-01T23:00:00Z", 5.0),
        obs("O3", "2026-11-02T12:00:00Z", 9.0),
    )
    assert daily(client, day="2026-11-01").json()["rainfall_mm"] == 5.0
    assert daily(client, day="2026-11-02").json()["rainfall_mm"] == 4.0

    # The midnight-adjacent reading really happened after midnight UTC.
    upload(client, obs("O2", "2026-11-02T01:00:00Z", 5.0, revision=2))

    assert daily(client, day="2026-11-01").json()["rainfall_mm"] == 0.0
    second = daily(client, day="2026-11-02").json()
    assert second["rainfall_mm"] == 9.0
    assert second["observation_ids"] == ["O2", "O3"]


def test_failure_preserves_existing_observations_and_days(client):
    upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 0.0),
        obs("O2", "2026-11-01T12:00:00Z", 6.0),
    )
    before_day = daily(client, day="2026-11-01").json()
    before_obs = client.get("/stations/S1/observations").json()

    failed = upload(
        client,
        obs("O2", "2026-11-01T12:00:00Z", 6.0, revision=2),  # would be a valid update
        obs("O3", "2026-11-01T18:00:00Z", 2.0),  # regression -> whole batch rejects
    )
    assert failed.status_code == 409

    assert daily(client, day="2026-11-01").json() == before_day
    assert client.get("/stations/S1/observations").json() == before_obs


def test_failure_in_one_station_does_not_change_other_station(client):
    upload(client, obs("A1", "2026-11-01T00:00:00Z", 0.0, station_id="S1"))
    upload(client, obs("B1", "2026-11-01T00:00:00Z", 0.0, station_id="S2"))
    upload(client, obs("B2", "2026-11-01T10:00:00Z", 4.0, station_id="S2"))

    failed = upload(
        client,
        obs("A2", "2026-11-01T10:00:00Z", 3.0, station_id="S1"),
        obs("B1", "2026-11-01T00:00:00Z", 1.0, station_id="S2"),  # revision conflict
    )
    assert failed.status_code == 409
    assert daily(client, station_id="S2", day="2026-11-01").json()["rainfall_mm"] == 4.0
    assert client.get("/stations/S1/observations").json()["observations"] == [
        {
            "observation_id": "A1",
            "station_id": "S1",
            "revision": 1,
            "observed_at": "2026-11-01T00:00:00Z",
            "cumulative_mm": 0.0,
            "is_reset": False,
            "is_retracted": False,
        }
    ]


def test_conflict_can_be_resolved_within_one_batch(client):
    upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 0.0),
        obs("O2", "2026-11-01T06:00:00Z", 3.0),
        obs("O3", "2026-11-01T12:00:00Z", 6.0),
    )
    # Moving O2 onto O3's instant only succeeds because the same batch also
    # retracts O3, leaving one active observation per instant.
    response = upload(
        client,
        obs("O2", "2026-11-01T12:00:00Z", 3.0, revision=2),
        obs("O3", "2026-11-01T12:00:00Z", 6.0, revision=2, is_retracted=True),
    )
    assert response.status_code == 200, response.text
    day = daily(client, day="2026-11-01").json()
    assert day["rainfall_mm"] == 3.0
    assert day["observation_ids"] == ["O2"]


def test_unretracting_into_a_conflict_is_rejected(client):
    upload(
        client,
        obs("A", "2026-11-01T06:00:00Z", 1.0),
        obs("B", "2026-11-01T06:00:00Z", 2.0, is_retracted=True),
    )
    restored = upload(client, obs("B", "2026-11-01T06:00:00Z", 2.0, revision=2))
    assert restored.status_code == 409
    assert restored.json()["error"]["details"][0]["code"] == "duplicate_active_observation"


def test_retraction_can_clear_a_regression(client):
    upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 0.0),
        obs("SPIKE", "2026-11-01T06:00:00Z", 50.0),
    )
    # A later reading of 30 after the spike would be a regression on its own.
    # Retracting the spike in the same batch keeps 0 -> 30 valid.
    fixed = upload(
        client,
        obs("O3", "2026-11-01T12:00:00Z", 30.0),
        obs("SPIKE", "2026-11-01T06:00:00Z", 50.0, revision=2, is_retracted=True),
    )
    assert fixed.status_code == 200, fixed.text
    day = daily(client, day="2026-11-01").json()
    assert day["rainfall_mm"] == 30.0
    assert day["observation_ids"] == ["O3"]


def test_duplicate_observation_in_one_batch_with_conflicting_content_rejects(client):
    response = upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 1.0),
        obs("O1", "2026-11-01T00:00:00Z", 2.0),
    )
    assert response.status_code == 409
    assert response.json()["error"]["details"][0]["code"] == "revision_conflict"


def test_later_revision_within_same_batch_wins(client):
    upload(client, obs("O1", "2026-11-01T00:00:00Z", 0.0))
    response = upload(
        client,
        obs("O2", "2026-11-01T12:00:00Z", 3.0, revision=1),
        obs("O2", "2026-11-01T12:00:00Z", 4.0, revision=2),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] == 1
    assert body["unchanged"] == 1

    assert daily(client, day="2026-11-01").json()["rainfall_mm"] == 4.0
    observations = client.get("/stations/S1/observations").json()["observations"]
    by_id = {item["observation_id"]: item for item in observations}
    assert by_id["O2"]["revision"] == 2
    assert by_id["O2"]["cumulative_mm"] == 4.0


def test_identical_duplicate_in_one_batch_is_noop(client):
    response = upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 3.0),
        obs("O1", "2026-11-01T00:00:00Z", 3.0),
    )
    assert response.status_code == 200
    assert response.json()["created"] == 1
    assert response.json()["unchanged"] == 1
