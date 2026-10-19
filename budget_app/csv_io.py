"""CSV 가져오기(import) / 내보내기(export).

고정 스키마 (UTF-8, 헤더 포함)::

    date,type,category,amount,memo,tags
    2026-10-02,expense,food,12000,점심,"점심,회사"

- 가져오기는 **전부 아니면 전무(all-or-nothing)**다. 한 행이라도 오류가 있으면 아무것도
  저장하지 않는다. ``--skip-invalid``를 주면 올바른 행만 저장한다.
- CSV를 한 행씩 읽어 검증하고, 바로 임시 파일에 쓴다. 파일 전체를 메모리에 올리지 않는다.
- 내보내기도 임시 파일에 쓴 뒤 교체한다. 실패해도 반쯤 쓴 CSV가 남지 않는다.
"""

from __future__ import annotations

import csv
import datetime as dt
import os
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Collection, ContextManager, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from .decorators import timed
from .errors import ConflictError, NotFoundError, ValidationError
from .models import Category, Transaction
from .services import (
    TRANSACTION_SEQUENCE,
    Clock,
    SearchCriteria,
    TransactionDraft,
    filter_transactions,
    oldest_first,
    timestamp,
)
from .storage import AtomicWriter, CategoryStore, SequenceStore, TransactionRepository
from .validation import normalize_category, parse_amount, parse_date, parse_memo, parse_tags, parse_type

CSV_COLUMNS: Tuple[str, ...] = ("date", "type", "category", "amount", "memo", "tags")
REQUIRED_COLUMNS: Tuple[str, ...] = ("date", "type", "category", "amount")
SCHEMA_HINT = "첫 줄에 date,type,category,amount,memo,tags 헤더가 있어야 합니다 (memo, tags는 생략 가능)."
MAX_SHOWN_ERRORS = 10  # 화면에 보여 줄 행 오류 수
MAX_STORED_ERRORS = 100  # 메모리에 담아 둘 행 오류 수 (그 이상은 개수만 센다)
UNNAMED_COLUMN = "(이름 없는 열)"


@dataclass(frozen=True)
class RowError:
    line: int  # CSV 파일의 줄 번호 (헤더가 1번 줄)
    message: str


@dataclass
class ImportReport:
    source: Path
    dry_run: bool = False
    total_rows: int = 0
    imported: int = 0
    errors: List[RowError] = field(default_factory=list)  # 앞쪽 MAX_STORED_ERRORS개만 보관
    error_count: int = 0  # 오류 행 전체 개수
    created_categories: List[str] = field(default_factory=list)
    ignored_columns: List[str] = field(default_factory=list)
    first_id: Optional[int] = None
    last_id: Optional[int] = None

    @property
    def skipped(self) -> int:
        return self.error_count

    def add_error(self, line: int, message: str) -> None:
        # 오류 행이 수십만 개인 파일이어도 메모리가 늘지 않도록 앞부분만 보관한다.
        self.error_count += 1
        if len(self.errors) < MAX_STORED_ERRORS:
            self.errors.append(RowError(line, message))


class ImportRejectedError(ValidationError):
    """오류 행이 있어 가져오기를 통째로 취소했다. 행별 오류를 메시지에 담는다."""

    def __init__(self, report: ImportReport) -> None:
        lines = [f"CSV {report.total_rows}행 중 {report.skipped}행에 오류가 있어 아무것도 가져오지 않았습니다."]
        lines.extend(format_row_errors(report.errors, report.error_count))
        super().__init__(
            "\n".join(lines),
            hint="오류를 고친 뒤 다시 실행하거나, 올바른 행만 가져오려면 --skip-invalid를 붙이세요. "
            "등록되지 않은 카테고리는 --create-categories로 함께 등록할 수 있습니다.",
        )
        self.report = report


def format_row_errors(errors: Sequence[RowError], total: Optional[int] = None) -> List[str]:
    total = len(errors) if total is None else total
    shown = errors[:MAX_SHOWN_ERRORS]
    lines = [f"  - {error.line}번째 줄: {error.message}" for error in shown]
    if total > len(shown):
        lines.append(f"  ... 외 {total - len(shown)}건")
    return lines


@dataclass(frozen=True)
class ExportReport:
    path: Path
    count: int


class TransactionSink(Protocol):
    """가져온 거래를 받는 곳. 실제 저장(StagedAppend)과 검증 전용(_DryRunSink)이 같은 모양을 가진다."""

    def add(self, item: Transaction) -> None: ...

    def discard(self) -> None: ...


class _DryRunSink:
    """``--dry-run``용: 아무것도 쓰지 않는다. 데이터 파일을 복사하지 않으므로 읽기 전용 폴더에서도 동작한다."""

    def add(self, item: Transaction) -> None:
        pass

    def discard(self) -> None:
        pass


def _first_undecodable_line(path: Path) -> Optional[int]:
    """UTF-8로 읽을 수 없는 첫 줄의 번호. UTF-8 멀티바이트 문자 안에는 줄바꿈 바이트가 나올 수 없어서 줄 단위로 검사해도 된다."""
    with open(path, "rb") as handle:
        for lineno, raw in enumerate(handle, start=1):
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                return lineno
    return None


def _csv_error_hint(exc: csv.Error) -> str:
    text = str(exc)
    if "NUL" in text:
        return "파일에 NUL 문자가 있습니다. 텍스트 형식의 CSV 파일이 맞는지 확인하세요."
    if "unexpected end of data" in text:
        return "큰따옴표로 연 값이 닫히지 않았습니다. 따옴표 짝을 확인하세요."
    if "field larger than field limit" in text:
        return "한 칸의 값이 너무 깁니다. 따옴표가 닫히지 않아 여러 줄이 한 칸으로 합쳐졌을 수 있습니다."
    return "따옴표가 제대로 닫혔는지 확인하세요."


def parse_csv_row(row: Mapping[Optional[str], object]) -> TransactionDraft:
    """CSV 한 행(dict)을 검증해 TransactionDraft로 바꾼다. 문제가 있으면 ValidationError."""
    if row.get(None):  # DictReader는 헤더보다 많은 값을 None 키에 담는다.
        raise ValidationError("값이 헤더보다 많습니다. 쉼표가 들어간 값은 큰따옴표로 감싸세요.")

    def cell(name: str) -> str:
        value = row.get(name)
        return value if isinstance(value, str) else ""

    for name in REQUIRED_COLUMNS:
        if not cell(name).strip():
            raise ValidationError(f"필수 값 '{name}'이(가) 비어 있습니다.")
    return TransactionDraft(
        date=parse_date(cell("date")),
        type=parse_type(cell("type")),
        category=normalize_category(cell("category")),
        amount=parse_amount(cell("amount")),
        memo=parse_memo(cell("memo")),
        tags=parse_tags(cell("tags")),
    )


def transaction_to_row(tx: Transaction) -> List[str]:
    return [tx.date.isoformat(), tx.type.value, tx.category, str(tx.amount), tx.memo, ",".join(tx.tags)]


def _normalize_header(fieldnames: Optional[Sequence[str]]) -> List[str]:
    if not fieldnames:
        raise ValidationError("CSV 파일이 비어 있거나 헤더가 없습니다.", hint=SCHEMA_HINT)
    header = [name.strip().lower() for name in fieldnames]
    # 'date,type,...,tags,'처럼 끝에 쉼표가 붙으면 이름 없는 열이 생긴다. 무시할 열이므로 중복 검사에서 뺀다.
    duplicates = sorted({name for name in header if name and header.count(name) > 1})
    if duplicates:
        raise ValidationError(f"헤더에 같은 열이 여러 번 있습니다: {', '.join(duplicates)}", hint=SCHEMA_HINT)
    missing = [name for name in REQUIRED_COLUMNS if name not in header]
    if missing:
        raise ValidationError(f"CSV에 필수 열이 없습니다: {', '.join(missing)}", hint=SCHEMA_HINT)
    return header


class CsvService:
    def __init__(
        self,
        transactions: TransactionRepository,
        categories: CategoryStore,
        sequences: SequenceStore,
        *,
        now: Clock = dt.datetime.now,
    ) -> None:
        self._transactions = transactions
        self._categories = categories
        self._sequences = sequences
        self._now = now

    @timed
    def import_csv(
        self,
        source: Path,
        *,
        skip_invalid: bool = False,
        create_categories: bool = False,
        dry_run: bool = False,
    ) -> ImportReport:
        if not source.exists():
            raise NotFoundError(
                f"CSV 파일을 찾을 수 없습니다: {source}",
                hint="경로를 확인하세요. 상대 경로는 지금 작업 중인 폴더를 기준으로 합니다.",
            )
        if not source.is_file():
            raise ValidationError(f"파일이 아닙니다: {source}", hint="CSV 파일 경로를 지정하세요.")

        report = ImportReport(source=source, dry_run=dry_run)
        known = set(self._categories.names())
        new_categories: Dict[str, None] = {}
        next_id = self._sequences.next_value(TRANSACTION_SEQUENCE, floor=self._transactions.max_id())
        created_at = timestamp(self._now)
        reader: Optional["csv.DictReader[str]"] = None
        line = 1  # 마지막으로 읽은 행이 끝난 줄 번호 (헤더는 1번 줄)
        # --dry-run이면 데이터 파일을 복사하지 않는 빈 저장소를 쓴다. 둘 다 add()/discard()를 가진다.
        staging: ContextManager[TransactionSink] = (
            nullcontext(_DryRunSink()) if dry_run else self._transactions.staged_append()
        )
        try:
            # utf-8-sig: 엑셀이 파일 맨 앞에 붙이는 BOM 문자(U+FEFF)를 자동으로 떼어 낸다.
            with open(source, encoding="utf-8-sig", newline="") as handle, staging as staged:
                # strict=True: 닫히지 않은 따옴표 등을 조용히 넘기지 않고 csv.Error로 알린다.
                # (기본값 False면 따옴표가 안 닫힌 뒤의 모든 줄이 한 칸으로 합쳐져 '정상'으로 읽힌다.)
                reader = csv.DictReader(handle, strict=True)
                header = _normalize_header(reader.fieldnames)
                reader.fieldnames = header
                report.ignored_columns = [name or UNNAMED_COLUMN for name in header if name not in CSV_COLUMNS]

                for row in reader:  # DictReader도 이터레이터다: 한 번에 한 행만 읽는다.
                    line = reader.line_num  # 따옴표 안 줄바꿈까지 센 '물리적' 줄 번호
                    report.total_rows += 1
                    try:
                        draft = parse_csv_row(row)
                    except ValidationError as exc:
                        report.add_error(line, exc.message)
                        continue
                    if draft.category not in known:
                        if not create_categories:
                            report.add_error(line, f"등록되지 않은 카테고리입니다: '{draft.category}'")
                            continue
                        known.add(draft.category)
                        new_categories[draft.category] = None
                    staged.add(Transaction(
                        id=next_id,
                        type=draft.type,
                        date=draft.date,
                        amount=draft.amount,
                        category=draft.category,
                        memo=draft.memo,
                        tags=draft.tags,
                        created_at=created_at,
                    ))
                    report.first_id = report.first_id or next_id
                    report.last_id = next_id
                    report.imported += 1
                    next_id += 1

                report.created_categories = list(new_categories)
                if dry_run:
                    staged.discard()
                    return report
                if report.errors and not skip_invalid:
                    staged.discard()
                    raise ImportRejectedError(report)
                if report.imported:
                    # 순서: 새 카테고리 → id 기록 → (with 블록을 나가며) 거래 파일 교체.
                    # 중간에 멈춰도 '쓰이지 않은 카테고리'나 '비어 있는 id 번호'만 남는다.
                    for name in report.created_categories:
                        self._categories.add(Category(name, created_at))
                    if report.last_id is not None:
                        self._sequences.commit(TRANSACTION_SEQUENCE, report.last_id)
        except UnicodeDecodeError:
            bad_line = _first_undecodable_line(source)
            where = f" ({bad_line}번째 줄)" if bad_line is not None else ""
            raise ValidationError(
                f"CSV 파일을 UTF-8로 읽을 수 없습니다{where}.",
                hint="엑셀이라면 'CSV UTF-8(쉼표로 분리)' 형식으로 다시 저장하세요.",
            ) from None
        except csv.Error as exc:
            # 오류가 난 행은 마지막으로 읽은 정상 행(line) 바로 다음 줄에서 시작한다.
            where = f" ({line + 1}번째 줄부터)" if reader is not None else ""
            raise ValidationError(f"CSV 형식 오류{where}: {exc}", hint=_csv_error_hint(exc)) from None
        return report

    @timed
    def export_csv(
        self,
        destination: Path,
        criteria: SearchCriteria,
        *,
        overwrite: bool = False,
        protected: Collection[Path] = (),
    ) -> ExportReport:
        if not criteria.has_period:
            raise ValidationError(
                "내보내기에는 기간 조건이 필요합니다.",
                hint="--month YYYY-MM 또는 --from YYYY-MM-DD / --to YYYY-MM-DD 중 하나 이상을 지정하세요.",
            )
        # samefile은 (장치, inode)를 비교하므로 대소문자만 다른 경로, 하드 링크, 심볼릭 링크도 알아본다.
        if destination.exists() and any(path.exists() and os.path.samefile(destination, path) for path in protected):
            raise ValidationError(
                f"데이터 파일에는 내보낼 수 없습니다: {destination}",
                hint="--out에 다른 파일 이름을 지정하세요.",
            )
        if destination.is_dir():
            raise ValidationError(f"폴더가 아니라 파일 경로를 지정하세요: {destination}", hint="예: --out report.csv")
        if not destination.parent.exists():
            raise NotFoundError(
                f"저장할 폴더가 없습니다: {destination.parent}",
                hint="폴더를 먼저 만들거나 경로를 확인하세요.",
            )
        if destination.exists() and not overwrite:
            raise ConflictError(f"이미 같은 이름의 파일이 있습니다: {destination}", hint="덮어쓰려면 --force를 붙이세요.")

        # 조건에 맞는 거래만 모아 날짜순으로 정렬한다 (메모리에는 '결과'만 올라간다).
        rows = oldest_first(filter_transactions(self._transactions.iter_all(), criteria))
        with AtomicWriter(destination, text=True, newline="") as writer:
            csv_writer = csv.writer(writer.stream)
            csv_writer.writerow(CSV_COLUMNS)
            for tx in rows:
                csv_writer.writerow(transaction_to_row(tx))
        return ExportReport(destination, len(rows))
