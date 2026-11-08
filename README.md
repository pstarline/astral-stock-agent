# Astral Stock Agent

미국 주식 Astral Paper 운영과 Telegram 보고를 위한 Python 3.11 프로젝트.
현재는 **검증된 보고 계층의 첫 구현**이며, 자동매매 MVP는 아직 완료되지 않았다.
기존 `pstarline/auto-stock`과 완전히 분리한다.

## 구현된 기능

- 공식 MCP 조회 결과에서 의도한 미국 주식 Paper 배포만 선택하는 경계 검사
- 정규화된 비공개 스냅샷 검증과 한국어 일일 보고 생성
- 계좌 평가액 대조, 주문과 체결 수량 대조, 오래된 평가·미완료 거래일 차단
- 미확인 손익을 0으로 만들지 않고 실현·미실현 손익 구분
- SQLite 보고 원장과 원자적 전송 선점으로 중복 실행 방지
- Telegram `sendMessage` 전송 코드, 리다이렉트 거절, 오류에서 토큰 제거
- 전송 불명확/프로세스 중단 후 자동 재전송 차단
- Public CI의 실제 보고 전송 차단, Python 3.11 단일 버전 CI 정의

## 실행 확인

```bash
python -m pip install '.[dev]'
python -m pytest -q
python -m astral_stock_agent.cli examples/paper_snapshot.json
```

예제는 항상 `검증용 예제 · 실제 거래 기록 아님`으로 표시되며 전송할 수 없다.
사용자가 개인 PC에 Python을 설치할 필요는 없다. 위 명령은 개발용 클라우드와
나중에 연결할 Public CI를 위한 것이다.

## 운영 경계

Astral이 전략 실행·주문·체결을 담당한다. Python 보고 모듈에는 주문, 배포,
계좌 연결 API가 없다. 공식 MCP는 Work의 인증된 연결을 통해 호출한다.
Python/CI가 ChatGPT의 OAuth 인증을 사용할 수 있다고 가정하지 않는다.

현재 자동 MCP 수집·정규화기, 미국 거래소 휴장 캘린더, 비공개 영속 운영 호스트,
Telegram 인증정보는 연결되지 않았다. `source=astral_mcp`는 출처 메타데이터이며
서명이나 진위 증명이 아니다. 신뢰된 수집기가 검증한 입력만 받아야 한다.
스냅샷의 `session_verified`는 거래소 캘린더 확인을 전제로 한다.

운영 입력 계약은 `examples/paper_snapshot.json`과 `report.py`를 따른다.
`orders`와 `fills`는 해당 거래일 주문·체결만 대상으로 한다. 전날부터 이어진
주문의 누적 체결을 처리하는 정규화는 아직 미구현이므로 불일치하면 차단한다.
`cash_usd`는 단순 주문 가능 금액이 아니라 평가액 대조에 사용하는 동일 범위의
현금 잔액이다. 별도 구매력이나 전략 배정액을 현금 잔액으로 대체하면 안 된다.

운영 전송은 신뢰된 **비공개** 실행 환경에서만 한다.
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`는 해당 환경의 비밀 설정으로 제공한다.
채팅·저장소·Public Actions secrets에 운영 인증정보를 넣지 않는다.
개인 숫자 채팅 ID만 허용하고, 봇 대화 시작과 수신인 확인 후 연결한다.

```bash
python -m astral_stock_agent.cli private/snapshot.json --send --database /private-volume/reports.sqlite
```

SQLite 파일은 실행 간 유지되는 비공개 볼륨에 있어야 한다. 삭제·초기화하거나
작업마다 새 파일을 만들면 중복 방지가 사라진다. 운영 볼륨이 없으면 실제 전송을
활성화하지 않는다. 현재 실행 호스트의 임시 파일을 영구 저장소라고 간주하지 않는다.

Telegram API 승인 응답은 사람의 수신·열람 확인이 아니다. `sending` 또는
`unknown` 상태는 중복 발송 방지를 위해 자동 복구하지 않는다. 수신 메시지와
Telegram 기록을 사람이 대조하고 복구 정책을 구현한 뒤 처리한다. 현재 도구에는
재전송/원장 초기화 명령이 없으며, 자동 정확히 1회 전송을 보장하지 않는다.

## 다음 작업

[프로젝트 상태](docs/STATUS.md), [아키텍처](docs/ARCHITECTURE.md),
[개발 지침](AGENTS.md)을 먼저 읽는다. 실거래는 별도 승인 단계다.
