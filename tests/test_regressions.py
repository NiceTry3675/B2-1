"""감사(audit)에서 발견된 문제들의 회귀 테스트."""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import zipfile
from pathlib import Path
from unittest import mock

from budget_app.cli import main
from budget_app.errors import StorageError, ValidationError
from budget_app.formatting import budget_rate
from budget_app.models import TransactionType, YearMonth
from budget_app.recurring import RecurringDraft
from budget_app.services import BudgetLevel, BudgetStatus, SearchCriteria, TransactionChanges
from budget_app.storage import AtomicWriter
from budget_app.validation import parse_memo, parse_tags

from tests.helpers import AppTestCase, CliTestCase, draft


class DataDirResolutionTest(CliTestCase):
    def test_unknown_home_user_is_friendly(self) -> None:
        result = self.run_cli("list", data_dir=Path("~no_such_user_budget_xyz/data"))
        self.assertEqual(result.code, 5)
        self.assertIn("'~'를 해석할 수 없습니다", result.err)
        self.assertNotIn("Traceback", result.err)

    def test_empty_data_dir_is_rejected(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        code = main(["--data-dir", "", "list"], stdout=out, stderr=err)
        self.assertEqual(code, 2)
        self.assertIn("비어 있습니다", err.getvalue())


class ErrorOutputRegressionTest(CliTestCase):
    def test_os_error_prints_single_error_line(self) -> None:
        self.run_cli("list")
        with mock.patch("budget_app.storage.JsonlFile.iter_raw", side_effect=PermissionError(13, "Permission denied", "x")):
            result = self.run_cli("list")
        self.assertEqual(result.code, 5)
        self.assertEqual(result.err.count("오류:"), 1, result.err)

    def test_unwritable_log_file_does_not_print_logging_traceback(self) -> None:
        self.data_dir.mkdir(parents=True)
        (self.data_dir / "app.log").mkdir()  # 로그 파일 자리에 폴더가 있어 열 수 없다
        result = self.run_cli("list")
        self.assertEqual(result.code, 0)
        self.assertNotIn("Logging error", result.err)
        self.assertNotIn("Traceback", result.err)

    def test_dash_value_hint(self) -> None:
        self.run_cli("list")
        result = self.run_cli("update", "--id", "1", "--memo", "-abc")
        self.assertEqual(result.code, 2)
        self.assertIn("--memo=값", result.err)


class _ClosedPipe(io.StringIO):
    def write(self, text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")


class BrokenPipeTest(CliTestCase):
    def test_closed_stdout_exits_quietly(self) -> None:
        err = io.StringIO()
        code = main(["--data-dir", str(self.data_dir), "list"], stdout=_ClosedPipe(), stderr=err)
        self.assertEqual(code, 0)
        self.assertNotIn("오류", err.getvalue().replace("안내", ""))


class StorageRegressionTest(AppTestCase):
    def test_deeply_nested_json_line_is_treated_as_corrupt(self) -> None:
        self.ctx.ledger.add(draft())
        with open(self.ctx.transactions.path, "a", encoding="utf-8") as handle:
            handle.write("[" * 100_000 + "]" * 100_000 + "\n")
        with self.assertLogs("budget_app.storage", level="WARNING"):
            self.assertEqual(len(self.ctx.ledger.recent(10).items), 1)

    def test_stored_lone_surrogate_is_treated_as_corrupt(self) -> None:
        self.ctx.ledger.add(draft())
        # JSON 문법으로는 올바르지만 UTF-8로 다시 쓸 수 없는 '짝 없는 서로게이트'(\\udcff)가 든 줄
        line = '{"id": 9, "type": "expense", "date": "2026-10-01", "amount": 1, "category": "food", "memo": "\\udcff"}'
        with open(self.ctx.transactions.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        with self.assertLogs("budget_app.storage", level="WARNING"):
            self.ctx.ledger.update(1, TransactionChanges(amount=5))
        self.assertIn(b"udcff", self.ctx.transactions.path.read_bytes())  # 원문 보존

    def test_duplicate_ids_only_first_line_changes(self) -> None:
        tx = self.ctx.ledger.add(draft(memo="first"))
        line = json.dumps(dict(tx.to_record(), memo="second"), ensure_ascii=False)
        with open(self.ctx.transactions.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        self.ctx.ledger.delete(tx.id)
        self.assertEqual([t.memo for t in self.ctx.transactions.iter_all()], ["second"])

    def test_fsync_failure_leaves_no_temp_file(self) -> None:
        target = self.ctx.budgets.path
        target.write_text("original\n", encoding="utf-8")
        with mock.patch("budget_app.storage.os.fsync", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                with AtomicWriter(target) as writer:
                    writer.write_line(b"new")
        self.assertEqual(target.read_text(encoding="utf-8"), "original\n")
        self.assertEqual([p.name for p in self.data_dir.iterdir() if p.name.endswith(".tmp")], [])

    def test_failed_append_is_rolled_back(self) -> None:
        self.ctx.ledger.add(draft())
        before = self.ctx.transactions.path.read_bytes()
        with mock.patch("budget_app.storage.os.fsync", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                self.ctx.transactions.add(self.ctx.ledger.get(1))
        self.assertEqual(self.ctx.transactions.path.read_bytes(), before)

    def test_read_only_file_is_not_silently_replaced(self) -> None:
        if os.name != "posix" or os.geteuid() == 0:
            self.skipTest("POSIX 일반 사용자 전용")
        tx = self.ctx.ledger.add(draft())
        os.chmod(self.ctx.transactions.path, 0o444)
        try:
            with self.assertRaises(StorageError):
                self.ctx.ledger.delete(tx.id)
        finally:
            os.chmod(self.ctx.transactions.path, 0o644)
        self.assertIsNotNone(self.ctx.transactions.get(tx.id))

    def test_symlinked_data_file_stays_a_symlink(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX 전용")
        real = self.root / "real-budgets.jsonl"
        real.write_text("", encoding="utf-8")
        self.ctx.budgets.path.unlink()
        self.ctx.budgets.path.symlink_to(real)
        self.ctx.budget_service.set_budget(YearMonth(2026, 10), 1000)
        self.assertTrue(self.ctx.budgets.path.is_symlink())
        self.assertIn("2026-10", real.read_text(encoding="utf-8"))

    def test_deleted_max_id_not_reused_even_if_meta_lost(self) -> None:
        for _ in range(3):
            self.ctx.ledger.add(draft())
        self.ctx.sequences.path.write_text("", encoding="utf-8")
        self.ctx.ledger.delete(3)
        self.assertEqual(self.ctx.ledger.add(draft()).id, 4)


class ValidationRegressionTest(AppTestCase):
    def test_control_characters_are_removed(self) -> None:
        self.assertEqual(parse_memo("빨강\x1b[31m글자\x07"), "빨강[31m글자")
        self.assertEqual(parse_tags("a\x1b,b"), ("a", "b"))

    def test_negative_amount_hint_is_context_neutral(self) -> None:
        from budget_app.validation import parse_amount

        with self.assertRaises(ValidationError) as caught:
            parse_amount("-1")
        self.assertIn("양의 정수", caught.exception.hint or "")


class BudgetRateDisplayTest(AppTestCase):
    def test_display_never_contradicts_level(self) -> None:
        month = YearMonth(2026, 10)
        cases = [
            (100_000, 79_999, "79.9%", BudgetLevel.OK),
            (100_000, 80_000, "80.0%", BudgetLevel.WARNING),
            (100_000, 100_000, "100.0%", BudgetLevel.EXHAUSTED),
            (100_000, 100_001, "100.1%", BudgetLevel.OVER),
            (3, 1, "33.3%", BudgetLevel.OK),
        ]
        for budget, spent, shown, level in cases:
            status = BudgetStatus(month, budget, spent)
            with self.subTest(spent=spent):
                self.assertEqual(budget_rate(status), shown)
                self.assertIs(status.level, level)


class ExportProtectionTest(AppTestCase):
    def test_hard_link_to_data_file_is_refused(self) -> None:
        self.ctx.ledger.add(draft())
        alias = self.root / "alias.csv"
        os.link(self.ctx.transactions.path, alias)
        crit = SearchCriteria(date_from=dt.date(2026, 1, 1))
        with self.assertRaises(ValidationError):
            self.ctx.csv.export_csv(alias, crit, overwrite=True, protected=self.ctx.data_files)

    def test_case_variant_path_is_refused_on_case_insensitive_fs(self) -> None:
        upper = self.ctx.transactions.path.with_name("TRANSACTIONS.jsonl")
        if not upper.exists():
            self.skipTest("대소문자를 구분하는 파일 시스템")
        crit = SearchCriteria(date_from=dt.date(2026, 1, 1))
        with self.assertRaises(ValidationError):
            self.ctx.csv.export_csv(upper, crit, overwrite=True, protected=self.ctx.data_files)


class CsvRegressionTest(AppTestCase):
    def test_utf8_error_reports_real_line(self) -> None:
        path = self.root / "cp949.csv"
        text = "date,type,category,amount,memo\n2026-10-01,expense,food,1,ok\n2026-10-02,expense,food,1,점심\n"
        path.write_bytes(text.encode("cp949"))
        with self.assertRaises(ValidationError) as caught:
            self.ctx.csv.import_csv(path)
        self.assertIn("3번째 줄", caught.exception.message)

    def test_dry_run_does_not_copy_data_file(self) -> None:
        path = self.root / "ok.csv"
        path.write_text("date,type,category,amount\n2026-10-01,expense,food,1\n", encoding="utf-8")
        with mock.patch.object(type(self.ctx.transactions), "staged_append", side_effect=AssertionError("copied")):
            report = self.ctx.csv.import_csv(path, dry_run=True)
        self.assertEqual(report.imported, 1)


class BackupRegressionTest(AppTestCase):
    def test_corrupted_zip_data_is_reported_as_damaged(self) -> None:
        for i in range(50):
            self.ctx.ledger.add(draft(memo=f"memo {i} " * 5))
        info = self.ctx.backups.create()
        data = bytearray(info.path.read_bytes())
        middle = len(data) // 3
        data[middle : middle + 40] = b"\x00" * 40
        info.path.write_bytes(bytes(data))
        with self.assertRaises(ValidationError):
            self.ctx.backups.restore(info.path.name)

    def test_list_skips_directories_and_sorts_same_second_newest_first(self) -> None:
        from budget_app.backup import BackupService

        fixed = dt.datetime(2026, 10, 18, 23, 55, 1)
        service = BackupService(self.data_dir, ["transactions.jsonl"], now=lambda: fixed)
        names = [service.create().path.name for _ in range(3)]
        (service.backup_dir / "backup-dir.zip").mkdir()
        listed = [info.path.name for info in service.list_backups()]
        self.assertEqual(listed, list(reversed(names)))

    def test_rule_ids_not_reused_after_removal(self) -> None:
        rule = self.ctx.recurring.add(RecurringDraft(1, TransactionType.EXPENSE, 1, "rent"))
        self.ctx.recurring.apply(YearMonth(2026, 1))
        self.ctx.recurring.remove(rule.id)
        self.ctx.sequences.path.write_text("", encoding="utf-8")  # meta가 사라져도
        new_rule = self.ctx.recurring.add(RecurringDraft(1, TransactionType.EXPENSE, 2, "rent"))
        self.assertNotEqual(new_rule.id, rule.id)
        self.assertEqual(len(self.ctx.recurring.apply(YearMonth(2026, 1)).created), 1)


class CancelledAddTest(CliTestCase):
    def test_cancel_does_not_create_new_category(self) -> None:
        result = self.run_cli("add", inputs=["2026-10-02", "e", "tea", "y", "100", "", "", "n"])
        self.assertEqual(result.code, 0)
        self.assertIn("아무것도 바뀌지 않았습니다", result.out)
        self.assertNotIn('"tea"', (self.data_dir / "categories.jsonl").read_text(encoding="utf-8"))

    def test_confirmed_add_creates_category(self) -> None:
        result = self.run_cli("add", inputs=["2026-10-02", "e", "tea", "y", "100", "", "", "y"])
        self.assertEqual(result.code, 0, result.err)
        self.assertIn('"tea"', (self.data_dir / "categories.jsonl").read_text(encoding="utf-8"))

    def test_backup_list_table(self) -> None:
        self.run_cli("backup")
        result = self.run_cli("backup", "list")
        self.assertIn("백업 파일", result.out)
        self.assertIn("최신순", result.out)


class DocReviewRegressionTest(AppTestCase):
    """학습 문서를 쓰며 발견한 문제들."""

    def write_csv(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_unclosed_quote_is_an_error_not_a_merged_row(self) -> None:
        path = self.write_csv(
            "q.csv",
            'date,type,category,amount,memo\n2026-10-01,expense,food,1,"unclosed\n2026-10-02,expense,food,2,ok\n',
        )
        with self.assertRaises(ValidationError) as caught:
            self.ctx.csv.import_csv(path)
        self.assertIn("2번째 줄부터", caught.exception.message)
        self.assertIn("따옴표", caught.exception.hint or "")
        self.assertEqual(list(self.ctx.transactions.iter_all()), [])

    def test_trailing_comma_header(self) -> None:
        path = self.write_csv("t.csv", "date,type,category,amount,,\n2026-10-01,expense,food,1,,\n")
        report = self.ctx.csv.import_csv(path)
        self.assertEqual(report.imported, 1)
        self.assertEqual(report.ignored_columns, ["(이름 없는 열)", "(이름 없는 열)"])

    def test_row_errors_are_capped_in_memory_but_counted(self) -> None:
        from budget_app.csv_io import MAX_STORED_ERRORS, ImportRejectedError

        rows = "".join("2026-10-01,expense,food,-1\n" for _ in range(150))
        path = self.write_csv("bad.csv", "date,type,category,amount\n" + rows)
        with self.assertRaises(ImportRejectedError) as caught:
            self.ctx.csv.import_csv(path)
        report = caught.exception.report
        self.assertEqual((report.skipped, len(report.errors)), (150, MAX_STORED_ERRORS))
        self.assertIn("외 140건", caught.exception.message)

    def test_amount_rejects_inner_spaces(self) -> None:
        from budget_app.validation import parse_amount

        self.assertEqual(parse_amount("12,000 원"), 12000)
        for text in ["1 2 3", "1 2,000"]:
            with self.subTest(text=text), self.assertRaises(ValidationError):
                parse_amount(text)

    def test_restore_does_not_lower_issued_ids(self) -> None:
        self.ctx.ledger.add(draft())
        info = self.ctx.backups.create()
        lost = self.ctx.ledger.add(draft(memo="복원 후에는 안전 백업에만 남는 거래"))
        self.ctx.backups.restore(info.path.name)
        self.assertGreater(self.ctx.ledger.add(draft()).id, lost.id)

    def test_export_cannot_overwrite_log_or_backups(self) -> None:
        self.ctx.ledger.add(draft())
        log = self.data_dir / "app.log"
        log.write_text("log", encoding="utf-8")
        backup = self.ctx.backups.create().path
        crit = SearchCriteria(date_from=dt.date(2026, 1, 1))
        for target in (log, backup):
            with self.subTest(target=target.name), self.assertRaises(ValidationError):
                self.ctx.csv.export_csv(target, crit, overwrite=True, protected=self.ctx.protected_files())

    def test_counting_iterator_restarts(self) -> None:
        from budget_app.services import CountingIterator

        counter = CountingIterator([1, 2, 3])
        list(counter)
        list(counter)
        self.assertEqual(counter.count, 3)


class ImportIdRangeTest(CliTestCase):
    def test_single_row_import_shows_single_id(self) -> None:
        path = self.write_csv("one.csv", "date,type,category,amount\n2026-10-01,expense,food,1\n")
        result = self.run_cli("import", "--from", str(path))
        self.assertIn("(id 1).", result.out)


if __name__ == "__main__":
    import unittest

    unittest.main()
