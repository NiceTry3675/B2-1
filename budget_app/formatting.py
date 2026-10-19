"""콘솔 출력 포맷터 (보너스 5.3: 외부 라이브러리 없이 표 정렬).

한글은 터미널에서 영문보다 두 칸을 차지한다. ``len("식비") == 2``지만 화면 폭은 4칸이다.
그래서 ``str.ljust()``로 맞추면 표가 어긋난다. 여기서는 ``unicodedata.east_asian_width``로
글자마다 화면 폭을 계산해서 정렬한다.

이 모듈은 '문자열을 만들어 돌려주기'만 한다. 실제 출력(print)은 CLI 계층이 한다.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Iterable, List, Literal, Optional, Sequence, Tuple

from .models import RecurringRule, Transaction
from .services import BudgetLevel, BudgetStatus, CategoryUsage, MonthlySummary, TransactionDraft

Align = Literal["left", "right", "center"]

ELLIPSIS = ".."


def printable(text: str) -> str:
    """제어 문자를 '?'로 바꾼다. 파일을 직접 편집해 들어간 ESC 문자 등이 터미널을 조작하지 못하게 한다."""
    return "".join("?" if unicodedata.category(char) == "Cc" else char for char in text)


def char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text: str) -> int:
    """터미널에서 차지하는 칸 수 (한글·전각 문자는 2칸)."""
    return sum(char_width(char) for char in text)


def truncate(text: str, max_width: int) -> str:
    if display_width(text) <= max_width:
        return text
    limit = max_width - display_width(ELLIPSIS)
    kept: List[str] = []
    used = 0
    for char in text:
        width = char_width(char)
        if used + width > limit:
            break
        kept.append(char)
        used += width
    return "".join(kept) + ELLIPSIS


def pad(text: str, width: int, align: Align = "left") -> str:
    gap = max(width - display_width(text), 0)
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


@dataclass(frozen=True)
class Column:
    header: str
    align: Align = "left"
    max_width: Optional[int] = None


def render_table(columns: Sequence[Column], rows: Iterable[Sequence[str]]) -> str:
    """열 너비를 내용에 맞춰 계산한 표 문자열을 만든다.

    ID | 날짜       | 카테고리 |     금액
    ---+------------+----------+---------
     1 | 2026-10-02 | food     | 12,000원
    """
    prepared = [
        [truncate(printable(cell), col.max_width) if col.max_width else printable(cell) for cell, col in zip(row, columns)]
        for row in rows
    ]
    widths = [
        max([display_width(col.header)] + [display_width(row[index]) for row in prepared])
        for index, col in enumerate(columns)
    ]

    def line(cells: Sequence[str], aligns: Sequence[Align]) -> str:
        return " | ".join(pad(cell, width, align) for cell, width, align in zip(cells, widths, aligns)).rstrip()

    header = line([col.header for col in columns], [col.align for col in columns])
    separator = "-+-".join("-" * width for width in widths)
    body = [line(row, [col.align for col in columns]) for row in prepared]
    return "\n".join([header, separator, *body])


def money(amount: int) -> str:
    return f"{amount:,}원"


def percent(value: float) -> str:
    return f"{value:.1f}%"


def budget_rate(status: BudgetStatus) -> str:
    """예산 사용률을 소수 첫째 자리까지 보여 준다.

    그냥 반올림하면 79.96%가 '80.0%'로 보이는데 경고는 없고, 100.04%가 '100.0%'로 보이는데
    '초과' 경고가 뜬다. 그래서 예산 이하면 내림, 초과면 올림해서 표시값과 경고가 어긋나지 않게 한다.
    """
    scaled = status.spent * 1000
    if status.spent <= status.budget:
        tenths = scaled // status.budget
    else:
        tenths = -(-scaled // status.budget)
    return f"{tenths // 10}.{tenths % 10}%"


def progress_bar(rate: float, width: int = 20) -> str:
    filled = min(width, int(round(rate / 100 * width)))
    return "[" + "#" * filled + "-" * (width - filled) + "]" + (">" if rate > 100 else "")


def format_tags(tags: Sequence[str]) -> str:
    return " ".join(f"#{tag}" for tag in tags)


# ---------------------------------------------------------------------------
# 거래
# ---------------------------------------------------------------------------

TRANSACTION_COLUMNS = (
    Column("ID", "right"),
    Column("날짜"),
    Column("구분"),
    Column("카테고리", max_width=14),
    Column("금액", "right"),
    Column("메모", max_width=24),
    Column("태그", max_width=20),
)


def transactions_table(transactions: Sequence[Transaction]) -> str:
    return render_table(
        TRANSACTION_COLUMNS,
        (
            [str(tx.id), tx.date.isoformat(), tx.type.label, tx.category, money(tx.amount), tx.memo, format_tags(tx.tags)]
            for tx in transactions
        ),
    )


def _detail_lines(pairs: Sequence[Tuple[str, str]]) -> str:
    width = max(display_width(label) for label, _ in pairs)
    return "\n".join(f"  {pad(label, width)} : {printable(value)}" for label, value in pairs)


def format_transaction(tx: Transaction) -> str:
    return _detail_lines(
        [
            ("id", str(tx.id)),
            ("날짜", tx.date.isoformat()),
            ("타입", f"{tx.type.label} ({tx.type.value})"),
            ("카테고리", tx.category),
            ("금액", money(tx.amount)),
            ("메모", tx.memo or "-"),
            ("태그", format_tags(tx.tags) or "-"),
        ]
    )


def format_draft(draft: TransactionDraft) -> str:
    return _detail_lines(
        [
            ("날짜", draft.date.isoformat()),
            ("타입", f"{draft.type.label} ({draft.type.value})"),
            ("카테고리", draft.category),
            ("금액", money(draft.amount)),
            ("메모", draft.memo or "-"),
            ("태그", format_tags(draft.tags) or "-"),
        ]
    )


def format_changes(before: Transaction, after: Transaction) -> str:
    def values(tx: Transaction) -> List[str]:
        return [tx.date.isoformat(), tx.type.value, tx.category, money(tx.amount), tx.memo or "-", format_tags(tx.tags) or "-"]

    labels = ["날짜", "타입", "카테고리", "금액", "메모", "태그"]
    rows = [
        [label, old, new, "*" if old != new else ""]
        for label, old, new in zip(labels, values(before), values(after))
    ]
    return render_table(
        [Column("항목"), Column("이전", max_width=30), Column("이후", max_width=30), Column("변경")],
        rows,
    )


# ---------------------------------------------------------------------------
# 요약과 예산
# ---------------------------------------------------------------------------


def budget_lines(status: BudgetStatus) -> List[str]:
    rate = budget_rate(status)
    values = [money(status.budget), money(status.spent), money(abs(status.remaining))]
    width = max(display_width(value) for value in values)
    lines = [
        f"  예산      : {pad(values[0], width, 'right')}",
        f"  사용      : {pad(values[1], width, 'right')}  {rate} {progress_bar(status.usage_rate)}",
    ]
    if status.level is BudgetLevel.OVER:
        lines.append(f"  초과 금액 : {pad(values[2], width, 'right')}")
        lines.append(f"[경고] {status.month} 예산을 {money(-status.remaining)} 초과했습니다! (사용률 {rate})")
    else:
        lines.append(f"  남은 예산 : {pad(values[2], width, 'right')}")
        if status.level is BudgetLevel.EXHAUSTED:
            lines.append(f"[주의] {status.month} 예산을 모두 사용했습니다. (사용률 {rate})")
        elif status.level is BudgetLevel.WARNING:
            lines.append(
                f"[주의] {status.month} 예산의 {rate}를 사용했습니다. ({BudgetStatus.WARNING_RATE}% 이상)"
            )
    return lines


def format_summary(summary: MonthlySummary) -> str:
    lines = [f"[{summary.month} 월별 요약]"]
    if not summary.has_data:
        lines.append("데이터 없음: 이 달에 기록된 거래가 없습니다.")
    else:
        values = [money(summary.income), money(summary.expense), money(summary.balance)]
        width = max(display_width(value) for value in values)
        lines.extend(
            [
                f"  총 수입 : {pad(values[0], width, 'right')}  ({summary.income_count}건)",
                f"  총 지출 : {pad(values[1], width, 'right')}  ({summary.expense_count}건)",
                f"  잔액    : {pad(values[2], width, 'right')}  (총수입 - 총지출)",
                "",
            ]
        )
        if summary.top_expenses:
            shown = len(summary.top_expenses)
            title = f"카테고리별 지출 TOP {shown}"
            if summary.expense_category_count > shown:
                title += f" (전체 {summary.expense_category_count}개 카테고리 중)"
            lines.append(title)
            table = render_table(
                [Column("순위", "right"), Column("카테고리"), Column("금액", "right"), Column("비율", "right"), Column("")],
                (
                    [str(rank), item.category, money(item.amount), percent(item.share), progress_bar(item.share, 10)]
                    for rank, item in enumerate(summary.top_expenses, start=1)
                ),
            )
            lines.extend("  " + row for row in table.splitlines())
        else:
            lines.append("이 달에는 지출 내역이 없습니다.")
    if summary.budget is not None:
        lines.append("")
        lines.append("예산")
        lines.extend(budget_lines(summary.budget))
    return "\n".join(lines)


def budgets_table(statuses: Sequence[BudgetStatus]) -> str:
    labels = {
        BudgetLevel.OK: "정상",
        BudgetLevel.WARNING: "주의",
        BudgetLevel.EXHAUSTED: "소진",
        BudgetLevel.OVER: "초과",
    }
    return render_table(
        [Column("월"), Column("예산", "right"), Column("사용", "right"), Column("사용률", "right"), Column("상태")],
        (
            [str(s.month), money(s.budget), money(s.spent), budget_rate(s), labels[s.level]]
            for s in statuses
        ),
    )


def categories_table(usages: Sequence[CategoryUsage]) -> str:
    return render_table(
        [Column("카테고리"), Column("거래 수", "right"), Column("반복 규칙", "right")],
        ([u.name, f"{u.transactions}건", f"{u.rules}건"] for u in usages),
    )


def backups_table(rows: Sequence[Tuple[str, int, int]]) -> str:
    """(파일 이름, 크기, 데이터 파일 수) 목록을 표로 만든다."""
    return render_table(
        [Column("백업 파일"), Column("크기", "right"), Column("데이터 파일", "right")],
        ([name, f"{size:,} bytes", f"{count}개"] for name, size, count in rows),
    )


def rules_table(rules: Sequence[RecurringRule]) -> str:
    return render_table(
        [
            Column("ID", "right"),
            Column("매월", "right"),
            Column("구분"),
            Column("카테고리"),
            Column("금액", "right"),
            Column("메모", max_width=24),
            Column("태그", max_width=20),
        ],
        (
            [str(r.id), f"{r.day}일", r.type.label, r.category, money(r.amount), r.memo, format_tags(r.tags)]
            for r in rules
        ),
    )
