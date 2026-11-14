"""Pydantic request/response models for the rainfall API."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Annotated, Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def _choice(*names: str) -> AliasChoices:
    return AliasChoices(*names)


class ObservationIn(BaseModel):
    """A single station observation supplied by a data operator.

    ``revision`` is the version of the observation.  Higher revisions replace
    lower ones.  ``is_reset`` marks a counter reset: the reading becomes the
    new baseline and contributes no incremental rain.  ``is_retracted`` marks
    the observation as withdrawn, which removes it from all calculations.

    Field aliases are accepted so that camelCase clients and common
    alternative names work without a translation layer.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    observation_id: str = Field(
        min_length=1,
        max_length=200,
        validation_alias=_choice("observation_id", "observationId", "id"),
    )
    station_id: str = Field(
        min_length=1,
        max_length=100,
        validation_alias=_choice("station_id", "stationId", "station"),
    )
    revision: int = Field(
        ge=1, validation_alias=_choice("revision", "revisionNumber", "rev")
    )
    observed_at: datetime = Field(
        validation_alias=_choice(
            "observed_at", "observedAt", "timestamp", "time", "observed_at_utc"
        )
    )
    cumulative_mm: float = Field(
        ge=0,
        validation_alias=_choice(
            "cumulative_mm", "cumulativeMm", "cumulative", "rainfall_mm", "value"
        ),
    )
    is_reset: bool = Field(
        default=False, validation_alias=_choice("is_reset", "isReset", "reset")
    )
    is_retracted: bool = Field(
        default=False,
        validation_alias=_choice(
            "is_retracted", "isRetracted", "retracted", "withdrawn", "is_withdrawn"
        ),
    )

    @field_validator("observation_id", "station_id")
    @classmethod
    def _strip_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("observed_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("observed_at must include a UTC offset")
        return value.astimezone(timezone.utc)

    @field_validator("cumulative_mm")
    @classmethod
    def _finite(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("cumulative_mm must be a finite number")
        return value

    # A retracted observation may still carry is_reset: retraction wins and
    # the record simply stops participating in any calculation.  Rejecting
    # that combination would make it awkward to withdraw a reset reading.


class ObservationsUpload(BaseModel):
    """Batch upload payload.

    Both ``{"observations": [...]}`` and a bare JSON array are accepted.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    observations: Annotated[list[ObservationIn], Field(min_length=1)]

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_list(cls, value: Any) -> Any:
        if isinstance(value, list):
            return {"observations": value}
        return value


class StationMergeRequest(BaseModel):
    """Merge a renamed station's old id (source) into its successor (target).

    Field aliases mirror the observation upload so that camelCase clients
    and old/new naming work without a translation layer.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source_station_id: str = Field(
        min_length=1,
        max_length=100,
        validation_alias=_choice(
            "source_station_id",
            "sourceStationId",
            "source",
            "old_station_id",
            "oldStationId",
        ),
    )
    target_station_id: str = Field(
        min_length=1,
        max_length=100,
        validation_alias=_choice(
            "target_station_id",
            "targetStationId",
            "target",
            "new_station_id",
            "newStationId",
        ),
    )

    @field_validator("source_station_id", "target_station_id")
    @classmethod
    def _strip_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def _ids_must_differ(self) -> "StationMergeRequest":
        if self.source_station_id == self.target_station_id:
            raise ValueError("source and target station ids must differ")
        return self


class StationMergeResponse(BaseModel):
    """Outcome of a station merge.

    ``already_merged`` is true when the pair had already been merged (the
    source id already resolves to the target), in which case nothing was
    migrated again.  ``canonical_station_id`` is the station id that now
    holds the combined history.
    """

    source_station_id: str
    target_station_id: str
    canonical_station_id: str
    already_merged: bool
    source_observations: int
    merged_observations: int
    affected_dates: list[date]


class UploadedObservation(BaseModel):
    observation_id: str
    station_id: str
    revision: int
    observed_at: datetime
    cumulative_mm: float
    is_reset: bool
    is_retracted: bool


class ObservationUploadResult(BaseModel):
    """Per-observation outcome; ``status`` is created/updated/unchanged."""

    observation_id: str
    revision: int
    status: str


class UploadResponse(BaseModel):
    accepted: int
    unchanged: int
    updated: int
    created: int
    results: list[ObservationUploadResult]
    affected_dates: list[date]


class DailyRainfallResponse(BaseModel):
    station_id: str
    date: date
    rainfall_mm: float
    observation_ids: list[str]


class StationObservationsResponse(BaseModel):
    station_id: str
    observations: list[UploadedObservation]


class HealthResponse(BaseModel):
    status: str
    version: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
