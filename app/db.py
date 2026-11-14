"""SQLite storage helpers.

The API keeps all state in a single SQLite file.  Observations are stored with
their canonical UTC timestamp and canonical decimal rain amount so that an
identical re-upload can be recognised exactly, and so that floating point noise
can never turn a harmless duplicate into a revision conflict.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    station_id       TEXT    NOT NULL,
    observation_id   TEXT    NOT NULL,
    revision         INTEGER NOT NULL CHECK (revision >= 1),
    observed_at      TEXT    NOT NULL,
    observed_day     TEXT    NOT NULL,
    cumulative_mm    TEXT    NOT NULL,
    is_reset         INTEGER NOT NULL CHECK (is_reset IN (0, 1)),
    is_retracted     INTEGER NOT NULL CHECK (is_retracted IN (0, 1)),
    updated_at       TEXT    NOT NULL,
    PRIMARY KEY (station_id, observation_id)
);

CREATE INDEX IF NOT EXISTS observations_by_station_time
    ON observations (station_id, observed_at, observed_day);

CREATE TABLE IF NOT EXISTS daily_rainfall (
    station_id       TEXT NOT NULL,
    day              TEXT NOT NULL,
    rainfall_mm      TEXT NOT NULL,
    observation_ids  TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (station_id, day)
);
"""

DEFAULT_DATABASE_PATH = "./data/rainfall.db"
MEMORY_URI = "file:rainfall?mode=memory&cache=shared"


class Database:
    """Small connection factory around a SQLite database path.

    ``:memory:`` is supported: a shared-cache in-memory database is held open
    by a keeper connection so that every request connection sees the same data.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.environ.get("DATABASE_PATH") or DEFAULT_DATABASE_PATH
        self._is_memory = self.path == ":memory:"
        self._keeper: sqlite3.Connection | None = None

    def initialize(self) -> None:
        if self._is_memory:
            if self._keeper is None:
                self._keeper = self._new_connection()
            with self.connection() as conn:
                conn.executescript(SCHEMA)
            return

        parent = Path(self.path).expanduser().resolve().parent
        parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            # WAL lets readers keep a consistent view while a writer holds the
            # batch transaction.  It is persistent per database file.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    def _new_connection(self) -> sqlite3.Connection:
        if self._is_memory:
            conn = sqlite3.connect(
                MEMORY_URI,
                uri=True,
                timeout=30,
                isolation_level=None,
                check_same_thread=False,
            )
        else:
            conn = sqlite3.connect(
                self.path,
                timeout=30,
                isolation_level=None,
                check_same_thread=False,
            )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def connect(self) -> sqlite3.Connection:
        return self._new_connection()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def close(self) -> None:
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run the body in an immediate transaction, rolling back on any error.

    ``BEGIN IMMEDIATE`` takes the write lock before the validation reads, so
    two concurrent uploads cannot both validate against the same old state.
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            # The transaction may already have been rolled back; the original
            # exception is the one worth propagating.
            pass
        raise
    conn.execute("COMMIT")
