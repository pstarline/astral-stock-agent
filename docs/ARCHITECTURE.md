# 아키텍처와 실제 연결 경계

## 최소 운영 경로

1. Astral의 공식 전략 생성/Explore 복사 → 저장 → 데이터 가용성 확인 → 백테스트.
2. 독립 검증 및 배포 미리보기 → 필요한 승인 → 전용 미국 주식 Astral Paper 계정 배포.
3. 신뢰된 비공개 수집기가 공식 MCP로 배포·계좌·포지션·주문·체결을 조회한다.
4. 계좌/전략/거래일을 대조하고 누락·페이지 한도·휴장·시세 시점을 검사해 정규화한다.
5. report.py가 검증하고, delivery.py가 영속 보고 원장 선점 후 Telegram에 전송한다.

**현재 3의 조회 순서와 활동 정규화 코드는 구현했지만, 실제 인증 브리지와 4의 전체 일일 대조·운영 호스트는 미연결**이다. Python의 astral.py는 공식
structuredContent를 입력받는 선택·검증 함수다. Astral 인증이나 주문 기능을 구현한
SDK로 오해하지 않는다. API URL을 추측하거나 ChatGPT의 OAuth 토큰을 추출하지 않는다.

## 역할별 구성과 권한

| 역할 | 입력과 출력 | 현재 상태 | 주문 권한 |
| --- | --- | --- | --- |
| 총괄 | 검증 근거 → 다음 작업/승인 | Work 수동 실행과 예약 지시 | 실거래 없음 |
| 시장조사 | 시점·출처가 있는 시장/뉴스 → 참고 보고 | 미구현 | 없음 |
| 종목 발굴 | Astral 지원·유동성 → 후보군 | 미구현 | 없음 |
| 전략 연구 | Explore → 원본 보존 복사/백테스트 | SPY 후보 1개 실제 검증 | 없음 |
| 독립 검증 | 규칙·거래·비용 → 승격 판정 | 비용 대조 완료, 독립 엔진 재현 미완료 | 없음 |
| 실행 | 승인된 Paper 배포 → 공식 거래 기록 | 미국 주식 배포 미완료 | 향후 Astral Paper만 |
| 보고 | 활동 정규화·대조된 스냅샷 → Telegram | 활동 조회 미리보기와 보고 코드 구현, 실전송 미완료 | 없음 |

별도 유료 AI 호출이나 상주 프로세스는 만들지 않는다. 운영 중인 전략은 연구
후보와 별도로 관리하며, 높은 백테스트 수익만으로 자동 승격하지 않는다.

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
