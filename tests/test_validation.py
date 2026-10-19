from __future__ import annotations

import datetime as dt
import unittest

from budget_app.errors import ValidationError
from budget_app.models import TransactionType, YearMonth
from budget_app.validation import (
    normalize_category,
    parse_amount,
    parse_date,
    parse_memo,
    parse_month,
    parse_positive_int,
    parse_tags,
    parse_type,
)


class ParseDateTest(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(parse_date(" 2026-10-18 "), dt.date(2026, 10, 18))
        self.assertEqual(parse_date("2028-02-29"), dt.date(2028, 2, 29))  # 윤년

    def test_rejects_bad_shape_and_impossible_dates(self) -> None:
        for text in ["", "2026/10/18", "2026-1-5", "18-10-2026", "2026-10-18T00:00", "abcd-ef-gh"]:
            with self.subTest(text=text), self.assertRaises(ValidationError):
                parse_date(text)
        for text in ["2026-02-30", "2026-13-01", "2027-02-29", "0000-01-01"]:
            with self.subTest(text=text), self.assertRaises(ValidationError) as caught:
                parse_date(text)
            self.assertIn("존재하지 않는", caught.exception.message)

    def test_error_has_hint(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            parse_date("2026.10.18")
        self.assertIn("YYYY-MM-DD", caught.exception.hint or "")


class ParseMonthTest(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(parse_month("2026-10"), YearMonth(2026, 10))

    def test_invalid(self) -> None:
        for text in ["2026-13", "2026-00", "2026-1", "202610", "", "2026-10-01"]:
            with self.subTest(text=text), self.assertRaises(ValidationError):
                parse_month(text)


class ParseTypeTest(unittest.TestCase):
    def test_aliases(self) -> None:
        for text in ["income", "INCOME", "i", "수입", " in "]:
            self.assertIs(parse_type(text), TransactionType.INCOME)
        for text in ["expense", "Expense", "e", "지출"]:
            self.assertIs(parse_type(text), TransactionType.EXPENSE)

    def test_invalid(self) -> None:
        for text in ["", "transfer", "x", "+"]:
            with self.subTest(text=text), self.assertRaises(ValidationError):
                parse_type(text)


class ParseAmountTest(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(parse_amount("12000"), 12000)
        self.assertEqual(parse_amount("12,000"), 12000)
        self.assertEqual(parse_amount(" 1,234,567원 "), 1234567)
        self.assertEqual(parse_amount("007"), 7)

    def test_rejects_zero_negative_decimal_and_garbage(self) -> None:
        cases = {
            "0": "0보다",
            "-500": "음수",
            "12.5": "양의 정수",
            "1,2,3": "양의 정수",
            "1e3": "양의 정수",
            "": "입력하세요",
            "+5": "양의 정수",
            "١٢": "양의 정수",  # 아랍 숫자처럼 isdigit()은 통과하지만 ASCII 숫자가 아닌 값
        }
        for text, fragment in cases.items():
            with self.subTest(text=text), self.assertRaises(ValidationError) as caught:
                parse_amount(text)
            self.assertIn(fragment, caught.exception.message)

    def test_rejects_absurd_amount(self) -> None:
        with self.assertRaises(ValidationError):
            parse_amount("10000000000000")


class TextFieldTest(unittest.TestCase):
    def test_memo_collapses_whitespace(self) -> None:
        self.assertEqual(parse_memo("  점심\n\t회식  "), "점심 회식")
        self.assertEqual(parse_memo(""), "")
        with self.assertRaises(ValidationError):
            parse_memo("가" * 201)

    def test_memo_rejects_unencodable_surrogates(self) -> None:
        with self.assertRaises(ValidationError):
            parse_memo("깨진\udcff입력")

    def test_tags(self) -> None:
        self.assertEqual(parse_tags("점심, #회사 ,점심,,  "), ("점심", "회사"))
        self.assertEqual(parse_tags("A,a"), ("a",))
        self.assertEqual(parse_tags(""), ())
        with self.assertRaises(ValidationError):
            parse_tags(",".join(f"t{i}" for i in range(11)))

    def test_category(self) -> None:
        self.assertEqual(normalize_category("  Food "), "food")
        self.assertEqual(normalize_category("식비"), "식비")
        self.assertEqual(normalize_category("eating  out"), "eating out")
        for text in ["", "-food", "_x", "a/b", "x" * 21, "foo!"]:
            with self.subTest(text=text), self.assertRaises(ValidationError):
                normalize_category(text)

    def test_positive_int(self) -> None:
        self.assertEqual(parse_positive_int("5", name="n"), 5)
        for text in ["0", "-1", "abc", "1.5", ""]:
            with self.subTest(text=text), self.assertRaises(ValidationError):
                parse_positive_int(text, name="n")
        with self.assertRaises(ValidationError):
            parse_positive_int("32", name="day", maximum=31)


if __name__ == "__main__":
    unittest.main()
