"""입력 검증: 사용자가 입력한 *문자열*을 도메인 값으로 바꾸는 순수 함수 모음.

모든 함수는 ``(str) -> 값`` 모양이고, 잘못된 입력에는 원인과 해결 힌트를 담은
ValidationError를 던진다. 대화형 입력(재입력 루프), argparse 옵션, CSV 가져오기가
같은 함수를 쓰므로 검증 규칙은 이 파일 한 곳에만 있다.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from typing import Dict, Optional, Tuple

from .errors import ValidationError
from .models import TransactionType, YearMonth, parse_iso_date

MAX_AMOUNT = 1_000_000_000_000  # 1조 원: 오타로 들어온 비현실적인 값 차단
MAX_MEMO_LENGTH = 200
MAX_TAGS = 10
MAX_TAG_LENGTH = 20
MAX_CATEGORY_LENGTH = 20

DATE_HINT = "YYYY-MM-DD 형식으로 입력하세요 (예: 2026-10-18)."
MONTH_HINT = "YYYY-MM 형식으로 입력하세요 (예: 2026-10)."
AMOUNT_HINT = "원 단위 양의 정수로 입력하세요 (예: 12000 또는 12,000). 소수점은 쓸 수 없습니다."
TYPE_HINT = "income(수입) 또는 expense(지출) 중 하나를 입력하세요."

_DATE_SHAPE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MONTH_SHAPE_RE = re.compile(r"[0-9]{4}-[0-9]{2}")
# "12000" 또는 세 자리마다 쉼표를 찍은 "12,000"만 허용한다. "1,2,3" 같은 값은 거부.
_AMOUNT_RE = re.compile(r"[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+")
_DIGITS_RE = re.compile(r"[0-9]+")
# 글자/숫자로 시작하고(밑줄·하이픈 X), 이후에는 글자·숫자·밑줄·하이픈·&·공백 허용. \w는 한글 포함.
_CATEGORY_RE = re.compile(r"[^\W_][\w&\- ]*")

TYPE_ALIASES: Dict[str, TransactionType] = {
    "income": TransactionType.INCOME,
    "i": TransactionType.INCOME,
    "in": TransactionType.INCOME,
    "수입": TransactionType.INCOME,
    "expense": TransactionType.EXPENSE,
    "e": TransactionType.EXPENSE,
    "ex": TransactionType.EXPENSE,
    "지출": TransactionType.EXPENSE,
}


def _clean_text(text: str) -> str:
    """줄바꿈·탭·연속 공백을 공백 하나로 바꾸고, 제어 문자(ESC 등)를 없앤다.

    제어 문자를 남기면 ``list`` 출력에서 터미널 색이나 커서를 조작할 수 있고 표 정렬도 깨진다.
    """
    collapsed = " ".join(text.split())
    return "".join(ch for ch in collapsed if unicodedata.category(ch) != "Cc")


def _ensure_encodable(value: str, field: str) -> None:
    """UTF-8로 저장할 수 없는 문자(깨진 입력에서 생기는 서로게이트 등)를 막는다."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationError(
            f"{field}에 저장할 수 없는 문자가 들어 있습니다.",
            hint="터미널 입력 인코딩이 UTF-8인지 확인한 뒤 다시 입력하세요.",
        ) from None


def parse_date(text: str) -> dt.date:
    value = text.strip()
    if not value:
        raise ValidationError("날짜를 입력하세요.", hint=DATE_HINT)
    if not _DATE_SHAPE_RE.fullmatch(value):
        raise ValidationError(f"날짜 형식이 올바르지 않습니다: '{value}'", hint=DATE_HINT)
    try:
        return parse_iso_date(value)
    except ValueError:
        raise ValidationError(
            f"존재하지 않는 날짜입니다: '{value}'",
            hint="월(01~12)과 그 달의 일 수를 확인하세요.",
        ) from None


def parse_month(text: str) -> YearMonth:
    value = text.strip()
    if not value:
        raise ValidationError("월을 입력하세요.", hint=MONTH_HINT)
    if not _MONTH_SHAPE_RE.fullmatch(value):
        raise ValidationError(f"월 형식이 올바르지 않습니다: '{value}'", hint=MONTH_HINT)
    try:
        return YearMonth.parse(value)
    except ValueError:
        raise ValidationError(f"존재하지 않는 월입니다: '{value}'", hint="월은 01~12 사이여야 합니다.") from None


def parse_type(text: str) -> TransactionType:
    value = text.strip()
    if not value:
        raise ValidationError("타입을 입력하세요.", hint=TYPE_HINT)
    try:
        return TYPE_ALIASES[value.lower()]
    except KeyError:
        raise ValidationError(f"허용되지 않은 타입입니다: '{value}'", hint=TYPE_HINT) from None


def parse_amount(text: str) -> int:
    original = text.strip()
    value = original
    if value.endswith("원"):
        value = value[:-1].rstrip()  # "12,000원", "12,000 원" 허용. 숫자 사이 공백("1 2 3")은 허용하지 않는다.
    if not value:
        raise ValidationError("금액을 입력하세요.", hint=AMOUNT_HINT)
    if value.startswith("-"):
        raise ValidationError(
            f"금액은 음수일 수 없습니다: '{original}'",
            hint="금액은 양의 정수로 입력하세요. 수입/지출 구분은 부호가 아니라 타입(income/expense)으로 합니다.",
        )
    if not _AMOUNT_RE.fullmatch(value):
        raise ValidationError(f"금액은 양의 정수여야 합니다: '{original}'", hint=AMOUNT_HINT)
    amount = int(value.replace(",", ""))
    if amount == 0:
        raise ValidationError("금액은 0보다 커야 합니다.", hint=AMOUNT_HINT)
    if amount > MAX_AMOUNT:
        raise ValidationError(
            f"금액이 너무 큽니다: {amount:,}원",
            hint=f"{MAX_AMOUNT:,}원 이하로 입력하세요.",
        )
    return amount


def parse_memo(text: str) -> str:
    value = _clean_text(text)  # 표 출력이 깨지지 않고, 저장 형식도 단순해진다.
    _ensure_encodable(value, "메모")
    if len(value) > MAX_MEMO_LENGTH:
        raise ValidationError(
            f"메모가 너무 깁니다 ({len(value)}자).",
            hint=f"{MAX_MEMO_LENGTH}자 이하로 입력하세요.",
        )
    return value


def parse_tag(text: str) -> str:
    """태그 하나를 정규화한다: 앞의 '#' 제거, 소문자, 공백 정리."""
    value = _clean_text(text.strip().lstrip("#")).lower()
    if not value:
        raise ValidationError("태그가 비어 있습니다.", hint="예: 점심, 회사")
    _ensure_encodable(value, "태그")
    if "," in value:
        raise ValidationError(f"태그에는 쉼표를 쓸 수 없습니다: '{value}'", hint="쉼표는 태그 구분자입니다.")
    if len(value) > MAX_TAG_LENGTH:
        raise ValidationError(
            f"태그가 너무 깁니다: '{value}'",
            hint=f"태그 하나는 {MAX_TAG_LENGTH}자 이하로 입력하세요.",
        )
    return value


def parse_tags(text: str) -> Tuple[str, ...]:
    """쉼표로 구분한 태그 목록. 빈 문자열이면 태그 없음 ``()``."""
    tags: Dict[str, None] = {}  # dict는 입력 순서를 지키면서 중복을 없애는 데 쓴다.
    for piece in text.split(","):
        if piece.strip():
            tags.setdefault(parse_tag(piece), None)
    if len(tags) > MAX_TAGS:
        raise ValidationError(
            f"태그가 너무 많습니다 ({len(tags)}개).",
            hint=f"태그는 최대 {MAX_TAGS}개까지 붙일 수 있습니다.",
        )
    return tuple(tags)


def normalize_category(text: str) -> str:
    """카테고리 이름을 정규화한다 (앞뒤 공백 제거, 소문자)."""
    value = " ".join(text.split()).lower()
    if not value:
        raise ValidationError("카테고리 이름을 입력하세요.", hint="예: food, transport, 식비")
    _ensure_encodable(value, "카테고리 이름")
    if len(value) > MAX_CATEGORY_LENGTH:
        raise ValidationError(
            f"카테고리 이름이 너무 깁니다: '{value}'",
            hint=f"{MAX_CATEGORY_LENGTH}자 이하로 입력하세요.",
        )
    if not _CATEGORY_RE.fullmatch(value):
        raise ValidationError(
            f"카테고리 이름에 쓸 수 없는 문자가 있습니다: '{value}'",
            hint="글자나 숫자로 시작해야 하고, 그 뒤에는 글자·숫자·공백·-·_·&만 쓸 수 있습니다.",
        )
    return value


def parse_positive_int(text: str, *, name: str, maximum: Optional[int] = None) -> int:
    value = text.strip()
    subject = f"{name}은(는) " if name else ""
    hint = f"1~{maximum} 사이의 숫자를 입력하세요." if maximum is not None else "숫자만 입력하세요. 예: 10"
    if not _DIGITS_RE.fullmatch(value) or int(value) == 0:
        raise ValidationError(f"{subject}1 이상의 정수여야 합니다: '{value}'", hint=hint)
    number = int(value)
    if maximum is not None and number > maximum:
        raise ValidationError(f"{subject}{maximum} 이하여야 합니다: {number}", hint=hint)
    return number


def parse_id(text: str) -> int:
    return parse_positive_int(text, name="id")


def parse_day_of_month(text: str) -> int:
    return parse_positive_int(text, name="일(day)", maximum=31)


def parse_keyword(text: str) -> str:
    value = _clean_text(text)
    if not value:
        raise ValidationError("검색어가 비어 있습니다.", hint="--q 뒤에 찾을 메모 키워드를 입력하세요.")
    return value
