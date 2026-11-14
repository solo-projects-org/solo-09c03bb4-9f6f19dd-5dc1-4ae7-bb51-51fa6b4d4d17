"""Pure-rule tests for station-day manifest reconciliation."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import reconcile
from app.reconcile import (
    ManifestError,
    ReceivedBatch,
    reconcile as run_reconcile,
)

H1 = "a" * 64
H2 = "b" * 64
H3 = "c" * 64


def batch(sha: str, *, rejected=0, foreign=0, detail=(), batch_id=None, filename=None):
    return ReceivedBatch(
        batch_id=batch_id or f"id-{sha[:6]}",
        filename=filename or f"{sha[:6]}.csv",
        sha256=sha,
        uploaded_at="2026-01-01T00:00:00+00:00",
        accepted_count=10,
        rejected_count=rejected,
        foreign_rows=foreign,
        foreign_detail=tuple(detail),
    )


# --------------------------------------------------------------------------
# manifest parsing / validation
# --------------------------------------------------------------------------


def test_manifest_accepts_bare_and_sha256sum_lines():
    text = f"  {H1}  some-file.csv  \n\n{H2}\n"
    assert reconcile.normalize_manifest(text) == [H1, H2]


def test_manifest_uppercased_hashes_are_normalised():
    assert reconcile.normalize_manifest(H1.upper()) == [H1]


def test_manifest_bad_line_reports_line_number():
    with pytest.raises(ManifestError) as exc:
        reconcile.normalize_manifest(f"{H1}\nnot-a-hash\n")
    assert exc.value.line_no == 2


def test_manifest_wrong_length_rejected():
    with pytest.raises(ManifestError):
        reconcile.normalize_manifest("a" * 63)


def test_manifest_empty_string_returns_empty_list():
    assert reconcile.normalize_manifest("  \n\t\n") == []


def test_validate_day_bounds():
    start, end = reconcile.day_bounds(date(2026, 1, 1))
    assert start == 1767225600
    assert end - start == 86400


def test_validate_day_rejects_bad_calendar_date():
    with pytest.raises(ManifestError):
        reconcile.validate_day("2026-02-30")


def test_validate_station_rules():
    assert reconcile.validate_station("  SZ_01-1. ") == "SZ_01-1."
    with pytest.raises(ManifestError):
        reconcile.validate_station("bad station!")
    with pytest.raises(ManifestError):
        reconcile.validate_station("")


# --------------------------------------------------------------------------
# reconciliation outcomes
# --------------------------------------------------------------------------


def test_perfect_one_to_one_match_allows_handover():
    result = run_reconcile("SZ01", "2026-01-01", [H1, H2], [batch(H1), batch(H2)])
    assert result.handover_ok is True
    assert {m.sha256 for m in result.matched} == {H1, H2}
    assert not (result.missing or result.duplicates or result.extras)


def test_missing_files_block_handover():
    result = run_reconcile("SZ01", "2026-01-01", [H1, H2], [batch(H1)])
    assert result.missing == [H2]
    assert result.handover_ok is False
    assert any("缺档" in b for b in result.blockers)


def test_extra_files_block_handover():
    result = run_reconcile("SZ01", "2026-01-01", [H1], [batch(H1), batch(H2)])
    assert [b.sha256 for b in result.extras] == [H2]
    assert result.handover_ok is False
    assert any("清单外档" in b for b in result.blockers)


def test_duplicate_receipts_block_handover_first_is_matched():
    first = batch(H1, batch_id="first", filename="a.csv")
    second = batch(H1, batch_id="second", filename="a-copy.csv")
    result = run_reconcile("SZ01", "2026-01-01", [H1], [second, first])
    assert result.handover_ok is False
    assert len(result.matched) == 1
    assert result.matched[0].batch.batch_id == "first"  # earliest upload wins
    assert [b.batch_id for b in result.duplicates] == ["second"]
    assert any("重复收档" in b for b in result.blockers)


def test_manifest_internal_duplicates_block_handover():
    result = run_reconcile("SZ01", "2026-01-01", [H1, H1], [batch(H1)])
    assert result.manifest_duplicates == [H1]
    assert result.handover_ok is False
    assert any("重复录入" in b for b in result.blockers)


def test_parse_rejects_make_matched_file_unqualified():
    bad = batch(H1, rejected=3)
    result = run_reconcile("SZ01", "2026-01-01", [H1], [bad])
    assert result.matched[0].content_ok is False
    assert "解析拒绝行" in result.matched[0].problems[0]
    assert result.handover_ok is False


def test_foreign_station_day_rows_make_matched_file_unqualified():
    bad = batch(H1, foreign=2, detail=("他站 1 行", "同站他日 1 行"))
    result = run_reconcile("SZ01", "2026-01-01", [H1], [bad])
    assert result.matched[0].content_ok is False
    problems = " ".join(result.matched[0].problems)
    assert "其他站日" in problems and "他站 1 行" in problems
    assert result.handover_ok is False


def test_clean_batch_with_foreign_hash_elsewhere_is_still_matched_clean():
    result = run_reconcile(
        "SZ01", "2026-01-01", [H1], [batch(H1), batch(H2, foreign=5)]
    )
    matched = result.matched[0]
    assert matched.sha256 == H1 and matched.content_ok is True
    # The extra file's own content problems do not flip the matched file, but
    # handover is still blocked because H2 is outside the manifest.
    assert result.handover_ok is False
    assert result.extras[0].content_ok is False


def test_empty_manifest_blocks_handover_even_without_batches():
    result = run_reconcile("SZ01", "2026-01-01", [], [])
    assert result.handover_ok is False
    assert any("清单为空" in b for b in result.blockers)


def test_order_independence_and_multiple_problems_accumulate():
    ghost = "d" * 64
    result = run_reconcile(
        "SZ01",
        "2026-01-01",
        [H1, H2, ghost],
        [batch(H2), batch(H1, rejected=1), batch(H3), batch(H3)],
    )
    assert set(result.missing) == {ghost}
    assert {m.sha256 for m in result.matched} == {H1, H2}
    # H3 is outside the manifest and arrives twice: one extra + one duplicate.
    assert {b.sha256 for b in result.extras} == {H3}
    assert [b.sha256 for b in result.duplicates] == [H3]
    assert result.handover_ok is False
    blocker_text = " ".join(result.blockers)
    assert "缺档" in blocker_text
    assert "重复收档" in blocker_text
    assert "清单外档" in blocker_text
    assert "内容不合格" in blocker_text
