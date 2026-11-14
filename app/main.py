from __future__ import annotations

import csv
import io
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from .ais import MAX_SPEED_KNOTS
from . import reconcile, recon_store
from .db import connect, init_db, read_tx
from .store import Store, UploadError, StaleRevisionError

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("AIS_DATA_DIR", BASE_DIR.parent / "data"))
DB_PATH = DATA_DIR / "ais.sqlite3"
MAX_UPLOAD_BYTES = int(os.environ.get("AIS_MAX_UPLOAD_BYTES", 64 * 1024 * 1024))

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    init_db(DB_PATH)
    conn = connect(DB_PATH)
    try:
        Store(DATA_DIR, conn).cleanup_staging()
    finally:
        conn.close()
    yield


app = FastAPI(title="岸站 AIS 可信航迹", lifespan=lifespan)


def _conn() -> sqlite3.Connection:
    return connect(DB_PATH)


def _store(conn: sqlite3.Connection) -> Store:
    return Store(DATA_DIR, conn)


def _render(request: Request, name: str, status_code: int = 200, **context):
    context.setdefault("max_speed", MAX_SPEED_KNOTS)
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def _fmt_utc(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    import datetime

    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


templates.env.filters["utc"] = _fmt_utc


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = _conn()
    try:
        with read_tx(conn):
            batches = conn.execute(
                "SELECT * FROM batches WHERE status = 'published' ORDER BY uploaded_at DESC, id DESC"
            ).fetchall()
            vessels = conn.execute(
                "SELECT * FROM vessels ORDER BY mmsi"
            ).fetchall()
    finally:
        conn.close()
    return _render(request, "index.html", batches=batches, vessels=vessels)


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    payload = bytearray()
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > MAX_UPLOAD_BYTES:
            return _render(
                request,
                "error.html",
                title="上传被拒绝",
                message=f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB 上限，既有航迹未改变。",
                status_code=413,
            )
    if not payload:
        return _render(
            request,
            "error.html",
            title="上传被拒绝",
            message="上传文件为空，既有航迹未改变。",
            status_code=400,
        )

    try:
        batch = await _publish_in_thread(file.filename or "upload.csv", bytes(payload))
    except UploadError as exc:
        return _render(
            request,
            "error.html",
            title="批次未发布",
            message=exc.message,
            status_code=exc.status,
        )

    return RedirectResponse(url=f"/batches/{batch.batch_id}", status_code=303)


async def _publish_in_thread(filename: str, payload: bytes):
    """Run blocking parse/fsync/SQLite work off the event loop.

    The connection is created inside the worker thread because sqlite3
    connections are bound to their creating thread.
    """

    import anyio

    def worker():
        conn = _conn()
        try:
            return _store(conn).publish(filename, payload)
        finally:
            conn.close()

    return await anyio.to_thread.run_sync(worker)


@app.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_detail(request: Request, batch_id: str):
    conn = _conn()
    try:
        with read_tx(conn):
            batch = conn.execute(
                "SELECT * FROM batches WHERE id = ? AND status = 'published'", (batch_id,)
            ).fetchone()
            if batch is None:
                raise HTTPException(status_code=404, detail="批次不存在或未发布")
            rejects = conn.execute(
                "SELECT * FROM rejects WHERE batch_id = ? ORDER BY line_no", (batch_id,)
            ).fetchall()
            rows = conn.execute(
                """
                SELECT r.*, v.status, v.reason, v.detail
                FROM raw_rows r
                LEFT JOIN vessels ve ON ve.mmsi = r.mmsi
                LEFT JOIN verdicts v
                       ON v.mmsi = r.mmsi AND v.version = ve.version AND v.raw_row_id = r.id
                WHERE r.batch_id = ?
                ORDER BY r.mmsi, r.utc, r.station
                """,
                (batch_id,),
            ).fetchall()
    finally:
        conn.close()
    return _render(
        request,
        "batch.html",
        batch=batch,
        rejects=rejects,
        rows=rows,
        trusted=sum(1 for r in rows if r["status"] == "trusted"),
        isolated=sum(1 for r in rows if r["status"] == "isolated"),
    )


@app.get("/batches/{batch_id}/original.csv")
def batch_original(request: Request, batch_id: str):
    conn = _conn()
    try:
        with read_tx(conn):
            batch = conn.execute(
                "SELECT * FROM batches WHERE id = ? AND status = 'published'", (batch_id,)
            ).fetchone()
            if batch is None:
                raise HTTPException(status_code=404, detail="批次不存在或未发布")
            path = DATA_DIR / batch["original_path"]
            filename = batch["filename"]
            digest = batch["sha256"]
            exists = path.is_file()
    finally:
        conn.close()
    if not exists:
        raise HTTPException(status_code=410, detail="该批次原档缺失，已不可下载")
    opened = open(path, "rb")
    return StreamingResponse(
        _file_chunks(opened),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": _attachment(filename),
            "X-Content-SHA256": digest,
        },
    )


@app.get("/vessels", response_class=HTMLResponse)
def vessels(request: Request):
    conn = _conn()
    try:
        with read_tx(conn):
            rows = conn.execute("SELECT * FROM vessels ORDER BY mmsi").fetchall()
    finally:
        conn.close()
    return _render(request, "vessels.html", vessels=rows)


@app.get("/vessels/{mmsi}", response_class=HTMLResponse)
def vessel_detail(request: Request, mmsi: str):
    conn = _conn()
    try:
        with read_tx(conn):
            vessel = conn.execute("SELECT * FROM vessels WHERE mmsi = ?", (mmsi,)).fetchone()
            if vessel is None:
                raise HTTPException(status_code=404, detail="没有该船的已发布数据")
            version = vessel["version"]
            points = conn.execute(
                "SELECT * FROM track_points WHERE mmsi = ? AND version = ? ORDER BY seq",
                (mmsi, version),
            ).fetchall()
            isolated = conn.execute(
                """
                SELECT r.id, r.line_no, r.batch_id, r.station, r.utc, r.lat, r.lon,
                       v.reason, v.detail
                FROM verdicts v
                JOIN raw_rows r ON r.id = v.raw_row_id
                WHERE v.mmsi = ? AND v.version = ? AND v.status = 'isolated'
                ORDER BY r.utc, r.station, r.id
                """,
                (mmsi, version),
            ).fetchall()
            batches = conn.execute(
                """
                SELECT DISTINCT b.*
                FROM batches b
                JOIN raw_rows r ON r.batch_id = b.id
                WHERE r.mmsi = ? AND b.status = 'published'
                ORDER BY b.uploaded_at, b.id
                """,
                (mmsi,),
            ).fetchall()
    finally:
        conn.close()
    return _render(
        request,
        "vessel.html",
        vessel=vessel,
        points=points,
        isolated=isolated,
        batches=batches,
        version=version,
    )


@app.get("/vessels/{mmsi}/track.csv")
def vessel_track(request: Request, mmsi: str):
    conn = _conn()
    try:
        with read_tx(conn):
            vessel = conn.execute("SELECT * FROM vessels WHERE mmsi = ?", (mmsi,)).fetchone()
            if vessel is None:
                raise HTTPException(status_code=404, detail="没有该船的已发布数据")
            version = vessel["version"]
            points = conn.execute(
                "SELECT * FROM track_points WHERE mmsi = ? AND version = ? ORDER BY seq",
                (mmsi, version),
            ).fetchall()
            data = _render_track_csv(vessel["mmsi"], points)
    finally:
        conn.close()
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": _attachment(f"track_{vessel['mmsi']}.csv"),
            "X-Track-Version": str(version),
        },
    )


def _render_track_csv(mmsi: str, points) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        ["mmsi", "utc_seconds", "utc_iso", "lat", "lon", "speed_knots", "station", "batch_id"]
    )
    import datetime

    for p in points:
        writer.writerow(
            [
                mmsi,
                p["utc"],
                datetime.datetime.fromtimestamp(p["utc"], datetime.timezone.utc).isoformat(),
                f"{p['lat']:.6f}",
                f"{p['lon']:.6f}",
                "" if p["speed_knots"] is None else f"{p['speed_knots']:.2f}",
                p["station"],
                p["batch_id"],
            ]
        )
    return buf.getvalue().encode("utf-8")


@app.get("/vessels/{mmsi}/isolated.csv")
def vessel_isolated(request: Request, mmsi: str):
    conn = _conn()
    try:
        with read_tx(conn):
            vessel = conn.execute("SELECT * FROM vessels WHERE mmsi = ?", (mmsi,)).fetchone()
            if vessel is None:
                raise HTTPException(status_code=404, detail="没有该船的已发布数据")
            version = vessel["version"]
            rows = conn.execute(
                """
                SELECT r.*, v.reason, v.detail
                FROM verdicts v
                JOIN raw_rows r ON r.id = v.raw_row_id
                WHERE v.mmsi = ? AND v.version = ? AND v.status = 'isolated'
                ORDER BY r.utc, r.station, r.id
                """,
                (mmsi, version),
            ).fetchall()
            data = _render_isolated_csv(mmsi, rows)
    finally:
        conn.close()
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": _attachment(f"isolated_{mmsi}.csv")},
    )


def _render_isolated_csv(mmsi: str, rows) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        [
            "mmsi",
            "utc_seconds",
            "lat",
            "lon",
            "station",
            "batch_id",
            "original_line",
            "reason",
            "detail",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                mmsi,
                r["utc"],
                f"{r['lat']:.6f}",
                f"{r['lon']:.6f}",
                r["station"],
                r["batch_id"],
                r["line_no"],
                r["reason"],
                r["detail"],
            ]
        )
    return buf.getvalue().encode("utf-8")


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


# --------------------------------------------------------------------------
# 站日原档交接对账
# --------------------------------------------------------------------------


def _validate_station_day(station: str, day: str):
    try:
        return reconcile.validate_station(station), reconcile.validate_day(day)
    except reconcile.ManifestError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@app.get("/reconcile", response_class=HTMLResponse)
def reconcile_index(request: Request):
    conn = _conn()
    try:
        with read_tx(conn):
            station_days = recon_store.list_station_days(conn)
    finally:
        conn.close()
    return _render(request, "reconcile_index.html", station_days=station_days)


@app.get("/reconcile/{station}/{day}", response_class=HTMLResponse)
def reconcile_page(request: Request, station: str, day: str):
    station, day_obj = _validate_station_day(station, day)
    day = day_obj.isoformat()
    conn = _conn()
    try:
        with read_tx(conn):
            revision, result = recon_store.build_reconciliation(conn, station, day)
    finally:
        conn.close()
    return _render(
        request,
        "reconcile.html",
        station=station,
        day=day,
        revision=revision,
        result=result,
        manifest_text="\n".join(result.expected),
        error=None,
        stale=False,
    )


@app.post("/reconcile/{station}/{day}", response_class=HTMLResponse)
async def reconcile_submit(
    request: Request,
    station: str,
    day: str,
    manifest_text: str = Form(...),
    revision_token: int = Form(...),
):
    station, day_obj = _validate_station_day(station, day)
    day = day_obj.isoformat()

    try:
        expected = reconcile.normalize_manifest(manifest_text)
    except reconcile.ManifestError as exc:
        # Invalid manifest: nothing is written; re-render with the pasted text.
        conn = _conn()
        try:
            with read_tx(conn):
                revision, result = recon_store.build_reconciliation(conn, station, day)
        finally:
            conn.close()
        return _render(
            request,
            "reconcile.html",
            station=station,
            day=day,
            revision=revision,
            result=result,
            manifest_text=manifest_text,
            error=exc.message,
            stale=False,
            status_code=400,
        )

    import anyio

    def worker() -> int:
        conn = _conn()
        try:
            return _store(conn).replace_manifest(station, day, expected, revision_token)
        finally:
            conn.close()

    try:
        await anyio.to_thread.run_sync(worker)
    except StaleRevisionError:
        # The page was opened against an older committed state.  Refuse the
        # write and keep both the previous manifest and the operator's input.
        conn = _conn()
        try:
            with read_tx(conn):
                revision, result = recon_store.build_reconciliation(conn, station, day)
        finally:
            conn.close()
        return _render(
            request,
            "reconcile.html",
            station=station,
            day=day,
            revision=revision,
            result=result,
            manifest_text=manifest_text,
            error=(
                "提交被拒绝：本页面打开后该站日的批次或清单已变化（修订号 "
                f"{revision_token} → {revision}）。请核对下方最新对账结果后重新提交；"
                "原清单未被修改。"
            ),
            stale=True,
            status_code=409,
        )

    return RedirectResponse(url=f"/reconcile/{station}/{day}", status_code=303)


def _file_chunks(fh, size: int = 1 << 20):
    try:
        while True:
            chunk = fh.read(size)
            if not chunk:
                break
            yield chunk
    finally:
        fh.close()


def _attachment(filename: str) -> str:
    from urllib.parse import quote

    safe = filename.replace("\r", "").replace("\n", "").replace('"', "'")
    ascii_fallback = safe.encode("ascii", "replace").decode("ascii")
    return (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(safe, safe='')}"
    )
