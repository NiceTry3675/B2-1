"""모델 계층: 가계부의 핵심 데이터 구조.

- 모든 모델은 ``frozen=True`` dataclass(불변 객체)다. 값을 바꿀 때는
  ``dataclasses.replace()``로 *새 객체*를 만든다. 실수로 원본이 바뀌는 일이 없다.
- ``to_record()`` / ``from_record()``는 저장소(JSONL 한 줄 = dict 하나)와 주고받는
  형태로 바꾼다.
- ``from_record()``는 파일에서 읽은 *신뢰할 수 없는* 데이터를 검사한다. 형식이 틀리면
  ValueError / KeyError / TypeError를 던지고, 저장소 계층이 이를 '손상된 줄'로 처리한다.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple

Record = Dict[str, Any]
"""저장 파일 한 줄에 대응하는 dict 타입 별칭."""

_ISO_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_YEAR_MONTH_RE = re.compile(r"[0-9]{4}-[0-9]{2}")


class TransactionType(str, Enum):
    """거래 종류. ``str``도 상속하므로 ``"income"`` 같은 문자열처럼 다룰 수 있다."""

    INCOME = "income"
    EXPENSE = "expense"

    @property
    def label(self) -> str:
        return "수입" if self is TransactionType.INCOME else "지출"


def parse_iso_date(text: str) -> dt.date:
    """``YYYY-MM-DD``를 엄격하게 해석한다. 형식이 틀리거나 없는 날짜면 ValueError."""
    if not _ISO_DATE_RE.fullmatch(text):
        raise ValueError(f"YYYY-MM-DD 형식이 아닙니다: {text!r}")
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"존재하지 않는 날짜입니다: {text!r}") from None


@dataclass(frozen=True, order=True)
class YearMonth:
    """'연-월' 값 객체. 문자열 ``"2026-10"`` 대신 이 타입을 쓰면 잘못된 월이 들어올 수 없다."""

    year: int
    month: int

    def __post_init__(self) -> None:
        if not 1 <= self.year <= 9999:
            raise ValueError(f"연도는 1~9999 사이여야 합니다: {self.year}")
        if not 1 <= self.month <= 12:
            raise ValueError(f"월은 1~12 사이여야 합니다: {self.month}")

    @classmethod
    def parse(cls, text: str) -> YearMonth:
        """``YYYY-MM``을 엄격하게 해석한다. 형식이 틀리면 ValueError."""
        if not _YEAR_MONTH_RE.fullmatch(text):
            raise ValueError(f"YYYY-MM 형식이 아닙니다: {text!r}")
        return cls(int(text[:4]), int(text[5:]))

    @classmethod
    def from_date(cls, value: dt.date) -> YearMonth:
        return cls(value.year, value.month)

    @property
    def first_day(self) -> dt.date:
        return dt.date(self.year, self.month, 1)

    @property
    def last_day(self) -> dt.date:
        return dt.date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])

    def contains(self, value: dt.date) -> bool:
        return value.year == self.year and value.month == self.month

    def clamp_day(self, day: int) -> dt.date:
        """그 달에 없는 날(예: 2월 31일)이면 그 달의 마지막 날로 맞춘다."""
        return dt.date(self.year, self.month, min(day, self.last_day.day))

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


# ---------------------------------------------------------------------------
# 레코드(dict) 필드 읽기 도우미: 파일 내용을 믿지 않고 타입을 하나씩 확인한다.
# ---------------------------------------------------------------------------


def _get_int(record: Mapping[str, Any], key: str) -> int:
    value = record[key]
    # bool은 int의 하위 클래스이므로(True == 1) 명시적으로 걸러낸다.
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise TypeError(f"'{key}' 값은 정수여야 합니다: {value!r}")


def _get_str(record: Mapping[str, Any], key: str, default: Optional[str] = None) -> str:
    value = record.get(key)
    if value is None:
        if default is None:
            raise KeyError(key)
        return default
    if not isinstance(value, str):
        raise TypeError(f"'{key}' 값은 문자열이어야 합니다: {value!r}")
    _require_encodable(value, key)
    return value


def _require_encodable(value: str, key: str) -> None:
    # JSON의 "\udcff" 같은 짝 없는 서로게이트는 읽을 수는 있어도 다시 UTF-8로 쓸 수 없다.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"'{key}'에 UTF-8로 저장할 수 없는 문자가 있습니다") from None


def _get_tags(record: Mapping[str, Any]) -> Tuple[str, ...]:
    value = record.get("tags")
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(tag, str) for tag in value):
        raise TypeError(f"'tags' 값은 문자열 목록이어야 합니다: {value!r}")
    for tag in value:
        _require_encodable(tag, "tags")
    return tuple(value)


def _get_type(record: Mapping[str, Any]) -> TransactionType:
    value = _get_str(record, "type")
    try:
        return TransactionType(value)
    except ValueError:
        raise ValueError(f"'type'은 income 또는 expense여야 합니다: {value!r}") from None


@dataclass(frozen=True)
class Transaction:
    """거래 내역 한 건."""

    id: int
    type: TransactionType
    date: dt.date
    amount: int
    category: str
    memo: str = ""
    tags: Tuple[str, ...] = ()
    recurring_id: Optional[int] = None  # 반복 규칙으로 자동 생성된 경우 그 규칙의 id
    created_at: str = ""  # 저장 시각 (ISO 8601)

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise ValueError(f"id는 1 이상이어야 합니다: {self.id}")
        if self.amount <= 0:
            raise ValueError(f"금액은 양수여야 합니다: {self.amount}")
        if not self.category:
            raise ValueError("카테고리가 비어 있습니다")

    @property
    def month(self) -> YearMonth:
        return YearMonth.from_date(self.date)

    @property
    def signed_amount(self) -> int:
        """수입은 +, 지출은 -로 부호를 붙인 금액."""
        return self.amount if self.type is TransactionType.INCOME else -self.amount

    def to_record(self) -> Record:
        record: Record = {
            "id": self.id,
            "type": self.type.value,
            "date": self.date.isoformat(),
            "amount": self.amount,
            "category": self.category,
            "memo": self.memo,
            "tags": list(self.tags),
            "created_at": self.created_at,
        }
        if self.recurring_id is not None:
            record["recurring_id"] = self.recurring_id
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Transaction:
        # memo/tags/created_at/recurring_id는 없어도 기본값으로 읽는다.
        # 나중에 필드를 추가해도 예전 파일을 그대로 읽을 수 있다 (하위 호환).
        recurring_id = record.get("recurring_id")
        return cls(
            id=_get_int(record, "id"),
            type=_get_type(record),
            date=parse_iso_date(_get_str(record, "date")),
            amount=_get_int(record, "amount"),
            category=_get_str(record, "category"),
            memo=_get_str(record, "memo", default=""),
            tags=_get_tags(record),
            recurring_id=None if recurring_id is None else _get_int(record, "recurring_id"),
            created_at=_get_str(record, "created_at", default=""),
        )


@dataclass(frozen=True)
class Category:
    """거래를 분류하는 카테고리."""

    name: str
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("카테고리 이름이 비어 있습니다")

    def to_record(self) -> Record:
        return {"name": self.name, "created_at": self.created_at}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Category:
        return cls(
            name=_get_str(record, "name"),
            created_at=_get_str(record, "created_at", default=""),
        )


@dataclass(frozen=True)
class Budget:
    """월 예산."""

    month: YearMonth
    amount: int

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError(f"예산은 양수여야 합니다: {self.amount}")

    def to_record(self) -> Record:
        return {"month": str(self.month), "amount": self.amount}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Budget:
        return cls(
            month=YearMonth.parse(_get_str(record, "month")),
            amount=_get_int(record, "amount"),
        )


@dataclass(frozen=True)
class RecurringRule:
    """매달 반복되는 거래 규칙 (예: 매월 25일 월급, 매월 1일 월세)."""

    id: int
    day: int  # 1~31. 그 달에 없는 날이면 마지막 날로 맞춘다.
    type: TransactionType
    amount: int
    category: str
    memo: str = ""
    tags: Tuple[str, ...] = ()
    created_at: str = ""

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise ValueError(f"id는 1 이상이어야 합니다: {self.id}")
        if not 1 <= self.day <= 31:
            raise ValueError(f"반복 일자는 1~31 사이여야 합니다: {self.day}")
        if self.amount <= 0:
            raise ValueError(f"금액은 양수여야 합니다: {self.amount}")
        if not self.category:
            raise ValueError("카테고리가 비어 있습니다")

    def date_for(self, month: YearMonth) -> dt.date:
        return month.clamp_day(self.day)

    def to_record(self) -> Record:
        return {
            "id": self.id,
            "day": self.day,
            "type": self.type.value,
            "amount": self.amount,
            "category": self.category,
            "memo": self.memo,
            "tags": list(self.tags),
            "created_at": self.created_at,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> RecurringRule:
        return cls(
            id=_get_int(record, "id"),
            day=_get_int(record, "day"),
            type=_get_type(record),
            amount=_get_int(record, "amount"),
            category=_get_str(record, "category"),
            memo=_get_str(record, "memo", default=""),
            tags=_get_tags(record),
            created_at=_get_str(record, "created_at", default=""),
        )
