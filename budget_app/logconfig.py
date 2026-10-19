"""로깅 설정.

로거 구조 (모두 ``budget_app`` 아래에 있음)::

    budget_app            ← 핸들러가 붙는 최상위 로거
    ├── budget_app.exec    : @log_execution 실행 로그
    ├── budget_app.perf    : @timed 실행 시간
    ├── budget_app.error   : 예기치 못한 오류의 상세 스택트레이스
    └── budget_app.storage : 손상된 줄 경고 등

- 파일(``<data-dir>/app.log``): INFO 이상 전부 기록 (실행 이력, 실행 시간, 오류 상세)
- 화면(stderr): 평소에는 WARNING 이상만. ``--verbose``면 INFO까지, ``--timing``이면 실행 시간도
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, TextIO

APP_LOGGER = "budget_app"
PERF_LOGGER = "budget_app.perf"
ERROR_LOGGER = "budget_app.error"
LOG_FILE_NAME = "app.log"

_OWNED_ATTR = "_budget_app_owned"


class _ConsoleFormatter(logging.Formatter):
    LABELS: Dict[int, str] = {
        logging.DEBUG: "디버그",
        logging.INFO: "로그",
        logging.WARNING: "경고",
        logging.ERROR: "오류",
        logging.CRITICAL: "치명적 오류",
    }

    def format(self, record: logging.LogRecord) -> str:
        label = self.LABELS.get(record.levelno, record.levelname)
        return f"{label}: {record.getMessage()}"


class _FileOnly(logging.Filter):
    """상세 오류 기록(스택트레이스)은 app.log에만 남긴다. 화면에는 handle_errors가 원인과 힌트를 따로 보여 준다."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(ERROR_LOGGER)


class _LazyFileHandler(logging.FileHandler):
    """처음 기록할 때 파일을 연다. 열 수 없으면 조용히 파일 로그만 끈다.

    로그 파일 문제(권한 없음 등) 때문에 프로그램 본래 기능이 멈추면 안 된다.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(str(path), encoding="utf-8", delay=True)
        self._disabled = False

    def emit(self, record: logging.LogRecord) -> None:
        if self._disabled:
            return
        try:
            if self.stream is None:  # 첫 기록: 데이터 폴더가 아직 없으면 만든다.
                Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
            super().emit(record)
        except OSError:
            self._disabled = True

    def handleError(self, record: logging.LogRecord) -> None:
        # 기본 동작은 '--- Logging error ---'와 스택트레이스를 stderr에 찍는다. 대신 파일 로그만 끈다.
        self._disabled = True


def _own(handler: logging.Handler) -> logging.Handler:
    setattr(handler, _OWNED_ATTR, True)
    return handler


def shutdown_logging() -> None:
    """이 모듈이 붙인 핸들러를 떼고 닫는다 (테스트에서 반복 실행할 때 사용)."""
    for name in (APP_LOGGER, PERF_LOGGER):
        target = logging.getLogger(name)
        owned: List[logging.Handler] = [h for h in target.handlers if getattr(h, _OWNED_ATTR, False)]
        for handler in owned:
            target.removeHandler(handler)
            handler.close()


def configure_logging(
    log_file: Optional[Path],
    stream: TextIO,
    *,
    verbose: bool = False,
    timing: bool = False,
) -> None:
    shutdown_logging()

    app_logger = logging.getLogger(APP_LOGGER)
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False  # 파이썬 루트 로거로 올려 보내 중복 출력하지 않는다.

    console = logging.StreamHandler(stream)
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    console.setFormatter(_ConsoleFormatter())
    console.addFilter(_FileOnly())
    app_logger.addHandler(_own(console))

    if timing and not verbose:  # verbose면 위의 console 핸들러가 이미 실행 시간을 출력한다.
        perf_console = logging.StreamHandler(stream)
        perf_console.setLevel(logging.INFO)
        perf_console.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger(PERF_LOGGER).addHandler(_own(perf_console))

    if log_file is not None:
        file_handler = _LazyFileHandler(log_file)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        app_logger.addHandler(_own(file_handler))
