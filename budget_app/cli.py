"""CLI 계층: 명령줄 인자 해석(argparse) → 서비스 호출 → 결과 출력.

이 파일은 '사용자와 대화하는 일'만 한다.
- 문자열 인자를 검증된 값으로 바꾼다 (validation 모듈 재사용)
- 서비스 메서드를 부른다
- 결과를 formatting 모듈로 문자열로 만들어 출력하고 종료 코드를 돌려준다

공통 관심사는 데코레이터가 맡는다.
- 모든 명령은 ``dispatch()``를 거친다. ``@handle_errors``가 예외를 메시지+종료 코드로 바꾼다.
- 각 명령 핸들러에는 ``@log_execution``(실행 로그)과 ``@timed``(실행 시간)가 붙어 있다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Callable, NoReturn, Optional, Sequence, TextIO, TypeVar

from . import __version__
from .context import DATA_DIR_ENV, AppContext, Console, resolve_data_dir
from .csv_io import CSV_COLUMNS, format_row_errors
from .decorators import handle_errors, log_execution, timed
from .errors import BudgetAppError, ExitCode, ValidationError
from .formatting import (
    backups_table,
    budget_lines,
    budget_rate,
    budgets_table,
    categories_table,
    format_changes,
    format_draft,
    format_summary,
    format_transaction,
    money,
    rules_table,
    transactions_table,
)
from .interactive import InputFunc, prompt_recurring_rule, prompt_transaction
from .logconfig import LOG_FILE_NAME, configure_logging
from .models import Transaction, TransactionType, YearMonth
from .services import BudgetLevel, SearchCriteria, TransactionChanges
from .validation import (
    normalize_category,
    parse_amount,
    parse_date,
    parse_id,
    parse_keyword,
    parse_memo,
    parse_month,
    parse_positive_int,
    parse_tag,
    parse_tags,
    parse_type,
)

Handler = Callable[[argparse.Namespace, AppContext], int]
T = TypeVar("T")

PROG = "python -m budget_app"
DEFAULT_LIST_LIMIT = 20
DEFAULT_TOP = 5
OK = int(ExitCode.OK)


# ---------------------------------------------------------------------------
# argparse 확장: 한국어 오류 메시지, --help만 지원 (옵션 표기 '--'로 통일)
# ---------------------------------------------------------------------------

_ARGPARSE_MESSAGES = (
    (re.compile(r"the following arguments are required: (.*)"), r"다음 인자가 필요합니다: \1"),
    (re.compile(r"unrecognized arguments: (.*)"), r"알 수 없는 인자입니다: \1"),
    (re.compile(r"argument (\S+): invalid choice: (.*?) \(choose from (.*)\)"), r"\1: 올바르지 않은 선택입니다: \2 (선택 가능: \3)"),
    (
        re.compile(r"argument (\S+): expected one argument"),
        r"\1: 값이 하나 필요합니다 (값이 '-'로 시작하면 \1=값 처럼 '='로 붙여 쓰세요)",
    ),
    (re.compile(r"argument (\S+): not allowed with argument (\S+)"), r"\1: \2와(과) 함께 쓸 수 없습니다"),
    (re.compile(r"argument (\S+): (.*)"), r"\1: \2"),
)


def _translate_argparse_message(message: str) -> str:
    for pattern, replacement in _ARGPARSE_MESSAGES:
        if pattern.search(message):
            return pattern.sub(replacement, message, count=1)
    return message


class KoreanArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(
            int(ExitCode.INVALID_INPUT),
            f"오류: {_translate_argparse_message(message)}\n힌트: '{self.prog} --help'로 사용법을 확인하세요.\n",
        )


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=32, width=100)

    def add_usage(self, usage: Optional[str], actions: Any, groups: Any, prefix: Optional[str] = None) -> None:
        super().add_usage(usage, actions, groups, prefix="사용법: " if prefix is None else prefix)


def _arg_type(parser: Callable[[str], T]) -> Callable[[str], T]:
    """validation 함수를 argparse ``type=``으로 쓸 수 있게 감싼다 (ValidationError → ArgumentTypeError)."""

    def convert(text: str) -> T:
        try:
            return parser(text)
        except ValidationError as exc:
            detail = f" ({exc.hint})" if exc.hint else ""
            raise argparse.ArgumentTypeError(f"{exc.message}{detail}") from None

    convert.__name__ = getattr(parser, "__name__", "value")
    return convert


def _positive_int(name: str, maximum: Optional[int] = None) -> Callable[[str], int]:
    return _arg_type(lambda text: parse_positive_int(text, name=name, maximum=maximum))


def _add_help(parser: argparse.ArgumentParser) -> None:
    # argparse 기본 제목(영문)을 한국어로 바꾼다.
    parser._optionals.title = "옵션"
    parser._positionals.title = "위치 인자"
    parser.add_argument("--help", action="help", default=argparse.SUPPRESS, help="이 도움말을 보여 주고 종료")


def _common_options() -> argparse.ArgumentParser:
    """모든 명령에 붙는 공통 옵션. 명령 앞/뒤 어디에 써도 된다 (default=SUPPRESS 활용)."""
    common = argparse.ArgumentParser(add_help=False)
    group = common.add_argument_group("공통 옵션")
    group.add_argument(
        "--data-dir",
        metavar="DIR",
        default=argparse.SUPPRESS,
        help=f"데이터 폴더 (기본값 ./data, 환경 변수 {DATA_DIR_ENV}로도 지정 가능)",
    )
    group.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help="실행 로그를 화면(stderr)에도 출력")
    group.add_argument("--timing", action="store_true", default=argparse.SUPPRESS, help="명령 실행 시간을 출력")
    group.add_argument("--debug", action="store_true", default=argparse.SUPPRESS, help="오류가 나면 상세 정보(스택트레이스)도 출력")
    return common


def _add_command(
    subparsers: Any,
    name: str,
    common: argparse.ArgumentParser,
    *,
    help: str,
    handler: Optional[Handler],
    description: Optional[str] = None,
    epilog: Optional[str] = None,
    needs_storage: bool = True,
) -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = subparsers.add_parser(
        name,
        help=help,
        description=description or help,
        epilog=epilog,
        parents=[common],
        add_help=False,
        formatter_class=_HelpFormatter,
    )
    _add_help(parser)
    parser.set_defaults(handler=handler, needs_storage=needs_storage)
    return parser


def _group_help(parser: argparse.ArgumentParser) -> Handler:
    """하위 명령 없이 그룹 명령(예: 'category')만 입력했을 때 그 그룹의 도움말을 보여 준다."""

    def show(args: argparse.Namespace, ctx: AppContext) -> int:
        parser.print_help(file=ctx.console.out)
        return OK

    return show


def _add_period_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", type=_arg_type(parse_date), help="시작일 (포함)")
    parser.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", type=_arg_type(parse_date), help="종료일 (포함)")
    parser.add_argument("--month", metavar="YYYY-MM", type=_arg_type(parse_month), help="해당 월 전체 (--from/--to와 함께 쓸 수 없음)")


def _add_filter_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--category", metavar="NAME", type=_arg_type(normalize_category), help="카테고리가 일치하는 거래")
    parser.add_argument("--type", metavar="TYPE", type=_arg_type(parse_type), help="income 또는 expense")
    parser.add_argument("--q", dest="keyword", metavar="KEYWORD", type=_arg_type(parse_keyword), help="메모에 키워드가 들어간 거래 (대소문자 무시)")
    parser.add_argument(
        "--tag",
        dest="tags",
        metavar="TAG",
        action="append",
        type=_arg_type(parse_tag),
        help="태그가 붙은 거래 (여러 번 쓰면 모든 태그를 가진 거래)",
    )


MAIN_EPILOG = f"""\
예시:
  {PROG} add                                         # 대화형으로 거래 추가
  {PROG} list --limit 10                             # 최신 10건
  {PROG} search --from 2026-10-01 --to 2026-10-31 --category food
  {PROG} summary --month 2026-10 --top 3
  {PROG} budget set --month 2026-10 --amount 1000000
  {PROG} update --id 3 --amount 15000 --memo "점심 회식"
  {PROG} delete --id 3
  {PROG} import --from examples/sample_transactions.csv
  {PROG} export --month 2026-10 --out october.csv

명령별 도움말: {PROG} <command> --help
종료 코드: 0 정상 / 1 일반 오류 / 2 입력 오류 / 3 대상 없음 / 4 충돌 / 5 파일 오류 / 130 중단
"""


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = KoreanArgumentParser(
        prog=PROG,
        description="파일(JSONL)에 저장하는 콘솔 가계부",
        epilog=MAIN_EPILOG,
        parents=[common],
        add_help=False,
        formatter_class=_HelpFormatter,
    )
    _add_help(parser)
    parser.add_argument("--version", action="version", version=f"budget_app {__version__}", help="버전 정보 출력 (명령 앞에만 쓸 수 있음)")
    sub = parser.add_subparsers(dest="command", metavar="<command>", title="명령")

    # add ---------------------------------------------------------------
    _add_command(
        sub, "add", common, help="거래 추가 (대화형 입력)", handler=cmd_add,
        description="날짜, 타입, 카테고리, 금액, 메모, 태그를 차례로 입력받아 거래를 저장합니다.\n"
        "잘못 입력하면 이유를 알려 주고 다시 묻습니다. 저장하면 새 거래의 id를 보여 줍니다.",
        epilog=f"예시:\n  {PROG} add",
    )

    # list --------------------------------------------------------------
    p = _add_command(
        sub, "list", common, help="거래 목록 (최신순)", handler=cmd_list,
        description="거래를 최신순(날짜 내림차순, 같은 날짜는 나중에 저장한 것 먼저)으로 보여 줍니다.\n"
        "파일 전체를 메모리에 올리지 않고, 한 줄씩 읽으며 최신 N건만 유지합니다.",
        epilog=f"예시:\n  {PROG} list\n  {PROG} list --limit 50",
    )
    p.add_argument("--limit", metavar="N", type=_positive_int(""), default=DEFAULT_LIST_LIMIT, help=f"보여 줄 최대 건수 (기본값 {DEFAULT_LIST_LIMIT})")

    # search ------------------------------------------------------------
    p = _add_command(
        sub, "search", common, help="조건 검색 (최신순)", handler=cmd_search,
        description="조건에 맞는 거래를 최신순으로 보여 줍니다. 여러 조건은 모두 만족해야 합니다(AND).",
        epilog=f"예시:\n  {PROG} search --from 2026-10-01 --to 2026-10-31\n"
        f"  {PROG} search --month 2026-10 --type expense --category food\n"
        f"  {PROG} search --q 점심 --tag 회사",
    )
    _add_period_options(p)
    _add_filter_options(p)
    p.add_argument("--limit", metavar="N", type=_positive_int(""), help="보여 줄 최대 건수 (기본값: 전부)")

    # summary -----------------------------------------------------------
    p = _add_command(
        sub, "summary", common, help="월별 요약 (수입/지출/잔액, 카테고리 TOP N, 예산)", handler=cmd_summary,
        description="한 달의 총수입, 총지출, 잔액과 카테고리별 지출 TOP N을 보여 줍니다.\n"
        "그 달에 예산이 설정되어 있으면 예산 사용률과 초과 경고도 함께 보여 줍니다.",
        epilog=f"예시:\n  {PROG} summary --month 2026-10\n  {PROG} summary --month 2026-10 --top 3",
    )
    p.add_argument("--month", metavar="YYYY-MM", type=_arg_type(parse_month), help="요약할 월 (기본값: 이번 달)")
    p.add_argument("--top", metavar="N", type=_positive_int(""), default=DEFAULT_TOP, help=f"카테고리별 지출 상위 N개 (기본값 {DEFAULT_TOP})")

    # budget ------------------------------------------------------------
    budget = _add_command(sub, "budget", common, help="월 예산 설정/조회/삭제", handler=None, needs_storage=False)
    budget.set_defaults(handler=_group_help(budget))
    budget_sub = budget.add_subparsers(dest="budget_command", metavar="<subcommand>", title="하위 명령")
    p = _add_command(
        budget_sub, "set", common, help="월 예산 설정 (이미 있으면 바꿈)", handler=cmd_budget_set,
        description="월 예산을 저장합니다. --month/--amount를 생략하면 대화형으로 묻습니다.",
        epilog=f"예시:\n  {PROG} budget set --month 2026-10 --amount 1000000",
    )
    p.add_argument("--month", metavar="YYYY-MM", type=_arg_type(parse_month), help="예산을 설정할 월")
    p.add_argument("--amount", metavar="AMOUNT", type=_arg_type(parse_amount), help="예산 금액 (원, 양의 정수)")
    _add_command(budget_sub, "list", common, help="설정된 예산과 사용률 목록", handler=cmd_budget_list)
    p = _add_command(budget_sub, "remove", common, help="월 예산 삭제", handler=cmd_budget_remove,
                     epilog=f"예시:\n  {PROG} budget remove --month 2026-10")
    p.add_argument("--month", metavar="YYYY-MM", type=_arg_type(parse_month), required=True, help="예산을 지울 월")

    # category ----------------------------------------------------------
    category = _add_command(sub, "category", common, help="카테고리 관리 (add/list/remove)", handler=None, needs_storage=False)
    category.set_defaults(handler=_group_help(category))
    category_sub = category.add_subparsers(dest="category_command", metavar="<subcommand>", title="하위 명령")
    p = _add_command(
        category_sub, "add", common, help="카테고리 추가", handler=cmd_category_add,
        description="새 카테고리를 등록합니다. 이름을 생략하면 대화형으로 묻습니다. 이름은 소문자로 저장됩니다.",
        epilog=f"예시:\n  {PROG} category add coffee\n  {PROG} category add 식비",
    )
    p.add_argument("name", nargs="?", type=_arg_type(normalize_category), help="카테고리 이름")
    _add_command(category_sub, "list", common, help="카테고리 목록과 사용 건수", handler=cmd_category_list)
    p = _add_command(
        category_sub, "remove", common, help="카테고리 삭제", handler=cmd_category_remove,
        description="카테고리를 삭제합니다.\n"
        "그 카테고리를 쓰는 거래나 반복 규칙이 있으면 삭제를 막습니다.\n"
        "--replace-with로 대체 카테고리를 주면 내역을 그쪽으로 옮긴 다음 삭제합니다.",
        epilog=f"예시:\n  {PROG} category remove coffee\n  {PROG} category remove coffee --replace-with food",
    )
    p.add_argument("name", nargs="?", type=_arg_type(normalize_category), help="삭제할 카테고리 이름")
    p.add_argument("--replace-with", metavar="NAME", type=_arg_type(normalize_category), help="사용 중인 내역을 옮길 대체 카테고리")

    # update ------------------------------------------------------------
    p = _add_command(
        sub, "update", common, help="거래 수정 (옵션 방식)", handler=cmd_update,
        description="--id로 고른 거래에서 지정한 필드만 바꿉니다 (옵션 방식으로 고정).\n"
        "지정하지 않은 필드는 그대로 둡니다. --memo \"\" 또는 --tags \"\"로 메모/태그를 비울 수 있습니다.",
        epilog=f"예시:\n  {PROG} update --id 3 --amount 15000\n"
        f"  {PROG} update --id 3 --category transport --memo \"택시\" --tags \"야근,택시\"\n"
        f"  {PROG} update --id 3 --memo=\"-5% 할인\"   # 값이 '-'로 시작하면 '='로 붙여 씁니다",
    )
    p.add_argument("--id", dest="tx_id", metavar="ID", type=_arg_type(parse_id), required=True, help="수정할 거래 id")
    p.add_argument("--date", metavar="YYYY-MM-DD", type=_arg_type(parse_date), help="새 날짜")
    p.add_argument("--type", metavar="TYPE", type=_arg_type(parse_type), help="새 타입 (income/expense)")
    p.add_argument("--category", metavar="NAME", type=_arg_type(normalize_category), help="새 카테고리 (등록된 것)")
    p.add_argument("--amount", metavar="AMOUNT", type=_arg_type(parse_amount), help="새 금액")
    p.add_argument("--memo", metavar="TEXT", type=_arg_type(parse_memo), help="새 메모 (\"\"이면 비움)")
    p.add_argument("--tags", metavar="TAGS", type=_arg_type(parse_tags), help="새 태그, 쉼표 구분 (\"\"이면 비움)")

    # delete ------------------------------------------------------------
    p = _add_command(
        sub, "delete", common, help="거래 삭제", handler=cmd_delete,
        description="--id로 고른 거래를 삭제합니다. 삭제 전에 내용을 보여 주고 확인을 받습니다.",
        epilog=f"예시:\n  {PROG} delete --id 3\n  {PROG} delete --id 3 --yes   # 확인 없이 삭제",
    )
    p.add_argument("--id", dest="tx_id", metavar="ID", type=_arg_type(parse_id), required=True, help="삭제할 거래 id")
    p.add_argument("--yes", action="store_true", help="확인 질문 없이 바로 삭제")

    # import ------------------------------------------------------------
    columns = ",".join(CSV_COLUMNS)
    p = _add_command(
        sub, "import", common, help="CSV에서 거래 가져오기", handler=cmd_import,
        description=f"CSV 파일(UTF-8, 헤더 포함)의 거래를 한꺼번에 등록합니다.\n"
        f"열: {columns}  (memo, tags는 생략 가능, tags는 쉼표 구분)\n"
        "한 행이라도 오류가 있으면 아무것도 저장하지 않습니다 (전부 아니면 전무).",
        epilog=f"예시:\n  {PROG} import --from examples/sample_transactions.csv\n"
        f"  {PROG} import --from bank.csv --dry-run\n"
        f"  {PROG} import --from bank.csv --skip-invalid --create-categories",
    )
    p.add_argument("--from", dest="source", metavar="CSV", type=Path, required=True, help="가져올 CSV 파일 경로")
    p.add_argument("--skip-invalid", action="store_true", help="오류가 있는 행은 건너뛰고 올바른 행만 저장")
    p.add_argument("--create-categories", action="store_true", help="등록되지 않은 카테고리를 자동으로 등록")
    p.add_argument("--dry-run", action="store_true", help="검증만 하고 저장하지 않음")

    # export ------------------------------------------------------------
    p = _add_command(
        sub, "export", common, help="조건에 맞는 거래를 CSV로 내보내기", handler=cmd_export,
        description=f"기간 조건(--month 또는 --from/--to)은 반드시 하나 이상 있어야 합니다.\n"
        f"열: {columns}  (날짜 오름차순, UTF-8, 헤더 포함)\n"
        "--category/--type/--q/--tag로 더 좁힐 수 있습니다.",
        epilog=f"예시:\n  {PROG} export --month 2026-10 --out october.csv\n"
        f"  {PROG} export --from 2026-01-01 --to 2026-06-30 --type expense --out h1.csv",
    )
    p.add_argument("--out", metavar="CSV", type=Path, required=True, help="만들 CSV 파일 경로")
    _add_period_options(p)
    _add_filter_options(p)
    p.add_argument("--force", action="store_true", help="같은 이름의 파일이 있으면 덮어쓰기")

    # recurring (보너스) -------------------------------------------------
    recurring = _add_command(sub, "recurring", common, help="반복 거래 규칙 (월급, 월세 등)", handler=None, needs_storage=False)
    recurring.set_defaults(handler=_group_help(recurring))
    recurring_sub = recurring.add_subparsers(dest="recurring_command", metavar="<subcommand>", title="하위 명령")
    _add_command(recurring_sub, "add", common, help="반복 규칙 추가 (대화형 입력)", handler=cmd_recurring_add)
    _add_command(recurring_sub, "list", common, help="반복 규칙 목록", handler=cmd_recurring_list)
    p = _add_command(recurring_sub, "remove", common, help="반복 규칙 삭제", handler=cmd_recurring_remove,
                     epilog=f"예시:\n  {PROG} recurring remove --id 1")
    p.add_argument("--id", dest="rule_id", metavar="ID", type=_arg_type(parse_id), required=True, help="삭제할 규칙 id")
    p = _add_command(
        recurring_sub, "apply", common, help="지정한 월에 반복 거래 생성", handler=cmd_recurring_apply,
        description="모든 반복 규칙으로 해당 월의 거래를 만듭니다. 같은 달에 여러 번 실행해도 한 번만 만듭니다.",
        epilog=f"예시:\n  {PROG} recurring apply --month 2026-11",
    )
    p.add_argument("--month", metavar="YYYY-MM", type=_arg_type(parse_month), help="거래를 만들 월 (기본값: 이번 달)")

    # backup (보너스) ----------------------------------------------------
    backup = _add_command(
        sub, "backup", common, help="데이터 백업/복원 (하위 명령 없이 실행하면 백업 생성)", handler=cmd_backup_create,
        description="데이터 파일 전체를 <data-dir>/backups/backup-YYYYMMDD-HHMMSS.zip으로 백업합니다.",
        epilog=f"예시:\n  {PROG} backup\n  {PROG} backup list\n  {PROG} backup restore --file backup-20261018-235501.zip",
    )
    backup_sub = backup.add_subparsers(dest="backup_command", metavar="<subcommand>", title="하위 명령")
    _add_command(backup_sub, "create", common, help="백업 생성 (기본 동작)", handler=cmd_backup_create)
    _add_command(backup_sub, "list", common, help="백업 목록", handler=cmd_backup_list)
    p = _add_command(
        backup_sub, "restore", common, help="백업에서 복원", handler=cmd_backup_restore,
        description="백업 파일로 데이터를 되돌립니다. 복원 직전 상태도 자동으로 백업합니다.",
    )
    p.add_argument("--file", dest="backup_file", metavar="NAME", required=True, help="백업 파일 이름 또는 경로")
    p.add_argument("--yes", action="store_true", help="확인 질문 없이 바로 복원")

    return parser


# ---------------------------------------------------------------------------
# 실행 진입점
# ---------------------------------------------------------------------------


@handle_errors
def dispatch(args: argparse.Namespace, ctx: AppContext) -> int:
    """모든 명령이 지나가는 관문: 저장소를 준비하고 명령 핸들러를 부른다."""
    if args.needs_storage:
        ctx.initialize()
    handler: Handler = args.handler
    return handler(args, ctx)


def _harden_std_streams() -> None:
    """터미널 인코딩이 한글을 못 쓰더라도 출력 때문에 프로그램이 죽지 않게 한다."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def _discard_stdout(out: TextIO) -> None:
    """읽는 쪽이 닫힌 표준 출력을 /dev/null로 돌린다. 종료할 때 남은 버퍼를 비우다 또 실패하지 않게 한다."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, out.fileno())
    except (OSError, ValueError):
        pass


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
    input_func: Optional[InputFunc] = None,
    today: Callable[[], dt.date] = dt.date.today,
) -> int:
    """프로그램 진입점. 입출력 스트림, input 함수, 오늘 날짜를 바꿔 끼울 수 있다 (테스트용)."""
    if stdout is None and stderr is None:
        _harden_std_streams()
    out = stdout or sys.stdout
    try:
        code = _run(argv, out, stderr or sys.stderr, input_func, today)
        out.flush()
    except BrokenPipeError:
        # 'list | head'처럼 출력을 받던 프로그램이 먼저 끝났다. 사용자 오류가 아니므로 조용히 끝낸다.
        _discard_stdout(out)
        return OK
    return code


def _run(
    argv: Optional[Sequence[str]],
    out: TextIO,
    err: TextIO,
    input_func: Optional[InputFunc],
    today: Callable[[], dt.date],
) -> int:
    parser = build_parser()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            args = parser.parse_args(argv)
    except SystemExit as exc:  # --help, --version, 사용법 오류
        return exc.code if isinstance(exc.code, int) else (OK if exc.code is None else int(ExitCode.ERROR))

    if getattr(args, "handler", None) is None:
        parser.print_help(file=out)
        return OK

    console = Console(out, err, input_func or input)
    try:
        data_dir = resolve_data_dir(getattr(args, "data_dir", None))
    except BudgetAppError as exc:  # dispatch() 전이라 @handle_errors 밖이므로 여기서 직접 알린다.
        console.error(exc.message, exc.hint)
        return int(exc.exit_code)
    configure_logging(
        data_dir / LOG_FILE_NAME,
        err,
        verbose=getattr(args, "verbose", False),
        timing=getattr(args, "timing", False),
    )
    ctx = AppContext(data_dir, console, debug=getattr(args, "debug", False), today=today)
    return dispatch(args, ctx)


# ---------------------------------------------------------------------------
# 공통 도우미
# ---------------------------------------------------------------------------


def _criteria_from_args(args: argparse.Namespace, *, require_period: bool) -> SearchCriteria:
    month: Optional[YearMonth] = args.month
    date_from, date_to = args.date_from, args.date_to
    if month is not None:
        if date_from is not None or date_to is not None:
            raise ValidationError(
                "--month와 --from/--to는 함께 쓸 수 없습니다.",
                hint="한 가지 방식으로만 기간을 지정하세요.",
            )
        date_from, date_to = month.first_day, month.last_day
    if require_period and date_from is None and date_to is None:
        raise ValidationError(
            "기간 조건이 필요합니다.",
            hint="--month YYYY-MM 또는 --from YYYY-MM-DD / --to YYYY-MM-DD 중 하나 이상을 지정하세요.",
        )
    return SearchCriteria(
        date_from=date_from,
        date_to=date_to,
        category=args.category,
        type=args.type,
        keyword=args.keyword,
        tags=tuple(args.tags or ()),
    )


def _current_month(ctx: AppContext) -> YearMonth:
    return YearMonth.from_date(ctx.today())


def _print_budget_alert(ctx: AppContext, tx: Transaction) -> None:
    """지출을 저장한 뒤, 그 달 예산을 넘었거나 거의 다 썼으면 바로 알려 준다."""
    if tx.type is not TransactionType.EXPENSE:
        return
    status = ctx.budget_service.status(tx.month)
    if status is None or status.level is BudgetLevel.OK:
        return
    if status.level is BudgetLevel.OVER:
        ctx.console.print(
            f"[경고] {status.month} 예산 {money(status.budget)}을(를) {money(-status.remaining)} 초과했습니다! "
            f"(사용률 {budget_rate(status)})"
        )
    else:
        ctx.console.print(
            f"[주의] {status.month} 예산의 {budget_rate(status)}를 사용했습니다. "
            f"(남은 예산 {money(status.remaining)})"
        )


# ---------------------------------------------------------------------------
# 명령 핸들러: (args, ctx) -> 종료 코드
# ---------------------------------------------------------------------------


@log_execution
@timed
def cmd_add(args: argparse.Namespace, ctx: AppContext) -> int:
    prompter = ctx.prompter()
    draft, new_category = prompt_transaction(prompter, ctx.category_service, ctx.today())
    prompter.say()
    prompter.say("입력한 내용")
    prompter.say(format_draft(draft))
    if new_category:
        prompter.say(f"  (카테고리 '{draft.category}'은(는) 저장할 때 새로 등록됩니다)")
    if not prompter.confirm("이 내용으로 저장할까요?", default=True):
        ctx.console.print("저장을 취소했습니다. 아무것도 바뀌지 않았습니다.")
        return OK
    if new_category:
        ctx.category_service.add(draft.category)
        ctx.console.print(f"카테고리 '{draft.category}'을(를) 등록했습니다.")
    tx = ctx.ledger.add(draft)
    ctx.console.print(f"거래를 저장했습니다. (id: {tx.id})")
    _print_budget_alert(ctx, tx)
    return OK


@log_execution
@timed
def cmd_list(args: argparse.Namespace, ctx: AppContext) -> int:
    result = ctx.ledger.recent(args.limit)
    if result.total == 0:
        ctx.console.print("저장된 거래가 없습니다. 'add' 명령으로 첫 거래를 추가해 보세요.")
        return OK
    ctx.console.print(transactions_table(result.items))
    ctx.console.print()
    message = f"전체 {result.total}건 중 최신 {len(result.items)}건을 표시했습니다."
    if result.total > len(result.items):
        message += " (--limit으로 개수를 바꿀 수 있습니다)"
    ctx.console.print(message)
    return OK


@log_execution
@timed
def cmd_search(args: argparse.Namespace, ctx: AppContext) -> int:
    criteria = _criteria_from_args(args, require_period=False)
    if criteria.category is not None and not ctx.category_service.exists(criteria.category):
        ctx.console.notice(f"'{criteria.category}'은(는) 등록되지 않은 카테고리입니다.")
    result = ctx.ledger.search(criteria, args.limit)
    ctx.console.print(f"검색 조건: {criteria.describe()}")
    if result.total == 0:
        ctx.console.print("조건에 맞는 거래가 없습니다.")
        return OK
    ctx.console.print(transactions_table(result.items))
    ctx.console.print()
    if result.total > len(result.items):
        ctx.console.print(f"조건에 맞는 {result.total}건 중 최신 {len(result.items)}건을 표시했습니다.")
    else:
        ctx.console.print(f"조건에 맞는 거래 {result.total}건")
    return OK


@log_execution
@timed
def cmd_summary(args: argparse.Namespace, ctx: AppContext) -> int:
    month: YearMonth = args.month or _current_month(ctx)
    summary = ctx.reports.monthly_summary(month, args.top)
    ctx.console.print(format_summary(summary))
    return OK


@log_execution
@timed
def cmd_budget_set(args: argparse.Namespace, ctx: AppContext) -> int:
    prompter = ctx.prompter()
    month: YearMonth = args.month or prompter.ask("예산을 설정할 월 (YYYY-MM)", parse_month, default=str(_current_month(ctx)))
    amount: int = args.amount or prompter.ask("예산 금액 (원)", parse_amount)
    budget, previous = ctx.budget_service.set_budget(month, amount)
    message = f"{budget.month} 예산을 {money(budget.amount)}으로 저장했습니다."
    if previous is not None:
        message += f" (이전 예산: {money(previous.amount)})"
    ctx.console.print(message)
    status = ctx.budget_service.status(month)
    if status is not None:
        ctx.console.print("\n".join(budget_lines(status)))
    return OK


@log_execution
@timed
def cmd_budget_list(args: argparse.Namespace, ctx: AppContext) -> int:
    statuses = ctx.budget_service.list_statuses()
    if not statuses:
        ctx.console.print("설정된 예산이 없습니다. 'budget set --month YYYY-MM --amount <금액>'으로 설정하세요.")
        return OK
    ctx.console.print(budgets_table(statuses))
    return OK


@log_execution
@timed
def cmd_budget_remove(args: argparse.Namespace, ctx: AppContext) -> int:
    budget = ctx.budget_service.remove(args.month)
    ctx.console.print(f"{budget.month} 예산({money(budget.amount)})을 삭제했습니다.")
    return OK


@log_execution
@timed
def cmd_category_list(args: argparse.Namespace, ctx: AppContext) -> int:
    usages = ctx.category_service.list_usage()
    ctx.console.print(categories_table(usages))
    ctx.console.print()
    ctx.console.print(f"카테고리 {len(usages)}개")
    return OK


@log_execution
@timed
def cmd_category_add(args: argparse.Namespace, ctx: AppContext) -> int:
    name: str = args.name or ctx.prompter().ask("추가할 카테고리 이름", normalize_category)
    category = ctx.category_service.add(name)
    ctx.console.print(f"카테고리 '{category.name}'을(를) 추가했습니다.")
    return OK


@log_execution
@timed
def cmd_category_remove(args: argparse.Namespace, ctx: AppContext) -> int:
    name: str = args.name or ctx.prompter().ask("삭제할 카테고리 이름", normalize_category)
    removal = ctx.category_service.remove(name, args.replace_with)
    if removal.replaced_with is None:
        ctx.console.print(f"카테고리 '{removal.name}'을(를) 삭제했습니다.")
    else:
        ctx.console.print(
            f"거래 {removal.moved_transactions}건, 반복 규칙 {removal.moved_rules}건을 "
            f"'{removal.replaced_with}'(으)로 옮기고 카테고리 '{removal.name}'을(를) 삭제했습니다."
        )
    return OK


@log_execution
@timed
def cmd_update(args: argparse.Namespace, ctx: AppContext) -> int:
    changes = TransactionChanges(
        date=args.date,
        type=args.type,
        category=args.category,
        amount=args.amount,
        memo=args.memo,
        tags=args.tags,
    )
    result = ctx.ledger.update(args.tx_id, changes)
    if not result.changed:
        ctx.console.print(f"id {args.tx_id}: 입력한 값이 기존 값과 같아서 바뀐 내용이 없습니다.")
        return OK
    ctx.console.print(f"거래를 수정했습니다. (id: {result.after.id})")
    ctx.console.print(format_changes(result.before, result.after))
    _print_budget_alert(ctx, result.after)
    return OK


@log_execution
@timed
def cmd_delete(args: argparse.Namespace, ctx: AppContext) -> int:
    tx = ctx.ledger.get(args.tx_id)
    ctx.console.print("삭제할 거래")
    ctx.console.print(format_transaction(tx))
    if not args.yes and not ctx.prompter().confirm("이 거래를 삭제할까요?", default=False):
        ctx.console.print("삭제를 취소했습니다. 아무것도 바뀌지 않았습니다.")
        return OK
    ctx.ledger.delete(args.tx_id)
    ctx.console.print(f"거래를 삭제했습니다. (id: {tx.id})")
    return OK


@log_execution
@timed
def cmd_import(args: argparse.Namespace, ctx: AppContext) -> int:
    report = ctx.csv.import_csv(
        args.source,
        skip_invalid=args.skip_invalid,
        create_categories=args.create_categories,
        dry_run=args.dry_run,
    )
    out = ctx.console
    if report.dry_run:
        out.print(f"[검증만 실행] {report.source}: 전체 {report.total_rows}행 중 {report.imported}행을 가져올 수 있습니다.")
    else:
        if report.first_id is None:
            id_range = ""
        elif report.first_id == report.last_id:
            id_range = f" (id {report.first_id})"
        else:
            id_range = f" (id {report.first_id}~{report.last_id})"
        out.print(f"가져오기 완료: 전체 {report.total_rows}행 중 {report.imported}건을 추가했습니다{id_range}.")
    if report.errors:
        out.print(f"오류가 있어 건너뛴 행 {report.skipped}개:")
        for line in format_row_errors(report.errors, report.error_count):
            out.print(line)
    if report.created_categories:
        verb = "등록할" if report.dry_run else "새로 등록한"
        out.print(f"{verb} 카테고리: {', '.join(report.created_categories)}")
    if report.ignored_columns:
        out.print(f"알 수 없어 무시한 열: {', '.join(report.ignored_columns)}")
    if report.dry_run:
        out.print("실제로는 아무것도 저장하지 않았습니다.")
        if report.errors and not args.skip_invalid:
            return int(ExitCode.INVALID_INPUT)
    elif report.imported:
        out.print("'list' 명령으로 가져온 거래를 확인하세요.")
    return OK


@log_execution
@timed
def cmd_export(args: argparse.Namespace, ctx: AppContext) -> int:
    criteria = _criteria_from_args(args, require_period=True)
    report = ctx.csv.export_csv(args.out, criteria, overwrite=args.force, protected=ctx.protected_files())
    ctx.console.print(f"내보내기 완료: {report.count}건 → {report.path}")
    ctx.console.print(f"조건: {criteria.describe()}")
    if report.count == 0:
        ctx.console.print("조건에 맞는 거래가 없어 헤더만 있는 CSV를 만들었습니다.")
    return OK


@log_execution
@timed
def cmd_recurring_add(args: argparse.Namespace, ctx: AppContext) -> int:
    prompter = ctx.prompter()
    draft, new_category = prompt_recurring_rule(prompter, ctx.category_service)
    if new_category:
        ctx.category_service.add(draft.category)
        ctx.console.print(f"카테고리 '{draft.category}'을(를) 등록했습니다.")
    rule = ctx.recurring.add(draft)
    ctx.console.print(
        f"반복 규칙을 저장했습니다. (id: {rule.id}, 매월 {rule.day}일 {rule.type.label} {money(rule.amount)} [{rule.category}])"
    )
    return OK


@log_execution
@timed
def cmd_recurring_list(args: argparse.Namespace, ctx: AppContext) -> int:
    rules = ctx.recurring.list_rules()
    if not rules:
        ctx.console.print("등록된 반복 규칙이 없습니다. 'recurring add'로 추가하세요.")
        return OK
    ctx.console.print(rules_table(rules))
    return OK


@log_execution
@timed
def cmd_recurring_remove(args: argparse.Namespace, ctx: AppContext) -> int:
    rule = ctx.recurring.remove(args.rule_id)
    ctx.console.print(f"반복 규칙을 삭제했습니다. (id: {rule.id}) 이미 만들어진 거래는 그대로 남습니다.")
    return OK


@log_execution
@timed
def cmd_recurring_apply(args: argparse.Namespace, ctx: AppContext) -> int:
    month: YearMonth = args.month or _current_month(ctx)
    report = ctx.recurring.apply(month)
    if not report.created and not report.skipped:
        ctx.console.print("등록된 반복 규칙이 없습니다. 'recurring add'로 추가하세요.")
        return OK
    ctx.console.print(f"{month} 반복 거래: {len(report.created)}건 생성, {len(report.skipped)}건 건너뜀")
    if report.created:
        ctx.console.print(transactions_table(report.created))
    for rule, reason in report.skipped:
        ctx.console.print(f"  - 규칙 {rule.id} ({rule.memo or rule.category}): {reason}")
    return OK


@log_execution
@timed
def cmd_backup_create(args: argparse.Namespace, ctx: AppContext) -> int:
    info = ctx.backups.create()
    ctx.console.print(f"백업을 만들었습니다: {info.path} ({info.size:,} bytes, 파일 {len(info.files)}개)")
    return OK


@log_execution
@timed
def cmd_backup_list(args: argparse.Namespace, ctx: AppContext) -> int:
    infos = ctx.backups.list_backups()
    if not infos:
        ctx.console.print("백업이 없습니다. 'backup'으로 만들 수 있습니다.")
        return OK
    ctx.console.print(backups_table([(info.path.name, info.size, len(info.files)) for info in infos]))
    ctx.console.print()
    ctx.console.print(f"백업 {len(infos)}개 (최신순), 폴더: {ctx.backups.backup_dir}")
    return OK


@log_execution
@timed
def cmd_backup_restore(args: argparse.Namespace, ctx: AppContext) -> int:
    question = f"'{args.backup_file}'로 복원할까요? 현재 데이터는 먼저 자동 백업됩니다."
    if not args.yes and not ctx.prompter().confirm(question, default=False):
        ctx.console.print("복원을 취소했습니다. 아무것도 바뀌지 않았습니다.")
        return OK
    report = ctx.backups.restore(args.backup_file)
    ctx.console.print(f"복원 완료: {report.restored_from.name} → 파일 {len(report.files)}개 ({', '.join(report.files)})")
    ctx.console.print(f"복원 직전 상태는 {report.safety_backup.path.name}에 백업했습니다.")
    return OK

