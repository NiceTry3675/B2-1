"""애플리케이션 예외 계층과 종료 코드.

예상 가능한 오류는 모두 BudgetAppError를 상속한다. 각 예외가 가지는 값:

- message   : 무엇이 잘못되었는지 (원인)
- hint      : 사용자가 다음에 무엇을 하면 되는지 (해결 힌트)
- exit_code : 프로세스 종료 코드

CLI 경계의 ``@handle_errors`` 데코레이터가 이 값을 읽어, 스택트레이스 대신
"원인 + 해결 힌트"를 출력하고 알맞은 종료 코드를 돌려준다.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Optional


class ExitCode(IntEnum):
    """프로세스 종료 코드. 0은 정상이고 나머지는 모두 오류다."""

    OK = 0
    ERROR = 1  # 분류되지 않은 오류, 대화형 입력 중단
    INVALID_INPUT = 2  # 입력 형식/값 오류 (argparse 사용법 오류와 같은 값)
    NOT_FOUND = 3  # 없는 id, 없는 파일 등
    CONFLICT = 4  # 중복 등록, 사용 중인 카테고리 삭제 시도 등
    STORAGE = 5  # 파일 읽기/쓰기 실패, 저장 경로 문제
    INTERRUPTED = 130  # Ctrl+C (관례: 128 + SIGINT(2))


class BudgetAppError(Exception):
    """가계부 애플리케이션의 모든 '예상된' 오류의 부모 클래스."""

    exit_code: ExitCode = ExitCode.ERROR

    def __init__(self, message: str, hint: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class ValidationError(BudgetAppError):
    """사용자 입력이 형식이나 규칙에 맞지 않는다."""

    exit_code = ExitCode.INVALID_INPUT


class NotFoundError(BudgetAppError):
    """요청한 대상(거래 id, 카테고리, 파일 등)이 없다."""

    exit_code = ExitCode.NOT_FOUND


class ConflictError(BudgetAppError):
    """현재 데이터 상태와 충돌한다 (중복, 사용 중인 항목 삭제 등)."""

    exit_code = ExitCode.CONFLICT


class StorageError(BudgetAppError):
    """저장 파일이나 폴더를 다룰 수 없다."""

    exit_code = ExitCode.STORAGE


class InputAbortedError(BudgetAppError):
    """대화형 입력이 끝났거나(EOF) 재입력 횟수를 모두 썼다."""

    exit_code = ExitCode.ERROR
