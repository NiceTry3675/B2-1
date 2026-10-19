from __future__ import annotations

import csv
import datetime as dt
import io

from budget_app.csv_io import CSV_COLUMNS, ImportRejectedError
from budget_app.errors import ConflictError, NotFoundError, ValidationError
from budget_app.services import SearchCriteria

from tests.helpers import AppTestCase, draft

HEADER = "date,type,category,amount,memo,tags\n"


class ImportTest(AppTestCase):
    def write(self, text: str, name: str = "in.csv", encoding: str = "utf-8") -> object:
        path = self.root / name
        path.write_bytes(text.encode(encoding))
        return path

    def test_valid_import(self) -> None:
        path = self.write(HEADER + '2026-10-01,expense,food,"12,000",점심,"점심,회사"\n2026-10-02,income,salary,100,,\n')
        report = self.ctx.csv.import_csv(path)  # type: ignore[arg-type]
        self.assertEqual((report.total_rows, report.imported, report.skipped), (2, 2, 0))
        self.assertEqual((report.first_id, report.last_id), (1, 2))
        txs = list(self.ctx.transactions.iter_all())
        self.assertEqual(txs[0].tags, ("점심", "회사"))
        self.assertEqual(txs[0].amount, 12000)
        self.assertEqual(self.ctx.ledger.add(draft()).id, 3)  # id 시퀀스도 이어진다

    def test_any_bad_row_rejects_everything(self) -> None:
        self.ctx.ledger.add(draft(memo="기존"))
        path = self.write(
            HEADER
            + "2026-10-01,expense,food,1000,ok,\n"
            + "2026-13-01,expense,food,1000,bad date,\n"
            + "2026-10-01,expense,food,-5,negative,\n"
            + "2026-10-01,transfer,food,5,bad type,\n"
            + "2026-10-01,expense,nope,5,unknown category,\n"
            + "2026-10-01,expense,food,5,too,many,values\n"
            + ",expense,food,5,missing date,\n"
        )
        with self.assertRaises(ImportRejectedError) as caught:
            self.ctx.csv.import_csv(path)  # type: ignore[arg-type]
        report = caught.exception.report
        self.assertEqual((report.total_rows, report.skipped), (7, 6))
        self.assertEqual([e.line for e in report.errors], [3, 4, 5, 6, 7, 8])
        self.assertEqual([tx.memo for tx in self.ctx.transactions.iter_all()], ["기존"])

    def test_skip_invalid(self) -> None:
        path = self.write(HEADER + "2026-10-01,expense,food,1000,ok,\n2026-10-01,expense,food,0,zero,\n")
        report = self.ctx.csv.import_csv(path, skip_invalid=True)  # type: ignore[arg-type]
        self.assertEqual((report.imported, report.skipped), (1, 1))
        self.assertEqual(len(list(self.ctx.transactions.iter_all())), 1)

    def test_create_categories(self) -> None:
        path = self.write(HEADER + "2026-10-01,expense,Coffee,4500,,\n")
        report = self.ctx.csv.import_csv(path, create_categories=True)  # type: ignore[arg-type]
        self.assertEqual(report.created_categories, ["coffee"])
        self.assertTrue(self.ctx.categories.exists("coffee"))

    def test_dry_run_writes_nothing(self) -> None:
        path = self.write(HEADER + "2026-10-01,expense,Coffee,4500,,\n2026-10-01,expense,food,1,,\n")
        before = self.ctx.transactions.path.read_bytes()
        report = self.ctx.csv.import_csv(path, dry_run=True, create_categories=True)  # type: ignore[arg-type]
        self.assertEqual(report.imported, 2)
        self.assertEqual(self.ctx.transactions.path.read_bytes(), before)
        self.assertFalse(self.ctx.categories.exists("coffee"))

    def test_header_problems(self) -> None:
        for text, fragment in [
            ("", "비어"),
            ("date,type,amount\n2026-10-01,expense,5\n", "category"),
            ("date,date,type,category,amount\n", "여러 번"),
        ]:
            with self.subTest(text=text), self.assertRaises(ValidationError) as caught:
                self.ctx.csv.import_csv(self.write(text))  # type: ignore[arg-type]
            self.assertIn(fragment, caught.exception.message)

    def test_header_is_normalized_and_bom_and_extra_columns_ok(self) -> None:
        path = self.write("\ufeff Date ,TYPE,Category,Amount,id\n2026-10-01,expense,food,5,77\n")
        report = self.ctx.csv.import_csv(path)  # type: ignore[arg-type]
        self.assertEqual((report.imported, report.ignored_columns), (1, ["id"]))

    def test_non_utf8_file(self) -> None:
        path = self.write(HEADER + "2026-10-01,expense,food,5,점심,\n", encoding="cp949")
        with self.assertRaises(ValidationError) as caught:
            self.ctx.csv.import_csv(path)  # type: ignore[arg-type]
        self.assertIn("UTF-8", caught.exception.message)

    def test_missing_file(self) -> None:
        with self.assertRaises(NotFoundError):
            self.ctx.csv.import_csv(self.root / "nope.csv")


class ExportTest(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ctx.ledger.add(draft(date="2026-10-15", memo="b", tags=("x", "y")))
        self.ctx.ledger.add(draft(date="2026-10-01", memo='a "quoted", comma'))
        self.ctx.ledger.add(draft(date="2026-11-01", memo="other month"))
        self.oct = SearchCriteria(date_from=dt.date(2026, 10, 1), date_to=dt.date(2026, 10, 31))

    def test_export_month_sorted_with_schema(self) -> None:
        out = self.root / "out.csv"
        report = self.ctx.csv.export_csv(out, self.oct)
        self.assertEqual(report.count, 2)
        rows = list(csv.reader(io.StringIO(out.read_text(encoding="utf-8"))))
        self.assertEqual(tuple(rows[0]), CSV_COLUMNS)
        self.assertEqual([r[4] for r in rows[1:]], ['a "quoted", comma', "b"])
        self.assertEqual(rows[2][5], "x,y")

    def test_round_trip(self) -> None:
        out = self.root / "out.csv"
        self.ctx.csv.export_csv(out, self.oct)
        before = [(t.date, t.memo, t.tags, t.amount) for t in self.ctx.ledger.search(self.oct).items]
        for tx in list(self.ctx.transactions.iter_all()):
            self.ctx.ledger.delete(tx.id)
        self.ctx.csv.import_csv(out)
        after = [(t.date, t.memo, t.tags, t.amount) for t in self.ctx.ledger.search(self.oct).items]
        self.assertEqual(before, after)

    def test_requires_period(self) -> None:
        with self.assertRaises(ValidationError):
            self.ctx.csv.export_csv(self.root / "x.csv", SearchCriteria(category="food"))

    def test_existing_file_needs_force(self) -> None:
        out = self.root / "out.csv"
        out.write_text("keep", encoding="utf-8")
        with self.assertRaises(ConflictError):
            self.ctx.csv.export_csv(out, self.oct)
        self.assertEqual(out.read_text(encoding="utf-8"), "keep")
        self.ctx.csv.export_csv(out, self.oct, overwrite=True)
        self.assertIn("date,type", out.read_text(encoding="utf-8"))

    def test_bad_destinations(self) -> None:
        with self.assertRaises(NotFoundError):
            self.ctx.csv.export_csv(self.root / "no" / "dir.csv", self.oct)
        with self.assertRaises(ValidationError):
            self.ctx.csv.export_csv(self.root, self.oct)
        with self.assertRaises(ValidationError):
            self.ctx.csv.export_csv(self.ctx.transactions.path, self.oct, overwrite=True, protected=self.ctx.data_files)
