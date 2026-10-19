# Python 콘솔 가계부 (`budget_app`)

수입과 지출을 **JSONL 파일**에 저장하는 콘솔 가계부입니다. 거래를 추가·조회·검색·수정·삭제할 수 있고,
월별 요약, 카테고리 관리, 예산 초과 경고, CSV 가져오기/내보내기도 됩니다. 백업·복원, 반복 거래 같은 보너스 기능도 있습니다.

- **표준 라이브러리만** 사용합니다 (설치할 패키지 없음). Python **3.9 이상**에서 동작합니다.
- 파일은 **제너레이터로 한 줄씩 스트리밍**해서 읽습니다. 수정·삭제·가져오기는 **임시 파일 → `os.replace`** 방식으로 원자적으로 씁니다.
- 예외 처리·실행 로그·실행 시간 측정은 **데코레이터**로 분리했습니다. 전 모듈에 **타입 힌트**가 있고 `mypy --strict`를 통과합니다.
- 설계와 개념을 설명한 학습 문서는 [`docs/`](docs/README.md)에 있습니다.

---

## 1. 실행 방법

프로젝트 루트(이 README가 있는 폴더)에서 실행합니다.

```bash
python3 -m budget_app --help
```

```bash
python3 -m budget_app <command> [options]
```

- 모든 명령과 하위 명령에서 `--help`로 사용법을 볼 수 있습니다. 예: `python3 -m budget_app search --help`
- 옵션 표기는 `--` 하나로 통일했습니다 (`-h` 같은 짧은 옵션은 없습니다).
- 입력 방식은 기본이 **대화형**입니다. 다만 `list`, `search`, `summary`, `import`, `export`, `delete`, `update`는 옵션 인자를 받습니다.
  `budget set`, `category add/remove`에서 옵션이나 인자를 생략하면 대화형으로 묻습니다.

### 빠른 시작

```bash
python3 -m budget_app import --from examples/sample_transactions.csv   # 샘플 거래 11건 가져오기
python3 -m budget_app list --limit 5                                    # 최신 5건
python3 -m budget_app budget set --month 2026-10 --amount 900000        # 10월 예산 설정
python3 -m budget_app summary --month 2026-10 --top 3                   # 10월 요약 + 예산 사용률
python3 -m budget_app add                                               # 대화형으로 거래 추가
```

### 공통 옵션 (명령 앞이나 뒤 어디에 써도 됩니다)

| 옵션 | 설명 |
| --- | --- |
| `--data-dir DIR` | 데이터 폴더 (기본값 `./data`). 환경 변수 `BUDGET_APP_DATA_DIR`로도 바꿀 수 있습니다. 우선순위: 옵션 > 환경 변수 > 기본값 |
| `--verbose` | 실행 로그(시작/완료/실패)를 화면(stderr)에도 출력 |
| `--timing` | 명령 실행 시간을 출력 (`@timed` 데코레이터) |
| `--debug` | 오류가 나면 상세 정보(스택트레이스)도 출력 (평소에는 숨김) |
| `--help` | 도움말. 모든 명령과 하위 명령에서 쓸 수 있습니다 |
| `--version` | 버전. 명령 앞에만 쓸 수 있습니다 (`python3 -m budget_app --version`) |

### 설치해서 쓰기 (선택)

설치하지 않아도 `python3 -m budget_app`으로 실행할 수 있습니다. `budget`이라는 짧은 명령으로 쓰고 싶을 때만 설치하세요.
편집 가능 설치(`-e`)에는 pip 21.3 이상이 필요합니다. macOS 기본 Python 3.9.6의 pip(21.2.4)는 먼저 업그레이드해야 합니다.
가상 환경 안에서 설치하는 것을 권장합니다.

```bash
python3 -m pip install --upgrade pip
```

```bash
python3 -m pip install -e .
```

설치하면 `budget list`처럼 `budget` 명령으로도 실행할 수 있습니다.

---

## 2. 저장 파일 위치와 형식

- 기본 위치는 **`./data/`**입니다 (실행한 폴더 기준). `--data-dir` 또는 `BUDGET_APP_DATA_DIR`로 바꿀 수 있습니다.
- 형식은 **JSONL (JSON Lines, UTF-8)**입니다. 한 줄에 JSON 객체 하나가 들어갑니다. 한글은 이스케이프하지 않고 그대로 저장합니다.
- 처음 실행할 때 폴더와 파일이 없으면 **자동으로 만들고** 안내 메시지를 출력합니다.
- 카테고리 파일이 없거나 비어 있으면 **기본 카테고리를 자동으로 만듭니다** (요구사항 4.4의 안 A):
  `food, transport, rent, utilities, shopping, health, salary, etc`

| 파일 | 내용 | 한 줄 예시 |
| --- | --- | --- |
| `transactions.jsonl` | 거래 내역 | `{"id": 3, "type": "expense", "date": "2026-10-02", "amount": 12000, "category": "food", "memo": "점심 김치찌개", "tags": ["점심", "회사"], "created_at": "2026-10-19T00:23:48"}` |
| `categories.jsonl` | 카테고리 | `{"name": "food", "created_at": "2026-10-19T00:23:48"}` |
| `budgets.jsonl` | 월 예산 (한 달에 하나) | `{"month": "2026-10", "amount": 900000}` |
| `recurring.jsonl` | 반복 거래 규칙 (보너스) | `{"id": 1, "day": 25, "type": "income", "amount": 3200000, "category": "salary", "memo": "월급", "tags": [], "created_at": "..."}` |
| `meta.jsonl` | id 발급 기록 (지금까지 쓴 최대 id) | `{"key": "transaction", "value": 11}` |
| `app.log` | 실행 로그 (명령, 결과, 실행 시간, 오류 상세) | `2026-10-19 00:23:48,123 [INFO] budget_app.perf: cmd_list 실행 시간: 3.21ms` |
| `backups/backup-YYYYMMDD-HHMMSS.zip` | 백업 (보너스) | 위 `*.jsonl` 파일 전부 + `manifest.json` |

### 거래(`Transaction`) 필드

| 필드 | 타입 | 규칙 |
| --- | --- | --- |
| `id` | 정수 | 1부터 증가하는 고유 번호입니다. 삭제한 번호는 다시 쓰지 않습니다 (`meta.jsonl`). |
| `type` | 문자열 | `income`(수입) / `expense`(지출) |
| `date` | 문자열 | `YYYY-MM-DD`, 실제로 있는 날짜 |
| `amount` | 정수 | 양수(원 단위), 최대 1조 |
| `category` | 문자열 | 등록된 카테고리 (소문자로 정규화) |
| `memo` | 문자열 | 선택, 200자 이하 |
| `tags` | 문자열 배열 | 선택, 최대 10개, 소문자로 정규화 |
| `recurring_id` | 정수 | 반복 규칙으로 만든 거래에만 있습니다 |
| `created_at` | 문자열 | 저장 시각 (ISO 8601) |

### 데이터 안전 장치

- **원자적 쓰기**: 수정·삭제·가져오기·예산 변경은 같은 폴더의 임시 파일에 먼저 쓰고, `fsync` 후 `os.replace`로 바꿉니다.
  중간에 오류가 나거나 Ctrl+C로 끊겨도 원본 파일은 그대로 남습니다.
- **손상된 줄 보존**: 직접 편집하다 JSON이 깨진 줄은 경고만 하고 건너뜁니다. 파일을 다시 쓸 때도 원문 그대로 보존합니다.
- **끊긴 마지막 줄 복구**: 마지막 줄이 줄바꿈 없이 끝나 있으면, 다음 추가 때 줄을 바꾼 뒤 씁니다. 새 줄이 깨진 줄에 붙지 않습니다.
- **가져오기는 전부 아니면 전무**: CSV에 오류 행이 하나라도 있으면 아무것도 저장하지 않습니다 (`--skip-invalid`로 바꿀 수 있음).
- **실패한 추가는 되돌림**: `add` 도중 쓰기에 실패하면(디스크 가득 참 등) 덧붙이던 부분을 잘라 내 원래 길이로 되돌립니다.
- **잠긴 파일은 바꾸지 않음**: 읽기 전용(`chmod 444`)으로 바꿔 둔 데이터 파일은 수정·삭제하지 않고 오류(종료 코드 5)로 알립니다.
- **심볼릭 링크 유지**: 데이터 파일이 링크라면 링크가 가리키는 실제 파일을 교체합니다.
- **id가 겹친 줄**: 파일을 직접 편집해 같은 id가 여러 줄에 있으면, `update`/`delete`는 첫 번째 줄(목록에 보이는 그 거래)만 바꿉니다.

---

## 3. 명령 요약

| 명령 | 입력 | 결과 |
| --- | --- | --- |
| `add` | 대화형 (날짜, 타입, 카테고리, 금액, 메모, 태그) | 저장 성공 메시지와 새 `id` |
| `list [--limit N]` | 기본값 20 | 최신순 목록, 전체 건수 |
| `search [--from D] [--to D] [--month M] [--category C] [--type T] [--q K] [--tag T]... [--limit N]` | 조건은 모두 AND | 최신순 결과 |
| `summary [--month YYYY-MM] [--top N]` | 기본값 이번 달, TOP 5 | 총수입·총지출·잔액, 카테고리 TOP N, 예산 사용률·경고 |
| `budget set [--month M] [--amount A]` / `budget list` / `budget remove --month M` | 생략한 값은 대화형으로 묻습니다 | 저장 결과, 예산별 사용률 |
| `category add [NAME]` / `category list` / `category remove [NAME] [--replace-with OTHER]` | | 추가/목록(사용 건수)/삭제 결과 |
| `update --id ID [--date] [--type] [--category] [--amount] [--memo] [--tags]` | **옵션 방식으로 고정** | 바뀌기 전과 후를 비교한 표 |
| `delete --id ID [--yes]` | 확인 질문 (`--yes`면 생략) | 삭제 성공/실패 |
| `import --from CSV [--skip-invalid] [--create-categories] [--dry-run]` | | 처리 건수, 오류 행 목록 |
| `export --out CSV` + `--month M` 또는 `--from D`/`--to D` + `[--category] [--type] [--q] [--tag] [--force]` | 기간 조건 필수 | 처리 건수, 만든 파일 경로 |
| `recurring add` / `list` / `remove --id ID` / `apply [--month M]` | 보너스 5.2 | 반복 규칙 관리와 월별 생성 |
| `backup` / `backup list` / `backup restore --file NAME [--yes]` | 보너스 5.1 | 타임스탬프가 붙은 zip 백업과 복원 |

### 동작 정책 (문서로 고정한 선택 사항)

| 항목 | 이 프로그램의 정책 |
| --- | --- |
| `update` 방식 | **안 A, 옵션 방식**입니다. 지정한 필드만 바꾸고, `--memo ""`와 `--tags ""`는 비우기입니다. 아무 필드도 없으면 오류(종료 코드 2). |
| 카테고리 초기화 | **안 A**입니다. 파일이 없거나 비어 있으면 기본 카테고리를 자동으로 만듭니다. 마지막 남은 카테고리는 지울 수 없습니다. |
| 사용 중인 카테고리 삭제 | 기본은 **삭제를 막습니다** (종료 코드 4). `--replace-with`로 **대체 카테고리**를 주면 거래와 반복 규칙을 옮긴 뒤 삭제합니다. |
| `add`에서 없는 카테고리 | 안내한 뒤 "새로 등록할까요?"라고 묻습니다. 예라고 답하면 거래를 **저장할 때 함께** 등록합니다(마지막에 저장을 취소하면 등록하지 않음). 아니라고 답하면 다시 고르게 합니다. |
| `-`로 시작하는 값 | `--memo=-5%할인`처럼 `=`로 붙여 씁니다. 띄어 쓰면 argparse가 새 옵션으로 해석합니다. |
| 최신순 | 날짜 내림차순입니다. 날짜가 같으면 나중에 저장한 거래가 먼저 나옵니다. |
| 입력 오류 (대화형) | 원인과 힌트를 보여 주고 다시 묻습니다. 5번 연속 틀리거나 입력이 끝나면(EOF) 취소합니다 (종료 코드 1). |
| `export` 파일이 이미 있음 | 덮어쓰지 않고 오류를 냅니다 (종료 코드 4). `--force`면 덮어씁니다. |
| 예산 경고 | 사용률 80% 이상이면 `[주의]`, 100%를 넘으면 `[경고]`입니다. `summary`, `budget set`, `add`, `update` 뒤에 표시합니다. |

---

## 4. 주요 명령 예시

아래 예시는 빠른 시작처럼 샘플 CSV(거래 11건)를 가져온 상태에서 각 명령을 따로 실행한 결과입니다.
그 달에 예산이 설정되어 있으면 `add`/`update` 출력 끝에 예산 사용률 안내(`[주의]`/`[경고]`)가 한 줄 더 나옵니다.

### 거래 추가 (대화형)

```text
$ python3 -m budget_app add
새 거래를 입력합니다. (Enter: [ ] 안의 기본값 사용, Ctrl+C: 취소)
날짜 (YYYY-MM-DD) [2026-10-19]: 2026-10-02
타입 (income=수입 / expense=지출): expense
  등록된 카테고리: food, transport, rent, utilities, shopping, health, salary, etc
카테고리: food
금액 (원, 양의 정수): -500
  ! 금액은 음수일 수 없습니다: '-500'
    힌트: 금액은 양의 정수로 입력하세요. 수입/지출 구분은 부호가 아니라 타입(income/expense)으로 합니다.
    다시 입력해 주세요. (남은 기회 4번)
금액 (원, 양의 정수): 12,000
메모 (선택, Enter로 건너뛰기): 점심 김치찌개
태그 (선택, 쉼표로 구분): 점심, 회사

입력한 내용
  날짜     : 2026-10-02
  타입     : 지출 (expense)
  카테고리 : food
  금액     : 12,000원
  메모     : 점심 김치찌개
  태그     : #점심 #회사
이 내용으로 저장할까요? [Y/n]:
거래를 저장했습니다. (id: 12)
```

### 목록 / 검색

```text
$ python3 -m budget_app list --limit 5
ID | 날짜       | 구분 | 카테고리  |        금액 | 메모           | 태그
---+------------+------+-----------+-------------+----------------+--------
11 | 2026-10-25 | 수입 | salary    | 3,200,000원 | 10월 급여      | #월급
10 | 2026-10-18 | 지출 | food      |     8,500원 | 카페 라떼      | #카페
 9 | 2026-10-15 | 지출 | health    |    15,000원 | 약국           |
 8 | 2026-10-12 | 지출 | utilities |    82,000원 | 전기·가스 요금 | #고정비
 7 | 2026-10-10 | 수입 | etc       |    50,000원 | 중고거래 판매  | #중고

전체 11건 중 최신 5건을 표시했습니다. (--limit으로 개수를 바꿀 수 있습니다)

$ python3 -m budget_app search --from 2026-10-01 --to 2026-10-31 --type expense --category food
검색 조건: 기간 2026-10-01 ~ 2026-10-31 / 카테고리 food / 타입 expense
ID | 날짜       | 구분 | 카테고리 |     금액 | 메모          | 태그
---+------------+------+----------+----------+---------------+------------
10 | 2026-10-18 | 지출 | food     |  8,500원 | 카페 라떼     | #카페
 6 | 2026-10-07 | 지출 | food     | 38,000원 | 친구와 저녁   | #저녁 #친구
 3 | 2026-10-02 | 지출 | food     | 12,000원 | 점심 김치찌개 | #점심 #회사

조건에 맞는 거래 3건
```

`search`의 다른 조건: `--q 점심` (메모 키워드, 대소문자 무시), `--tag 회사 --tag 점심` (두 태그를 모두 가진 거래), `--month 2026-10`, `--limit 10`.

### 월별 요약 + 예산

```text
$ python3 -m budget_app budget set --month 2026-10 --amount 900000
2026-10 예산을 900,000원으로 저장했습니다.
  예산      : 900,000원
  사용      : 852,850원  94.7% [###################-]
  남은 예산 :  47,150원
[주의] 2026-10 예산의 94.7%를 사용했습니다. (80% 이상)

$ python3 -m budget_app summary --month 2026-10 --top 3
[2026-10 월별 요약]
  총 수입 : 3,250,000원  (2건)
  총 지출 :   852,850원  (8건)
  잔액    : 2,397,150원  (총수입 - 총지출)

카테고리별 지출 TOP 3 (전체 6개 카테고리 중)
  순위 | 카테고리  |      금액 |  비율 |
  -----+-----------+-----------+-------+-------------
     1 | rent      | 650,000원 | 76.2% | [########--]
     2 | utilities |  82,000원 |  9.6% | [#---------]
     3 | food      |  58,500원 |  6.9% | [#---------]

예산
  예산      : 900,000원
  사용      : 852,850원  94.7% [###################-]
  남은 예산 :  47,150원
[주의] 2026-10 예산의 94.7%를 사용했습니다. (80% 이상)

$ python3 -m budget_app summary --month 2030-01
[2030-01 월별 요약]
데이터 없음: 이 달에 기록된 거래가 없습니다.
```

예산을 넘으면 `[경고] 2026-10 예산을 52,850원 초과했습니다! (사용률 106.7%)`처럼 표시합니다.
사용률은 표시값과 경고가 어긋나지 않도록 예산 이하이면 내림, 초과이면 올림해서 소수 첫째 자리까지 보여 줍니다.

### 수정 / 삭제

```text
$ python3 -m budget_app update --id 3 --amount 15000 --memo "점심 회식"
거래를 수정했습니다. (id: 3)
항목     | 이전          | 이후        | 변경
---------+---------------+-------------+-----
날짜     | 2026-10-02    | 2026-10-02  |
타입     | expense       | expense     |
카테고리 | food          | food        |
금액     | 12,000원      | 15,000원    | *
메모     | 점심 김치찌개 | 점심 회식   | *
태그     | #점심 #회사   | #점심 #회사 |
[주의] 2026-10 예산의 95.0%를 사용했습니다. (남은 예산 44,150원)

$ python3 -m budget_app delete --id 4 --yes
삭제할 거래
  id       : 4
  ...
거래를 삭제했습니다. (id: 4)

$ python3 -m budget_app delete --id 4 --yes
오류: id 4에 해당하는 거래가 없습니다.
힌트: 'list'나 'search' 명령으로 올바른 id를 확인하세요.
$ echo $?
3
```

### 카테고리

```bash
python3 -m budget_app category list                              # 카테고리별 거래 수
python3 -m budget_app category add coffee
python3 -m budget_app category remove food                       # 사용 중이면 막음 (종료 코드 4)
python3 -m budget_app category remove food --replace-with etc    # 내역을 etc로 옮긴 뒤 삭제
```

### 가져오기 / 내보내기

```bash
python3 -m budget_app import --from examples/sample_transactions.csv
python3 -m budget_app import --from examples/invalid_transactions.csv --dry-run   # 검증만
python3 -m budget_app import --from bank.csv --skip-invalid --create-categories
python3 -m budget_app export --month 2026-10 --out october.csv
python3 -m budget_app export --from 2026-01-01 --to 2026-06-30 --type expense --out h1.csv
```

```text
$ python3 -m budget_app import --from examples/invalid_transactions.csv
오류: CSV 8행 중 7행에 오류가 있어 아무것도 가져오지 않았습니다.
  - 3번째 줄: 존재하지 않는 날짜입니다: '2026-02-30'
  - 4번째 줄: 허용되지 않은 타입입니다: 'transfer'
  - 5번째 줄: 금액은 음수일 수 없습니다: '-3000'
  ...
힌트: 오류를 고친 뒤 다시 실행하거나, 올바른 행만 가져오려면 --skip-invalid를 붙이세요. ...
```

### 백업 / 반복 거래 (보너스)

```bash
python3 -m budget_app backup                                     # data/backups/backup-20261019-002443.zip
python3 -m budget_app backup list
python3 -m budget_app backup restore --file backup-20261019-002443.zip   # 복원 직전 상태도 자동 백업
python3 -m budget_app recurring add                              # 대화형: 매월 25일 월급 등
python3 -m budget_app recurring apply --month 2026-11            # 여러 번 실행해도 한 번만 생성
```

---

## 5. `import` / `export` CSV 스키마

인코딩은 **UTF-8**이고 **첫 줄은 헤더**입니다. 엑셀이 붙이는 BOM이 있어도 읽을 수 있습니다.

| 열 | 필수 | 설명 |
| --- | --- | --- |
| `date` | Y | `YYYY-MM-DD` |
| `type` | Y | `income` / `expense` |
| `category` | Y | 등록된 카테고리 (없으면 오류, `--create-categories`면 자동 등록) |
| `amount` | Y | 양의 정수 (`12000`, `"12,000"` 모두 가능) |
| `memo` | N | 문자열 |
| `tags` | N | 쉼표(`,`)로 구분한 문자열. 쉼표가 있으므로 큰따옴표로 감쌉니다: `"점심,회사"` |

```csv
date,type,category,amount,memo,tags
2026-10-01,expense,rent,650000,10월 월세,고정비
2026-10-02,expense,food,12000,점심 김치찌개,"점심,회사"
2026-10-25,income,salary,3200000,10월 급여,월급
```

- **가져오기**: 헤더의 대소문자와 앞뒤 공백은 무시합니다. 모르는 열(예: `id`)은 무시하고 알려 줍니다.
  행마다 `add`와 같은 검증 규칙을 적용하고, 오류는 파일의 몇 번째 줄인지 함께 보여 줍니다.
- **내보내기**: 위 6개 열만, 날짜 오름차순으로 씁니다. 내보낸 파일은 그대로 다시 가져올 수 있습니다.

---

## 6. 입력 검증 규칙

| 항목 | 허용 | 거부 예 |
| --- | --- | --- |
| 날짜 | `2026-10-18` (실제로 있는 날짜) | `2026/10/18`, `2026-1-5`, `2026-02-30` |
| 월 | `2026-10` | `2026-13`, `202610` |
| 타입 | `income`, `expense` (별칭 `i`/`e`/`수입`/`지출`, 대소문자 무시) | `transfer` |
| 금액 | `12000`, `12,000`, `12000원` | `0`, `-500`, `12.5`, `1,2,3`, `1e3` |
| 카테고리 | 글자/숫자로 시작, 20자 이하 (`food`, `식비`, `eating out`) | 등록되지 않은 이름, `-x`, `a/b` |
| 메모 | 200자 이하 (줄바꿈·연속 공백은 공백 하나로) | 201자 이상 |
| 태그 | 쉼표 구분, 최대 10개, 하나당 20자 이하, `#` 접두사 허용 | 11개 이상 |

---

## 7. 오류 메시지와 종료 코드

스택트레이스 대신 **원인**과 **해결 힌트**를 출력합니다 (stderr).

```text
오류: 'food' 카테고리를 쓰는 거래 3건, 반복 규칙 0건이 있어 삭제할 수 없습니다.
힌트: 'category remove food --replace-with <다른 카테고리>'로 내역을 옮긴 뒤 삭제하세요.
```

| 코드 | 의미 | 예 |
| --- | --- | --- |
| 0 | 정상 종료 | 취소를 고른 경우도 포함 |
| 1 | 일반 오류 | 대화형 입력 중단(EOF, 5번 연속 오류), 예기치 못한 오류 |
| 2 | 입력 오류 | 잘못된 날짜/금액/옵션, 기간 없는 `export`, CSV 검증 실패, `update --category`나 CSV 행에 등록되지 않은 카테고리를 준 경우 |
| 3 | 대상 없음 | 없는 `id`, 없는 파일, `category remove`나 `--replace-with`에 준 카테고리가 없는 경우 |
| 4 | 충돌 | 이미 있는 카테고리, 사용 중인 카테고리 삭제, 이미 있는 내보내기 파일 |
| 5 | 파일 오류 | 권한 없음, 디스크 오류, 데이터 경로가 폴더가 아님 |
| 130 | 사용자 중단 | Ctrl+C |

---

## 8. 프로젝트 구조

```text
budget_app/
├── __main__.py     # python -m budget_app 진입점
├── cli.py          # CLI 계층: argparse, 명령 핸들러, 출력
├── interactive.py  # 대화형 입력 (재입력 루프, 예/아니오 확인)
├── formatting.py   # 표 정렬, 요약 문자열 (한글 폭 계산)
├── context.py      # 객체 조립 (저장소 → 서비스), 초기화
├── services.py     # 서비스 계층: 거래/카테고리/예산/요약 규칙
├── csv_io.py       # CSV 가져오기/내보내기 서비스
├── recurring.py    # 반복 거래 서비스 (보너스)
├── backup.py       # 백업/복원 서비스 (보너스)
├── storage.py      # 저장소 계층: JSONL 스트리밍 읽기, 원자적 쓰기
├── models.py       # 모델 계층: Transaction, Category, Budget, RecurringRule, YearMonth
├── validation.py   # 문자열 → 도메인 값 검증
├── decorators.py   # @handle_errors, @log_execution, @timed
├── logconfig.py    # 로깅 설정 (app.log, --verbose, --timing)
└── errors.py       # 예외 계층, 종료 코드
tests/              # unittest 155개 (표준 라이브러리만 사용)
examples/           # 샘플 CSV (정상 / 오류 예시)
docs/               # 학습 문서
```

의존 방향은 `cli → services → storage → models` 한 방향입니다. 아래 계층은 위 계층을 모릅니다.
자세한 설명은 [docs/01-architecture.md](docs/01-architecture.md)를 보세요.

---

## 9. 테스트와 타입 검사

```bash
python3 -m unittest discover -s tests -t . -v
```

```bash
python3 -m mypy
```

mypy는 별도로 설치해야 합니다 (`pip install mypy`). 설정은 `pyproject.toml`에 있습니다 (`strict = true`, Python 3.9 기준).

---

## 10. 알려진 한계

- 여러 프로세스가 **동시에** 같은 데이터 폴더에 쓰는 상황은 고려하지 않았습니다 (파일 잠금 없음). 한 사람이 쓰는 콘솔 도구를 전제로 합니다.
- 금액은 원 단위 정수만 다룹니다 (통화·소수점 없음).
- `export`와 `--limit` 없는 `search`는 조건에 맞는 거래를 메모리에 모아 정렬합니다. 파일 전체가 아니라 '결과'만 모으지만,
  결과가 아주 많으면 그만큼 메모리를 씁니다. `list`, `search --limit N`, `summary`는 파일 크기와 상관없이 메모리를 일정하게 씁니다.
- 반복 거래로 만든 거래를 지운 뒤 `recurring apply`를 다시 실행하면, 그 거래가 다시 생깁니다.
