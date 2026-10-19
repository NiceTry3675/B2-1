"""스트리밍 처리 검증: 큰 파일에서도 list/search/summary가 파일 전체를 메모리에 올리지 않는다."""

from __future__ import annotations

import datetime as dt
import json
import tracemalloc
from unittest import mock

from budget_app.models import YearMonth
from budget_app.services import SearchCriteria
from budget_app.storage import JsonlFile

from tests.helpers import AppTestCase

N = 20_000


class StreamingTest(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        start = dt.date(2020, 1, 1)
        with open(self.ctx.transactions.path, "w", encoding="utf-8") as handle:
            for i in range(1, N + 1):
                record = {
                    "id": i,
                    "type": "expense",
                    "date": (start + dt.timedelta(days=i % 2000)).isoformat(),
                    "amount": 1000 + i,
                    "category": "food",
                    "memo": "메모 " * 10,
                    "tags": ["태그"],
                }
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.file_size = self.ctx.transactions.path.stat().st_size

    def peak_memory(self, func) -> int:  # type: ignore[no-untyped-def]
        tracemalloc.start()
        try:
            func()
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    def test_list_memory_is_bounded(self) -> None:
        result = None

        def run() -> None:
            nonlocal result
            result = self.ctx.ledger.recent(10)

        peak = self.peak_memory(run)
        assert result is not None
        self.assertEqual(result.total, N)
        self.assertEqual(len(result.items), 10)
        # 파일 크기(수 MB)보다 훨씬 적은 메모리만 쓴다
        self.assertLess(peak, self.file_size / 4, f"peak={peak:,} file={self.file_size:,}")

    def test_summary_memory_is_bounded(self) -> None:
        peak = self.peak_memory(lambda: self.ctx.reports.monthly_summary(YearMonth(2021, 1), top=5))
        self.assertLess(peak, self.file_size / 4)

    def test_get_stops_reading_early(self) -> None:
        original = JsonlFile.iter_raw
        consumed = 0

        def counting(self_file: JsonlFile):  # type: ignore[no-untyped-def]
            nonlocal consumed
            for raw in original(self_file):
                consumed += 1
                yield raw

        with mock.patch.object(JsonlFile, "iter_raw", counting):
            self.assertIsNotNone(self.ctx.transactions.get(5))
        self.assertEqual(consumed, 5)

    def test_search_streams_through_filter(self) -> None:
        crit = SearchCriteria(date_from=dt.date(2020, 1, 1), date_to=dt.date(2020, 1, 10))
        result = self.ctx.ledger.search(crit, limit=3)
        self.assertEqual(len(result.items), 3)
        self.assertTrue(all(tx.date <= dt.date(2020, 1, 10) for tx in result.items))
