# 자체 Paper의 Render 운영

사용자 2026-11-15 선택: Astral 구독을 필수로 사용하지 않는 자체 엔진과 Render 운영.
이 문서는 코드 배포 절차이며 실제 생성/운영/수신 완료 기록이 아니다.

## 배포 구성

저장소 루트 render.yaml: 유료 Starter Worker 1개, 1GB 디스크 1개, Python3.11.18.
디스크 /var/data 안의 astral/prices, paper, outbox, control을 별도0700 경로로 사용한다.
Worker에는 외부 HTTP 수신 포트가 없고 Astral/외부 AI를 호출하지 않는다.
자동 배포와 일일 실행/후보 승인/Telegram 발송은 기본 꺼짐이다.
Render에서 실제 Blueprint 검증과 배포 상태 확인이 필요하다. 문서 검토/YAML 파싱은
Render 공식 API의 validation 성공이나 실제 디스크 영속성을 증명하지 않는다.

Render 사용 선택은 확인했다. 앞서 안내한 최소 구성 밖의 증설/추가 유료 서비스는
자동으로 선택하지 않는다. 현재 플러그인 계정 연결은 확인되지 않았으며 생성/결제0회다.

## 비공개 설정

Render 서비스의 비공개 환경 설정에서만 입력한다. 토큰/가격/원장/설정 원문을 채팅,
Public 저장소, Actions 로그에 넣지 않는다. 기존 GitHub Secrets를 추출하지 않는다.

| 설정 | 의미 |
| --- | --- |
| PAPER_CONFIG_JSON | archived_sma_record_v1 후보 설정. 기존 규칙 ID, 창, 고정 가상 수량, 초기 가상 현금, 비용, trade_from을 명시한다. 검증/승인된 후보만 사용한다. |
| PAPER_US_SYMBOL | 실제 미국 주식으로 별도 확인한 단일 종목. 설정 symbol과 같아야 한다. |
| PAPER_BOOTSTRAP_SESSION | 첫 운영할 NYSE 거래일 YYYY-MM-DD. trade_from과 같아야 하며 과거 성과를 이월하지 않는다. |
| ALPHAVANTAGE_API_KEY | 승인된 무료 개인 키. 공개 demo를 운영 시세로 사용하지 않는다. |
| TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID | 본인이 보유한 기존 봇과 개인 대화. Render에 직접 비공개 입력한다. |
| PAPER_WORKER_ENABLED | 기본 false. 후보/저장소/키 점검 뒤 true로 활성화한다. |
| PAPER_CANDIDATE_APPROVED | 기본 false. 실제 독립 검증·사용자 승인 확인 후에만 true로 설정한다. 플래그 자체가 검증은 아니다. |
| PAPER_REPORTING_ENABLED | 기본 false. 실제 개인 대화 대상과 일일 보고 운영 승인을 확인한 뒤 true로 설정한다. |

첫 거래일 활성화 전에 규칙/비용/가상 현금·수량 가정과 키·대상을 확정한다.
실제 투자금/위험 한도나 미검증 전략을 배포 설정에서 자동 선정하지 않는다.
실제 장 마감 일봉 게시 시점은 공급자 검증 대상이며90분 대기는 신선도 보장이 아니다.

## 첫 디스크 준비

아래는 최초 한 번, 아직 원장/제어 기록이 없는 새 디스크에서만 운영자가 명시 실행한다.
재시작/오류 복구에서 다시 초기화하지 않는다. 기존 디렉터리에 덮어쓰기하지 않는다.
해당 Render 서비스의 비공개 셸에서 실행하고 실제 경로/권한을 확인한다.

```bash
umask 077
mkdir /var/data/astral
mkdir /var/data/astral/prices /var/data/astral/paper /var/data/astral/outbox /var/data/astral/control
python -m astral_stock_agent.cli init-outbox --database /var/data/astral/outbox/reports.sqlite
```

처음에는 Worker/보고 활성화를 끈 상태로 배포/영속 디스크 연결/설정 오류 비노출을 확인한다.
새 디스크의4개 경로와 큐를 준비하고 검증·승인된 후보 설정을 점검한 뒤 첫 거래일을 정한다.
실제 수집→기록→큐→전송 경로를 확인하며 기존 일회 연결 테스트 승인을 재사용하지 않는다.

## 매일 실행과 복구

- 60초 간격으로 예정 NYSE 세션을 검사하며 네트워크 조회는 하지 않는다. 휴장/장중/
  폐장90분 전에는 대기한다. 조기 폐장/서머타임도 기존 검증 캘린더를 따른다.
- 오늘 거래일만 대상으로 하며 첫 실행 전에 control에0600 불변 attempt를 fsync한다.
  같은 거래일의 완료 기록이 있으면 시세 재조회/재계산/발송을 모두 생략한다.
- 새 보고는 기존 Outbox가 sending을 먼저 commit한 뒤 최대1회 시도한다.
  pending/sending/unknown은 자동 전송/재전송하지 않는다. sent는 API 수락만 뜻한다.
- 수집 실패·누락 시세·회계/권한 오류·기존 pending 또는 전송 결과 불명확은 Worker를
  중지 상태로 고정한다. 재시작에도 미완료 attempt를 확인해 다른 거래일 진행을 차단한다.
  운영자가 시세/원장/큐와 Telegram 실제 수신을 대조하기 전 marker를 지우거나 큐를 초기화하지 않는다.
- control의 attempt와 complete는 단일 트랜잭션이 아니다. 중간 종료는 보수적으로 중단한다.
  운영 중단으로 거래일을 놓치면 자동 과거 체결/성과 생성 없이 복구 절차가 필요하다.
- SIGTERM 시 대기를 종료한다. 실행 중에는 현재 호출을 마치며 강제 종료는 불명확 상태로 남을 수 있다.
- 디스크 스냅샷/수동 복원을 정상 발송 원장의 증명으로 보지 않는다. 과거 원장 복원으로 이미
  발송한 보고가 미발송으로 되돌아갈 수 있어 운영 재개 전에 외부 실제 수신과 대조해야 한다.

정지: PAPER_WORKER_ENABLED=false로 변경해 재배포한다. 권한/원장/토큰을 삭제하지 않는다.
정상 활성화 후 실제 일일 기록, 재시작 대조, 실제 Telegram 수신을 확인해야 MVP 완료다.
매시간 AI 개발과 매일 Paper 실행은 별개이며 공개 전략 탐색은 MVP 이후 주1회다.

공식 근거(2026-11-15 확인):
- https://render.com/docs/background-workers
- https://render.com/docs/disks
- https://render.com/docs/blueprint-spec
