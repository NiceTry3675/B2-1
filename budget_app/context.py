"""객체 조립(Composition Root): 저장소 → 서비스 순서로 객체를 만들어 하나로 묶는다.

모든 클래스는 필요한 것을 생성자로 받는다(의존성 주입). 객체를 만드는 곳이 여기
한 곳뿐이라, 데이터 폴더·입출력 스트림·시계(now)를 바꿔 끼우기 쉽다. 테스트도 쉬워진다.
"""

from __future__ import annotations

import datetime as dt
import os
import traceback
from pathlib import Path
from typing import Callable, List, Mapping, Optional, TextIO, Tuple

from .backup import BackupService
from .csv_io import CsvService
from .errors import StorageError, ValidationError
from .interactive import InputFunc, Prompter
from .logconfig import LOG_FILE_NAME
from .recurring import RULE_SEQUENCE, RecurringService
from .services import TRANSACTION_SEQUENCE, BudgetService, CategoryService, LedgerService, ReportService
from .storage import (
    BudgetStore,
    CategoryStore,
    FileBackedStore,
    RecurringRuleStore,
    SequenceStore,
    TransactionRepository,
)

DEFAULT_DATA_DIR = Path("data")
DATA_DIR_ENV = "BUDGET_APP_DATA_DIR"
DEFAULT_CATEGORIES: Tuple[str, ...] = (
    "food",
    "transport",
    "rent",
    "utilities",
    "shopping",
    "health",
    "salary",
    "etc",
)


def resolve_data_dir(cli_value: Optional[str], environ: Mapping[str, str] = os.environ) -> Path:
    """데이터 폴더 우선순위: ``--data-dir`` > 환경 변수 ``BUDGET_APP_DATA_DIR`` > ``./data``."""
    if cli_value is not None:
        source, value = "--data-dir", cli_value
    elif environ.get(DATA_DIR_ENV):
        source, value = DATA_DIR_ENV, environ[DATA_DIR_ENV]
    else:
        return DEFAULT_DATA_DIR
    if not value.strip():
        raise ValidationError(f"{source} 값이 비어 있습니다.", hint="데이터 폴더 경로를 지정하세요 (예: --data-dir ./data).")
    try:
        return Path(value).expanduser()
    except RuntimeError:  # '~없는사용자/...'처럼 홈 폴더를 찾을 수 없는 경우
        raise StorageError(
            f"데이터 폴더 경로의 '~'를 해석할 수 없습니다: {value}",
            hint="절대 경로나 '~/폴더' 형식으로 지정하세요.",
        ) from None


class Console:
    """화면 입출력 묶음. 정상 출력은 out(stdout), 안내·오류는 err(stderr)로 보낸다."""

    def __init__(self, out: TextIO, err: TextIO, input_func: InputFunc) -> None:
        self.out = out
        self.err = err
        self.input_func = input_func

    def print(self, text: str = "") -> None:
        print(text, file=self.out)

    def notice(self, text: str) -> None:
        print(f"안내: {text}", file=self.err)

    def error(self, message: str, hint: Optional[str] = None) -> None:
        print(f"오류: {message}", file=self.err)
        if hint:
            print(f"힌트: {hint}", file=self.err)


class AppContext:
    """한 번의 명령 실행에 필요한 모든 객체."""

    def __init__(
        self,
        data_dir: Path,
        console: Console,
        *,
        debug: bool = False,
        today: Callable[[], dt.date] = dt.date.today,
        now: Callable[[], dt.datetime] = dt.datetime.now,
    ) -> None:
        self.data_dir = data_dir
        self.console = console
        self.debug = debug
        self.today = today
        self._initialized = False

        # 저장소 계층: 파일 하나 = 저장소 하나
        self.transactions = TransactionRepository(data_dir)
        self.categories = CategoryStore(data_dir)
        self.budgets = BudgetStore(data_dir)
        self.recurring_rules = RecurringRuleStore(data_dir)
        self.sequences = SequenceStore(data_dir)

        # 서비스 계층: 저장소를 주입받는다
        self.category_service = CategoryService(self.categories, self.transactions, self.recurring_rules, now=now)
        self.ledger = LedgerService(self.transactions, self.categories, self.sequences, now=now)
        self.budget_service = BudgetService(self.budgets, self.transactions)
        self.reports = ReportService(self.transactions, self.budgets)
        self.csv = CsvService(self.transactions, self.categories, self.sequences, now=now)
        self.recurring = RecurringService(
            self.recurring_rules, self.transactions, self.categories, self.sequences, now=now
        )
        self.backups = BackupService(
            data_dir,
            [path.name for path in self.data_files],
            sequences=self.sequences,
            sequence_keys=[TRANSACTION_SEQUENCE, RULE_SEQUENCE],
            now=now,
        )

    @property
    def stores(self) -> List[FileBackedStore]:
        return [self.transactions, self.categories, self.budgets, self.recurring_rules, self.sequences]

    @property
    def data_files(self) -> List[Path]:
        return [store.path for store in self.stores]

    def protected_files(self) -> List[Path]:
        """export가 덮어쓰면 안 되는 파일: 데이터 파일, 실행 로그, 백업 파일."""
        backups = sorted(self.backups.backup_dir.glob("*")) if self.backups.backup_dir.is_dir() else []
        return [*self.data_files, self.data_dir / LOG_FILE_NAME, *backups]

    def prompter(self) -> Prompter:
        return Prompter(self.console.input_func, self.console.out)

    def initialize(self) -> None:
        """데이터 폴더와 저장 파일을 준비한다. 처음 실행이면 무엇을 만들었는지 알려 준다."""
        if self._initialized:
            return
        if self.data_dir.exists() and not self.data_dir.is_dir():
            raise StorageError(
                f"데이터 경로가 폴더가 아닙니다: {self.data_dir}",
                hint="--data-dir 옵션으로 폴더 경로를 지정하세요.",
            )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        created = [store.path.name for store in self.stores if store.ensure_file()]
        if created:
            self.console.notice(f"저장 파일을 새로 만들었습니다: {', '.join(created)} (위치: {self.data_dir.resolve()})")
        seeded = self.category_service.ensure_defaults(DEFAULT_CATEGORIES)
        if seeded:
            self.console.notice(f"카테고리가 비어 있어 기본 카테고리를 만들었습니다: {', '.join(seeded)}")
        self._initialized = True

    def report_error(self, message: str, hint: Optional[str] = None, exc: Optional[BaseException] = None) -> None:
        """``@handle_errors``가 부르는 오류 출력 함수 (ErrorReporter 프로토콜 구현)."""
        self.console.error(message, hint)
        if self.debug and exc is not None:
            print("\n[debug] 상세 정보:", file=self.console.err)
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=self.console.err)
