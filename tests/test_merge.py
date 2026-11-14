from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from conftest import daily, obs, upload
from fastapi.testclient import TestClient

from app.db import Database
from app.main import create_app
from app.models import ObservationIn
from app.rainfall import (
    get_daily_rainfall,
    ingest_batch,
    list_station_observations,
    merge_stations,
    resolve_station,
)


def merge(client, source: str, target: str):
    return client.post(
        "/stations/merge",
        json={"source_station_id": source, "target_station_id": target},
    )


def test_merge_combines_observations_and_daily_reports(client):
    upload(
        client,
        obs("O1", "2026-11-01T00:00:00Z", 0.0, station_id="OLD1"),
        obs("O2", "2026-11-01T06:00:00Z", 3.0, station_id="OLD1"),
    )
    upload(
        client,
        obs("N1", "2026-11-01T12:00:00Z", 8.0, station_id="NEW1"),
        obs("N2", "2026-11-01T18:00:00Z", 9.0, station_id="NEW1"),
    )

    response = merge(client, "OLD1", "NEW1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source_station_id"] == "OLD1"
    assert body["target_station_id"] == "NEW1"
    assert body["canonical_station_id"] == "NEW1"
    assert body["already_merged"] is False
    assert body["source_observations"] == 2
    assert body["merged_observations"] == 4
    assert body["affected_dates"] == ["2026-11-01"]

    # Both ids now serve the merged result, reported under the canonical id.
    for station in ("OLD1", "NEW1"):
        result = daily(client, station_id=station, day="2026-11-01").json()
        assert result["station_id"] == "NEW1"
        assert result["rainfall_mm"] == 9.0
        assert result["observation_ids"] == ["N1", "N2", "O2"]

        listing = client.get(f"/stations/{station}/observations").json()
        assert listing["station_id"] == "NEW1"
        ids = [item["observation_id"] for item in listing["observations"]]
        assert ids == ["O1", "O2", "N1", "N2"]
        assert all(item["station_id"] == "NEW1" for item in listing["observations"])


def test_merge_keeps_higher_revision_for_shared_observation_id(client):
    upload(
        client,
        obs("A", "2026-11-01T00:00:00Z", 0.0, station_id="OLD1"),
        obs("X", "2026-11-01T06:00:00Z", 5.0, station_id="OLD1", revision=2),
    )
    upload(
        client,
        obs("X", "2026-11-01T06:00:00Z", 4.0, station_id="NEW1", revision=1),
        obs("B", "2026-11-01T12:00:00Z", 7.0, station_id="NEW1"),
    )

    response = merge(client, "OLD1", "NEW1")
    assert response.status_code == 200, response.text
    assert response.json()["merged_observations"] == 3

    listing = client.get("/stations/NEW1/observations").json()["observations"]
    by_id = {item["observation_id"]: item for item in listing}
    assert by_id["X"]["revision"] == 2
    assert by_id["X"]["cumulative_mm"] == 5.0
    # Merged timeline: 0 -> 5 -> 7.
    assert daily(client, station_id="NEW1", day="2026-11-01").json()["rainfall_mm"] == 7.0


def test_merge_keeps_target_revision_when_it_is_higher(client):
    upload(client, obs("X", "2026-11-01T06:00:00Z", 4.0, station_id="OLD1", revision=1))
    upload(client, obs("X", "2026-11-01T06:00:00Z", 5.0, station_id="NEW1", revision=3))

    response = merge(client, "OLD1", "NEW1")
    assert response.status_code == 200, response.text
    assert response.json()["merged_observations"] == 1

    listing = client.get("/stations/NEW1/observations").json()["observations"]
    assert len(listing) == 1
    assert listing[0]["revision"] == 3
    assert listing[0]["cumulative_mm"] == 5.0


def test_merge_deduplicates_identical_shared_revision(client):
    upload(
        client,
        obs("A", "2026-11-01T00:00:00Z", 0.0, station_id="OLD1"),
        obs("X", "2026-11-01T06:00:00Z", 2.0, station_id="OLD1"),
    )
    upload(
        client,
        obs("X", "2026-11-01T06:00:00Z", 2.0, station_id="NEW1"),
        obs("B", "2026-11-01T12:00:00Z", 5.0, station_id="NEW1"),
    )

    response = merge(client, "OLD1", "NEW1")
    assert response.status_code == 200, response.text
    assert response.json()["merged_observations"] == 3
    assert daily(client, station_id="NEW1", day="2026-11-01").json()["rainfall_mm"] == 5.0


def test_merge_rejects_shared_revision_with_different_content(client):
    upload(client, obs("X", "2026-11-01T06:00:00Z", 2.0, station_id="OLD1"))
    upload(client, obs("X", "2026-11-01T06:00:00Z", 3.0, station_id="NEW1"))

    response = merge(client, "OLD1", "NEW1")
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "merge_rejected"
    assert body["error"]["details"][0]["code"] == "revision_conflict"
    assert body["error"]["details"][0]["observation_id"] == "X"

    # Both stations keep their original data ...
    old_listing = client.get("/stations/OLD1/observations").json()
    new_listing = client.get("/stations/NEW1/observations").json()
    assert old_listing["observations"][0]["cumulative_mm"] == 2.0
    assert new_listing["observations"][0]["cumulative_mm"] == 3.0
    # ... and no alias was recorded: the old id still resolves to itself.
    assert old_listing["station_id"] == "OLD1"


def test_merge_rejects_same_revision_with_different_timestamp(client):
    upload(client, obs("X", "2026-11-01T06:00:00Z", 2.0, station_id="OLD1"))
    upload(client, obs("X", "2026-11-01T07:00:00Z", 2.0, station_id="NEW1"))

    response = merge(client, "OLD1", "NEW1")
    assert response.status_code == 409
    assert response.json()["error"]["details"][0]["code"] == "revision_conflict"


def test_merge_rejects_duplicate_active_instant_across_stations(client):
    upload(client, obs("A", "2026-11-01T06:00:00Z", 1.0, station_id="OLD1"))
    # Same UTC instant as A, written with a different offset.
    upload(client, obs("B", "2026-11-01T08:00:00+02:00", 2.0, station_id="NEW1"))

    response = merge(client, "OLD1", "NEW1")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "merge_rejected"
    assert response.json()["error"]["details"][0]["code"] == "duplicate_active_observation"

    # Nothing migrated: both stations still hold exactly their own rows.
    assert [
        item["observation_id"]
        for item in client.get("/stations/OLD1/observations").json()["observations"]
    ] == ["A"]
    assert [
        item["observation_id"]
        for item in client.get("/stations/NEW1/observations").json()["observations"]
    ] == ["B"]


def test_merge_allows_same_instant_when_one_side_is_retracted(client):
    upload(client, obs("A", "2026-11-01T06:00:00Z", 1.0, station_id="OLD1"))
    upload(
        client,
        obs("B", "2026-11-01T06:00:00Z", 2.0, station_id="NEW1", is_retracted=True),
    )

    response = merge(client, "OLD1", "NEW1")
    assert response.status_code == 200, response.text
    assert response.json()["merged_observations"] == 2
    listing = client.get("/stations/NEW1/observations").json()["observations"]
    by_id = {item["observation_id"]: item for item in listing}
    assert by_id["B"]["is_retracted"] is True


def test_merge_rejects_cumulative_regression_across_stations(client):
    upload(
        client,
        obs("A", "2026-11-01T00:00:00Z", 10.0, station_id="OLD1"),
        obs("B", "2026-11-01T06:00:00Z", 12.0, station_id="OLD1"),
    )
    upload(client, obs("C", "2026-11-01T12:00:00Z", 11.0, station_id="NEW1"))

    response = merge(client, "OLD1", "NEW1")
    assert response.status_code == 409
    details = response.json()["error"]["details"]
    assert details[0]["code"] == "cumulative_regression_without_reset"
    assert details[0]["observation_id"] == "C"

    # No partial daily results and no alias were left behind.
    assert daily(client, station_id="OLD1", day="2026-11-01").json()["rainfall_mm"] == 2.0
    assert daily(client, station_id="NEW1", day="2026-11-01").json()["rainfall_mm"] == 0.0
    assert client.get("/stations/OLD1/observations").json()["station_id"] == "OLD1"


def test_merge_allows_regression_marked_as_reset(client):
    upload(
        client,
        obs("A", "2026-11-01T00:00:00Z", 10.0, station_id="OLD1"),
        obs("B", "2026-11-01T06:00:00Z", 12.0, station_id="OLD1"),
    )
    upload(
        client,
        obs("R", "2026-11-01T12:00:00Z", 1.0, station_id="NEW1", is_reset=True),
        obs("C", "2026-11-01T18:00:00Z", 3.0, station_id="NEW1"),
    )

    response = merge(client, "OLD1", "NEW1")
    assert response.status_code == 200, response.text
    # 2 (B) + 2 (C measured from the reset baseline of 1).
    assert daily(client, station_id="NEW1", day="2026-11-01").json()["rainfall_mm"] == 4.0


def test_merge_retry_does_not_migrate_twice(client):
    upload(client, obs("A", "2026-11-01T00:00:00Z", 0.0, station_id="OLD1"))
    upload(client, obs("B", "2026-11-01T06:00:00Z", 2.0, station_id="NEW1"))

    first = merge(client, "OLD1", "NEW1")
    assert first.status_code == 200
    assert first.json()["already_merged"] is False

    second = merge(client, "OLD1", "NEW1")
    assert second.status_code == 200
    body = second.json()
    assert body["already_merged"] is True
    assert body["source_observations"] == 0
    assert body["merged_observations"] == 2
    assert body["affected_dates"] == []

    # The reversed pair is the same already-merged pair.
    reverse = merge(client, "NEW1", "OLD1")
    assert reverse.status_code == 200
    assert reverse.json()["already_merged"] is True

    assert daily(client, station_id="NEW1", day="2026-11-01").json()["rainfall_mm"] == 2.0
    assert len(client.get("/stations/NEW1/observations").json()["observations"]) == 2


def test_upload_to_old_id_after_merge_reaches_target(client):
    upload(client, obs("A", "2026-11-01T00:00:00Z", 0.0, station_id="OLD1"))
    upload(client, obs("B", "2026-11-01T06:00:00Z", 2.0, station_id="NEW1"))
    assert merge(client, "OLD1", "NEW1").status_code == 200

    # Late backfill still addressed to the old id.
    response = upload(client, obs("LATE", "2026-11-01T12:00:00Z", 5.0, station_id="OLD1"))
    assert response.status_code == 200
    assert response.json()["created"] == 1

    listing = client.get("/stations/NEW1/observations").json()["observations"]
    assert [item["observation_id"] for item in listing] == ["A", "B", "LATE"]
    assert all(item["station_id"] == "NEW1" for item in listing)
    assert daily(client, station_id="OLD1", day="2026-11-01").json()["rainfall_mm"] == 5.0


def test_revision_rules_apply_across_the_alias(client):
    upload(client, obs("A", "2026-11-01T00:00:00Z", 0.0, station_id="OLD1"))
    upload(client, obs("B", "2026-11-01T06:00:00Z", 2.0, station_id="NEW1", revision=2))
    assert merge(client, "OLD1", "NEW1").status_code == 200

    # A stale lower revision uploaded under the old id is ignored, and an
    # identical re-upload under the old id is idempotent.
    stale = upload(
        client, obs("B", "2026-11-01T06:00:00Z", 99.0, station_id="OLD1", revision=1)
    )
    assert stale.status_code == 200
    assert stale.json()["unchanged"] == 1
    again = upload(
        client, obs("B", "2026-11-01T06:00:00Z", 2.0, station_id="OLD1", revision=2)
    )
    assert again.json()["unchanged"] == 1
    assert daily(client, station_id="NEW1", day="2026-11-01").json()["rainfall_mm"] == 2.0


def test_batch_mixing_old_and_new_ids_writes_one_station(client):
    upload(client, obs("A", "2026-11-01T00:00:00Z", 0.0, station_id="OLD1"))
    upload(client, obs("B", "2026-11-01T06:00:00Z", 2.0, station_id="NEW1"))
    assert merge(client, "OLD1", "NEW1").status_code == 200

    # One batch may mix both ids; after alias resolution they are the same
    # station, and the same observation id in both spellings deduplicates.
    response = upload(
        client,
        obs("C", "2026-11-01T12:00:00Z", 5.0, station_id="OLD1"),
        obs("C", "2026-11-01T12:00:00Z", 5.0, station_id="NEW1"),
        obs("D", "2026-11-01T18:00:00Z", 6.0, station_id="NEW1"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] == 2
    assert body["unchanged"] == 1

    listing = client.get("/stations/NEW1/observations").json()["observations"]
    assert [item["observation_id"] for item in listing] == ["A", "B", "C", "D"]
    assert daily(client, station_id="OLD1", day="2026-11-01").json()["rainfall_mm"] == 6.0


def test_second_merge_redirects_existing_alias(client):
    upload(client, obs("A", "2026-11-01T00:00:00Z", 0.0, station_id="S1"))
    upload(client, obs("B", "2026-11-01T06:00:00Z", 2.0, station_id="S2"))
    upload(client, obs("C", "2026-11-01T12:00:00Z", 5.0, station_id="S3"))

    assert merge(client, "S1", "S2").status_code == 200
    assert merge(client, "S2", "S3").status_code == 200

    # The oldest id now resolves to the newest station.
    result = daily(client, station_id="S1", day="2026-11-01").json()
    assert result["station_id"] == "S3"
    assert result["rainfall_mm"] == 5.0

    # And uploads addressed to it land in the newest station.
    upload(client, obs("D", "2026-11-01T18:00:00Z", 6.0, station_id="S1"))
    listing = client.get("/stations/S3/observations").json()["observations"]
    assert [item["observation_id"] for item in listing] == ["A", "B", "C", "D"]


def test_merge_onto_an_alias_targets_the_canonical_station(client):
    upload(client, obs("A", "2026-11-01T00:00:00Z", 0.0, station_id="S1"))
    upload(client, obs("B", "2026-11-01T06:00:00Z", 2.0, station_id="S2"))
    assert merge(client, "S1", "S2").status_code == 200

    upload(client, obs("C", "2026-11-01T12:00:00Z", 4.0, station_id="S3"))
    response = merge(client, "S3", "S1")  # S1 is an alias of S2
    assert response.status_code == 200
    assert response.json()["canonical_station_id"] == "S2"

    listing = client.get("/stations/S2/observations").json()["observations"]
    assert [item["observation_id"] for item in listing] == ["A", "B", "C"]
    assert daily(client, station_id="S3", day="2026-11-01").json()["rainfall_mm"] == 4.0


def test_merge_with_same_source_and_target_is_invalid(client):
    response = merge(client, "S1", "S1")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_merge_requires_both_station_ids(client):
    assert client.post("/stations/merge", json={"source_station_id": "S1"}).status_code == 422
    assert (
        client.post(
            "/stations/merge",
            json={"source_station_id": "  ", "target_station_id": "S2"},
        ).status_code
        == 422
    )
    assert client.post("/stations/merge", json={}).status_code == 422


def test_merge_accepts_camel_case_and_old_new_aliases(client):
    response = client.post(
        "/stations/merge",
        json={"sourceStationId": "A", "targetStationId": "B"},
    )
    assert response.status_code == 200
    assert response.json()["canonical_station_id"] == "B"

    response = client.post(
        "/stations/merge",
        json={"old_station_id": "C", "new_station_id": "B"},
    )
    assert response.status_code == 200
    assert response.json()["canonical_station_id"] == "B"


def test_merge_with_unknown_source_registers_alias(client):
    upload(client, obs("B", "2026-11-01T06:00:00Z", 2.0, station_id="NEW1"))

    response = merge(client, "GHOST", "NEW1")
    assert response.status_code == 200
    assert response.json()["source_observations"] == 0
    assert response.json()["merged_observations"] == 1

    # Backfill addressed to the old id now lands in the new station.
    upload(client, obs("A", "2026-11-01T00:00:00Z", 0.0, station_id="GHOST"))
    listing = client.get("/stations/NEW1/observations").json()["observations"]
    assert [item["observation_id"] for item in listing] == ["A", "B"]


def test_failed_merge_leaves_no_alias_and_allows_retry(client):
    upload(client, obs("X", "2026-11-01T06:00:00Z", 2.0, station_id="OLD1"))
    upload(client, obs("X", "2026-11-01T06:00:00Z", 3.0, station_id="NEW1"))
    assert merge(client, "OLD1", "NEW1").status_code == 409

    # The old id is still its own station: uploads to it do not leak into
    # the would-be target.
    upload(client, obs("Y", "2026-11-01T12:00:00Z", 4.0, station_id="OLD1"))
    assert [
        item["observation_id"]
        for item in client.get("/stations/NEW1/observations").json()["observations"]
    ] == ["X"]

    # Fixing the conflict (a higher revision on the old side wins) lets the
    # same merge succeed afterwards.
    upload(client, obs("X", "2026-11-01T06:00:00Z", 2.0, station_id="OLD1", revision=2))
    upload(client, obs("Y", "2026-11-01T12:00:00Z", 4.0, station_id="OLD1", revision=2, is_retracted=True))
    response = merge(client, "OLD1", "NEW1")
    assert response.status_code == 200, response.text
    listing = client.get("/stations/NEW1/observations").json()["observations"]
    by_id = {item["observation_id"]: item for item in listing}
    assert by_id["X"]["revision"] == 2
    assert by_id["X"]["cumulative_mm"] == 2.0
    assert by_id["Y"]["is_retracted"] is True


def test_merge_carries_retracted_observations(client):
    upload(
        client,
        obs("A", "2026-11-01T00:00:00Z", 0.0, station_id="OLD1"),
        obs("W", "2026-11-01T03:00:00Z", 1.0, station_id="OLD1", is_retracted=True),
    )
    upload(client, obs("B", "2026-11-01T06:00:00Z", 2.0, station_id="NEW1"))

    response = merge(client, "OLD1", "NEW1")
    assert response.status_code == 200, response.text
    assert response.json()["merged_observations"] == 3

    listing = client.get("/stations/NEW1/observations").json()["observations"]
    by_id = {item["observation_id"]: item for item in listing}
    assert by_id["W"]["is_retracted"] is True
    assert daily(client, station_id="NEW1", day="2026-11-01").json()["rainfall_mm"] == 2.0


def test_merge_recomputes_daily_across_day_boundary(client):
    upload(client, obs("A", "2026-11-01T23:00:00Z", 4.0, station_id="OLD1"))
    upload(client, obs("B", "2026-11-02T01:00:00Z", 9.0, station_id="NEW1"))

    response = merge(client, "OLD1", "NEW1")
    assert response.status_code == 200
    assert response.json()["affected_dates"] == ["2026-11-02"]

    result = daily(client, station_id="NEW1", day="2026-11-02").json()
    assert result["rainfall_mm"] == 5.0
    assert result["observation_ids"] == ["B"]
    # A only establishes the baseline, so its own day stays at zero.
    assert daily(client, station_id="OLD1", day="2026-11-01").json()["rainfall_mm"] == 0.0


def test_alias_persists_across_restarts(tmp_path):
    db_path = str(tmp_path / "persist.db")

    first_db = Database(db_path)
    with TestClient(create_app(first_db)) as first_client:
        upload(first_client, obs("O1", "2026-11-01T00:00:00Z", 0.0, station_id="OLD"))
        upload(first_client, obs("N1", "2026-11-01T06:00:00Z", 2.0, station_id="NEW"))
        response = first_client.post(
            "/stations/merge",
            json={"source_station_id": "OLD", "target_station_id": "NEW"},
        )
        assert response.status_code == 200

    second_db = Database(db_path)
    with TestClient(create_app(second_db)) as second_client:
        result = second_client.get("/stations/OLD/daily/2026-11-01")
        assert result.status_code == 200
        assert result.json()["station_id"] == "NEW"
        assert result.json()["rainfall_mm"] == 2.0

        listing = second_client.get("/stations/OLD/observations").json()
        assert listing["station_id"] == "NEW"
        assert [item["observation_id"] for item in listing["observations"]] == ["O1", "N1"]

        retry = second_client.post(
            "/stations/merge",
            json={"source_station_id": "OLD", "target_station_id": "NEW"},
        )
        assert retry.status_code == 200
        assert retry.json()["already_merged"] is True


def _obs_in(observation_id: str, station_id: str, at: datetime, cumulative: float):
    return ObservationIn(
        observation_id=observation_id,
        station_id=station_id,
        revision=1,
        observed_at=at,
        cumulative_mm=cumulative,
    )


def test_merge_and_upload_concurrent_are_ordered_by_commit(tmp_path):
    """A merge racing an upload must not lose data or double-migrate.

    Both operations take the write lock up front, so they commit in some
    order; whichever it is, every observation ends up under the target id
    and the daily total reflects the full merged series.
    """

    db = Database(str(tmp_path / "concurrent.db"))
    db.initialize()
    try:
        with db.connection() as conn:
            ingest_batch(
                conn,
                [_obs_in("O1", "S1", datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc), 0.0)],
            )

        late = [
            _obs_in("O2", "S1", datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc), 6.0)
        ]

        def do_merge():
            with db.connection() as conn:
                return merge_stations(conn, "S1", "S2")

        def do_upload():
            with db.connection() as conn:
                return ingest_batch(conn, late)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(do_merge), pool.submit(do_upload)]
            for future in futures:
                future.result()

        with db.connection() as conn:
            assert resolve_station(conn, "S1") == "S2"
            rows = list_station_observations(conn, "S2")
            assert {row["observation_id"] for row in rows} == {"O1", "O2"}
            day = get_daily_rainfall(conn, "S1", date(2026, 11, 1))
            assert day["station_id"] == "S2"
            assert day["rainfall_mm"] == 6.0

            # Retrying the same pair after the race is a no-op.
            outcome = merge_stations(conn, "S1", "S2")
            assert outcome["already_merged"] is True
            assert outcome["merged_observations"] == 2
    finally:
        db.close()
