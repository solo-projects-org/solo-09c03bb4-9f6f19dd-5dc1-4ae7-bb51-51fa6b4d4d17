"""HTTP tests for station-day handover reconciliation.

Covers the full operator flow (upload -> manifest -> verdict), content
qualification (parse rejects / foreign station-day rows), stale-page
rejection with manifest preservation, revision semantics across publications
and snapshot consistency of a single page render.
"""

from __future__ import annotations

import hashlib
import io
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as dbmod
from app import main as mainmod
from app import recon_store

BASE = 1767225600  # 2026-01-01T00:00:00Z
HEADER = "station,mmsi,utc,lat,lon\n"
STATION_DAY_URL = "/reconcile/SZ01/2026-01-01"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(mainmod, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(mainmod, "DB_PATH", tmp_path / "data" / "ais.sqlite3")
    mainmod.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with TestClient(mainmod.app) as c:
        yield c


def upload(client, name, body: bytes | str):
    payload = body.encode() if isinstance(body, str) else body
    return client.post(
        "/upload",
        files={"file": (name, io.BytesIO(payload), "text/csv")},
        follow_redirects=False,
    )


def csv_text(*rows: str) -> str:
    return HEADER + "".join(r + "\n" for r in rows)


def sha(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def open_page(client, url=STATION_DAY_URL):
    resp = client.get(url)
    assert resp.status_code == 200
    token = re.search(r'name="revision_token" value="(\d+)"', resp.text).group(1)
    return resp, int(token)


def save_manifest(client, text, token, url=STATION_DAY_URL):
    return client.post(
        url,
        data={"manifest_text": text, "revision_token": token},
        follow_redirects=False,
    )


def test_empty_station_day_is_not_handover_ready(client):
    page, token = open_page(client)
    assert "不可交接" in page.text
    assert "清单为空" in page.text
    assert token == 0
    assert client.get("/reconcile").status_code == 200


def test_clean_one_to_one_manifest_allows_handover(client):
    body = csv_text(
        f"SZ01,413000001,{BASE},22.5,114.2",
        f"SZ01,413000001,{BASE + 60},22.51,114.21",
    )
    upload(client, "clean.csv", body)
    digest = sha(body)

    _, token = open_page(client)
    resp = save_manifest(client, digest, token)
    assert resp.status_code == 303
    assert resp.headers["location"] == STATION_DAY_URL

    page, new_token = open_page(client)
    assert "可交接" in page.text
    assert "不可交接" not in page.text
    assert digest in page.text
    assert new_token == token + 1  # manifest replacement bumps revision


def test_parse_rejects_in_matched_archive_block_handover(client):
    body = csv_text(
        f"SZ01,413000001,{BASE},22.5,114.2",
        "SZ01,bad-mmsi,1,2,3",
    )
    upload(client, "with_reject.csv", body)
    digest = sha(body)

    _, token = open_page(client)
    assert save_manifest(client, digest, token).status_code == 303
    page, _ = open_page(client)
    assert "不可交接" in page.text
    assert "内容不合格" in page.text
    assert "解析拒绝行" in page.text


def test_foreign_station_and_other_day_rows_block_handover(client):
    other_day = BASE + 2 * 86400
    body = csv_text(
        f"SZ01,413000001,{BASE},22.5,114.2",
        f"HK02,413000001,{BASE},22.5,114.2",
        f"SZ01,413000001,{other_day},22.6,114.3",
    )
    upload(client, "mixed_station_day.csv", body)
    digest = sha(body)

    _, token = open_page(client)
    save_manifest(client, digest, token)
    page, _ = open_page(client)
    assert "不可交接" in page.text
    assert "他站 1 行" in page.text
    assert "同站他日 1 行" in page.text

    # The same batch is *not* attributed to the other station's workbench as a
    # SZ01 receipt, but HK02 sees its own row and can reconcile independently.
    hk_page, hk_token = open_page(client, "/reconcile/HK02/2026-01-01")
    assert digest in hk_page.text
    assert "他站 2 行" in hk_page.text  # two SZ01 rows are foreign to HK02
    assert save_manifest(client, digest, hk_token, url="/reconcile/HK02/2026-01-01").status_code == 303
    assert "不可交接" in client.get("/reconcile/HK02/2026-01-01").text


def test_missing_extra_and_duplicate_are_listed(client):
    clean = csv_text(f"SZ01,413000001,{BASE},22.5,114.2")
    upload(client, "clean.csv", clean)
    h_clean = sha(clean)
    extra = csv_text(f"SZ01,413000002,{BASE + 30},22.4,114.1")
    upload(client, "extra.csv", extra)
    h_extra = sha(extra)
    # Re-send the exact same bytes: same hash, second receipt.
    upload(client, "clean_copy.csv", clean)

    ghost = "f" * 64
    _, token = open_page(client)
    save_manifest(client, f"{h_clean}\n{ghost}", token)
    page, _ = open_page(client)

    assert "缺档 1 份" in page.text
    assert ghost in page.text
    assert "清单外档 1 份" in page.text
    assert h_extra in page.text
    assert "重复收档 1 份" in page.text
    assert "不可交接" in page.text


def test_supplemental_upload_flips_verdict_without_manifest_edit(client):
    clean = csv_text(f"SZ01,413000001,{BASE},22.5,114.2")
    upload(client, "clean.csv", clean)
    digest = sha(clean)
    _, token = open_page(client)
    save_manifest(client, digest, token)
    assert "可交接" in client.get(STATION_DAY_URL).text

    # A late re-send of the identical archive creates a duplicate receipt.
    upload(client, "clean_resend.csv", clean)
    page = client.get(STATION_DAY_URL).text
    assert "重复收档 1 份" in page
    assert "不可交接" in page

    # A supplemental *different* batch that completes nothing new adds an extra.
    extra = csv_text(f"SZ01,413000009,{BASE + 90},22.2,114.0")
    upload(client, "late_extra.csv", extra)
    page = client.get(STATION_DAY_URL).text
    assert "清单外档 1 份" in page


def test_stale_page_submission_is_rejected_and_manifest_kept(client):
    first = csv_text(f"SZ01,413000001,{BASE},22.5,114.2")
    upload(client, "first.csv", first)
    h_first = sha(first)
    _, token = open_page(client)
    save_manifest(client, h_first, token)  # revision advances to token+1

    # The page still carrying the old token must be refused.
    stale = save_manifest(client, "e" * 64, token)
    assert stale.status_code == 409
    assert "提交被拒绝" in stale.text
    assert str(token) in stale.text

    # Previous manifest is untouched: the stored hash still renders.
    page_resp, current_token = open_page(client)
    page = page_resp.text
    assert h_first in page
    assert "e" * 64 not in page
    assert current_token == token + 1

    # A concurrent publication also invalidates an open page.
    second = csv_text(f"SZ01,413000002,{BASE + 10},22.1,114.0")
    upload(client, "second.csv", second)
    stale2 = save_manifest(client, h_first, current_token)
    assert stale2.status_code == 409
    page, refreshed = open_page(client)
    assert refreshed == current_token + 1
    assert h_first in page.text  # manifest still preserved


def test_invalid_manifest_is_rejected_without_touching_stored_one(client):
    body = csv_text(f"SZ01,413000001,{BASE},22.5,114.2")
    upload(client, "clean.csv", body)
    digest = sha(body)
    _, token = open_page(client)
    save_manifest(client, digest, token)

    _, token2 = open_page(client)
    resp = save_manifest(client, "xyz-not-a-hash", token2)
    assert resp.status_code == 400
    assert "不是合法的 SHA-256" in resp.text
    # The pasted text is retained in the form so the operator can fix it...
    assert "xyz-not-a-hash" in resp.text
    # ...while the committed manifest is unchanged.
    assert digest in client.get(STATION_DAY_URL).text


def test_replacing_manifest_recomputes_verdict(client):
    body = csv_text(f"SZ01,413000001,{BASE},22.5,114.2")
    upload(client, "clean.csv", body)
    digest = sha(body)

    _, token = open_page(client)
    save_manifest(client, "9" * 64, token)  # only a missing hash
    assert "缺档 1 份" in client.get(STATION_DAY_URL).text

    _, token2 = open_page(client)
    save_manifest(client, digest, token2)
    page, _ = open_page(client)
    assert "可交接" in page.text
    assert "缺档" not in page.text


def test_revision_advances_per_touching_station_day_only(client):
    sza = csv_text(f"SZ01,413000001,{BASE},22.5,114.2")
    upload(client, "sz.csv", sza)
    _, sz_token = open_page(client)
    assert sz_token == 1

    # A batch for another station/day does not move SZ01/2026-01-01.
    hk = csv_text(f"HK02,413000002,{BASE + 2 * 86400},22.3,114.0")
    upload(client, "hk.csv", hk)
    _, sz_token_again = open_page(client)
    assert sz_token_again == 1

    _, hk_token = open_page(client, "/reconcile/HK02/2026-01-03")
    assert hk_token == 1

    # The station-day index exposes both.
    index = client.get("/reconcile").text
    assert "SZ01" in index and "HK02" in index
    assert "2026-01-01" in index and "2026-01-03" in index


def test_page_render_uses_one_committed_snapshot(client):
    """The batches, manifest and revision on a page come from one snapshot."""

    body = csv_text(f"SZ01,413000001,{BASE},22.5,114.2")
    upload(client, "clean.csv", body)
    digest = sha(body)
    _, token = open_page(client)
    save_manifest(client, digest, token)

    # Read through the same helper the route uses, inside one transaction.
    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        with dbmod.read_tx(conn):
            revision, result = recon_store.build_reconciliation(conn, "SZ01", "2026-01-01")
            assert revision == 2
            assert result.handover_ok is True
            assert result.matched[0].sha256 == digest

            # A writer committing on another connection cannot alter the
            # repeatable-read snapshot this request is pinned to.
            other = csv_text(f"SZ01,413000007,{BASE + 5},22.1,114.0")
            upload(client, "concurrent.csv", other)

            _, result_still = recon_store.build_reconciliation(conn, "SZ01", "2026-01-01")
            assert result_still.handover_ok is True
            assert not result_still.extras
    finally:
        conn.close()

    # After the writer commits, a fresh request sees the extra archive.
    page, _ = open_page(client)
    assert "清单外档 1 份" in page.text
    assert "不可交接" in page.text


def test_restart_keeps_manifest_and_revision(client):
    body = csv_text(f"SZ01,413000001,{BASE},22.5,114.2")
    upload(client, "clean.csv", body)
    digest = sha(body)
    _, token = open_page(client)
    save_manifest(client, digest, token)

    with TestClient(mainmod.app) as c2:
        page, token_after = open_page(c2)
        assert token_after == token + 1
        assert "可交接" in page.text
        assert digest in page.text
