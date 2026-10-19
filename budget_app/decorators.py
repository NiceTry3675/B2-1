"""공통 관심사(cross-cutting concerns)를 비즈니스 코드에서 떼어 내는 데코레이터 모음.

- ``@timed``         : 실행 시간을 재서 ``budget_app.perf`` 로거에 남긴다.
- ``@log_execution`` : 시작/완료/실패를 ``budget_app.exec`` 로거에 남긴다.
- ``@handle_errors`` : 예외를 '원인 + 해결 힌트' 메시지와 종료 코드로 바꾼다 (CLI 경계 전용).

명령 핸들러는 "무엇을 할지"만 담고, 시간 재기·로그·예외 변환은 데코레이터가 맡는다.
그래서 핸들러마다 try/except나 time.perf_counter()를 반복해서 쓸 필요가 없다.
"""

from __future__ import annotations

import argparse
import functools
import logging
import time
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, TypeVar, cast

from .errors import BudgetAppError, ExitCode

F = TypeVar("F", bound=Callable[..., Any])

perf_logger = logging.getLogger("budget_app.perf")
exec_logger = logging.getLogger("budget_app.exec")
error_logger = logging.getLogger("budget_app.error")


def timed(func: F) -> F:
    """함수 실행 시간을 밀리초 단위로 기록한다. 예외가 나도 ``finally``에서 기록한다.

    주의: 제너레이터 함수에 붙이면 '제너레이터 객체를 만드는 시간'만 재게 된다.
    실제 반복은 호출한 쪽이 나중에 하기 때문이다.
    """

    @functools.wraps(func)  # 원래 함수의 이름·docstring을 래퍼에 복사한다.
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            perf_logger.info("%s 실행 시간: %.2fms", func.__qualname__, elapsed_ms)

    return cast(F, wrapper)


def log_execution(func: F) -> F:
    """함수의 시작, 결과, 예외를 실행 로그로 남긴다. 예외는 삼키지 않고 다시 던진다."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        name = func.__qualname__
        exec_logger.info("시작 %s %s", name, _describe_call(args, kwargs))
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            exec_logger.info("실패 %s: %s: %s", name, type(exc).__name__, exc)
            raise
        exec_logger.info("완료 %s -> %r", name, result)
        return result

    return cast(F, wrapper)


class ErrorReporter(Protocol):
    """``handle_errors``가 요구하는 최소 기능. 이 메서드만 있으면 어떤 객체든 된다 (구조적 타이핑)."""

    def report_error(
        self, message: str, hint: Optional[str] = None, exc: Optional[BaseException] = None
    ) -> None: ...


R = TypeVar("R", bound=ErrorReporter)


def handle_errors(
    func: Callable[[argparse.Namespace, R], int],
) -> Callable[[argparse.Namespace, R], int]:
    """명령 실행 중 생긴 예외를 사용자 메시지와 종료 코드로 바꾼다.

    - BudgetAppError   : 예외에 담긴 원인/힌트/종료 코드를 그대로 쓴다.
    - KeyboardInterrupt: 130. 원자적 쓰기 덕분에 기존 파일은 안전하다고 알려 준다.
    - OSError          : 5. 권한·디스크 공간·경로를 확인하라고 안내한다.
    - 그 밖의 예외      : 1. 상세 내용은 로그 파일에 남기고 ``--debug``를 안내한다.
    """

    @functools.wraps(func)
    def wrapper(args: argparse.Namespace, ctx: R) -> int:
        try:
            return func(args, ctx)
        except BudgetAppError as exc:
            ctx.report_error(exc.message, exc.hint, exc)
            return int(exc.exit_code)
        except KeyboardInterrupt as exc:
            ctx.report_error(
                "사용자가 작업을 중단했습니다.",
                "저장 파일은 파일마다 '임시 파일 → 교체' 방식으로 써서 반쯤 쓴 파일은 남지 않습니다. "
                "여러 파일을 바꾸는 명령은 먼저 끝난 파일까지만 반영될 수 있지만, 순서를 정해 두어 데이터가 서로 어긋나지 않습니다.",
                exc,
            )
            return int(ExitCode.INTERRUPTED)
        except BrokenPipeError:
            # 'list | head'처럼 출력을 받던 쪽이 먼저 끝난 경우다. 오류가 아니므로 main()이 조용히 끝낸다.
            raise
        except OSError as exc:
            error_logger.exception("파일 처리 오류")
            ctx.report_error(
                f"파일을 처리하다가 문제가 생겼습니다: {_describe_os_error(exc)}",
                "경로가 맞는지, 쓰기 권한과 디스크 여유 공간이 있는지 확인하세요.",
                exc,
            )
            return int(ExitCode.STORAGE)
        except Exception as exc:  # 마지막 안전망: 사용자에게 스택트레이스를 보이지 않는다.
            error_logger.exception("예기치 못한 오류")
            ctx.report_error(
                f"예기치 못한 오류가 발생했습니다: {type(exc).__name__}: {exc}",
                "--debug 옵션을 붙여 다시 실행하면 상세 정보를 볼 수 있습니다. 데이터 폴더의 app.log에도 기록됩니다.",
                exc,
            )
            return int(ExitCode.ERROR)

    return wrapper


def _describe_os_error(exc: OSError) -> str:
    reason = exc.strerror or str(exc)
    return f"{reason} ({exc.filename})" if exc.filename else reason


def _describe_value(value: Any) -> str:
    if isinstance(value, argparse.Namespace):
        shown = {
            key: item
            for key, item in vars(value).items()
            if not callable(item) and key != "needs_storage"
        }
        return repr(shown)
    if value is None or isinstance(value, (str, int, float, bool)):
        return repr(value)
    return f"<{type(value).__name__}>"


def _describe_call(args: Sequence[Any], kwargs: Mapping[str, Any]) -> str:
    parts = [_describe_value(arg) for arg in args]
    parts.extend(f"{key}={_describe_value(item)}" for key, item in kwargs.items())
    return ", ".join(parts)
