"""대화형 입력: ``input()``으로 값을 차례로 묻고, 잘못된 값이면 이유를 알려 주고 다시 묻는다.

``input`` 함수를 직접 부르지 않고 생성자로 받는다(의존성 주입). 그래서 테스트에서는
미리 준비한 답을 돌려주는 가짜 함수를 넣어 사람 없이 대화형 흐름을 검증할 수 있다.
"""

from __future__ import annotations

import datetime as dt
from typing import Callable, Optional, TextIO, Tuple, TypeVar

from .errors import InputAbortedError, ValidationError
from .recurring import RecurringDraft
from .services import CategoryService, TransactionDraft
from .validation import (
    normalize_category,
    parse_amount,
    parse_date,
    parse_day_of_month,
    parse_memo,
    parse_tags,
    parse_type,
)

InputFunc = Callable[[str], str]
T = TypeVar("T")

MAX_ATTEMPTS = 5
_YES = {"y", "yes", "예", "네", "ㅇ"}
_NO = {"n", "no", "아니오", "아니요", "ㄴ"}


class Prompter:
    """값 하나를 물어보고 검증 함수를 통과할 때까지 다시 묻는다 (최대 ``max_attempts``번)."""

    def __init__(self, input_func: InputFunc, out: TextIO, *, max_attempts: int = MAX_ATTEMPTS) -> None:
        self._input = input_func
        self._out = out
        self._max_attempts = max_attempts

    def say(self, text: str = "") -> None:
        print(text, file=self._out)

    def read(self, prompt: str) -> str:
        try:
            return self._input(prompt)
        except EOFError:
            raise InputAbortedError(
                "입력이 끝나서(EOF) 작업을 취소했습니다.",
                hint="대화형 명령은 터미널에서 직접 실행하고, 묻는 값을 모두 입력하세요.",
            ) from None

    def ask(self, label: str, parser: Callable[[str], T], *, default: Optional[str] = None) -> T:
        """``label``을 묻고 ``parser``로 변환한 값을 돌려준다.

        - Enter만 누르면 ``default``를 쓴다 (default가 ""이면 '선택 항목 건너뛰기').
        - parser가 ValidationError를 던지면 원인과 힌트를 보여 주고 다시 묻는다.
        """
        suffix = f" [{default}]" if default else ""
        for attempt in range(1, self._max_attempts + 1):
            raw = self.read(f"{label}{suffix}: ").strip()
            if not raw and default is not None:
                raw = default
            try:
                return parser(raw)
            except ValidationError as exc:
                self.say(f"  ! {exc.message}")
                if exc.hint:
                    self.say(f"    힌트: {exc.hint}")
                if attempt < self._max_attempts:
                    self.say(f"    다시 입력해 주세요. (남은 기회 {self._max_attempts - attempt}번)")
        raise InputAbortedError(
            f"'{label}' 값이 {self._max_attempts}번 연속 올바르지 않아 작업을 취소했습니다.",
            hint="입력 형식을 확인한 뒤 다시 실행하세요.",
        )

    def confirm(self, question: str, *, default: bool) -> bool:
        choices = "[Y/n]" if default else "[y/N]"
        for _ in range(self._max_attempts):
            answer = self.read(f"{question} {choices}: ").strip().lower()
            if not answer:
                return default
            if answer in _YES:
                return True
            if answer in _NO:
                return False
            self.say("  y(예) 또는 n(아니오)으로 답해 주세요.")
        raise InputAbortedError(
            "확인 질문에 올바른 답을 받지 못해 작업을 취소했습니다.",
            hint="y 또는 n으로 답하세요. delete와 backup restore는 --yes로 확인 질문을 건너뛸 수 있습니다.",
        )


def ask_category(prompter: Prompter, categories: CategoryService) -> Tuple[str, bool]:
    """등록된 카테고리를 고르게 한다. 없는 이름이면 새로 등록할지 묻는다.

    반환값은 ``(이름, 새로 등록해야 하는지)``다. 여기서 바로 등록하지 않고, 사용자가 마지막에
    저장을 확인했을 때 CLI가 등록한다. 그래서 저장을 취소하면 카테고리도 생기지 않는다.
    """
    prompter.say(f"  등록된 카테고리: {', '.join(categories.names())}")
    for _ in range(MAX_ATTEMPTS):
        name = prompter.ask("카테고리", normalize_category)
        if categories.exists(name):
            return name, False
        prompter.say(f"  ! '{name}'은(는) 등록되지 않은 카테고리입니다.")
        if prompter.confirm(f"  '{name}' 카테고리를 새로 등록할까요?", default=False):
            prompter.say(f"  저장할 때 카테고리 '{name}'도 함께 등록합니다.")
            return name, True
        prompter.say(f"  등록된 카테고리 중에서 골라 주세요: {', '.join(categories.names())}")
    raise InputAbortedError(
        f"카테고리를 {MAX_ATTEMPTS}번 연속 고르지 못해 작업을 취소했습니다.",
        hint="'category list'로 목록을 확인하거나 'category add <이름>'으로 먼저 등록하세요.",
    )


def prompt_transaction(
    prompter: Prompter, categories: CategoryService, today: dt.date
) -> Tuple[TransactionDraft, bool]:
    """거래 필드를 차례로 묻는다. 반환값: ``(초안, 카테고리를 새로 등록해야 하는지)``."""
    prompter.say("새 거래를 입력합니다. (Enter: [ ] 안의 기본값 사용, Ctrl+C: 취소)")
    tx_date = prompter.ask("날짜 (YYYY-MM-DD)", parse_date, default=today.isoformat())
    tx_type = prompter.ask("타입 (income=수입 / expense=지출)", parse_type)
    category, is_new = ask_category(prompter, categories)
    amount = prompter.ask("금액 (원, 양의 정수)", parse_amount)
    memo = prompter.ask("메모 (선택, Enter로 건너뛰기)", parse_memo, default="")
    tags = prompter.ask("태그 (선택, 쉼표로 구분)", parse_tags, default="")
    draft = TransactionDraft(type=tx_type, date=tx_date, amount=amount, category=category, memo=memo, tags=tags)
    return draft, is_new


def prompt_recurring_rule(prompter: Prompter, categories: CategoryService) -> Tuple[RecurringDraft, bool]:
    """반복 규칙 필드를 차례로 묻는다. 반환값: ``(초안, 카테고리를 새로 등록해야 하는지)``."""
    prompter.say("반복 거래 규칙을 입력합니다. 'recurring apply --month YYYY-MM'으로 해당 월에 거래를 만듭니다.")
    day = prompter.ask("매월 며칠 (1~31, 없는 날은 그 달 마지막 날)", parse_day_of_month)
    rule_type = prompter.ask("타입 (income=수입 / expense=지출)", parse_type)
    category, is_new = ask_category(prompter, categories)
    amount = prompter.ask("금액 (원, 양의 정수)", parse_amount)
    memo = prompter.ask("메모 (선택, Enter로 건너뛰기)", parse_memo, default="")
    tags = prompter.ask("태그 (선택, 쉼표로 구분)", parse_tags, default="")
    draft = RecurringDraft(day=day, type=rule_type, amount=amount, category=category, memo=memo, tags=tags)
    return draft, is_new
