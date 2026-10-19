"""보너스 기능: 백업/복원, 반복 거래, 표 정렬."""

from __future__ import annotations

import datetime as dt
import zipfile

from budget_app.errors import NotFoundError, ValidationError
from budget_app.formatting import Column, display_width, pad, render_table, truncate
from budget_app.models import TransactionType, YearMonth
from budget_app.recurring import RecurringDraft

from tests.helpers import AppTestCase, CliTestCase, draft
import unittest


class BackupTest(AppTestCase):
    def test_create_list_restore(self) -> None:
        self.ctx.ledger.add(draft(memo="before backup"))
        info = self.ctx.backups.create()
        self.assertRegex(info.path.name, r"^backup-\d{8}-\d{6}\.zip$")
        self.assertIn("transactions.jsonl", info.files)
        self.ctx.ledger.add(draft(memo="after backup"))

        report = self.ctx.backups.restore(info.path.name)
        memos = [tx.memo for tx in self.ctx.transactions.iter_all()]
        self.assertEqual(memos, ["before backup"])
        self.assertTrue(report.safety_backup.path.exists())
        self.assertEqual(len(self.ctx.backups.list_backups()), 2)

        # 안전 백업으로 다시 되돌릴 수 있다
        self.ctx.backups.restore(report.safety_backup.path.name)
        self.assertEqual([tx.memo for tx in self.ctx.transactions.iter_all()], ["before backup", "after backup"])

    def test_same_second_backups_get_unique_names(self) -> None:
        fixed = dt.datetime(2026, 10, 18, 23, 55, 1)
        from budget_app.backup import BackupService

        service = BackupService(self.data_dir, ["transactions.jsonl"], now=lambda: fixed)
        names = {service.create().path.name for _ in range(3)}
        self.assertEqual(len(names), 3)

    def test_restore_rejects_bad_files_and_zip_slip(self) -> None:
        with self.assertRaises(NotFoundError):
            self.ctx.backups.restore("nope.zip")
        self.ctx.backups.backup_dir.mkdir(parents=True, exist_ok=True)
        not_zip = self.ctx.backups.backup_dir / "backup-x.zip"
        not_zip.write_text("hello", encoding="utf-8")
        with self.assertRaises(ValidationError):
            self.ctx.backups.restore(not_zip.name)
        evil = self.ctx.backups.backup_dir / "backup-evil.zip"
        with zipfile.ZipFile(evil, "w") as archive:
            archive.writestr("../../escape.jsonl", "x")
        with self.assertRaises(ValidationError):
            self.ctx.backups.restore(evil.name)
        self.assertFalse((self.root / "escape.jsonl").exists())


class RecurringTest(AppTestCase):
    def test_apply_is_idempotent_and_clamps_day(self) -> None:
        salary = self.ctx.recurring.add(RecurringDraft(25, TransactionType.INCOME, 3_000_000, "salary", "월급"))
        rent = self.ctx.recurring.add(RecurringDraft(31, TransactionType.EXPENSE, 500_000, "rent", "월세"))
        feb = YearMonth(2026, 2)
        first = self.ctx.recurring.apply(feb)
        self.assertEqual([tx.date for tx in first.created], [dt.date(2026, 2, 25), dt.date(2026, 2, 28)])
        self.assertEqual({tx.recurring_id for tx in first.created}, {salary.id, rent.id})
        second = self.ctx.recurring.apply(feb)
        self.assertEqual((len(second.created), len(second.skipped)), (0, 2))
        third = self.ctx.recurring.apply(YearMonth(2026, 3))
        self.assertEqual(len(third.created), 2)
        self.assertEqual(len(list(self.ctx.transactions.iter_all())), 4)

    def test_ids_continue_after_manual_transactions(self) -> None:
        self.ctx.ledger.add(draft())
        self.ctx.recurring.add(RecurringDraft(1, TransactionType.EXPENSE, 1, "rent"))
        created = self.ctx.recurring.apply(YearMonth(2026, 1)).created
        self.assertEqual(created[0].id, 2)
        self.assertEqual(self.ctx.ledger.add(draft()).id, 3)

    def test_category_in_use_by_rule_is_protected_and_movable(self) -> None:
        from budget_app.errors import ConflictError

        self.ctx.category_service.add("gym")
        self.ctx.recurring.add(RecurringDraft(1, TransactionType.EXPENSE, 1, "gym"))
        with self.assertRaises(ConflictError):
            self.ctx.category_service.remove("gym")
        removal = self.ctx.category_service.remove("gym", replace_with="health")
        self.assertEqual(removal.moved_rules, 1)
        self.assertEqual(self.ctx.recurring.list_rules()[0].category, "health")

    def test_remove_rule(self) -> None:
        rule = self.ctx.recurring.add(RecurringDraft(1, TransactionType.EXPENSE, 1, "rent"))
        self.ctx.recurring.remove(rule.id)
        with self.assertRaises(NotFoundError):
            self.ctx.recurring.remove(rule.id)


class RecurringCliTest(CliTestCase):
    def test_add_list_apply(self) -> None:
        added = self.run_cli("recurring", "add", inputs=["25", "income", "salary", "3000000", "월급", ""])
        self.assertEqual(added.code, 0, added.err)
        self.assertIn("25일", self.run_cli("recurring", "list").out)
        applied = self.run_cli("recurring", "apply", "--month", "2026-11")
        self.assertIn("1건 생성", applied.out)
        again = self.run_cli("recurring", "apply", "--month", "2026-11")
        self.assertIn("0건 생성", again.out)


class TableFormattingTest(unittest.TestCase):
    def test_display_width(self) -> None:
        self.assertEqual(display_width("abc"), 3)
        self.assertEqual(display_width("식비"), 4)
        self.assertEqual(display_width("a식b"), 4)

    def test_pad_and_truncate(self) -> None:
        self.assertEqual(pad("식비", 6), "식비  ")
        self.assertEqual(pad("식비", 6, "right"), "  식비")
        self.assertEqual(truncate("가나다라마바", 7), "가나..")
        self.assertLessEqual(display_width(truncate("가나다라마바", 7)), 7)

    def test_columns_line_up_with_korean(self) -> None:
        table = render_table(
            [Column("이름"), Column("금액", "right")],
            [["식비", "12,000원"], ["transport", "1,450원"]],
        )
        lines = table.splitlines()
        positions = {display_width(line.split("|")[0]) for line in lines if "|" in line}
        self.assertEqual(len(positions), 1, table)


if __name__ == "__main__":
    unittest.main()
