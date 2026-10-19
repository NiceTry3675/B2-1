"""저장소 계층: JSONL 파일 입출력.

설계 원칙
---------
1. **스트리밍 읽기**: 파일은 항상 제너레이터로 한 줄씩 읽는다. 파일 전체를 메모리에 올리지 않는다.
2. **원자적 쓰기**: 파일을 다시 써야 하는 작업(수정/삭제/가져오기)은
   "임시 파일에 쓰기 → fsync → ``os.replace``로 교체" 순서로 한다. 중간에 실패하거나
   Ctrl+C로 끊겨도 원본 파일은 그대로 남는다.
3. **손상된 줄 보존**: 읽을 수 없는 줄은 경고만 하고 건너뛴다. 파일을 다시 쓸 때도
   그 줄의 원문 바이트를 그대로 옮겨 적어서, 프로그램 때문에 데이터가 사라지지 않게 한다.

JSONL(JSON Lines)은 "한 줄 = JSON 객체 하나" 형식이다. 줄 단위로 읽고 추가할 수 있어서
스트리밍과 append에 잘 맞는다.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import (
    IO,
    Any,
    Callable,
    ContextManager,
    Dict,
    Generic,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Protocol,
    Set,
    Tuple,
    Type,
    TypeVar,
)

from .errors import StorageError
from .models import Budget, Category, RecurringRule, Transaction, YearMonth

logger = logging.getLogger(__name__)

ENCODING = "utf-8"
_UTF8_BOM = b"\xef\xbb\xbf"

T = TypeVar("T")
Decoder = Callable[[Mapping[str, Any]], T]
Encoder = Callable[[T], Mapping[str, Any]]


def encode_record(record: Mapping[str, Any]) -> bytes:
    """레코드(dict)를 JSONL 한 줄(줄바꿈 제외)로 직렬화한다. 한글은 이스케이프하지 않는다."""
    return json.dumps(record, ensure_ascii=False).encode(ENCODING)


# ---------------------------------------------------------------------------
# 원자적 파일 쓰기
# ---------------------------------------------------------------------------


class AtomicWriter:
    """임시 파일에 쓴 뒤 ``os.replace``로 한 번에 교체하는 컨텍스트 매니저.

    사용 예::

        with AtomicWriter(path) as writer:
            writer.write_line(b'{"id": 1}')
        # 블록이 예외 없이 끝나면 교체(commit), 예외가 나거나 discard()를 부르면 원본 유지

    ``os.replace``는 같은 파일 시스템 안에서 원자적이다. 그래서 다른 프로세스나
    다음 실행은 '예전 파일' 아니면 '완성된 새 파일'만 보게 된다. 반쯤 쓴 파일은 볼 수 없다.
    임시 파일을 대상 파일과 같은 폴더에 만드는 것도 이 때문이다.
    """

    def __init__(self, target: Path, *, text: bool = False, newline: Optional[str] = None) -> None:
        # 심볼릭 링크라면 실제 파일을 교체한다. 링크 자체를 일반 파일로 덮어쓰지 않기 위해서다.
        self.target = Path(os.path.realpath(target))
        self._text = text
        self._newline = newline
        self._stream: Optional[IO[Any]] = None
        self._tmp_path: Optional[Path] = None
        self._discarded = False

    def __enter__(self) -> AtomicWriter:
        if self.target.exists() and not os.access(self.target, os.W_OK):
            # os.replace는 읽기 전용 파일도 바꿔 버리므로, 사용자가 잠가 둔 파일은 미리 확인한다.
            raise StorageError(
                f"쓰기가 금지된 파일이라 바꿀 수 없습니다: {self.target}",
                hint="파일 권한을 확인하세요 (예: chmod u+w <파일>).",
            )
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.target.parent), prefix=f".{self.target.name}.", suffix=".tmp"
        )
        self._tmp_path = Path(tmp_name)
        try:
            if self._text:
                self._stream = os.fdopen(fd, "w", encoding=ENCODING, newline=self._newline)
            else:
                self._stream = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            self._tmp_path.unlink()
            raise
        return self

    @property
    def stream(self) -> IO[Any]:
        if self._stream is None:
            raise RuntimeError("AtomicWriter는 with 블록 안에서만 쓸 수 있습니다.")
        return self._stream

    def write_line(self, data: bytes) -> None:
        self.stream.write(data + b"\n")

    def discard(self) -> None:
        """블록이 끝나도 교체하지 않고 임시 파일을 버린다."""
        self._discarded = True

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        stream, tmp_path = self._stream, self._tmp_path
        self._stream = None
        if stream is None or tmp_path is None:
            return
        committed = False
        try:
            if exc_type is None and not self._discarded:
                stream.flush()
                os.fsync(stream.fileno())  # 디스크에 실제로 기록된 뒤에 교체한다.
                stream.close()
                _apply_permissions(tmp_path, self.target)
                os.replace(tmp_path, self.target)
                committed = True
                _fsync_directory(self.target.parent)
        finally:
            if not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass  # 이미 실패한 쓰기(디스크 가득 참 등)를 닫다가 난 오류: 원래 예외를 가리지 않는다.
            if not committed:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


def _apply_permissions(tmp_path: Path, target: Path) -> None:
    """mkstemp는 0600 권한으로 만든다. 기존 파일 권한(없으면 umask 기본값)을 따르게 맞춘다."""
    try:
        if target.exists():
            shutil.copymode(target, tmp_path)
        else:
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp_path, 0o666 & ~umask)
    except OSError:
        pass  # 권한 맞추기는 최선의 노력(best effort)이다. 실패해도 데이터에는 영향이 없다.


def _fsync_directory(directory: Path) -> None:
    """POSIX에서는 이름 바꾸기(rename)도 폴더를 fsync해야 디스크에 확정된다."""
    if os.name != "posix":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# JSONL 파일 하나를 다루는 저수준 클래스
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawLine:
    """파일의 한 줄 원문. data에는 끝의 줄바꿈이 빠져 있다."""

    lineno: int
    data: bytes


class StagedAppend(Generic[T]):
    """``JsonlFile.staged_append()`` 블록 안에서 새 항목을 모으는 객체."""

    def __init__(self, writer: AtomicWriter, encoder: Encoder[T]) -> None:
        self._writer = writer
        self._encoder = encoder
        self.count = 0

    def add(self, item: T) -> None:
        self._writer.write_line(encode_record(self._encoder(item)))
        self.count += 1

    def discard(self) -> None:
        self._writer.discard()


class JsonlFile:
    """JSON Lines 파일 하나에 대한 읽기/추가/재작성 기능."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._warned: Set[Tuple[int, bytes]] = set()

    def ensure(self) -> bool:
        """파일이 없으면 빈 파일을 만든다. 새로 만들었으면 True."""
        if self.path.exists():
            if not self.path.is_file():
                raise StorageError(
                    f"저장 파일 자리에 파일이 아닌 것이 있습니다: {self.path}",
                    hint="같은 이름의 폴더를 옮기거나 --data-dir로 다른 위치를 지정하세요.",
                )
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.path, "xb"):
                pass
        except FileExistsError:
            return False
        return True

    def iter_raw(self) -> Iterator[RawLine]:
        """파일을 한 줄씩 읽어 yield한다 (빈 줄은 건너뜀).

        제너레이터이므로 ``for``가 다음 값을 요청할 때만 다음 줄을 읽는다.
        파일이 1GB여도 한 번에 메모리에 있는 것은 '현재 줄' 하나뿐이다.
        """
        try:
            handle = open(self.path, "rb")
        except FileNotFoundError:
            return
        with handle:  # 제너레이터가 끝나거나 닫히면(close) 파일도 닫힌다.
            for lineno, line in enumerate(handle, start=1):
                data = line.rstrip(b"\r\n")
                if lineno == 1 and data.startswith(_UTF8_BOM):
                    data = data[len(_UTF8_BOM) :]
                if data.strip():
                    yield RawLine(lineno, data)

    def decode(self, raw: RawLine, decoder: Decoder[T]) -> Optional[T]:
        """한 줄을 객체로 바꾼다. 읽을 수 없으면 경고를 남기고 None."""
        try:
            obj = json.loads(raw.data.decode(ENCODING))
            if not isinstance(obj, dict):
                raise ValueError("JSON 객체({...}) 형태가 아닙니다")
            return decoder(obj)
        # JSON/UTF-8 오류는 ValueError의 하위 클래스다. RecursionError는 지나치게 깊이 중첩된 JSON에서 난다.
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            self._warn_corrupt(raw, exc)
            return None

    def iter_items(self, decoder: Decoder[T]) -> Iterator[T]:
        """올바른 줄만 객체로 바꿔 하나씩 yield한다 (제너레이터 파이프라인의 출발점)."""
        for raw in self.iter_raw():
            item = self.decode(raw, decoder)
            if item is not None:
                yield item

    def append(self, record: Mapping[str, Any]) -> None:
        """레코드 한 줄을 파일 끝에 추가한다. 기존 줄은 건드리지 않는다."""
        payload = encode_record(record) + b"\n"  # 직렬화를 먼저 끝내서 실패하면 아무것도 쓰지 않는다.
        self.ensure()
        if self._needs_leading_newline():
            # 이전 실행이 마지막 줄을 쓰다가 끊겼다면 그 줄과 붙지 않게 줄을 바꾼다.
            payload = b"\n" + payload
        # 버퍼 없이(buffering=0) 열어서, 쓰다가 실패하면 늘어난 부분을 잘라 내고 원래 길이로 되돌린다.
        with open(self.path, "ab", buffering=0) as handle:
            start = handle.seek(0, os.SEEK_END)
            try:
                remaining = memoryview(payload)
                while remaining:
                    written = handle.write(remaining)
                    remaining = remaining[written:]
                os.fsync(handle.fileno())
            except BaseException:
                try:
                    os.ftruncate(handle.fileno(), start)
                except OSError:
                    pass
                raise

    def rewrite(
        self,
        decoder: Decoder[T],
        transform: Callable[[T], Optional[T]],
        encoder: Encoder[T],
        *,
        extra: Iterable[T] = (),
    ) -> int:
        """모든 항목에 ``transform``을 적용해 파일 전체를 원자적으로 다시 쓴다.

        - ``transform``이 None을 반환하면 삭제, 다른 값이면 교체, 같은 값이면 원문 유지
        - 손상된 줄은 원문 그대로 보존
        - ``extra`` 항목은 파일 끝에 추가

        반환값은 바뀐(삭제·교체·추가) 항목 수다. 0이면 파일을 전혀 건드리지 않는다.
        읽기와 쓰기가 모두 스트리밍이라 메모리 사용량은 파일 크기와 무관하다.
        """
        self.ensure()
        changed = 0
        with AtomicWriter(self.path) as writer:
            for raw in self.iter_raw():
                item = self.decode(raw, decoder)
                if item is None:
                    writer.write_line(raw.data)
                    continue
                result = transform(item)
                if result is None:
                    changed += 1
                elif result == item:
                    writer.write_line(raw.data)  # 바뀌지 않은 줄은 원문 그대로 (모르는 필드도 보존)
                else:
                    writer.write_line(encode_record(encoder(result)))
                    changed += 1
            for item in extra:
                writer.write_line(encode_record(encoder(item)))
                changed += 1
            if changed == 0:
                writer.discard()
        return changed

    @contextmanager
    def staged_append(self, encoder: Encoder[T]) -> Iterator[StagedAppend[T]]:
        """여러 항목을 '전부 아니면 전무(all-or-nothing)'로 추가한다.

        기존 내용을 임시 파일에 복사하고 새 항목을 덧붙인 다음, 블록이 정상적으로
        끝날 때만 원본과 교체한다. 블록에서 예외가 나거나 ``discard()``를 부르면
        아무것도 반영되지 않는다.
        """
        self.ensure()
        with AtomicWriter(self.path) as writer:
            self._copy_into(writer)
            staged = StagedAppend(writer, encoder)
            yield staged
            if staged.count == 0:
                writer.discard()

    def _copy_into(self, writer: AtomicWriter) -> None:
        with open(self.path, "rb") as source:
            shutil.copyfileobj(source, writer.stream)  # 고정 크기 버퍼 단위로 복사 (스트리밍)
            if source.tell() > 0:
                source.seek(-1, os.SEEK_END)
                if source.read(1) != b"\n":
                    writer.stream.write(b"\n")

    def _needs_leading_newline(self) -> bool:
        try:
            with open(self.path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    return False
                handle.seek(-1, os.SEEK_END)
                return handle.read(1) != b"\n"
        except FileNotFoundError:
            return False

    def _warn_corrupt(self, raw: RawLine, exc: Exception) -> None:
        key = (raw.lineno, raw.data)
        if key in self._warned:  # 한 번 실행하는 동안 같은 줄은 한 번만 경고한다.
            return
        self._warned.add(key)
        logger.warning(
            "%s %d번째 줄을 읽을 수 없어 건너뜁니다 (%s). 이 줄은 파일을 다시 쓸 때도 원문 그대로 보존됩니다.",
            self.path.name,
            raw.lineno,
            _describe_decode_error(exc),
        )


def _describe_decode_error(exc: Exception) -> str:
    if isinstance(exc, KeyError):
        return f"필수 항목 '{exc.args[0]}' 없음"
    if isinstance(exc, UnicodeDecodeError):
        return "UTF-8이 아닌 바이트"
    if isinstance(exc, json.JSONDecodeError):
        return "JSON 형식 오류"
    return str(exc)


# ---------------------------------------------------------------------------
# 도메인별 저장소: 파일 하나 = 클래스 하나
# ---------------------------------------------------------------------------


def first_match(predicate: Callable[[T], bool], change: Callable[[T], Optional[T]]) -> Callable[[T], Optional[T]]:
    """``predicate``를 처음 만족하는 항목 하나에만 ``change``를 적용하는 transform을 만든다.

    파일을 직접 편집해서 같은 id가 여러 줄에 있어도, ``get()``이 보여 준 그 한 줄만 바뀐다.
    """
    done = False

    def transform(item: T) -> Optional[T]:
        nonlocal done
        if not done and predicate(item):
            done = True
            return change(item)
        return item

    return transform


class FileBackedStore(Protocol):
    """파일 하나를 맡는 저장소가 공통으로 가진 기능 (초기화·백업에서 사용)."""

    @property
    def path(self) -> Path: ...

    def ensure_file(self) -> bool: ...


class TransactionRepository:
    """거래 내역 저장소 (``transactions.jsonl``)."""

    FILE_NAME = "transactions.jsonl"

    def __init__(self, data_dir: Path) -> None:
        self._file = JsonlFile(data_dir / self.FILE_NAME)

    @property
    def path(self) -> Path:
        return self._file.path

    def ensure_file(self) -> bool:
        return self._file.ensure()

    def iter_all(self) -> Iterator[Transaction]:
        """저장된 순서대로 거래를 하나씩 yield한다 (스트리밍)."""
        yield from self._file.iter_items(Transaction.from_record)

    def get(self, tx_id: int) -> Optional[Transaction]:
        for tx in self.iter_all():
            if tx.id == tx_id:
                return tx  # 찾으면 바로 멈춘다. 나머지 줄은 읽지 않는다.
        return None

    def max_id(self) -> int:
        return max((tx.id for tx in self.iter_all()), default=0)

    def add(self, tx: Transaction) -> None:
        self._file.append(tx.to_record())

    def rewrite(self, transform: Callable[[Transaction], Optional[Transaction]]) -> int:
        return self._file.rewrite(Transaction.from_record, transform, Transaction.to_record)

    def replace(self, updated: Transaction) -> bool:
        """같은 id의 (첫 번째) 거래를 ``updated``로 바꾼다. 바뀐 것이 있으면 True."""
        return self.rewrite(first_match(lambda tx: tx.id == updated.id, lambda tx: updated)) > 0

    def delete(self, tx_id: int) -> bool:
        """같은 id의 (첫 번째) 거래를 지운다. 지웠으면 True."""
        return self.rewrite(first_match(lambda tx: tx.id == tx_id, lambda tx: None)) > 0

    def staged_append(self) -> ContextManager[StagedAppend[Transaction]]:
        """``with repo.staged_append() as staged: staged.add(tx)`` 형태로 일괄 추가."""
        return self._file.staged_append(Transaction.to_record)


class CategoryStore:
    """카테고리 저장소 (``categories.jsonl``)."""

    FILE_NAME = "categories.jsonl"

    def __init__(self, data_dir: Path) -> None:
        self._file = JsonlFile(data_dir / self.FILE_NAME)

    @property
    def path(self) -> Path:
        return self._file.path

    def ensure_file(self) -> bool:
        return self._file.ensure()

    def iter_all(self) -> Iterator[Category]:
        yield from self._file.iter_items(Category.from_record)

    def names(self) -> List[str]:
        """등록 순서를 지킨 카테고리 이름 목록 (중복 제거). 카테고리는 수가 적어 리스트로 돌려준다."""
        names: Dict[str, None] = {}
        for category in self.iter_all():
            names.setdefault(category.name, None)
        return list(names)

    def exists(self, name: str) -> bool:
        return any(category.name == name for category in self.iter_all())

    def is_empty(self) -> bool:
        return next(self.iter_all(), None) is None

    def add(self, category: Category) -> None:
        self._file.append(category.to_record())

    def add_many(self, categories: Iterable[Category]) -> int:
        return self._file.rewrite(
            Category.from_record, lambda c: c, Category.to_record, extra=list(categories)
        )

    def remove(self, name: str) -> bool:
        return (
            self._file.rewrite(
                Category.from_record,
                lambda c: None if c.name == name else c,
                Category.to_record,
            )
            > 0
        )


class BudgetStore:
    """월 예산 저장소 (``budgets.jsonl``). 한 달에 예산은 하나다."""

    FILE_NAME = "budgets.jsonl"

    def __init__(self, data_dir: Path) -> None:
        self._file = JsonlFile(data_dir / self.FILE_NAME)

    @property
    def path(self) -> Path:
        return self._file.path

    def ensure_file(self) -> bool:
        return self._file.ensure()

    def iter_all(self) -> Iterator[Budget]:
        yield from self._file.iter_items(Budget.from_record)

    def get(self, month: YearMonth) -> Optional[Budget]:
        found: Optional[Budget] = None
        for budget in self.iter_all():
            if budget.month == month:
                found = budget  # 직접 편집해서 같은 달이 여러 줄이면 마지막 값을 쓴다.
        return found

    def upsert(self, budget: Budget) -> Optional[Budget]:
        """해당 월 예산을 저장(없으면 추가, 있으면 교체)하고 이전 값을 돌려준다."""
        previous = self.get(budget.month)
        self._file.rewrite(
            Budget.from_record,
            lambda b: None if b.month == budget.month else b,
            Budget.to_record,
            extra=[budget],
        )
        return previous

    def remove(self, month: YearMonth) -> bool:
        return (
            self._file.rewrite(
                Budget.from_record, lambda b: None if b.month == month else b, Budget.to_record
            )
            > 0
        )


class RecurringRuleStore:
    """반복 거래 규칙 저장소 (``recurring.jsonl``)."""

    FILE_NAME = "recurring.jsonl"

    def __init__(self, data_dir: Path) -> None:
        self._file = JsonlFile(data_dir / self.FILE_NAME)

    @property
    def path(self) -> Path:
        return self._file.path

    def ensure_file(self) -> bool:
        return self._file.ensure()

    def iter_all(self) -> Iterator[RecurringRule]:
        yield from self._file.iter_items(RecurringRule.from_record)

    def get(self, rule_id: int) -> Optional[RecurringRule]:
        for rule in self.iter_all():
            if rule.id == rule_id:
                return rule
        return None

    def max_id(self) -> int:
        return max((rule.id for rule in self.iter_all()), default=0)

    def add(self, rule: RecurringRule) -> None:
        self._file.append(rule.to_record())

    def rewrite(self, transform: Callable[[RecurringRule], Optional[RecurringRule]]) -> int:
        return self._file.rewrite(RecurringRule.from_record, transform, RecurringRule.to_record)

    def delete(self, rule_id: int) -> bool:
        return self.rewrite(first_match(lambda rule: rule.id == rule_id, lambda rule: None)) > 0


@dataclass(frozen=True)
class SequenceEntry:
    """``meta.jsonl``의 한 줄: 이름 붙은 일련번호의 '지금까지 발급한 최댓값'."""

    key: str
    value: int

    def to_record(self) -> Dict[str, Any]:
        return {"key": self.key, "value": self.value}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> SequenceEntry:
        key, value = record["key"], record["value"]
        if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TypeError("meta 레코드 형식이 올바르지 않습니다")
        return cls(key, value)


class SequenceStore:
    """id 발급기 (``meta.jsonl``).

    거래 id는 1, 2, 3...처럼 늘어나는 정수다. 가장 큰 id를 삭제한 뒤에도 그 번호를
    다시 쓰지 않도록 '지금까지 발급한 최댓값(high-water mark)'을 따로 저장한다.
    """

    FILE_NAME = "meta.jsonl"

    def __init__(self, data_dir: Path) -> None:
        self._file = JsonlFile(data_dir / self.FILE_NAME)

    @property
    def path(self) -> Path:
        return self._file.path

    def ensure_file(self) -> bool:
        return self._file.ensure()

    def current(self, key: str) -> int:
        value = 0
        for entry in self._file.iter_items(SequenceEntry.from_record):
            if entry.key == key:
                value = entry.value
        return value

    def next_value(self, key: str, *, floor: int = 0) -> int:
        """다음에 발급할 번호. ``floor``는 실제 데이터에 이미 있는 최대 번호다.

        meta 파일이 사라졌거나 실제 데이터보다 뒤처져 있어도, 데이터의 최댓값보다
        큰 번호를 내주므로 id가 겹치지 않는다.
        """
        return max(self.current(key), floor) + 1

    def commit(self, key: str, last_value: int) -> None:
        """``last_value``까지 발급했음을 기록한다. 기록된 값은 줄어들지 않는다."""
        value = max(self.current(key), last_value)
        self._file.rewrite(
            SequenceEntry.from_record,
            lambda e: None if e.key == key else e,
            SequenceEntry.to_record,
            extra=[SequenceEntry(key, value)],
        )
