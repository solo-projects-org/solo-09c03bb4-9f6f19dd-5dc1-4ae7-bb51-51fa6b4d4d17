"""Read-side service for station-day reconciliation.

Every public helper runs *inside one ``BEGIN`` snapshot transaction* on the
same connection, so the batch list, per-batch content statistics, the manifest
and the revision token presented on a page all come from a single committed
state.  A concurrent publication cannot make one request mix old and new rows.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from . import reconcile
from .reconcile import ReceivedBatch, Reconciliation


def get_revision(conn: sqlite3.Connection, station: str, day: str) -> int:
    row = conn.execute(
        "SELECT revision FROM station_day_state WHERE station = ? AND day = ?",
        (station, day),
    ).fetchone()
    return row["revision"] if row else 0


def get_manifest(conn: sqlite3.Connection, station: str, day: str) -> list[str]:
    rows = conn.execute(
        "SELECT expected_sha256 FROM manifests WHERE station = ? AND day = ? ORDER BY ord",
        (station, day),
    ).fetchall()
    return [r["expected_sha256"] for r in rows]


def list_station_days(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Station-days known to the system (from batches and/or manifests)."""

    return conn.execute(
        """
        SELECT s.station, s.day, s.revision, s.updated_at,
               (SELECT COUNT(*) FROM manifests m
                 WHERE m.station = s.station AND m.day = s.day) AS manifest_count
        FROM station_day_state s
        ORDER BY s.day DESC, s.station
        """
    ).fetchall()


def _received_batches(
    conn: sqlite3.Connection, station: str, day: str
) -> list[ReceivedBatch]:
    """Published batches that contain at least one position for this station-day.

    A batch "belongs" to the station-day when at least one parsed row matches
    both station and UTC calendar day.  Per-batch counts of parse rejections
    and rows belonging to *other* station-days are computed in the same
    snapshot, so content qualification never sees a half-published batch.
    """

    start_s, end_s = reconcile.day_bounds(date.fromisoformat(day))

    batches = conn.execute(
        """
        SELECT b.id, b.filename, b.sha256, b.uploaded_at,
               b.accepted_count, b.rejected_count
        FROM batches b
        WHERE b.status = 'published'
          AND EXISTS (
              SELECT 1 FROM raw_rows r
              WHERE r.batch_id = b.id AND r.station = ?
                AND r.utc >= ? AND r.utc < ?
          )
        ORDER BY b.uploaded_at, b.id
        """,
        (station, start_s, end_s),
    ).fetchall()

    result: list[ReceivedBatch] = []
    for b in batches:
        foreign = conn.execute(
            """
            SELECT COUNT(*) AS c,
                   SUM(CASE WHEN r.station = ? THEN 1 ELSE 0 END) AS other_day,
                   SUM(CASE WHEN r.station <> ? THEN 1 ELSE 0 END) AS other_station
            FROM raw_rows r
            WHERE r.batch_id = ?
              AND (r.station <> ? OR r.utc < ? OR r.utc >= ?)
            """,
            (station, station, b["id"], station, start_s, end_s),
        ).fetchone()
        foreign_count = int(foreign["c"] or 0)
        detail: list[str] = []
        if foreign["other_day"]:
            detail.append(f"同站他日 {foreign['other_day']} 行")
        if foreign["other_station"]:
            detail.append(f"他站 {foreign['other_station']} 行")
        result.append(
            ReceivedBatch(
                batch_id=b["id"],
                filename=b["filename"],
                sha256=b["sha256"],
                uploaded_at=b["uploaded_at"],
                accepted_count=b["accepted_count"],
                rejected_count=b["rejected_count"],
                foreign_rows=foreign_count,
                foreign_detail=tuple(detail),
            )
        )
    return result


def load_station_day(
    conn: sqlite3.Connection, station: str, day: str
) -> tuple[int, list[str], list[ReceivedBatch]]:
    """Return revision, manifest hashes and received batches from one snapshot."""

    revision = get_revision(conn, station, day)
    expected = get_manifest(conn, station, day)
    received = _received_batches(conn, station, day)
    return revision, expected, received


def build_reconciliation(
    conn: sqlite3.Connection, station: str, day: str
) -> tuple[int, Reconciliation]:
    revision, expected, received = load_station_day(conn, station, day)
    return revision, reconcile.reconcile(station, day, expected, received)
