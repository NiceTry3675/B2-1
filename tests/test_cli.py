"""CLI 통합 테스트: main()을 직접 호출해 출력과 종료 코드를 검사한다."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.helpers import TODAY, CliTestCase

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_CSV = PROJECT_ROOT / "examples" / "sample_transactions.csv"

ALL_COMMANDS = [
    [],
    ["add"], ["list"], ["search"], ["summary"], ["update"], ["delete"], ["import"], ["export"],
    ["budget"], ["budget", "set"], ["budget", "list"], ["budget", "remove"],
    ["category"], ["category", "add"], ["category", "list"], ["category", "remove"],
    ["recurring"], ["recurring", "add"], ["recurring", "list"], ["recurring", "remove"], ["recurring", "apply"],
    ["backup"], ["backup", "create"], ["backup", "list"], ["backup", "restore"],
]


class HelpTest(CliTestCase):
    def test_every_command_supports_help(self) -> None:
        for command in ALL_COMMANDS:
            with self.subTest(command=command):
                result = self.run_cli(*command, "--help")
                self.assertEqual(result.code, 0)
                self.assertIn("사용법", result.out)
                self.assertIn("--help", result.out)
        self.assertFalse(self.data_dir.exists(), "--help는 데이터 파일을 만들지 않는다")

    def test_short_h_is_not_an_option(self) -> None:
        self.assertEqual(self.run_cli("list", "-h").code, 2)

    def test_no_command_prints_help(self) -> None:
        result = self.run_cli()
        self.assertEqual(result.code, 0)
        self.assertIn("<command>", result.out)

    def test_group_without_subcommand_prints_group_help(self) -> None:
        result = self.run_cli("category")
        self.assertEqual(result.code, 0)
        self.assertIn("remove", result.out)


class FirstRunTest(CliTestCase):
    def test_creates_files_and_default_categories(self) -> None:
        result = self.run_cli("list")
        self.assertEqual(result.code, 0)
        self.assertIn("저장 파일을 새로 만들었습니다", result.err)
        self.assertIn("기본 카테고리", result.err)
        for name in ["transactions.jsonl", "categories.jsonl", "budgets.jsonl"]:
            self.assertTrue((self.data_dir / name).is_file(), name)
        second = self.run_cli("list")
        self.assertEqual(second.err, "")

    def test_data_dir_option_after_command_and_env_var(self) -> None:
        other = self.root / "other"
        result = self.run_cli("list", "--data-dir", str(other))
        self.assertEqual(result.code, 0)
        self.assertTrue((other / "transactions.jsonl").exists())

    def test_data_dir_that_is_a_file(self) -> None:
        bad = self.root / "file"
        bad.write_text("x", encoding="utf-8")
        result = self.run_cli("list", data_dir=bad)
        self.assertEqual(result.code, 5)
        self.assertIn("폴더가 아닙니다", result.err)


class AddFlowTest(CliTestCase):
    def test_interactive_add_with_reprompts(self) -> None:
        result = self.run_cli(
            "add",
            inputs=[
                "2026-02-30", "2026-10-02",  # 없는 날짜 → 재입력
                "transfer", "expense",  # 잘못된 타입 → 재입력
                "nope", "n", "food",  # 없는 카테고리 → 등록 거절 → 다시 고름
                "-5", "0", "12,000",  # 음수/0 → 재입력
                "점심", "점심, 회사",
                "",  # 저장 확인 (기본값 Y)
            ],
        )
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("존재하지 않는 날짜", result.out)
        self.assertIn("허용되지 않은 타입", result.out)
        self.assertIn("등록되지 않은 카테고리", result.out)
        self.assertIn("음수", result.out)
        self.assertIn("(id: 1)", result.out)
        record = json.loads((self.data_dir / "transactions.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(record["amount"], 12000)
        self.assertEqual(record["tags"], ["점심", "회사"])

    def test_default_date_is_today_and_new_category_registration(self) -> None:
        result = self.run_cli("add", inputs=["", "e", "coffee", "y", "4500", "", "", "y"])
        self.assertEqual(result.code, 0, result.err)
        record = json.loads((self.data_dir / "transactions.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(record["date"], TODAY.isoformat())  # main(today=...)으로 주입한 '오늘'
        self.assertEqual(record["category"], "coffee")
        self.assertIn("coffee", (self.data_dir / "categories.jsonl").read_text(encoding="utf-8"))

    def test_cancel_saves_nothing(self) -> None:
        result = self.run_cli("add", inputs=["2026-10-02", "e", "food", "100", "", "", "n"])
        self.assertEqual(result.code, 0)
        self.assertIn("취소", result.out)
        self.assertEqual((self.data_dir / "transactions.jsonl").read_text(encoding="utf-8"), "")

    def test_eof_aborts_with_error_code(self) -> None:
        result = self.run_cli("add", inputs=["2026-10-02"])
        self.assertEqual(result.code, 1)
        self.assertIn("EOF", result.err)

    def test_too_many_wrong_answers(self) -> None:
        result = self.run_cli("add", inputs=["bad"] * 5)
        self.assertEqual(result.code, 1)
        self.assertIn("5번 연속", result.err)

    def test_budget_warning_after_add(self) -> None:
        self.run_cli("budget", "set", "--month", "2026-10", "--amount", "10000")
        result = self.run_cli("add", inputs=["2026-10-02", "e", "food", "15000", "", "", "y"])
        self.assertIn("[경고]", result.out)
        self.assertIn("초과", result.out)


class CommandsTest(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        result = self.run_cli("import", "--from", str(SAMPLE_CSV))
        self.assertEqual(result.code, 0, result.err)

    def test_list_limit_and_order(self) -> None:
        result = self.run_cli("list", "--limit", "2")
        self.assertEqual(result.code, 0)
        lines = [line for line in result.out.splitlines() if "|" in line and line.strip()[0].isdigit()]
        self.assertEqual(len(lines), 2)
        self.assertIn("2026-10-25", lines[0])
        self.assertIn("최신 2건", result.out)

    def test_search(self) -> None:
        result = self.run_cli("search", "--from", "2026-10-01", "--to", "2026-10-31", "--category", "food")
        self.assertEqual(result.code, 0)
        self.assertIn("거래 3건", result.out)
        none = self.run_cli("search", "--q", "존재하지않는키워드")
        self.assertIn("조건에 맞는 거래가 없습니다", none.out)

    def test_summary_with_budget(self) -> None:
        self.run_cli("budget", "set", "--month", "2026-10", "--amount", "800000")
        result = self.run_cli("summary", "--month", "2026-10", "--top", "2")
        self.assertEqual(result.code, 0)
        for fragment in ["총 수입", "총 지출", "잔액", "TOP 2", "rent", "사용률" if False else "%", "[경고]"]:
            self.assertIn(fragment, result.out)

    def test_summary_no_data(self) -> None:
        result = self.run_cli("summary", "--month", "2030-01")
        self.assertEqual(result.code, 0)
        self.assertIn("데이터 없음", result.out)

    def test_update_and_delete(self) -> None:
        updated = self.run_cli("update", "--id", "3", "--amount", "15000", "--memo", "")
        self.assertEqual(updated.code, 0, updated.err)
        self.assertIn("수정했습니다", updated.out)
        missing = self.run_cli("update", "--id", "999", "--amount", "1")
        self.assertEqual(missing.code, 3)
        self.assertIn("없습니다", missing.err)
        deleted = self.run_cli("delete", "--id", "3", inputs=["y"])
        self.assertEqual(deleted.code, 0)
        self.assertIn("삭제했습니다", deleted.out)
        again = self.run_cli("delete", "--id", "3", "--yes")
        self.assertEqual(again.code, 3)

    def test_category_commands(self) -> None:
        self.assertEqual(self.run_cli("category", "add", "coffee").code, 0)
        self.assertEqual(self.run_cli("category", "add", "coffee").code, 4)
        blocked = self.run_cli("category", "remove", "food")
        self.assertEqual(blocked.code, 4)
        self.assertIn("--replace-with", blocked.err)
        moved = self.run_cli("category", "remove", "food", "--replace-with", "coffee")
        self.assertEqual(moved.code, 0)
        listing = self.run_cli("category", "list")
        self.assertNotIn("food ", listing.out)
        self.assertIn("coffee", listing.out)

    def test_export(self) -> None:
        out = self.root / "oct.csv"
        result = self.run_cli("export", "--month", "2026-10", "--out", str(out))
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("10건", result.out)
        self.assertTrue(out.read_text(encoding="utf-8").startswith("date,type,category,amount,memo,tags"))
        self.assertEqual(self.run_cli("export", "--out", str(self.root / "x.csv")).code, 2)
        self.assertEqual(self.run_cli("export", "--month", "2026-10", "--out", str(out)).code, 4)

    def test_import_rejected_shows_rows_and_hint(self) -> None:
        bad = self.write_csv("bad.csv", "date,type,category,amount\n2026-10-01,expense,food,-1\n")
        result = self.run_cli("import", "--from", str(bad))
        self.assertEqual(result.code, 2)
        self.assertIn("2번째 줄", result.err)
        self.assertIn("--skip-invalid", result.err)

    def test_import_dry_run_exit_code(self) -> None:
        bad = self.write_csv("bad.csv", "date,type,category,amount\n2026-10-01,expense,food,-1\n")
        self.assertEqual(self.run_cli("import", "--from", str(bad), "--dry-run").code, 2)
        good = self.write_csv("good.csv", "date,type,category,amount\n2026-10-01,expense,food,1\n")
        self.assertEqual(self.run_cli("import", "--from", str(good), "--dry-run").code, 0)


class ErrorOutputTest(CliTestCase):
    def test_usage_errors_are_korean_and_exit_2(self) -> None:
        for argv in (["list", "--limit", "0"], ["summary", "--month", "2026-13"], ["frob"], ["delete"]):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.code, 2)
                self.assertIn("오류:", result.err)
                self.assertIn("힌트:", result.err)
                self.assertNotIn("Traceback", result.err)

    def test_unexpected_error_is_friendly_and_debug_shows_trace(self) -> None:
        from unittest import mock

        with mock.patch("budget_app.services.LedgerService.recent", side_effect=ZeroDivisionError("boom")):
            result = self.run_cli("list")
            self.assertEqual(result.code, 1)
            self.assertIn("예기치 못한 오류", result.err)
            self.assertNotIn("Traceback", result.err)
            debug = self.run_cli("list", "--debug")
            self.assertIn("Traceback", debug.err)

    def test_os_error_maps_to_storage_exit_code(self) -> None:
        from unittest import mock

        self.run_cli("list")
        with mock.patch("budget_app.storage.JsonlFile.iter_raw", side_effect=PermissionError(13, "Permission denied", "x")):
            result = self.run_cli("list")
        self.assertEqual(result.code, 5)
        self.assertIn("권한", result.err)

    def test_timing_and_verbose_and_log_file(self) -> None:
        result = self.run_cli("list", "--timing")
        self.assertRegex(result.err, r"cmd_list 실행 시간: \d+\.\d+ms")
        verbose = self.run_cli("list", "--verbose")
        self.assertIn("시작 cmd_list", verbose.err)
        log = (self.data_dir / "app.log").read_text(encoding="utf-8")
        self.assertIn("완료 cmd_list -> 0", log)


class SubprocessTest(CliTestCase):
    """실제 `python -m budget_app` 실행으로 진입점과 종료 코드를 확인한다."""

    def run_module(self, *argv: str, stdin: str = "") -> subprocess.CompletedProcess:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, "-m", "budget_app", "--data-dir", str(self.data_dir), *argv],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(PROJECT_ROOT),
            env=env,
            timeout=60,
        )

    def test_module_entrypoint(self) -> None:
        ok = self.run_module("list")
        self.assertEqual(ok.returncode, 0)
        bad = self.run_module("delete", "--id", "1", "--yes")
        self.assertEqual(bad.returncode, 3)
        self.assertNotIn("Traceback", bad.stderr)
        added = self.run_module("add", stdin="2026-10-01\ne\nfood\n1000\n\n\ny\n")
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertIn("(id: 1)", added.stdout)
