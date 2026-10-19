"""반복 거래 (보너스 5.2): 월급·월세처럼 매달 반복되는 거래를 규칙으로 등록하고 특정 월에 생성한다.

- 규칙: "매월 day일, type, category, amount, memo, tags"
- ``apply(month)``는 **멱등(idempotent)**하다. 같은 달에 여러 번 실행해도 규칙마다 거래가
  한 번만 생긴다. 생성된 거래에 ``recurring_id``를 남겨서, 이미 만든 규칙을 알아본다.
- 그 달에 없는 날(예: 31일 규칙의 2월)은 그 달의 마지막 날로 맞춘다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import List, Set, Tuple

from .errors import NotFoundError
from .models import RecurringRule, Transaction, TransactionType, YearMonth
from .services import TRANSACTION_SEQUENCE, Clock, timestamp
from .storage import CategoryStore, RecurringRuleStore, SequenceStore, TransactionRepository

RULE_SEQUENCE = "recurring_rule"


@dataclass(frozen=True)
class RecurringDraft:
    day: int
    type: TransactionType
    amount: int
    category: str
    memo: str = ""
    tags: Tuple[str, ...] = ()


@dataclass
class ApplyReport:
    month: YearMonth
    created: List[Transaction] = field(default_factory=list)
    skipped: List[Tuple[RecurringRule, str]] = field(default_factory=list)


class RecurringService:
    def __init__(
        self,
        rules: RecurringRuleStore,
        transactions: TransactionRepository,
        categories: CategoryStore,
        sequences: SequenceStore,
        *,
        now: Clock = dt.datetime.now,
    ) -> None:
        self._rules = rules
        self._transactions = transactions
        self._categories = categories
        self._sequences = sequences
        self._now = now

    def add(self, draft: RecurringDraft) -> RecurringRule:
        # 지운 규칙의 id라도 이미 거래를 만들었다면 다시 쓰면 안 된다 (apply가 '이미 생성됨'으로 착각한다).
        used_by_transactions = max((tx.recurring_id or 0 for tx in self._transactions.iter_all()), default=0)
        floor = max(self._rules.max_id(), used_by_transactions)
        rule_id = self._sequences.next_value(RULE_SEQUENCE, floor=floor)
        rule = RecurringRule(
            id=rule_id,
            day=draft.day,
            type=draft.type,
            amount=draft.amount,
            category=draft.category,
            memo=draft.memo,
            tags=draft.tags,
            created_at=timestamp(self._now),
        )
        self._sequences.commit(RULE_SEQUENCE, rule_id)
        self._rules.add(rule)
        return rule

    def list_rules(self) -> List[RecurringRule]:
        return sorted(self._rules.iter_all(), key=lambda rule: (rule.day, rule.id))

    def remove(self, rule_id: int) -> RecurringRule:
        rule = self._rules.get(rule_id)
        if rule is not None:
            self._sequences.commit(RULE_SEQUENCE, rule_id)  # 지운 규칙 id를 다시 쓰지 않도록 기록
        if rule is None or not self._rules.delete(rule_id):
            raise NotFoundError(
                f"id {rule_id}에 해당하는 반복 규칙이 없습니다.",
                hint="'recurring list'로 규칙 id를 확인하세요.",
            )
        return rule

    def apply(self, month: YearMonth) -> ApplyReport:
        report = ApplyReport(month)
        rules = self.list_rules()
        if not rules:
            return report

        # 거래 파일을 한 번만 훑어서 '최대 id'와 '이 달에 이미 만든 규칙'을 함께 구한다.
        max_id = 0
        already: Set[int] = set()
        for tx in self._transactions.iter_all():
            max_id = max(max_id, tx.id)
            if tx.recurring_id is not None and month.contains(tx.date):
                already.add(tx.recurring_id)

        known = set(self._categories.names())
        next_id = self._sequences.next_value(TRANSACTION_SEQUENCE, floor=max_id)
        created_at = timestamp(self._now)
        with self._transactions.staged_append() as staged:
            for rule in rules:
                if rule.id in already:
                    report.skipped.append((rule, "이 달에 이미 생성됨"))
                    continue
                if rule.category not in known:
                    report.skipped.append((rule, f"카테고리 '{rule.category}'가 등록되어 있지 않음"))
                    continue
                tx = Transaction(
                    id=next_id,
                    type=rule.type,
                    date=rule.date_for(month),
                    amount=rule.amount,
                    category=rule.category,
                    memo=rule.memo,
                    tags=rule.tags,
                    recurring_id=rule.id,
                    created_at=created_at,
                )
                staged.add(tx)
                report.created.append(tx)
                next_id += 1
            if report.created:
                self._sequences.commit(TRANSACTION_SEQUENCE, report.created[-1].id)
        return report
