"""콘솔 가계부 애플리케이션.

계층 구조 (위에서 아래로만 의존한다)::

    cli            명령줄 인자 해석, 화면 출력          ─┐
    interactive    대화형 입력 (재입력 루프)             │ 사용자와 대화
    formatting     표/요약 문자열 만들기                ─┘
    context        객체 조립 (Composition Root)
    services       비즈니스 규칙 (거래/카테고리/예산/요약)
    csv_io, recurring, backup   부가 기능 서비스
    storage        JSONL 파일 I/O (스트리밍 읽기, 원자적 쓰기)
    models         dataclass 데이터 구조
    validation     문자열 → 도메인 값 변환과 검증
    decorators     공통 관심사 (예외 처리 / 로그 / 실행 시간)
    errors         예외 계층과 종료 코드
"""

__version__ = "1.0.0"
