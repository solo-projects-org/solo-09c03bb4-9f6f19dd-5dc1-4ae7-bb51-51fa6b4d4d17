"""站日原档交接对账的纯规则。

本模块不访问数据库，所有判定都是输入数据的纯函数，便于单测。

对账模型
--------
* 清单（manifest）：值班员录入的预期原档 SHA-256，每行一个，可直接粘贴
  ``sha256sum`` 输出（``<hash>  <filename>``，文件名部分忽略）。
* 实收（received）：已发布批次中，**含有**本站日本岸站定位行的批次原档，
  附带内容质检所需的统计（解析拒绝行数、其他站日定位行数）。
* 结论：

  - 缺档：清单里有、实收里没有的 SHA-256；
  - 重复收档：同一 SHA-256 实收超过一份（第一份参与匹配，其余列为重复）；
  - 清单外档：实收 SHA-256 不在清单中；
  - 清单重复：清单自身同一 SHA-256 录入多次（无法一一对应）；
  - 内容不合格：匹配到的批次含解析拒绝行，或含其他站日/其他岸站的定位行。

只有清单与实收**一一对应**且每档内容合格时，``handover_ok`` 才为真。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable, Sequence

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STATION_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class ManifestError(ValueError):
    """清单无法解析；调用方应拒绝提交并保留原清单。"""

    def __init__(self, message: str, line_no: int | None = None):
        super().__init__(message)
        self.message = message
        self.line_no = line_no


@dataclass(frozen=True)
class ReceivedBatch:
    """一个已发布批次原档及其对当前站日的内容质检结果。"""

    batch_id: str
    filename: str
    sha256: str
    uploaded_at: str
    accepted_count: int
    rejected_count: int
    foreign_rows: int
    foreign_detail: tuple[str, ...] = ()

    @property
    def content_ok(self) -> bool:
        return self.rejected_count == 0 and self.foreign_rows == 0

    def content_problems(self) -> list[str]:
        problems: list[str] = []
        if self.rejected_count:
            problems.append(f"含 {self.rejected_count} 行解析拒绝行")
        if self.foreign_rows:
            detail = "；".join(self.foreign_detail) if self.foreign_detail else ""
            problems.append(
                f"含 {self.foreign_rows} 行其他站日/其他岸站定位"
                + (f"（{detail}）" if detail else "")
            )
        return problems


@dataclass
class MatchedFile:
    sha256: str
    batch: ReceivedBatch
    content_ok: bool
    problems: list[str]


@dataclass
class Reconciliation:
    station: str
    day: str
    expected: list[str]
    manifest_duplicates: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    matched: list[MatchedFile] = field(default_factory=list)
    duplicates: list[ReceivedBatch] = field(default_factory=list)
    extras: list[ReceivedBatch] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def handover_ok(self) -> bool:
        return not self.blockers


# --------------------------------------------------------------------------
# 输入校验
# --------------------------------------------------------------------------


def validate_station(station: str) -> str:
    station = (station or "").strip()
    if not STATION_RE.match(station):
        raise ManifestError(
            "岸站标识需为 1–64 位字母、数字或 . _ - 字符", None
        )
    return station


def validate_day(text: str) -> date:
    text = (text or "").strip()
    match = DAY_RE.match(text)
    if not match:
        raise ManifestError("UTC 日期需为 YYYY-MM-DD 格式", None)
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError as exc:
        raise ManifestError(f"UTC 日期不是合法日历日期: {text}", None) from exc


def day_bounds(day: date) -> tuple[int, int]:
    """Return ``[start_utc, end_utc)`` epoch seconds for the UTC calendar day."""

    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    start_s = int(start.timestamp())
    return start_s, start_s + 86400


def normalize_manifest(text: str) -> list[str]:
    """Parse the pasted manifest into an ordered list of lowercase hashes.

    Accepts bare hashes (one per line) and ``sha256sum`` style lines
    (``<hash>  <filename>``).  Blank lines are ignored; duplicates are kept so
    the caller can flag them.  Any malformed line raises :class:`ManifestError`
    with the 1-based line number; the caller must keep the previous manifest.
    """

    hashes: list[str] = []
    for line_no, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        token = line.split()[0].lower()
        if not SHA256_RE.match(token):
            raise ManifestError(
                f"第 {line_no} 行不是合法的 SHA-256（应为 64 位十六进制）: {line[:80]!r}",
                line_no,
            )
        hashes.append(token)
    return hashes


# --------------------------------------------------------------------------
# 对账
# --------------------------------------------------------------------------


def reconcile(
    station: str,
    day: str,
    expected_hashes: Sequence[str],
    received: Iterable[ReceivedBatch],
) -> Reconciliation:
    """Compare the manifest with received batches and decide handover readiness.

    Pure function.  ``received`` may be in any order; it is consumed once and
    grouped by SHA-256 preserving arrival (``uploaded_at``) order.
    """

    result = Reconciliation(station=station, day=day, expected=list(expected_hashes))

    expected_counts: dict[str, int] = {}
    for digest in expected_hashes:
        expected_counts[digest] = expected_counts.get(digest, 0) + 1
    result.manifest_duplicates = sorted(h for h, n in expected_counts.items() if n > 1)

    by_hash: dict[str, list[ReceivedBatch]] = {}
    for batch in received:
        by_hash.setdefault(batch.sha256, []).append(batch)
    for batches in by_hash.values():
        batches.sort(key=lambda b: (b.uploaded_at, b.batch_id))

    seen_hashes: set[str] = set()
    for digest in expected_hashes:
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        batches = by_hash.get(digest, [])
        if not batches:
            result.missing.append(digest)
            continue
        first = batches[0]
        problems = first.content_problems()
        result.matched.append(
            MatchedFile(
                sha256=digest,
                batch=first,
                content_ok=not problems,
                problems=problems,
            )
        )
        result.duplicates.extend(batches[1:])

    expected_set = set(expected_hashes)
    for digest, batches in sorted(by_hash.items()):
        if digest not in expected_set:
            # Every received copy is outside the manifest.  The first one is
            # reported as the extra archive; further copies are duplicate
            # receipts on top of being outside the manifest.
            result.extras.extend(batches[:1])
            result.duplicates.extend(batches[1:])

    if not expected_hashes:
        result.blockers.append("清单为空，未录入任何预期原档")
    if result.manifest_duplicates:
        result.blockers.append(
            f"清单内重复录入 {len(result.manifest_duplicates)} 个 SHA-256，无法一一对应"
        )
    if result.missing:
        result.blockers.append(f"缺档 {len(result.missing)} 份")
    if result.duplicates:
        result.blockers.append(f"重复收档 {len(result.duplicates)} 份")
    if result.extras:
        result.blockers.append(f"清单外档 {len(result.extras)} 份")
    bad_content = [m for m in result.matched if not m.content_ok]
    if bad_content:
        result.blockers.append(f"内容不合格原档 {len(bad_content)} 份")

    return result
