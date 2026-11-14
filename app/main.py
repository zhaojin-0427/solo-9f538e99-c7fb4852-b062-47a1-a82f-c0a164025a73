"""FastAPI application exposing the rainfall ingestion and query API."""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .db import Database
from .errors import BatchRejected
from .models import (
    DailyRainfallResponse,
    ErrorDetail,
    ErrorResponse,
    HealthResponse,
    ObservationUploadResult,
    ObservationsUpload,
    StationObservationsResponse,
    UploadedObservation,
    UploadResponse,
)
from .rainfall import (
    get_daily_rainfall,
    ingest_batch,
    list_station_observations,
)

logger = logging.getLogger("rainfall")


def _safe_input(value: Any) -> Any:
    """Coerce a validation input to something JSON serialisable."""

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_input(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _safe_input(item) for key, item in value.items()}
    return repr(value)


def create_app(database: Database | None = None) -> FastAPI:
    """Application factory; the database can be injected for tests."""

    db = database or Database()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        db.initialize()
        try:
            yield
        finally:
            db.close()

    app = FastAPI(
        title="Rainfall Daily Totals API",
        version=__version__,
        description=(
            "Ingest station observations idempotently and maintain trustworthy "
            "daily rainfall totals derived from cumulative counters."
        ),
        lifespan=lifespan,
    )
    app.state.db = db

    def get_db() -> Database:
        return app.state.db

    @app.exception_handler(BatchRejected)
    async def _batch_rejected_handler(
        _: Request, exc: BatchRejected
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=ErrorResponse(
                error=ErrorDetail(
                    code="batch_rejected",
                    message=(
                        "The batch was rejected and no observations or daily "
                        "results were changed."
                    ),
                    details=exc.violations,
                )
            ).model_dump(mode="json"),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Pydantic may attach non-JSON-serialisable values (such as the raised
        # ValueError) in ``ctx``; render them as text.
        details = []
        for error in exc.errors():
            item = dict(error)
            context = item.get("ctx")
            if isinstance(context, dict):
                item["ctx"] = {key: str(value) for key, value in context.items()}
            if "input" in item:
                item["input"] = _safe_input(item["input"])
            details.append(item)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=ErrorResponse(
                error=ErrorDetail(
                    code="request_validation_error",
                    message="The request payload is invalid.",
                    details=details,
                )
            ).model_dump(mode="json"),
        )

    @app.exception_handler(sqlite3.Error)
    async def _database_error_handler(_: Request, exc: sqlite3.Error) -> JSONResponse:
        logger.exception("database error")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=ErrorResponse(
                error=ErrorDetail(
                    code="database_error",
                    message="The database operation failed and was rolled back.",
                )
            ).model_dump(mode="json"),
        )

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__)

    @app.post(
        "/observations",
        response_model=UploadResponse,
        status_code=status.HTTP_200_OK,
        tags=["observations"],
        summary="Upload a batch of station observations",
    )
    def upload_observations(
        payload: ObservationsUpload,
        database: Database = Depends(get_db),
    ) -> UploadResponse:
        """Apply a batch of observations.

        The whole batch is validated and applied atomically.  Duplicate
        uploads are idempotent; higher revisions replace lower ones; any rule
        violation rejects the entire batch without changing stored data.
        """

        with database.connection() as conn:
            outcome = ingest_batch(conn, payload.observations)

        return UploadResponse(
            accepted=outcome["accepted"],
            created=outcome["created"],
            updated=outcome["updated"],
            unchanged=outcome["unchanged"],
            results=[
                ObservationUploadResult(**item) for item in outcome["results"]
            ],
            affected_dates=outcome["affected_dates"],
        )

    def _daily_response(
        station_id: str, day: date, database: Database
    ) -> DailyRainfallResponse:
        with database.connection() as conn:
            result = get_daily_rainfall(conn, station_id, day)
        if result is None:
            # A day with no contributing reading is 0 mm with no contributing
            # observations, not 404: absence of rain is a trustworthy answer.
            return DailyRainfallResponse(
                station_id=station_id,
                date=day,
                rainfall_mm=0.0,
                observation_ids=[],
            )
        return DailyRainfallResponse(**result)

    @app.get(
        "/stations/{station_id}/daily/{day}",
        response_model=DailyRainfallResponse,
        tags=["rainfall"],
        summary="Get the daily rainfall total and contributing observations",
    )
    def daily_rainfall(
        station_id: str = Path(min_length=1, max_length=100),
        day: date = Path(description="UTC calendar day, YYYY-MM-DD"),
        database: Database = Depends(get_db),
    ) -> DailyRainfallResponse:
        """Return a day's total and the ids of observations that produced it."""
        return _daily_response(station_id, day, database)

    @app.get(
        "/daily",
        response_model=DailyRainfallResponse,
        tags=["rainfall"],
        summary="Get the daily rainfall total using query parameters",
    )
    def daily_rainfall_query(
        station_id: str = Query(alias="station_id", min_length=1, max_length=100),
        day: date = Query(alias="date", description="UTC calendar day, YYYY-MM-DD"),
        database: Database = Depends(get_db),
    ) -> DailyRainfallResponse:
        """Same result as the path-based endpoint, for clients that prefer
        query parameters."""
        return _daily_response(station_id, day, database)

    @app.get(
        "/stations/{station_id}/observations",
        response_model=StationObservationsResponse,
        tags=["observations"],
        summary="List the effective state of a station's observations",
    )
    def station_observations(
        station_id: str = Path(min_length=1, max_length=100),
        database: Database = Depends(get_db),
    ) -> StationObservationsResponse:
        """Show the current revision of every observation for traceability."""

        with database.connection() as conn:
            rows = list_station_observations(conn, station_id)

        return StationObservationsResponse(
            station_id=station_id,
            observations=[UploadedObservation(**row) for row in rows],
        )

    return app


app = create_app()


def main() -> None:  # pragma: no cover - container entry point
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
