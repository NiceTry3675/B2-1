from __future__ import annotations

import datetime as dt
import json
import os
from typing import Iterator, List
from unittest import mock

from budget_app.models import Budget, Transaction, TransactionType, YearMonth
from budget_app.storage import AtomicWriter, BudgetStore, JsonlFile, SequenceStore, TransactionRepository

from tests.helpers import TempDirTestCase


def make_tx(tx_id: int, date: str = "2026-10-01", amount: int = 1000, category: str = "food") -> Transaction:
    return Transaction(
        id=tx_id,
        type=TransactionType.EXPENSE,
        date=dt.date.fromisoformat(date),
        amount=amount,
        category=category,
    )


class AtomicWriterTest(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.data_dir.mkdir()
        self.target = self.data_dir / "file.jsonl"
        self.target.write_bytes(b"original\n")

    def leftovers(self) -> List[str]:
        return [p.name for p in self.data_dir.iterdir() if p.name.endswith(".tmp")]

    def test_commit_replaces_file(self) -> None:
        with AtomicWriter(self.target) as writer:
            writer.write_line(b"new")
        self.assertEqual(self.target.read_bytes(), b"new\n")
        self.assertEqual(self.leftovers(), [])

    def test_exception_keeps_original_and_removes_temp(self) -> None:
        with self.assertRaises(RuntimeError):
            with AtomicWriter(self.target) as writer:
                writer.write_line(b"half written")
                raise RuntimeError("boom")
        self.assertEqual(self.target.read_bytes(), b"original\n")
        self.assertEqual(self.leftovers(), [])

    def test_keyboard_interrupt_keeps_original(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            with AtomicWriter(self.target) as writer:
                writer.write_line(b"half written")
                raise KeyboardInterrupt
        self.assertEqual(self.target.read_bytes(), b"original\n")
        self.assertEqual(self.leftovers(), [])

    def test_discard(self) -> None:
        with AtomicWriter(self.target) as writer:
            writer.write_line(b"ignored")
            writer.discard()
        self.assertEqual(self.target.read_bytes(), b"original\n")
        self.assertEqual(self.leftovers(), [])

    def test_failed_replace_keeps_original(self) -> None:
        with mock.patch("budget_app.storage.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                with AtomicWriter(self.target) as writer:
                    writer.write_line(b"new")
        self.assertEqual(self.target.read_bytes(), b"original\n")
        self.assertEqual(self.leftovers(), [])

    def test_preserves_permissions(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX 권한 전용")
        os.chmod(self.target, 0o640)
        with AtomicWriter(self.target) as writer:
            writer.write_line(b"new")
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o640)


class JsonlFileTest(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.data_dir.mkdir()
        self.repo = TransactionRepository(self.data_dir)

    def test_missing_file_yields_nothing(self) -> None:
        self.assertEqual(list(self.repo.iter_all()), [])

    def test_iter_is_lazy_generator(self) -> None:
        for i in range(1, 4):
            self.repo.add(make_tx(i))
        items: Iterator[Transaction] = self.repo.iter_all()
        self.assertEqual(next(items).id, 1)  # 첫 줄만 읽고 멈춘 상태

    def test_corrupt_lines_skipped_on_read_and_preserved_on_rewrite(self) -> None:
        self.repo.add(make_tx(1))
        with open(self.repo.path, "ab") as handle:
            handle.write(b"{broken json\n")
            handle.write(b'{"id": 7, "type": "expense", "date": "2026-02-30", "amount": 1, "category": "food"}\n')
            handle.write(b"\xff\xfe invalid utf-8\n")
            handle.write(b'["not", "an", "object"]\n')
        self.repo.add(make_tx(2))

        with self.assertLogs("budget_app.storage", level="WARNING") as logs:
            ids = [tx.id for tx in self.repo.iter_all()]
        self.assertEqual(ids, [1, 2])
        self.assertEqual(len(logs.records), 4)

        self.assertTrue(self.repo.delete(1))  # 같은 줄 경고는 한 번만 나오므로 여기서는 조용하다
        raw = self.repo.path.read_bytes()
        self.assertIn(b"{broken json\n", raw)
        self.assertIn(b"\xff\xfe invalid utf-8\n", raw)
        self.assertIn(b'"date": "2026-02-30"', raw)
        self.assertNotIn(b'"id": 1,', raw)

    def test_append_repairs_missing_trailing_newline(self) -> None:
        self.repo.path.write_bytes(json.dumps(make_tx(1).to_record()).encode())  # 줄바꿈 없음
        self.repo.add(make_tx(2))
        self.assertEqual([tx.id for tx in self.repo.iter_all()], [1, 2])

    def test_partial_last_line_does_not_swallow_next_append(self) -> None:
        self.repo.add(make_tx(1))
        with open(self.repo.path, "ab") as handle:
            handle.write(b'{"id": 2, "type": "exp')  # 쓰다가 전원이 나간 줄
        with self.assertLogs("budget_app.storage", level="WARNING"):
            self.repo.add(make_tx(3))
            self.assertEqual([tx.id for tx in self.repo.iter_all()], [1, 3])

    def test_bom_and_blank_lines_are_tolerated(self) -> None:
        line = json.dumps(make_tx(1).to_record()).encode()
        self.repo.path.write_bytes(b"\xef\xbb\xbf" + line + b"\n\n   \n")
        self.assertEqual([tx.id for tx in self.repo.iter_all()], [1])

    def test_unchanged_lines_keep_unknown_fields(self) -> None:
        record = make_tx(1).to_record()
        record["future_field"] = "keep me"
        self.repo.path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        self.repo.add(make_tx(2))
        self.repo.delete(2)
        self.assertIn("future_field", self.repo.path.read_text(encoding="utf-8"))

    def test_rewrite_without_change_does_not_touch_file(self) -> None:
        self.repo.add(make_tx(1))
        before = self.repo.path.stat().st_mtime_ns
        self.assertFalse(self.repo.delete(999))
        self.assertEqual(self.repo.path.stat().st_mtime_ns, before)

    def test_staged_append_is_all_or_nothing(self) -> None:
        self.repo.add(make_tx(1))
        with self.assertRaises(ValueError):
            with self.repo.staged_append() as staged:
                staged.add(make_tx(2))
                raise ValueError("중간 실패")
        self.assertEqual([tx.id for tx in self.repo.iter_all()], [1])

        with self.repo.staged_append() as staged:
            staged.add(make_tx(2))
            staged.add(make_tx(3))
        self.assertEqual([tx.id for tx in self.repo.iter_all()], [1, 2, 3])

    def test_ensure_rejects_directory(self) -> None:
        from budget_app.errors import StorageError

        (self.data_dir / "x.jsonl").mkdir()
        with self.assertRaises(StorageError):
            JsonlFile(self.data_dir / "x.jsonl").ensure()


class StoresTest(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.data_dir.mkdir()

    def test_budget_upsert_keeps_single_row_per_month(self) -> None:
        store = BudgetStore(self.data_dir)
        oct_ = YearMonth(2026, 10)
        self.assertIsNone(store.upsert(Budget(oct_, 100)))
        previous = store.upsert(Budget(oct_, 200))
        self.assertEqual(previous, Budget(oct_, 100))
        self.assertEqual(list(store.iter_all()), [Budget(oct_, 200)])

    def test_sequence_never_goes_backwards(self) -> None:
        seq = SequenceStore(self.data_dir)
        self.assertEqual(seq.next_value("t"), 1)
        seq.commit("t", 5)
        seq.commit("t", 3)
        self.assertEqual(seq.current("t"), 5)
        self.assertEqual(seq.next_value("t", floor=10), 11)  # 데이터가 meta보다 앞서 있으면 데이터 기준


class ModelRecordTest(TempDirTestCase):
    def test_round_trip(self) -> None:
        tx = Transaction(
            id=3,
            type=TransactionType.INCOME,
            date=dt.date(2026, 10, 25),
            amount=3_200_000,
            category="salary",
            memo="월급",
            tags=("월급",),
            recurring_id=1,
            created_at="2026-10-19T00:00:00",
        )
        self.assertEqual(Transaction.from_record(json.loads(json.dumps(tx.to_record()))), tx)

    def test_old_record_without_optional_fields(self) -> None:
        tx = Transaction.from_record({"id": 1, "type": "expense", "date": "2026-10-01", "amount": 5, "category": "food"})
        self.assertEqual((tx.memo, tx.tags, tx.recurring_id), ("", (), None))

    def test_rejects_bad_records(self) -> None:
        bad = [
            {"id": True, "type": "expense", "date": "2026-10-01", "amount": 5, "category": "food"},
            {"id": 1, "type": "expense", "date": "2026-10-01", "amount": -5, "category": "food"},
            {"id": 1, "type": "gift", "date": "2026-10-01", "amount": 5, "category": "food"},
            {"id": 1, "type": "expense", "date": "2026-10-01", "amount": "5", "category": "food"},
            {"id": 1, "type": "expense", "amount": 5, "category": "food"},
            {"id": 1, "type": "expense", "date": "2026-10-01", "amount": 5, "category": "food", "tags": "a,b"},
        ]
        for record in bad:
            with self.subTest(record=record), self.assertRaises((ValueError, KeyError, TypeError)):
                Transaction.from_record(record)

