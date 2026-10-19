from __future__ import annotations

import datetime as dt

from budget_app.errors import ConflictError, NotFoundError, ValidationError
from budget_app.models import TransactionType, YearMonth
from budget_app.services import (
    BudgetLevel,
    BudgetStatus,
    SearchCriteria,
    TransactionChanges,
    newest_first,
)

from tests.helpers import AppTestCase, draft


class LedgerTest(AppTestCase):
    def test_add_assigns_increasing_ids_and_never_reuses(self) -> None:
        first = self.ctx.ledger.add(draft())
        second = self.ctx.ledger.add(draft())
        self.assertEqual((first.id, second.id), (1, 2))
        self.ctx.ledger.delete(2)
        third = self.ctx.ledger.add(draft())
        self.assertEqual(third.id, 3)  # 삭제된 2를 다시 쓰지 않는다

    def test_add_recovers_when_meta_file_is_lost(self) -> None:
        for _ in range(3):
            self.ctx.ledger.add(draft())
        self.ctx.sequences.path.write_text("", encoding="utf-8")
        self.assertEqual(self.ctx.ledger.add(draft()).id, 4)

    def test_add_requires_registered_category(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            self.ctx.ledger.add(draft(category="nope"))
        self.assertIn("category add nope", caught.exception.hint or "")
        self.assertEqual(list(self.ctx.transactions.iter_all()), [])

    def test_recent_is_newest_first_with_tiebreak_and_limit(self) -> None:
        self.ctx.ledger.add(draft(date="2026-10-05", memo="a"))
        self.ctx.ledger.add(draft(date="2026-10-01", memo="b"))
        self.ctx.ledger.add(draft(date="2026-10-05", memo="c"))  # 같은 날짜, 나중에 저장
        self.ctx.ledger.add(draft(date="2026-09-30", memo="d"))
        result = self.ctx.ledger.recent(3)
        self.assertEqual([tx.memo for tx in result.items], ["c", "a", "b"])
        self.assertEqual(result.total, 4)

    def test_newest_first_without_limit(self) -> None:
        txs = [self.ctx.ledger.add(draft(date=d)) for d in ["2026-01-02", "2026-01-01", "2026-01-03"]]
        self.assertEqual([tx.id for tx in newest_first(txs)], [3, 1, 2])

    def test_update(self) -> None:
        tx = self.ctx.ledger.add(draft(memo="old", tags=("a",)))
        result = self.ctx.ledger.update(tx.id, TransactionChanges(amount=500, memo="", tags=()))
        self.assertTrue(result.changed)
        stored = self.ctx.ledger.get(tx.id)
        self.assertEqual((stored.amount, stored.memo, stored.tags, stored.category), (500, "", (), "food"))
        self.assertEqual(stored.created_at, tx.created_at)

    def test_update_errors(self) -> None:
        tx = self.ctx.ledger.add(draft())
        with self.assertRaises(ValidationError):
            self.ctx.ledger.update(tx.id, TransactionChanges())
        with self.assertRaises(NotFoundError):
            self.ctx.ledger.update(999, TransactionChanges(amount=1))
        with self.assertRaises(ValidationError):
            self.ctx.ledger.update(tx.id, TransactionChanges(category="nope"))
        self.assertFalse(self.ctx.ledger.update(tx.id, TransactionChanges(amount=tx.amount)).changed)

    def test_delete_missing(self) -> None:
        with self.assertRaises(NotFoundError):
            self.ctx.ledger.delete(42)


class SearchTest(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        add = self.ctx.ledger.add
        add(draft(date="2026-09-30", category="food", memo="저녁", tags=("친구",)))
        add(draft(date="2026-10-01", category="food", memo="점심 김밥", tags=("점심", "회사")))
        add(draft(date="2026-10-15", category="transport", memo="택시"))
        add(draft(date="2026-10-20", type=TransactionType.INCOME, category="salary", memo="월급"))
        add(draft(date="2026-10-31", category="food", memo="Lunch", tags=("점심",)))

    def ids(self, criteria: SearchCriteria) -> list:
        return [tx.id for tx in self.ctx.ledger.search(criteria).items]

    def test_period_inclusive(self) -> None:
        crit = SearchCriteria(date_from=dt.date(2026, 10, 1), date_to=dt.date(2026, 10, 31))
        self.assertEqual(self.ids(crit), [5, 4, 3, 2])

    def test_open_ended_period(self) -> None:
        self.assertEqual(self.ids(SearchCriteria(date_to=dt.date(2026, 10, 1))), [2, 1])
        self.assertEqual(self.ids(SearchCriteria(date_from=dt.date(2026, 10, 20))), [5, 4])

    def test_combined_filters(self) -> None:
        self.assertEqual(self.ids(SearchCriteria(category="food", tags=("점심",))), [5, 2])
        self.assertEqual(self.ids(SearchCriteria(tags=("점심", "회사"))), [2])
        self.assertEqual(self.ids(SearchCriteria(type=TransactionType.INCOME)), [4])
        self.assertEqual(self.ids(SearchCriteria(keyword="lunch")), [5])  # 대소문자 무시
        self.assertEqual(self.ids(SearchCriteria(keyword="점심")), [2])

    def test_limit_and_total(self) -> None:
        result = self.ctx.ledger.search(SearchCriteria(category="food"), limit=1)
        self.assertEqual(([tx.id for tx in result.items], result.total), ([5], 3))

    def test_from_after_to_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SearchCriteria(date_from=dt.date(2026, 10, 2), date_to=dt.date(2026, 10, 1))


class CategoryServiceTest(AppTestCase):
    def test_defaults_seeded_once(self) -> None:
        names = self.ctx.category_service.names()
        self.assertIn("food", names)
        self.assertEqual(self.ctx.category_service.ensure_defaults(["x"]), [])

    def test_empty_file_is_reseeded(self) -> None:
        self.ctx.categories.path.write_text("", encoding="utf-8")
        self.assertTrue(self.ctx.category_service.ensure_defaults(["food", "etc"]))

    def test_add_duplicate(self) -> None:
        self.ctx.category_service.add("coffee")
        with self.assertRaises(ConflictError):
            self.ctx.category_service.add("coffee")

    def test_remove_unused(self) -> None:
        self.ctx.category_service.add("coffee")
        self.ctx.category_service.remove("coffee")
        self.assertNotIn("coffee", self.ctx.category_service.names())

    def test_remove_in_use_is_blocked(self) -> None:
        self.ctx.ledger.add(draft(category="food"))
        with self.assertRaises(ConflictError) as caught:
            self.ctx.category_service.remove("food")
        self.assertIn("--replace-with", caught.exception.hint or "")
        self.assertIn("food", self.ctx.category_service.names())

    def test_remove_with_replacement_moves_transactions(self) -> None:
        tx = self.ctx.ledger.add(draft(category="food"))
        removal = self.ctx.category_service.remove("food", replace_with="etc")
        self.assertEqual(removal.moved_transactions, 1)
        self.assertEqual(self.ctx.ledger.get(tx.id).category, "etc")
        self.assertNotIn("food", self.ctx.category_service.names())

    def test_remove_errors(self) -> None:
        with self.assertRaises(NotFoundError):
            self.ctx.category_service.remove("nope")
        with self.assertRaises(NotFoundError):
            self.ctx.category_service.remove("food", replace_with="nope")
        with self.assertRaises(ValidationError):
            self.ctx.category_service.remove("food", replace_with="food")

    def test_last_category_cannot_be_removed(self) -> None:
        for name in self.ctx.category_service.names()[1:]:
            self.ctx.category_service.remove(name)
        with self.assertRaises(ConflictError):
            self.ctx.category_service.remove(self.ctx.category_service.names()[0])

    def test_usage_counts(self) -> None:
        self.ctx.ledger.add(draft(category="food"))
        self.ctx.ledger.add(draft(category="food"))
        usage = {u.name: u.transactions for u in self.ctx.category_service.list_usage()}
        self.assertEqual((usage["food"], usage["rent"]), (2, 0))


class SummaryAndBudgetTest(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        add = self.ctx.ledger.add
        add(draft(date="2026-10-25", type=TransactionType.INCOME, category="salary", amount=3_000_000))
        add(draft(date="2026-10-01", category="rent", amount=500_000))
        add(draft(date="2026-10-02", category="food", amount=30_000))
        add(draft(date="2026-10-03", category="food", amount=20_000))
        add(draft(date="2026-10-04", category="transport", amount=50_000))
        add(draft(date="2026-11-01", category="food", amount=99_999))  # 다른 달
        self.oct = YearMonth(2026, 10)

    def test_summary_totals_and_top(self) -> None:
        summary = self.ctx.reports.monthly_summary(self.oct, top=2)
        self.assertEqual((summary.income, summary.expense, summary.balance), (3_000_000, 600_000, 2_400_000))
        self.assertEqual([(c.category, c.amount) for c in summary.top_expenses], [("rent", 500_000), ("food", 50_000)])
        self.assertEqual(summary.expense_category_count, 3)
        self.assertIsNone(summary.budget)
        # food와 transport가 50,000원으로 같으면 이름순
        self.assertEqual(summary.top_expenses[1].category, "food")

    def test_summary_without_data(self) -> None:
        summary = self.ctx.reports.monthly_summary(YearMonth(2025, 1), top=5)
        self.assertFalse(summary.has_data)
        self.assertEqual(summary.top_expenses, ())

    def test_budget_status_in_summary(self) -> None:
        self.ctx.budget_service.set_budget(self.oct, 500_000)
        status = self.ctx.reports.monthly_summary(self.oct, top=5).budget
        assert status is not None
        self.assertEqual((status.spent, status.remaining), (600_000, -100_000))
        self.assertAlmostEqual(status.usage_rate, 120.0)
        self.assertIs(status.level, BudgetLevel.OVER)

    def test_budget_set_replaces_and_lists(self) -> None:
        _, previous = self.ctx.budget_service.set_budget(self.oct, 1_000_000)
        self.assertIsNone(previous)
        _, previous = self.ctx.budget_service.set_budget(self.oct, 700_000)
        assert previous is not None
        self.assertEqual(previous.amount, 1_000_000)
        self.ctx.budget_service.set_budget(YearMonth(2026, 11), 50_000)
        statuses = self.ctx.budget_service.list_statuses()
        self.assertEqual([(str(s.month), s.budget, s.spent) for s in statuses],
                         [("2026-10", 700_000, 600_000), ("2026-11", 50_000, 99_999)])

    def test_budget_remove(self) -> None:
        self.ctx.budget_service.set_budget(self.oct, 1)
        self.ctx.budget_service.remove(self.oct)
        self.assertIsNone(self.ctx.budget_service.status(self.oct))
        with self.assertRaises(NotFoundError):
            self.ctx.budget_service.remove(self.oct)

    def test_budget_levels(self) -> None:
        month = self.oct
        self.assertIs(BudgetStatus(month, 100, 79).level, BudgetLevel.OK)
        self.assertIs(BudgetStatus(month, 100, 80).level, BudgetLevel.WARNING)
        self.assertIs(BudgetStatus(month, 100, 100).level, BudgetLevel.EXHAUSTED)
        self.assertIs(BudgetStatus(month, 100, 101).level, BudgetLevel.OVER)
