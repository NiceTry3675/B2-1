# 10. 스스로 점검하기: 설명 연습, 요구사항 체크리스트, 확장 과제

과제의 최종 목표는 "만들었다"가 아니라 **"왜 이렇게 만들었는지 설명할 수 있다"**입니다.
이 문서로 마지막 점검을 합니다.

1. **A. 말로 설명하기**: 질문마다 먼저 소리 내어 1~2분 동안 답해 보고, 그다음 모범 답안을 펼쳐 비교합니다.
2. **B. 코드 읽기 퀴즈**: 코드를 보고 결과를 예측한 뒤, 직접 실행해서 확인합니다.
3. **C. 요구사항 ↔ 구현 체크리스트**: 요구사항마다 어디에 구현됐는지, 어떻게 확인하는지 정리했습니다.
4. **D. 확장 과제**: 구조를 이해했는지 확인하는 가장 좋은 방법은 기능을 하나 더 붙여 보는 것입니다.

---

## A. 말로 설명하기 (과제 목표 5개)

### A-1. 파일 기반 저장으로 CRUD/검색/요약/입출력 구현

**Q1. 저장 형식으로 왜 JSONL을 골랐나요? CSV나 JSON 파일 하나와 비교해 보세요.**

<details>
<summary>모범 답안</summary>

- JSONL은 **한 줄이 레코드 하나**입니다. 그래서 (1) 추가할 때 파일 끝에 한 줄만 덧붙이면 되고(`append`),
  (2) 읽을 때 한 줄씩 처리할 수 있어 스트리밍에 잘 맞습니다.
- JSON 파일 하나(`[{...}, {...}]`)는 한 건을 추가하거나 읽을 때도 **전체를 파싱하고 전체를 다시 써야** 합니다.
  중간에 하나만 깨져도 파일 전체를 읽을 수 없습니다.
- CSV도 줄 단위지만 **타입 정보가 없습니다**(모두 문자열). 태그 목록 같은 중첩 구조를 담기 어렵고,
  쉼표·따옴표·줄바꿈 이스케이프 규칙 때문에 손으로 고치다 깨지기 쉽습니다.
  JSONL은 `tags: ["점심", "회사"]`처럼 배열과 정수를 그대로 저장합니다.
- 필드를 새로 추가해도 `from_record`가 `_get_str(record, "memo", default="")`처럼 없는 필드를 기본값으로 읽어서, **예전 파일과 호환**됩니다.
- CSV는 사람이 엑셀로 주고받는 **교환 형식**(import/export)으로만 씁니다. 저장 형식과 교환 형식을 분리했습니다.
</details>

**Q2. 거래 하나를 수정할 때 파일에서 실제로 어떤 일이 일어나나요? 수정 도중 전원이 꺼지면요?**

<details>
<summary>모범 답안</summary>

`LedgerService.update` → `TransactionRepository.replace` → `JsonlFile.rewrite` 순서로 호출되고, 파일은 이렇게 바뀝니다.

1. 같은 폴더에 `.transactions.jsonl.xxxx.tmp` 임시 파일을 만듭니다(`AtomicWriter`, `tempfile.mkstemp`). 그 전에 원본이 읽기 전용인지 확인해, 그렇다면 `StorageError`(종료 코드 5)로 멈춥니다.
2. 원본을 한 줄씩 읽으면서(스트리밍) 대상 id의 줄만 새 내용으로, 나머지는 **원문 바이트 그대로** 임시 파일에 씁니다.
3. `flush` → `os.fsync`로 디스크에 확실히 기록합니다.
4. `os.replace(임시, 원본)`으로 한 번에 바꿉니다. 같은 파일 시스템 안에서 이 동작은 원자적입니다.

1~3단계에서 꺼지면 원본은 그대로이고, 임시 파일만 남을 수 있습니다(숨김 파일이고 데이터에는 영향 없음).
4단계는 원자적이라 "예전 파일" 아니면 "새 파일" 중 하나만 존재합니다. 반쯤 쓴 파일은 생기지 않습니다.
예외가 나거나 Ctrl+C로 끊기면 `AtomicWriter.__exit__`가 임시 파일을 지우고 원본을 건드리지 않습니다.
</details>

**Q3. 삭제한 거래의 id가 다시 쓰이지 않는 이유는 무엇이고, 어떻게 보장하나요?**

<details>
<summary>모범 답안</summary>

- id를 "현재 최대 id + 1"로만 정하면, 가장 큰 id를 지운 뒤 같은 번호가 다시 나옵니다.
  예전 출력이나 메모에 적어 둔 "id 12"가 다른 거래를 가리키게 되어 혼란스럽습니다.
- 그래서 `meta.jsonl`에 **지금까지 발급한 최댓값(high-water mark)**을 저장합니다.
  다음 번호는 `max(meta 값, 실제 파일의 최대 id) + 1`입니다(`SequenceStore.next_value`의 `floor`).
  meta 파일이 사라지거나 뒤처져도 실제 데이터보다 큰 번호가 나오므로 id가 겹치지 않습니다.
- 순서도 중요합니다. **번호 발급을 먼저 기록(`commit`)하고 그다음 거래를 추가**합니다.
  그 사이에 죽으면 번호 하나가 비지만(gap), 같은 번호가 두 번 나오지는 않습니다.
- 지울 때와 복원할 때도 같은 원칙입니다. `LedgerService.delete`는 거래 줄을 지우기 **전에** 지울 id를 `commit`하고,
  `backup restore`는 복원 전에 읽어 둔 최댓값을 복원이 끝난 뒤 다시 `commit`합니다(`BackupService._issued_ids` / `_keep_issued_ids`).
  그래서 meta가 뒤처져 있었거나 예전 `meta.jsonl`이 복원되어도, 앱으로 발급한 번호는 다시 나오지 않습니다.
</details>

**Q4. CSV 가져오기에서 "전부 아니면 전무(all-or-nothing)"를 어떻게 구현했나요?**

<details>
<summary>모범 답안</summary>

`TransactionRepository.staged_append()`는 기존 파일 내용을 임시 파일에 복사한 컨텍스트를 엽니다.
CSV를 한 행씩 읽어 검증하고, 통과한 거래를 그 임시 파일에 덧붙입니다.
루프가 끝난 뒤 오류가 있으면(`--skip-invalid`가 아닐 때) `discard()`하고 `ImportRejectedError`를 던집니다.
그러면 임시 파일은 버려지고 원본은 그대로입니다. 오류가 없으면 `with` 블록을 빠져나가면서 `os.replace`로 한 번에 반영됩니다.
CSV 전체를 메모리에 올리지 않고도, 결과는 "모두 반영" 아니면 "아무것도 반영 안 함" 둘 중 하나가 됩니다.
오류 행도 개수(`error_count`)는 모두 세지만 내용은 앞쪽 100건(`MAX_STORED_ERRORS`)만 보관하므로, 오류가 아주 많은 파일에서도 메모리가 늘지 않습니다.
`--dry-run`이면 같은 자리에 아무것도 쓰지 않는 `_DryRunSink`를 `nullcontext`로 감싸 넣습니다. 데이터 파일을 복사하지도 않고,
두 객체는 `add()`/`discard()` 모양이 같아서(`TransactionSink` 프로토콜) 가져오기 코드는 둘을 구별하지 않습니다.
</details>

### A-2. 클래스/모듈 구조화와 계층별 책임

**Q5. 모델 / 저장소 / 서비스 / CLI 계층은 각각 무엇을 책임지고, 무엇을 모르나요?**

<details>
<summary>모범 답안</summary>

| 계층 | 책임 | 모르는 것 |
| --- | --- | --- |
| 모델 (`models.py`) | 데이터 모양과 불변 조건(금액 > 0 등), dict 변환 | 파일, 화면, 비즈니스 흐름 |
| 저장소 (`storage.py`) | JSONL 읽기/쓰기, 스트리밍, 원자적 교체, 손상된 줄 보존 | "카테고리는 등록된 것만" 같은 규칙, 화면 |
| 서비스 (`services.py` 등) | 비즈니스 규칙(카테고리 확인, 사용 중 삭제 금지, 요약 계산, id 발급) | `print`, `input`, argparse, 파일 형식 |
| CLI (`cli.py`, `interactive.py`, `formatting.py`) | 인자 해석, 대화형 입력, 출력 문자열, 종료 코드 | 파일이 어떻게 저장되는지 |

의존 방향은 `cli → services → storage → models` 한 방향뿐입니다.
그래서 저장 방식을 SQLite로 바꾸면 storage만 바꾸면 되고, GUI를 붙이면 services를 그대로 다시 쓸 수 있습니다.
</details>

**Q6. 서비스가 직접 `print("오류: ...")`를 하지 않고 예외를 던지는 이유는 무엇인가요?**

<details>
<summary>모범 답안</summary>

- 서비스는 "어디에 어떻게 보여 줄지"를 모릅니다. 화면일 수도, 테스트일 수도, 나중에 웹 API일 수도 있습니다.
- 예외를 던지면 **호출한 쪽이 상황에 맞게 처리**합니다. CLI에서는 `@handle_errors`가 메시지와 종료 코드로 바꾸고,
  테스트에서는 `assertRaises(NotFoundError)`로 검증하고, 대화형 입력에서는 `ValidationError`를 잡아 다시 묻습니다.
- 출력을 섞으면 서비스를 다시 쓰기 어렵고, 테스트에서 화면 출력을 일일이 비교해야 합니다.
</details>

**Q7. `context.py`의 `AppContext`는 왜 필요한가요? "의존성 주입"과 연결해 설명해 보세요.**

<details>
<summary>모범 답안</summary>

각 클래스는 필요한 객체를 직접 만들지 않고 **생성자로 받습니다**. 예를 들어 `LedgerService(transactions, categories, sequences, now=...)`입니다.
`AppContext`는 이 객체들을 올바른 순서(저장소 → 서비스)로 **한 곳에서 조립**하는 곳(Composition Root)입니다.
덕분에 데이터 폴더, 입출력 스트림, `input` 함수, 오늘 날짜(`today`), 현재 시각(`now`)을 바꿔 끼우기 쉽습니다.
테스트는 임시 폴더, `StringIO`, 가짜 input(`FakeInput`), 고정한 오늘 날짜(`main(..., today=lambda: TODAY)`)를 넣어
사람 없이, 실행하는 날짜와 상관없이 대화형 흐름까지 검증합니다.
</details>

### A-3. 제너레이터와 스트리밍

**Q8. `list --limit 10`은 거래가 100만 건이어도 메모리를 적게 씁니다. 어떻게 가능한가요?**

<details>
<summary>모범 답안</summary>

1. `JsonlFile.iter_raw`는 파일 객체를 `for line in handle`로 **한 줄씩** 읽고 `yield`합니다. 메모리에는 현재 줄 하나만 있습니다.
2. `iter_items` → `iter_all`이 그 줄을 `Transaction`으로 바꿔 다시 `yield`합니다. 중간 리스트가 없습니다.
3. `newest_first(items, limit=10)`은 `heapq.nlargest(10, ...)`를 씁니다. 이 함수는 "지금까지 본 것 중 최신 10개"만
   힙에 들고 나머지는 버립니다. 메모리는 O(10)이고, 시간은 O(n log 10)입니다.

결국 파일 크기와 상관없이 메모리에는 "현재 줄 1개 + 최신 10개"만 남습니다.
`tests/test_streaming.py`가 `tracemalloc`으로 최대 메모리가 파일 크기의 1/4보다 작은지 검사합니다.
</details>

**Q9. `yield`가 있는 함수를 호출하면 무슨 일이 일어나나요? 일반 함수와 무엇이 다른가요?**

<details>
<summary>모범 답안</summary>

함수 본문이 **바로 실행되지 않고** 제너레이터 객체가 반환됩니다.
`next()`(또는 `for`)로 값을 요청할 때마다 본문이 다음 `yield`까지 실행된 뒤 그 값을 내주고 **멈춥니다**. 지역 변수와 실행 위치는 보존됩니다.
본문이 끝나면 `StopIteration`이 발생하고 `for`문이 끝납니다.
그래서 (1) 필요한 만큼만 계산하고(지연 평가), (2) 한 번 다 쓰면 다시 처음부터 돌 수 없으며(일회성),
(3) 중간에 멈추면 나머지는 아예 읽지 않습니다. `TransactionRepository.get`이 id를 찾는 즉시 멈추는 것이 이 성질 덕분입니다.
</details>

**Q10. 이 프로젝트에서 일부러 리스트로 모으는(materialize) 곳은 어디이고, 왜 괜찮은가요?**

<details>
<summary>모범 답안</summary>

- `CategoryStore.names()`: 카테고리는 수십 개 수준이라 리스트로 돌려줘도 부담이 없고, 여러 번 확인하기 편합니다.
- `search`에 `--limit`이 없을 때와 `export`: 결과를 날짜순으로 **정렬**하려면 결과 전체가 필요합니다.
  그래도 파일 전체가 아니라 **조건에 맞은 결과만** 모읍니다. 이 한계는 README에 적어 두었습니다.
- `ReportService.monthly_summary`의 `Counter`: 메모리가 거래 수가 아니라 **카테고리 수**에 비례합니다.
</details>

### A-4. 데코레이터로 공통 관심사 분리

**Q11. `@handle_errors`, `@log_execution`, `@timed`가 없다면 각 명령 핸들러는 어떻게 생겼을까요?**

<details>
<summary>모범 답안</summary>

명령 핸들러(`cmd_*`) 21개마다 다음 코드가 반복됩니다.

```python
def cmd_list(args, ctx):
    started = time.perf_counter()
    logger.info("시작 ...")
    try:
        ...  # 실제 일
    except BudgetAppError as exc:
        print(f"오류: {exc.message}"); print(f"힌트: {exc.hint}"); return exc.exit_code
    except KeyboardInterrupt:
        ...
    finally:
        logger.info("실행 시간 ...", time.perf_counter() - started)
```

정책(예: 오류 메시지 형식, 종료 코드 규칙)을 바꾸려면 21곳을 고쳐야 하고, 한 곳만 빠뜨려도 동작이 달라집니다.
데코레이터를 쓰면 공통 동작은 `decorators.py` 한 곳에 있고, 핸들러에는 "무엇을 할지"만 남습니다.
</details>

**Q12. `@handle_errors`는 왜 각 핸들러가 아니라 `cli.dispatch` 한 곳에만 붙였나요?**

<details>
<summary>모범 답안</summary>

예외를 메시지와 종료 코드로 바꾸는 일은 **프로그램의 경계(CLI)에서 한 번만** 해야 합니다.
`dispatch`는 모든 명령이 지나가는 관문이고, 저장소 초기화(`ctx.initialize()`)도 여기서 하므로
초기화 중 오류(예: 데이터 경로가 파일인 경우)까지 같은 방식으로 처리됩니다.
(`dispatch`에 오기 전에 끝나는 두 가지만 따로 알립니다. argparse 사용법 오류는 `KoreanArgumentParser.error`가, `--data-dir ""` 같은 경로 해석 오류(`resolve_data_dir`)는 `_run`이 같은 "오류 + 힌트" 형식으로 출력합니다.)
서비스 안에서 예외를 삼키면 호출한 쪽(테스트, 대화형 재입력 루프)이 실패를 알 수 없게 됩니다.
</details>

**Q13. `@log_execution @timed def f(...)`에서 실제 호출 순서는 어떻게 되나요?**

<details>
<summary>모범 답안</summary>

`f = log_execution(timed(f))`와 같습니다. 바깥 래퍼부터 실행되므로
`log_execution 시작 로그 → timed 시작 시각 기록 → f 실행 → timed 실행 시간 기록 → log_execution 완료 로그` 순서입니다.
`timed`는 `try/finally`로 시간을 기록하므로 `f`가 예외를 던져도 시간이 남습니다.
그다음 `log_execution`이 실패를 기록하고 예외를 다시 던집니다.
</details>

### A-5. 타입 힌트로 계약 명확히 하기

**Q14. 타입 힌트가 이 프로젝트에서 실제로 막아 주는 실수의 예를 들어 보세요.**

<details>
<summary>모범 답안</summary>

- `BudgetService.status(month: YearMonth)`에 문자열 `"2026-10"`을 넘기면 mypy가 잡아 줍니다.
  값 객체 `YearMonth` 덕분에 "검증 안 된 월 문자열"이 서비스 안으로 들어오지 못합니다.
- `LedgerService.get`은 `Transaction`을 반환하지만(없으면 예외), `TransactionRepository.get`은 `Optional[Transaction]`을 반환합니다.
  저장소 결과를 `None` 확인 없이 쓰면 mypy가 경고합니다.
- `TransactionChanges`의 필드는 `Optional`입니다. `None`은 "바꾸지 않음", `""`/`()`는 "비움"이라는 계약이 타입에 드러납니다.
- `handle_errors`는 `ErrorReporter` Protocol을 요구합니다. `report_error` 메서드가 없는 객체를 넘기면 정적 검사에서 실패합니다.
</details>

**Q15. 타입 힌트가 있는데 `from_record`는 왜 또 타입을 검사하나요?**

<details>
<summary>모범 답안</summary>

타입 힌트는 **정적 검사 도구(mypy)를 위한 약속**이고, 실행 중에는 강제되지 않습니다.
파일 내용은 사람이 직접 편집했을 수 있는 **신뢰할 수 없는 외부 입력**입니다.
그래서 경계(파일 → 객체)에서 `isinstance` 검사와 `__post_init__` 불변 조건으로 런타임 검증을 합니다.
검증을 통과한 뒤의 내부 코드는 타입 힌트를 믿고 단순하게 작성합니다. "경계에서 검증, 내부에서는 신뢰"입니다.
</details>

---

## B. 코드 읽기 퀴즈

먼저 결과를 예측한 다음 실행해서 확인합니다. 실습 폴더: `--data-dir /tmp/budget-quiz`

**B-1.** 다음 코드는 무엇을 출력할까요?

```python
def numbers():
    print("start")
    yield 1
    print("middle")
    yield 2
    print("end")

gen = numbers()
print("created")
print(next(gen))
print(list(gen))
print(list(gen))
```

<details>
<summary>정답</summary>

```text
created
start
1
middle
end
[2]
[]
```

호출만으로는 본문이 실행되지 않습니다(`created`가 먼저 출력됨). 제너레이터는 한 번 다 쓰면 다시 비어 있습니다.
</details>

**B-2.** `transactions.jsonl`의 3번째 줄을 `{broken`으로 망가뜨린 뒤 `delete --id 1 --yes`를 실행하면, 3번째 줄은 어떻게 될까요?

<details>
<summary>정답</summary>

경고가 출력되지만 그 줄은 **원문 그대로 보존**됩니다. `JsonlFile.rewrite`는 해석할 수 없는 줄을 `writer.write_line(raw.data)`로 그대로 옮겨 적습니다.
직접 확인해 보세요.

```bash
python3 -m budget_app --data-dir /tmp/budget-quiz import --from examples/sample_transactions.csv
```

```bash
sed -i '' '3s/.*/{broken/' /tmp/budget-quiz/transactions.jsonl
```

(Linux에서는 `sed -i '3s/.*/{broken/' ...`)

```bash
python3 -m budget_app --data-dir /tmp/budget-quiz delete --id 1 --yes
```

```bash
grep -n broken /tmp/budget-quiz/transactions.jsonl
```

1번 거래 줄이 지워졌으므로 `grep` 결과는 `2:{broken`처럼 한 줄 앞으로 당겨져 보입니다. 줄 내용은 그대로입니다.
</details>

**B-3.** 다음 명령들의 종료 코드를 예측해 보세요.

| 명령 | 예측 |
| --- | --- |
| `summary --month 2026-13` | ? |
| `delete --id 999 --yes` | ? |
| `category add food` (이미 있음) | ? |
| `export --out x.csv` (기간 없음) | ? |
| `printf '' \| ... add` (입력 없음) | ? |

<details>
<summary>정답</summary>

2 (입력 오류), 3 (대상 없음), 4 (충돌), 2 (입력 오류), 1 (입력 중단).
`echo $?`로 확인합니다. 종료 코드는 `errors.py`의 `ExitCode`에 정의되어 있습니다.
</details>

**B-4.** 2026-10 예산이 100,000원이고 10월 지출이 80,000원입니다. `summary --month 2026-10`의 예산 부분은 어떤 문구로 끝날까요? 지출이 100,000원, 100,001원이면요?

<details>
<summary>정답</summary>

- 80,000원 → `[주의] 2026-10 예산의 80.0%를 사용했습니다. (80% 이상)` (`BudgetLevel.WARNING`)
- 100,000원 → `[주의] 2026-10 예산을 모두 사용했습니다. (사용률 100.0%)` (`BudgetLevel.EXHAUSTED`)
- 100,001원 → `[경고] 2026-10 예산을 1원 초과했습니다! (사용률 100.1%)` (`BudgetLevel.OVER`)

기준은 `services.BudgetStatus.level`에 있고, 비교는 `spent * 100 >= budget * 80`처럼 정수로 합니다.
100,001원의 실제 사용률은 100.001%인데 `100.1%`로 보이는 이유는 `formatting.budget_rate`가 예산을 넘으면 올림, 넘지 않으면 내림하기 때문입니다.
같은 이유로 79,999원은 `79.9%`로 보이고 `[주의]`도 나오지 않습니다. 표시값과 경고가 서로 어긋나지 않게 하려는 규칙입니다.
</details>

**B-5.** 같은 날짜에 거래 A, B를 이 순서로 저장했습니다. `list`에서는 어느 것이 먼저 나올까요? 그 이유를 코드에서 찾아보세요.

<details>
<summary>정답</summary>

B가 먼저 나옵니다. `services._recency_key`가 `(날짜, 파일 안 순서 index)`를 키로 쓰고, 큰 값부터 정렬합니다.
날짜가 같으면 파일에 나중에 저장된(index가 큰) 거래가 더 "최신"입니다.
</details>

---

## C. 요구사항 ↔ 구현 체크리스트

확인 방법에 적은 `tests.…` 이름은 프로젝트 루트에서 `python3 -m unittest <이름>`으로 바로 실행할 수 있습니다.

| 요구사항 | 구현 위치 | 확인 방법 |
| --- | --- | --- |
| 2. 10개 기능 (`add`~`export`) | `cli.build_parser`, `cmd_*` 핸들러 | `python3 -m budget_app --help` |
| 2. 데이터 3개 이상 파일 | `transactions/categories/budgets/recurring/meta.jsonl` | `ls data/` |
| 2. README 구성 | `README.md` 1·2·4·5장 | 실행 방법, 저장 위치/형식, 명령 예시, CSV 스키마 |
| 4.1 `python -m budget_app`, 모든 명령 `--help`, `--` 표기 | `__main__.py`, `cli._add_help` | `tests.test_cli.HelpTest` |
| 4.1 대화형 기본 + 옵션 허용 | `interactive.Prompter`, `cmd_add`, `cmd_budget_set` | `add` 실행 |
| 4.2 `Transaction` dataclass, 클래스 2개 이상 | `models.py` (frozen dataclass 5개), 저장소/서비스 클래스 | 코드 |
| 4.3 입력 검증 (날짜/금액/타입/카테고리) | `validation.py`, `Prompter.ask` 재입력 | `tests.test_validation`, `tests.test_cli.AddFlowTest` |
| 4.4 JSONL, `./data`, `--data-dir`, 초기 생성 안내 | `context.resolve_data_dir`, `AppContext.initialize` | 빈 폴더로 `list` |
| 4.4 빈 카테고리 정책 (안 A: 기본값 자동 생성) | `CategoryService.ensure_defaults` | `categories.jsonl` 비우고 실행 |
| 4.5 `add` 대화형, 등록된 카테고리, id 출력 | `cmd_add`, `ask_category` | `add` 실행 |
| 4.6 `list` 최신순, `--limit` 기본값, 스트리밍 | `LedgerService.recent`, `newest_first` | `tests.test_streaming` |
| 4.7 `update` (안 A: 옵션 방식, 문서 고정), `delete --id`, 없는 id | `cmd_update`, `cmd_delete`, `NotFoundError` | `update --id 999 --amount 1` → 종료 코드 3 |
| 4.7 안정적인 재작성 (임시 파일 + 원자적 교체) | `JsonlFile.rewrite`, `AtomicWriter` | `tests.test_storage.AtomicWriterTest` |
| 4.8 `search` 조건 6종, 최신순, 스트리밍 | `SearchCriteria.matches`, `filter_transactions` | `tests.test_services.SearchTest` |
| 4.9 `summary --month --top`, 데이터 없음 | `ReportService.monthly_summary`, `format_summary` | `summary --month 2030-01` |
| 4.10 `budget set`, 사용률/초과 경고, 영구 저장 | `BudgetService`, `BudgetStatus`, `budgets.jsonl` | `tests.test_services.SummaryAndBudgetTest` |
| 4.11 `category add/list/remove`, 사용 중 처리 | `CategoryService.remove` (막기 + `--replace-with`) | `category remove food` → 종료 코드 4 |
| 4.12 `import --from`, `export --out` + 기간 필수, 고정 스키마 | `csv_io.py` | `tests.test_csv_io` |
| 4.13 데코레이터 구현·적용 | `decorators.py`, `cli.dispatch`, `cmd_*`, 서비스 메서드 | `--timing`, `--verbose`, `data/app.log` |
| 4.14 원인 + 힌트, 종료 코드 0/0 아님 | `errors.py`, `handle_errors`, `KoreanArgumentParser` | `tests.test_cli.ErrorOutputTest` |
| 4.15 모듈 3개 이상, 계층 분리 | `budget_app/` 파일 16개 | `01-architecture.md` |
| 5.1 백업 (타임스탬프) | `backup.py` | `backup`, `backup list` |
| 5.2 반복 거래 | `recurring.py` | `recurring apply --month 2026-02` 두 번 |
| 5.3 표 정렬 (외부 라이브러리 없이) | `formatting.py` | `list` 출력의 한글 정렬 |
| 5.4 임시 파일 + rename | `storage.AtomicWriter` | `tests.test_storage.AtomicWriterTest.test_failed_replace_keeps_original` |

---

## D. 확장 과제

구조를 이해했다면 아래 기능을 **어느 계층에 무엇을 추가하면 되는지** 먼저 말해 보고 구현해 보세요.
구현한 뒤에는 `python3 -m unittest discover -s tests -t .`가 계속 통과하는지 확인합니다.

| 난이도 | 과제 | 힌트 |
| --- | --- | --- |
| ★ | `search` 결과의 수입/지출 합계를 맨 아래에 출력 | `CountingIterator`처럼 지나가는 값을 합산하는 래퍼를 만들면 스트리밍이 유지됩니다 |
| ★ | `stats` 명령: 전체 거래 수, 첫 거래일, 마지막 거래일 | 서비스에 메서드 하나(한 번 훑기), CLI에 핸들러 하나 |
| ★★ | `summary --year 2026`: 월별 수입/지출 추이 표 | `Counter[YearMonth]`로 한 번에 집계, `render_table` 재사용 |
| ★★ | 반복 규칙에 시작 월/종료 월 추가 | `RecurringRule`에 Optional 필드 추가, `from_record` 하위 호환 유지 |
| ★★ | CSV 내보내기 수식 주입(formula injection) 방어 (`=`, `+`, `-`, `@`로 시작하는 메모) | 내보낼 때만 처리하고, 가져올 때는 원래 값으로 되돌릴지 정책을 정하기 |
| ★★★ | 동시 실행 보호: 쓰기 작업에 파일 잠금 | POSIX `fcntl.flock`, Windows는 다른 방식이 필요. 잠금을 얻지 못하면 `ConflictError` |
| ★★★ | 저장소를 SQLite로 바꾸기 (`sqlite3` 표준 모듈) | `TransactionRepository`와 같은 메서드를 가진 클래스를 만들고 `AppContext`에서 바꿔 끼우기. 데이터 파일을 zip으로 묶는 `backup.py`는 함께 손봐야 하지만, 나머지 서비스와 CLI는 그대로여야 합니다 |
| ★★★ | `undo` 명령: 마지막 변경 되돌리기 | 변경 전에 자동 백업을 남기거나, 변경 이력(journal) 파일을 따로 두기 |

> 마지막 과제를 해 보면 계층 분리의 효과를 직접 체감할 수 있습니다. 저장소만 바꿨는데 나머지가 그대로 동작하면 설계가 제 역할을 하고 있다는 뜻입니다.
