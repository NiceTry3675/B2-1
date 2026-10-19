"""테스트 공용 도우미: 임시 데이터 폴더, 가짜 input(), CLI 실행기."""

from __future__ import annotations

import datetime as dt
import io
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from budget_app.cli import main
from budget_app.context import AppContext, Console
from budget_app.logconfig import shutdown_logging
from budget_app.models import TransactionType
from budget_app.services import TransactionDraft


class FakeInput:
    """미리 준비한 답을 차례로 돌려주는 input() 대용품. 답이 떨어지면 EOFError."""

    def __init__(self, answers: Iterable[str] = ()) -> None:
        self.answers: List[str] = list(answers)
        self.prompts: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


@dataclass
class CliResult:
    code: int
    out: str
    err: str
    prompts: List[str]


def draft(
    date: str = "2026-10-02",
    type: TransactionType = TransactionType.EXPENSE,
    amount: int = 12000,
    category: str = "food",
    memo: str = "",
    tags: tuple = (),
) -> TransactionDraft:
    return TransactionDraft(
        type=type, date=dt.date.fromisoformat(date), amount=amount, category=category, memo=memo, tags=tags
    )


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data_dir = self.root / "data"

    def tearDown(self) -> None:
        shutdown_logging()
        self._tmp.cleanup()


class AppTestCase(TempDirTestCase):
    """초기화된 AppContext(기본 카테고리 포함)를 제공한다."""

    def setUp(self) -> None:
        super().setUp()
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.ctx = AppContext(self.data_dir, Console(self.out, self.err, FakeInput()))
        self.ctx.initialize()


TODAY = dt.date(2026, 10, 19)  # CLI 테스트의 '오늘'. 실제 시계에 기대지 않아야 자정 무렵에도 결과가 같다.


class CliTestCase(TempDirTestCase):
    def run_cli(self, *argv: str, inputs: Iterable[str] = (), data_dir: Optional[Path] = None) -> CliResult:
        out, err = io.StringIO(), io.StringIO()
        fake = FakeInput(inputs)
        target = data_dir if data_dir is not None else self.data_dir
        code = main(
            ["--data-dir", str(target), *argv], stdout=out, stderr=err, input_func=fake, today=lambda: TODAY
        )
        return CliResult(code, out.getvalue(), err.getvalue(), fake.prompts)

    def write_csv(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path
