"""서비스 계층: 가계부의 비즈니스 규칙.

서비스는 저장소(파일 I/O)와 CLI(입출력 화면) 사이에 있다.

- 화면 출력, ``input()``, argparse를 전혀 모른다. → 테스트하기 쉽고 다른 UI에서도 다시 쓸 수 있다.
- 파일 형식(JSONL)을 모른다. 저장소 메서드만 호출한다.
- "카테고리는 등록된 것만", "사용 중인 카테고리는 삭제 금지" 같은 규칙을 여기서 지킨다.

조회와 집계는 저장소의 제너레이터를 그대로 이어 받아 처리한다.
파일 → ``iter_all()`` → ``filter_transactions()`` → ``heapq.nlargest()``
"""

from __future__ import annotations

import datetime as dt
import heapq
from collections import Counter
from dataclasses import dataclass, fields, replace
from enum import Enum
from typing import Any, Callable, Dict, Generic, Iterable, Iterator, List, Optional, Sequence, Tuple, TypeVar

from .decorators import timed
from .errors import ConflictError, NotFoundError, ValidationError
from .models import Budget, Category, Transaction, TransactionType, YearMonth
from .storage import BudgetStore, CategoryStore, RecurringRuleStore, SequenceStore, TransactionRepository

T = TypeVar("T")
Clock = Callable[[], dt.datetime]

TRANSACTION_SEQUENCE = "transaction"


def timestamp(now: Clock) -> str:
    return now().replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# 입력/조건/결과를 나타내는 작은 데이터 구조
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransactionDraft:
    """아직 id가 없는, 저장 전 거래. CLI가 만들고 서비스가 id를 붙여 Transaction으로 바꾼다."""

    type: TransactionType
    date: dt.date
    amount: int
    category: str
    memo: str = ""
    tags: Tuple[str, ...] = ()


@dataclass(frozen=True)
class TransactionChanges:
    """``update``에서 바꿀 필드. None인 필드는 '바꾸지 않음'이다."""

    date: Optional[dt.date] = None
    type: Optional[TransactionType] = None
    category: Optional[str] = None
    amount: Optional[int] = None
    memo: Optional[str] = None
    tags: Optional[Tuple[str, ...]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self) if getattr(self, f.name) is not None}

    def is_empty(self) -> bool:
        return not self.as_dict()

    def apply_to(self, tx: Transaction) -> Transaction:
        return replace(tx, **self.as_dict())


@dataclass(frozen=True)
class SearchCriteria:
    """검색 조건. 모든 조건은 AND로 묶인다. 비어 있는 조건은 검사하지 않는다."""

    date_from: Optional[dt.date] = None
    date_to: Optional[dt.date] = None
    category: Optional[str] = None
    type: Optional[TransactionType] = None
    keyword: Optional[str] = None
    tags: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.date_from is not None and self.date_to is not None and self.date_from > self.date_to:
            raise ValidationError(
                f"시작일({self.date_from})이 종료일({self.date_to})보다 늦습니다.",
                hint="--from 날짜가 --to 날짜와 같거나 더 앞서도록 지정하세요.",
            )

    @property
    def has_period(self) -> bool:
        return self.date_from is not None or self.date_to is not None

    @property
    def is_empty(self) -> bool:
        return not (self.has_period or self.category or self.type or self.keyword or self.tags)

    def matches(self, tx: Transaction) -> bool:
        if self.date_from is not None and tx.date < self.date_from:
            return False
        if self.date_to is not None and tx.date > self.date_to:
            return False
        if self.category is not None and tx.category != self.category:
            return False
        if self.type is not None and tx.type is not self.type:
            return False
        if self.keyword is not None and self.keyword.casefold() not in tx.memo.casefold():
            return False
        if self.tags and not set(self.tags).issubset(tx.tags):
            return False
        return True

    def describe(self) -> str:
        parts: List[str] = []
        if self.has_period:
            start = self.date_from.isoformat() if self.date_from else "처음"
            end = self.date_to.isoformat() if self.date_to else "끝"
            parts.append(f"기간 {start} ~ {end}")
        if self.category:
            parts.append(f"카테고리 {self.category}")
        if self.type:
            parts.append(f"타입 {self.type.value}")
        if self.keyword:
            parts.append(f"메모에 '{self.keyword}' 포함")
        if self.tags:
            parts.append("태그 " + ", ".join(f"#{tag}" for tag in self.tags))
        return " / ".join(parts) if parts else "조건 없음 (전체)"


@dataclass(frozen=True)
class ListResult:
    """조회 결과. ``items``는 화면에 보여 줄 거래, ``total``은 조건에 맞은 전체 건수."""

    items: List[Transaction]
    total: int


@dataclass(frozen=True)
class UpdateResult:
    before: Transaction
    after: Transaction

    @property
    def changed(self) -> bool:
        return self.before != self.after


# ---------------------------------------------------------------------------
# 제너레이터 도우미: 중간 리스트 없이 항목을 흘려보낸다.
# ---------------------------------------------------------------------------


def filter_transactions(items: Iterable[Transaction], criteria: SearchCriteria) -> Iterator[Transaction]:
    """조건에 맞는 거래만 통과시키는 제너레이터."""
    for tx in items:
        if criteria.matches(tx):
            yield tx


class CountingIterator(Generic[T]):
    """항목을 그대로 흘려보내면서 몇 개가 지나갔는지 센다."""

    def __init__(self, items: Iterable[T]) -> None:
        self._items = items
        self.count = 0

    def __iter__(self) -> Iterator[T]:
        self.count = 0  # 다시 순회하면 처음부터 센다.
        for item in self._items:
            self.count += 1
            yield item


def _recency_key(pair: Tuple[int, Transaction]) -> Tuple[dt.date, int]:
    index, tx = pair
    return (tx.date, index)  # 날짜가 같으면 파일에 나중에 저장된 것(index가 큰 것)이 더 최신


def newest_first(items: Iterable[Transaction], limit: Optional[int] = None) -> List[Transaction]:
    """최신순(날짜 내림차순, 같은 날짜는 나중에 저장한 것 먼저)으로 정렬한다.

    ``limit``이 있으면 ``heapq.nlargest``로 '지금까지 본 것 중 최신 limit개'만 들고 있으므로,
    거래가 백만 건이어도 메모리에는 limit개만 남는다.
    """
    indexed = enumerate(items)
    if limit is None:
        ordered = sorted(indexed, key=_recency_key, reverse=True)
    else:
        ordered = heapq.nlargest(limit, indexed, key=_recency_key)
    return [tx for _, tx in ordered]


def oldest_first(items: Iterable[Transaction]) -> List[Transaction]:
    """날짜 오름차순 (같은 날짜는 저장 순서대로)."""
    return [tx for _, tx in sorted(enumerate(items), key=_recency_key)]


# ---------------------------------------------------------------------------
# 거래 (CRUD + 조회)
# ---------------------------------------------------------------------------


class LedgerService:
    """거래 추가·조회·검색·수정·삭제."""

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

    def add(self, draft: TransactionDraft) -> Transaction:
        self.require_category(draft.category)
        new_id = self._sequences.next_value(TRANSACTION_SEQUENCE, floor=self._transactions.max_id())
        tx = Transaction(
            id=new_id,
            type=draft.type,
            date=draft.date,
            amount=draft.amount,
            category=draft.category,
            memo=draft.memo,
            tags=draft.tags,
            created_at=timestamp(self._now),
        )
        # id 발급을 먼저 기록한다. 그 사이에 멈추면 번호 하나가 비지만, 같은 id가 두 번 나오지는 않는다.
        self._sequences.commit(TRANSACTION_SEQUENCE, new_id)
        self._transactions.add(tx)
        return tx

    def get(self, tx_id: int) -> Transaction:
        tx = self._transactions.get(tx_id)
        if tx is None:
            raise NotFoundError(
                f"id {tx_id}에 해당하는 거래가 없습니다.",
                hint="'list'나 'search' 명령으로 올바른 id를 확인하세요.",
            )
        return tx

    def update(self, tx_id: int, changes: TransactionChanges) -> UpdateResult:
        if changes.is_empty():
            raise ValidationError(
                "수정할 항목을 지정하지 않았습니다.",
                hint="--date, --type, --category, --amount, --memo, --tags 중 하나 이상을 지정하세요.",
            )
        if changes.category is not None:
            self.require_category(changes.category)
        before = self.get(tx_id)
        after = changes.apply_to(before)
        result = UpdateResult(before, after)
        if result.changed and not self._transactions.replace(after):
            raise NotFoundError(f"id {tx_id}에 해당하는 거래가 없습니다.", hint="다른 곳에서 먼저 삭제되었을 수 있습니다.")
        return result

    def delete(self, tx_id: int) -> Transaction:
        tx = self.get(tx_id)
        # 지운 id가 다시 발급되지 않도록 먼저 기록한다 (meta.jsonl이 사라졌거나 뒤처진 경우 대비).
        self._sequences.commit(TRANSACTION_SEQUENCE, tx_id)
        if not self._transactions.delete(tx_id):
            raise NotFoundError(f"id {tx_id}에 해당하는 거래가 없습니다.", hint="다른 곳에서 먼저 삭제되었을 수 있습니다.")
        return tx

    def recent(self, limit: int) -> ListResult:
        counter = CountingIterator(self._transactions.iter_all())
        items = newest_first(counter, limit)
        return ListResult(items, counter.count)

    def search(self, criteria: SearchCriteria, limit: Optional[int] = None) -> ListResult:
        counter = CountingIterator(self.iter_matching(criteria))
        items = newest_first(counter, limit)
        return ListResult(items, counter.count)

    def iter_matching(self, criteria: SearchCriteria) -> Iterator[Transaction]:
        return filter_transactions(self._transactions.iter_all(), criteria)

    def require_category(self, name: str) -> None:
        if not self._categories.exists(name):
            names = ", ".join(self._categories.names()) or "(없음)"
            raise ValidationError(
                f"등록되지 않은 카테고리입니다: '{name}'",
                hint=f"사용 가능한 카테고리: {names}. 새 카테고리는 'category add {name}'로 먼저 등록하세요.",
            )


# ---------------------------------------------------------------------------
# 카테고리
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryUsage:
    name: str
    transactions: int
    rules: int


@dataclass(frozen=True)
class CategoryRemoval:
    name: str
    replaced_with: Optional[str]
    moved_transactions: int
    moved_rules: int


class CategoryService:
    """카테고리 등록·조회·삭제. 사용 중인 카테고리는 대체 카테고리 없이 지울 수 없다."""

    def __init__(
        self,
        categories: CategoryStore,
        transactions: TransactionRepository,
        rules: RecurringRuleStore,
        *,
        now: Clock = dt.datetime.now,
    ) -> None:
        self._categories = categories
        self._transactions = transactions
        self._rules = rules
        self._now = now

    def names(self) -> List[str]:
        return self._categories.names()

    def exists(self, name: str) -> bool:
        return self._categories.exists(name)

    def list_usage(self) -> List[CategoryUsage]:
        # 거래 파일을 한 번만 훑으면서 카테고리별로 센다. 메모리는 카테고리 수만큼만 쓴다.
        tx_counts = Counter(tx.category for tx in self._transactions.iter_all())
        rule_counts = Counter(rule.category for rule in self._rules.iter_all())
        return [CategoryUsage(name, tx_counts[name], rule_counts[name]) for name in self.names()]

    def add(self, name: str) -> Category:
        if self._categories.exists(name):
            raise ConflictError(f"이미 등록된 카테고리입니다: '{name}'", hint="'category list'로 목록을 확인하세요.")
        category = Category(name, created_at=timestamp(self._now))
        self._categories.add(category)
        return category

    def ensure_defaults(self, defaults: Sequence[str]) -> List[str]:
        """카테고리가 하나도 없으면 기본 카테고리를 만든다 (요구사항 4.4의 '안 A')."""
        if not self._categories.is_empty():
            return []
        created_at = timestamp(self._now)
        self._categories.add_many(Category(name, created_at) for name in defaults)
        return list(defaults)

    def remove(self, name: str, replace_with: Optional[str] = None) -> CategoryRemoval:
        names = self.names()
        if name not in names:
            raise NotFoundError(f"등록되지 않은 카테고리입니다: '{name}'", hint="'category list'로 목록을 확인하세요.")
        if replace_with is not None:
            if replace_with == name:
                raise ValidationError(
                    "대체 카테고리는 삭제할 카테고리와 달라야 합니다.",
                    hint="--replace-with에는 등록된 다른 카테고리를 지정하세요 ('category list'로 확인).",
                )
            if replace_with not in names:
                raise NotFoundError(
                    f"대체 카테고리 '{replace_with}'가 등록되어 있지 않습니다.",
                    hint=f"먼저 'category add {replace_with}'로 등록하세요.",
                )
        if len(names) == 1:
            raise ConflictError("마지막 남은 카테고리는 삭제할 수 없습니다.", hint="새 카테고리를 먼저 추가한 뒤 삭제하세요.")

        used_by_tx = sum(1 for tx in self._transactions.iter_all() if tx.category == name)
        used_by_rules = sum(1 for rule in self._rules.iter_all() if rule.category == name)
        if replace_with is None:
            if used_by_tx or used_by_rules:
                raise ConflictError(
                    f"'{name}' 카테고리를 쓰는 거래 {used_by_tx}건, 반복 규칙 {used_by_rules}건이 있어 삭제할 수 없습니다.",
                    hint=f"'category remove {name} --replace-with <다른 카테고리>'로 내역을 옮긴 뒤 삭제하세요.",
                )
            self._categories.remove(name)
            return CategoryRemoval(name, None, 0, 0)

        target = replace_with
        # 순서가 중요하다: 내역을 먼저 옮기고 카테고리를 마지막에 지운다. 중간에 멈춰도
        # '카테고리가 하나 더 남아 있는' 안전한 상태가 되고, 어떤 거래도 없는 카테고리를 가리키지 않는다.
        moved_tx = self._transactions.rewrite(
            lambda tx: replace(tx, category=target) if tx.category == name else tx
        )
        moved_rules = self._rules.rewrite(
            lambda rule: replace(rule, category=target) if rule.category == name else rule
        )
        self._categories.remove(name)
        return CategoryRemoval(name, target, moved_tx, moved_rules)


# ---------------------------------------------------------------------------
# 예산과 월별 요약
# ---------------------------------------------------------------------------


class BudgetLevel(Enum):
    OK = "ok"
    WARNING = "warning"  # 80% 이상 사용
    EXHAUSTED = "exhausted"  # 정확히 100% 사용
    OVER = "over"  # 초과


@dataclass(frozen=True)
class BudgetStatus:
    month: YearMonth
    budget: int
    spent: int

    WARNING_RATE = 80  # 퍼센트. 비교는 정수로 해서 부동소수점 오차가 경계값을 바꾸지 않게 한다.

    @property
    def usage_rate(self) -> float:
        return self.spent / self.budget * 100

    @property
    def remaining(self) -> int:
        return self.budget - self.spent

    @property
    def is_over(self) -> bool:
        return self.spent > self.budget

    @property
    def level(self) -> BudgetLevel:
        if self.spent > self.budget:
            return BudgetLevel.OVER
        if self.spent == self.budget:
            return BudgetLevel.EXHAUSTED
        if self.spent * 100 >= self.budget * self.WARNING_RATE:
            return BudgetLevel.WARNING
        return BudgetLevel.OK


def monthly_expense(transactions: Iterable[Transaction], month: YearMonth) -> int:
    return sum(
        tx.amount for tx in transactions if tx.type is TransactionType.EXPENSE and month.contains(tx.date)
    )


class BudgetService:
    def __init__(self, budgets: BudgetStore, transactions: TransactionRepository) -> None:
        self._budgets = budgets
        self._transactions = transactions

    def set_budget(self, month: YearMonth, amount: int) -> Tuple[Budget, Optional[Budget]]:
        budget = Budget(month, amount)
        previous = self._budgets.upsert(budget)
        return budget, previous

    def remove(self, month: YearMonth) -> Budget:
        budget = self._budgets.get(month)
        if budget is None or not self._budgets.remove(month):
            raise NotFoundError(f"{month} 예산이 설정되어 있지 않습니다.", hint="'budget list'로 설정된 예산을 확인하세요.")
        return budget

    def status(self, month: YearMonth) -> Optional[BudgetStatus]:
        budget = self._budgets.get(month)
        if budget is None:
            return None
        return BudgetStatus(month, budget.amount, monthly_expense(self._transactions.iter_all(), month))

    def list_statuses(self) -> List[BudgetStatus]:
        budgets: Dict[YearMonth, Budget] = {}
        for budget in self._budgets.iter_all():
            budgets[budget.month] = budget  # 같은 달이 여러 줄이면 마지막 값
        if not budgets:
            return []
        spent: Counter[YearMonth] = Counter()
        for tx in self._transactions.iter_all():  # 한 번만 훑으면서 예산이 있는 달의 지출을 모두 합산
            if tx.type is TransactionType.EXPENSE and tx.month in budgets:
                spent[tx.month] += tx.amount
        return [BudgetStatus(month, budgets[month].amount, spent[month]) for month in sorted(budgets)]


@dataclass(frozen=True)
class CategoryTotal:
    category: str
    amount: int
    share: float  # 그 달 총지출에서 차지하는 비율(%)


@dataclass(frozen=True)
class MonthlySummary:
    month: YearMonth
    income: int
    expense: int
    income_count: int
    expense_count: int
    top_expenses: Tuple[CategoryTotal, ...]
    expense_category_count: int
    budget: Optional[BudgetStatus]

    @property
    def balance(self) -> int:
        return self.income - self.expense

    @property
    def transaction_count(self) -> int:
        return self.income_count + self.expense_count

    @property
    def has_data(self) -> bool:
        return self.transaction_count > 0


class ReportService:
    def __init__(self, transactions: TransactionRepository, budgets: BudgetStore) -> None:
        self._transactions = transactions
        self._budgets = budgets

    @timed
    def monthly_summary(self, month: YearMonth, top: int) -> MonthlySummary:
        """한 달 요약. 파일을 한 번만 스트리밍하며 합계와 카테고리별 지출을 동시에 계산한다."""
        income = expense = income_count = expense_count = 0
        by_category: Counter[str] = Counter()
        for tx in self._transactions.iter_all():
            if not month.contains(tx.date):
                continue
            if tx.type is TransactionType.INCOME:
                income += tx.amount
                income_count += 1
            else:
                expense += tx.amount
                expense_count += 1
                by_category[tx.category] += tx.amount

        ranked = sorted(by_category.items(), key=lambda item: (-item[1], item[0]))[:top]
        top_expenses = tuple(
            CategoryTotal(name, amount, amount / expense * 100 if expense else 0.0) for name, amount in ranked
        )
        budget = self._budgets.get(month)
        status = BudgetStatus(month, budget.amount, expense) if budget else None
        return MonthlySummary(
            month=month,
            income=income,
            expense=expense,
            income_count=income_count,
            expense_count=expense_count,
            top_expenses=top_expenses,
            expense_category_count=len(by_category),
            budget=status,
        )
