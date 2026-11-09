# 아키텍처와 실제 연결 경계

## 최소 운영 경로

1. Astral의 공식 전략 생성/Explore 복사 → 저장 → 데이터 가용성 확인 → 백테스트.
2. 독립 검증 및 배포 미리보기 → 필요한 승인 → 전용 미국 주식 Astral Paper 계정 배포.
3. 신뢰된 비공개 수집기가 공식 MCP로 배포·계좌·포지션·주문·체결을 조회한다.
4. 계좌/전략/거래일을 대조하고 누락·페이지 한도·휴장·시세 시점을 검사해 정규화한다.
5. report.py가 검증하고, delivery.py가 영속 보고 원장 선점 후 Telegram에 전송한다.

**현재 3의 조회 순서/활동 정규화와 4의 예정 정규장 캘린더 검증을 구현했지만, 실제 인증 브리지와 실제 원장 연결·운영 호스트는 미연결**이다. Python의 astral.py는 공식
structuredContent를 입력받는 선택·검증 함수다. Astral 인증이나 주문 기능을 구현한
SDK로 오해하지 않는다. API URL을 추측하거나 ChatGPT의 OAuth 토큰을 추출하지 않는다.

## 역할별 구성과 권한

| 역할 | 입력과 출력 | 현재 상태 | 주문 권한 |
| --- | --- | --- | --- |
| 총괄 | 검증 근거 → 다음 작업/승인 | Work 수동 실행과 예약 지시 | 실거래 없음 |
| 시장조사 | 시점·출처가 있는 시장/뉴스 → 참고 보고 | 미구현 | 없음 |
| 종목 발굴 | Astral 지원·유동성 → 후보군 | 미구현 | 없음 |
| 자체 전략 연구 | 자체 규칙 → 후보/백테스트 | SMA 문법·연구 계산 구현, 배포 후보 미검증 | 없음 |
| 공개 전략 탐색 | Explore 신규/중요 변경 → 출처·버전·근거가 있는 독립 검증 후보 | 이슈 #17, MVP 이후 구현·주 1회 탐색 | 없음 |
| 독립 검증 | 규칙·거래·비용 → 승격 판정 | 비용 대조 완료, 독립 엔진 재현 미완료 | 없음 |
| 실행 | 승인된 Paper 배포 → 공식 거래 기록 | 미국 주식 배포 미완료 | 향후 Astral Paper만 |
| 보고 | 중복 JSON 필드 거부·활동 정규화·대조된 스냅샷 → Telegram | 입력 검증·활동 조회 미리보기와 보고 코드 구현, 실전송 미완료 | 없음 |

별도 유료 AI 호출이나 상주 프로세스는 만들지 않는다. 운영 중인 전략은 연구
후보와 별도로 관리하며, 높은 백테스트 수익만으로 자동 승격하지 않는다.

공개 탐색은 초기 검색·조회만 사용한다. 자체/공개 후보는 같은 독립 검증 기준을 적용하며
성과 기간·비용·체결 의미·과최적화·자료 품질·재현성과 Paper/백테스트 구분을 보존한다.
확인할 수 없는 버전/지표를 만들지 않는다. 중복 수집·검증을 막고, 저장/복사는 필요한 때만
별도로 활성화한다. 주간 탐색은 아직 구현/등록되지 않았으며 매시간 개발 예약과 다르다.
구현 순서는 Paper·Telegram MVP → 공개 탐색 → 공통 검증 → 조건부 전략 선택이다.

## 확인된 공식 인터페이스

- 전략: astral_strategy_build / astral_explore_save / astral_saved_strategy_save
- 백테스트: availability → start → status → 완료한 정확한 ID의 get
- 배포: deploy_preview → 실제 미리보기 승인 → deploy
- 관측: broker_accounts_list / deployments_list / portfolio_positions /
  order_list / trade_history / strategy_event_log / equity_curve
- 전달: Telegram HTTPS Bot API sendMessage. Astral 내장 알림의 Telegram 지원은 확인되지 않았다.

지원 증권사는 공식 문서와 개별 계정의 실행 가능 범위를 함께 확인한다.
공식 목록에는 Alpaca Paper, E*TRADE, moomoo, tastytrade, Public, Webull US 등과
일부 가상자산 연결이 표시된다. 토스증권은 확인한 공식 목록에 없다.
문서의 가상자산 지원 설명과 표에 차이가 있으므로 미래 실거래 지원을 단정하지 않는다.
현재 연결된 계정은 Paper뿐이며 새 실계좌를 연결하지 않았다.

## Public CI와 비용

공식 문서상 Public 저장소의 표준 GitHub-hosted 러너 사용은 무료다. 작업당 최대
6시간 등 제한이 있으며, 저장공간·캐시·확대 러너는 별도 제한/과금 범위가 존재한다.
현재 CI는 5분 상한의 표준 Ubuntu 테스트만 정의하고 캐시/아티팩트 업로드를 하지 않는다.
GitHub Actions는 테스트 용도다. 상주 거래 서버나 비공개 영속 DB로 사용하지 않는다.
현재 Private Actions 소진 여부와 관계없이 기존 저장소는 변경하지 않는다.

## 공개 가능한 범위

공개물은 자체 작성한 연결 코드·합성 테스트·문서다. Explore 전략의 전체 소스,
시장 데이터 원본, 계좌·주문 원장, 서명된 다운로드 URL/토큰은 공개하지 않는다.
Explore 복사 가능은 임의 외부 재배포 라이선스와 같다고 가정하지 않는다.
Astral 약관은 서비스를 통한 공유 콘텐츠 사용을 명시하고 있으므로 원본은
Astral 안에 보존하고 저장소에는 출처 링크와 연구 결과만 기록한다.

## 공식 근거 (확인일: 2026-11-08)

- https://heyastral.ai/docs/mcp
- https://heyastral.ai/docs/brokers
- https://heyastral.ai/docs/strategies
- https://heyastral.ai/docs/alerts-notifications
- https://heyastral.ai/terms
- https://docs.github.com/en/actions/concepts/billing-and-usage
- https://docs.github.com/en/actions/reference/limits
- https://core.telegram.org/bots/api#sendmessage
- https://learn.chatgpt.com/docs/automations

## 정규장 일정 검증

무료 공개 패키지 exchange-calendars 4.13.2의 XNYS 캘린더를 고정 사용한다.
NYSE 공식 2026~2028 휴장 및 조기 폐장 일정과 대조한 날짜만 지원한다.
예정 정규장 시간(뉴욕 시간 기준 09:30~16:00 또는 조기 폐장)을 UTC로 변환해 사용한다.
서머타임은 미국 시간대 일정에 따라 처리한다. 패키지 오류·미지원 날짜에는 평일 규칙으로 대체하지 않는다.

report.render는 선언된 session_verified 값 외에도 폐장 시각을 독립적으로 검사한다.
collect_session_activity는 최신 종료 거래일 또는 명시한 종료 거래일을 읽기 수집기에 연결한다.
이 검증은 일일 잔고/주문/체결 대조나 실시간 임시 휴장/심볼 거래 중단의 검증을 대신하지 않는다.
2029년 이후 사용 전에는 공식 일정 재확인 및 검증 범위 확장이 필요하다.

근거(확인일 2026-11-08):
- https://www.nyse.com/trade/hours-calendars
- https://pypi.org/project/exchange_calendars/
- https://github.com/gerrymanoim/exchange_calendars

## 종가 장부 대조

계좌 조회 미리보기는 account_observation.py가 별도로 제공한다. 기존 preflight 수신/대상
검증 뒤 공식 현금 필드를 보존하고, 잔액 기준 시각·평가액·손익·주문·체결은 미확인으로 남긴다.
Work에서 실제 계좌/배포 응답을 입력해 정규화를 확인했다. 이 자료는 비공개 관측일 뿐 종가 장부가
아니며 아래 대조/전송의 입력으로 자동 승격하지 않는다. 계좌 ID/이름은 보고 출력에서 제외하지만
현금 금액도 비공개 자료이므로 Public CI의 실제 관측 출력은 금지한다.

reconciliation.py는 이전/현재의 정확한 종가 장부, 같은 실행 회차의 기간별 회계 근거와
실제 연결 ID가 있는 주문/체결을 받아 현금·보유 수량·순손익을 대조한다.
출처가 불완전한 거래 관측 데이터, 입출금·수수료·기타 수익 누락, 다른 실행 회차나
전 거래일 누락을 임의 보정하지 않는다. 계산 검증과 원천 확보/진위 검증은 별개다.
이 단계의 내부 session_accounting 계약을 Astral의 새 API로 주장하지 않는다.
현재 조회한 이벤트 로그/거래 이력만으로 이 입력을 완성할 수 있다고 검증하지 못했다.
합성 예제 미리보기는 전송하지 않는다. 미국 주식용 Paper 대상과 공식 원장 연결이 다음 우선순위다.
