# Astral Stock Agent

미국 주식 Astral Paper 운영과 Telegram 보고를 위한 Python 3.11 프로젝트.
현재는 **검증된 보고 계층의 첫 구현**이며, 자동매매 MVP는 아직 완료되지 않았다.
기존 `pstarline/auto-stock`과 완전히 분리한다.

## 구현된 기능

- 공식 MCP 조회 결과에서 의도한 미국 주식 Paper 배포만 선택하는 경계 검사
- 계좌/배포 확인 후 주문·거래 이력을 읽는 비동기 수집 순서와 원본 응답 정규화
- 주문과 연결 ID가 없는 거래 관측 기록을 분리하는 조회 미리보기
- 정규화된 비공개 스냅샷 검증과 한국어 일일 보고 생성
- 계좌 평가액 대조, 주문과 체결 수량 대조, 오래된 평가·미완료 거래일 차단
- NYSE 정규장 캘린더에 따른 공휴일·조기 폐장·서머타임 검증과 최근 종료 거래일 선택
- 미확인 손익을 0으로 만들지 않고 실현·미실현 손익 구분
- 전후 종가 장부와 자금 유출입·기타 순수익·수수료·체결을 대조하는 일일 계산
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

## Paper 실행 전 읽기 점검

`preflight.collect_preflight`는 인증된 콜백으로 계좌·배포 두 조회만 수행한다.
대상 계좌는 명시하며 기존 전략을 자동 선택하거나 주문/거래 이력을 조회하지 않는다.
계좌 기본 조건이 맞아도 배포·원장·전송이 검증되지 않으면 진행 장애를 반환한다.
출력에는 계좌 ID·이름·잔고·원본 오류를 포함하지 않는다. 주문 권한은 항상 없다.

```bash
python -m astral_stock_agent.cli private/preflight.json --preflight --us-symbol SPY
```

입력은 `version/source/account_ref/strategy_ref/responses`를 갖는 비공개 내부 전달 형식이다.
`responses`에는 accounts/deployments의 도구명·빈 인자·수신 시각·실제 구조화 응답을 넣는다.
이는 자동 OAuth 연결이나 영속 호스트 설치 기능이 아니다. 실제 조회로 만든 입력은
공개 저장소에 넣지 않는다. 배포 부재는 당일 거래/체결/손익 0의 증명이 아니며
`--send`, `--activity`, `--reconcile`과 함께 사용할 수 없다.

## 공식 Paper 계좌 조회 보고 미리보기

```bash
python -m astral_stock_agent.cli private/preflight.json --account-observation --us-symbol SPY
```

기존 preflight의 원본 응답 전달 형식을 사용한다. `account_observation`은 공식 계좌와
배포 두 조회의 대상·완전성·수신 시점을 검증한 뒤 cash/unassigned_cash/reserved_cash를
각각 표시한다. `collect_account_observation`은 이미 인증된 콜백으로 두 조회를 한 번씩 수행한다.
Work가 받은 실제 응답을 정규화기에 연결해 비공개 미리보기를 확인했다.
이는 OAuth 인증을 Python으로 자동 이전하는 기능이나 실제 일일 손익 보고가 아니다.

현금 필드를 평가액·매수 가능 금액·종가 잔고로 바꾸지 않고, 서로 합산되는 관계도 가정하지 않는다.
빠진 값은 null/미확인으로 유지하며 잘못된 값은 거부한다. 수신 시각은 잔액 기준 시각이 아니므로
balance_as_of/valuation_as_of/trading_day 및 손익·주문·체결·보유는 null로 남긴다.
활성 배포가 없어도 거래·손익 0으로 채우지 않는다. 원본 계좌 식별자·이름은 보고에 넣지 않는다.
금액과 원본 입력 자체는 비공개 운영 자료이며 저장소/공개 로그에 넣지 않는다.
새 CLI 모드는 미리보기 전용으로 --send/다른 모드와 함께 쓸 수 없고 실제 출처의 Public CI
미리보기도 차단한다. source 라벨은 인증이나 원천 진위의 증명이 아니므로 신뢰된 수집기만 사용한다.

GitHub의 Telegram 토큰/Chat ID는 수동 점검에서 인증·개인 대화 조회에 성공했다.
이를 Work/Python 운영 환경에 Secret이 연결됐다는 뜻으로 보지 않는다. 별도 OAuth 인증 없는
공식 MCP initialize 요청은401과 공식 resource metadata를 반환했다. 해당 metadata에서
읽기 전용 권한 범위는 확인하지 못했으며 ChatGPT 인증정보를 추출/재사용하지 않는다.

## Astral 조회 내역 미리보기

```bash
python -m astral_stock_agent.cli examples/astral_activity.json --activity --us-symbol SPY
```

실제 MCP 응답 구조를 바탕으로 만든 **합성 예제**다. 실제 계좌·거래 원본은 공개하지 않는다.
`activity.collect_activity`는 인증된 `call_tool(name, arguments)` 비동기 함수를 주입받는다.
계좌/배포를 조회해 미국 주식 Paper 대상을 검증한 후에만 해당 계좌/전략의 주문과
거래 이력을 조회한다. OAuth 연결을 자동으로 가져오는 기능은 아니다.
실제 Work 도구 호출 브리지와 정기 운영 호스트 연결은 아직 남아 있다.

`examples/astral_activity.json`은 네 호출의 도구명·인자·응답 수신 시각·구조화된 원본
응답을 담는 **프로젝트 내부 전달 형식**이다. Astral의 새 API 형식으로 주장하지 않는다.
`--us-symbol`에는 Astral의 지원 심볼 확인 등을 거친 미국 주식/ETF 목록만 넣어야 한다.
미리보기는 그 목록의 금융상품 분류를 독립적으로 확인해주지 않는다.
`collect_session_activity`는 최근 종료 NYSE 정규장을 선택하고 실제 개장·폐장 시각으로
조회 범위를 만든다. 명시한 날짜가 휴장/미종료이면 외부 조회 전에 차단한다.
장 마감 시각과 정확히 같은 체결도 조회 범위에서 누락하지 않는다.

중요한 원본 차이:

- Paper 주문에도 `source=live`가 올 수 있다. 계좌 유형, provider, 전략 실행 모드를 함께 검사한다.
- 주문 이력에는 intent ID가 있지만 거래 이력에는 주문/체결 ID가 없다. 시간·수량·가격으로
  임의 연결하지 않으며 동일한 거래 행도 임의 중복 제거하지 않는다.
- 주문 상태 `FILLED`를 요청 수량과 곱해 추정 체결/손익으로 만들지 않는다.
- 원본 source_id는 조회 도구마다 의미가 다르다. 거래 이력은 saved strategy ID이며,
  equity curve의 source_id는 배포 ID로 관찰되므로 서로 바꿔 쓰지 않는다.
- 최신 응답 수신은 최신 시세·장부를 의미하지 않는다. 실제 equity curve 응답에서도
  캐시 지연이 관찰되어, 계좌 손익이나 잔고를 신선한 값처럼 채우지 않는다.

잘린 응답·행 수 불일치·조회 상한 200건 도달·잘못된 계좌/전략/심볼·비정상 수치·
오래되거나 서로 1분 넘게 벌어진 응답은 차단한다. 조회구간은 UTC 등 명시적 시간대의
[start, end)이며 임의 조회 자체는 휴장 여부나 일일 전체 거래의 완전성을 증명하지 않는다.
세션 수집기를 사용하면 예정된 정규장 일정만 별도로 검증된다.
행이 0개여도 당일 체결 없음으로 단정하지 않는다. 활동 미리보기는
`ledger_reconciled=false`, `complete_daily_snapshot=false`이며 Telegram에 전송할 수 없다.

## 일일 장부 대조 미리보기

```bash
python -m astral_stock_agent.cli examples/daily_reconciliation.json --reconcile --us-symbol SPY
```

위 파일은 계산 검증용 합성 예제이며 실제 Astral 원장/응답이 아니다. 합성 예제는 전송할 수 없다.
`--reconcile`은 기본적으로 미리보기다. 운영 전송에는 신뢰된 수집기가 준비한 비공개
전/현 종가 장부와 기간 회계 근거를 사용해 `--reconcile --send`로 실행해야 한다.
`--send`만으로 단일 스냅샷을 보내는 경로는 차단한다. 출처 라벨만 바꾸는 것은
실제 수집·검증을 대신하지 않는다. 실제 공식 데이터와 Telegram 수신의 종단 간 연결은 미검증이다.

`reconciliation.reconcile_daily`에는 신뢰된 정규화기가 수집·보관한 다음 자료가 필요하다.

- 전 거래일과 당일의 정확한 정규장 폐장 시점 스냅샷(기존 보고 schema v1).
- 세 자료 모두 일치하는 account_ref / strategy_ref / deployment_ref / run_ref.
- 완전한 session_accounting 내부 계약: 시작/끝 시각, 수신 시각, 순자금 유입,
  기타 순수익, 체결 수수료, `pnl_basis=net_of_execution_fees` 및 출처.
- 공식 식별자로 연결된 당일 주문/체결, 승인된 미국 종목 목록, 수수료를 포함한
  누적 실현 손익 및 현재 미실현 손익.

회계 자료의 null/누락을 0으로 채우지 않는다. 기존 거래 관측 미리보기에는 공식 체결
연결 ID와 완전한 수수료/입출금 근거가 없으므로, 이를 장부로 승격하거나 가짜 원장을
만들어 입력하지 않는다. kind/source/complete 표시는 인증이나 원천의 진위 증명이 아니다.
일치하는 세 자료를 만들어낸다는 사실만으로 원천 검증이 완료되는 것도 아니다.
공식 자료 확보와 영속 보관·출처 검증은 별도 수집기 책임이다.

검사하는 관계:

- 기말 현금 = 기초 현금 + 순자금 유입 + 기타 순수익 + 매도대금 - 매수대금 - 수수료.
- 기말 수량 = 기초 수량 + 매수 체결 - 매도 체결. 수량은 허용오차 없이 대조한다.
- 당일 총손익 = 기말 평가액 - 기초 평가액 - 순자금 유입.
- 총손익 = 순실현 손익 변화 + 미실현 손익 변화 + 기타 순수익.

금액 오차는 센트 단위 반올림을 고려해 $0.01 이내만 허용한다. 수수료는 이미
손익 기준에 반영되므로 총손익에서 다시 차감하지 않는다. FIFO/세금/원가를 임의 계산하지
않고 공식 순손익 장부를 대조한다. 분할·주식 현물 이전·전일 미완결 부분 체결 등의 별도
근거가 필요한 사례, 누락된 기초 장부와 재배포/다른 실행 회차는 자동 보정하지 않는다.
원본 장부와 거래 기록을 변경하지 않고 대조한 보고 복사본만 반환한다.

## 기록된 전략 검증 자료 재현

```bash
python -m astral_stock_agent.cli audit-research private/research.json private/prices.csv --us-symbol SPY
```

`audit-research`는 완전한 일봉 CSV 검증 → 정규장 누락 검사 → 고정 수량 SMA 후보의
다음 거래일 시가·비용 계산 → Astral 전체 곡선 대조를 연결한다. 네트워크 호출이나
백테스트 재실행 없이 기록된 자료를 사용하며 입력을 수정하거나 원장을 만들지 않는다.
Public Actions에서는 파일을 읽기 전에 차단하며 전송·배포 옵션을 받지 않는다.
실제 입력과 출력은 비공개 연구 자료로 취급한다. 사용자가 직접 실행할 설정 작업은 아니다.

비공개 manifest v1의 필드는 다음과 같다. 이는 프로젝트 내부 전달 형식이며 Astral API가 아니다.

| 필드 | 내용 |
| --- | --- |
| `version` | 정수 `1` |
| `price_response` | CSV와 함께 받은 `astral_price_get`의 구조화 응답. 체크섬·행 수·일봉 메타데이터 필요 |
| `backtest_response` | 정확한 성공 실행의 전체 비축약 `astral_backtest_get` 응답. MCP `structuredContent` 포장도 허용 |
| `expected_run` | `backtest_id`, 정수 `strategy_id`, 명시한 정수 `first_compared_index` |
| `research` | `symbol`, `start`, `end`, `first_day`, `last_day`, `trade_from`, `window`, `units`, `initial_cash`, `commission_bps`, `slippage_bps`, `label_convention` |

연구 날짜는 ISO 형식이며 `start/end`는 시간대 포함 범위, 나머지 날짜는 `YYYY-MM-DD`다.
금액·수량·비용은 명시한 십진 문자열 또는 정수, `window`는 정수다.
`label_convention`은 `session_date_at_utc_midnight` 또는
`session_date_at_new_york_midnight`를 원자료에 맞게 명시한다.
원자료를 자르거나 비교 경계를 추측하지 않는다. 비교 시작 인덱스는 외부의 첫 판단 가능
라벨 및 로컬의 첫 판단 가능 종가와 같아야 한다. 다른 워밍업이면 별도 연구 가정으로
`trade_from`을 명시해야 하며, 그 조정이 전략 동등성의 증거가 되지는 않는다.

출력은 비교/불일치 건수·최대 금액 차이·가정 체결 건수·마지막 미실행 결정 유무와
검증 한계만 포함한다. 원본 시세·전략/계좌 ID는 출력하지 않는다.
전체 곡선 일치 시 종료 코드0, 불일치 또는 불완전한 자료는2다.
코드0도 외부 규칙·비용·기업행위·실제 Paper 체결이나 배포 승인을 뜻하지 않는다.
연구 설정은 입력자의 가정이며 외부 실행의 설정과 일치하는지는 별도 검증 대상이다.
마지막 종가 결정을 체결로 바꾸거나 날짜를 이동시켜 곡선을 맞추지 않는다.

## 운영 경계

Astral이 전략 실행·주문·체결을 담당한다. Python 보고 모듈에는 주문, 배포,
계좌 연결 API가 없다. 공식 MCP는 Work의 인증된 연결을 통해 호출한다.
Python/CI가 ChatGPT의 OAuth 인증을 사용할 수 있다고 가정하지 않는다.

현재 MCP 자동 호출 브리지, 잔고·체결 대조를 완료하는 일일 정규화기, 비공개 영속 운영 호스트,
Telegram 인증정보는 연결되지 않았다. `source=astral_mcp`는 출처 메타데이터이며
서명이나 진위 증명이 아니다. 신뢰된 수집기가 검증한 입력만 받아야 한다.
스냅샷의 `session_verified=true`만으로 승인하지 않는다. 보고 코드가 독립적으로
NYSE 캘린더의 실제 예정 폐장과 입력 폐장 시각을 대조하며, 정규장 밖 체결을 차단한다.
무료 공개 패키지 `exchange-calendars==4.13.2`를 사용하며 검증 범위는 2026~2028년이다.
범위 밖 날짜나 캘린더 오류는 차단한다. 정규장 일정은 거래소/종목의 실시간 거래 중단,
임시 휴장 공지, 시세·장부의 최신성이나 체결 완전성을 확인한 증거가 아니다.
운영 전 최신 거래소 공지와 해당 심볼·계좌 상태를 별도로 확인해야 한다.

운영 입력 계약은 `examples/paper_snapshot.json`과 `report.py`를 따른다.
`orders`와 `fills`는 해당 거래일 주문·체결만 대상으로 한다. 전날부터 이어진
주문의 누적 체결을 처리하는 정규화는 아직 미구현이므로 불일치하면 차단한다.
`cash_usd`는 단순 주문 가능 금액이 아니라 평가액 대조에 사용하는 동일 범위의
현금 잔액이다. 별도 구매력이나 전략 배정액을 현금 잔액으로 대체하면 안 된다.
`available_cash_usd`는 주문 예약 등을 제외한 같은 범위의 실제 사용 가능 현금이며,
미확인이면 null로 둔다. 현금 잔액과 별도로 표시하고 임의로 추정하지 않는다.

운영 전송은 신뢰된 **비공개** 실행 환경에서만 한다.
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`는 해당 환경의 비밀 설정으로 제공한다.
채팅·저장소·Public Actions secrets에 거래 보고용 운영 인증정보를 넣지 않는다.
개인 숫자 채팅 ID만 허용하고, 봇 대화 시작과 수신인 확인 후 연결한다.

### 수동 Telegram 초기 설정 — 설정 전용 예외 승인

사용자는 별도 안내에 따라 `TELEGRAM_BOT_TOKEN`을 이 저장소의 Actions Secret으로
등록했다고 알렸다. 현재 Work에는 해당 값이 주입되지 않으며, 연결 앱으로 Secret 값을
읽어 올 수 있다고 가정하지 않는다. 위 비공개 운영 정책과 안내의 불일치를 바로잡기 위해
`.github/workflows/telegram-setup.yml`을 준비했다. 사용자는 2026-11-09 보안 검토 후
진행하도록 승인했고, 검토에서 이 범위의 중대한 노출/오발송 문제를 발견하지 않았다.
이에 설정 전용 병합/수동 실행을 진행한다. 기존 `ci.yml`은 비밀정보 없이 유지한다.

병합 후 절차:

1. 본인이 만든 봇의 개인 대화에 `/astral_setup`을 보낸다.
2. 10분 안에 저장소 Actions의 `Telegram setup (manual only)`에서 main을 선택해 수동 실행한다.
3. 봇이 해당 대화에 돌려준 숫자를 `TELEGRAM_CHAT_ID` Actions Secret으로 등록한다.
   토큰·ID를 채팅, 공개 로그, PR 또는 이슈에 붙여넣지 않는다.

이 작업은 저장소 소유자의 main 수동 실행만 허용한다. 체크아웃·외부 액션·의존성 설치·
아티팩트·GitHub 쓰기 권한이 없다. 토큰은 한 단계에만 주입한다.
공식 `getWebhookInfo/getMe/getUpdates`를 각각 한 번 조회한다. 최근 메시지 응답에는
다른 대화의 내용도 포함될 수 있으나 메모리에서만 검사하며 원문/ID/이름/오류/URL을
기록하지 않는다. offset·allowed_updates·웹훅 설정을 변경하지 않는다.
웹훅이 있거나 큐가 상한에 도달하면 중단한다. 10분 이내 직접 입력한 명령이 있는 개인
대화가 정확히 하나일 때만 그 대화에 설정 ID 안내를 한 번 보낸다. 전달/그룹/봇 메시지는
대상에서 제외하며 설정을 자동 저장하지 않는다. API 오류나 불명확한 발송은 재시도하지 않는다.

사용자 승인은 설정용 안내에 한정된다. 계좌/매매 데이터·실제 거래 보고·주기 실행·운영 원장과
기존 CLI의 Public CI 차단은 변경하지 않는다. 성공 로그도 실제 수신이나 자동 보고 준비를
증명하지 않는다. 실제 봇 대화에서 안내를 확인한 뒤 다음 운영 단계를 진행한다.

설정 실패 로그에는 고정된 진단 단계와 안내만 출력한다. Secret 누락/잘못된 토큰 형식,
Telegram 인증·접근·충돌·요청 제한, 기존 웹훅, 명령 미수신/만료, 다중 대화/큐 상한을 구분한다.
토큰 복사 때 붙은 앞뒤 공백·줄바꿈은 제거하되 토큰 중간 문자나 따옴표를 고치지 않는다.
토큰·수신 ID·사용자 이름·대화 내용·원문 오류·URL은 출력하지 않는다.
`전송 시도: 있음`이면 Telegram 대화를 먼저 대조하며 자동/맹목 재실행하지 않는다.
새 코드 실행은 이전 실패 작업의 Re-run이 아니라 최신 main의 **Run workflow**로 시작한다.
`전송 시도: 없음`이고 명령 미수신이면 본인 봇에 새 `/astral_setup`을 보낸 뒤 수동 실행한다.

`TELEGRAM_CHAT_ID` 등록 후에는 Actions의 **Telegram connection check (manual only)**를
main에서 수동 실행한다. 새 명령이나 메시지 전송 없이 등록값을 검사한다.
이 작업은 소유자 main 수동 실행에서 두 Secret을 한 단계에 주입하고 공식 getMe/getChat만
각각 한 번 호출한다. 체크아웃·외부 액션·설치·아티팩트·쓰기 권한·자동 재시도는 없다.
숫자 ID와 실제 응답의 정확한 ID/private 유형을 대조하며, 잘못된 설정은 조회 전에 거부한다.
로그에는 고정 오류나 확인값 네 개만 남는다. bot_authenticated/private_chat_verified가
true여도 send_permission_verified/delivery_verified는 false다. 사람의 신원이나 실제
수신·영속 운영 준비·일일 자동 보고 완성을 증명하지 않는다. 운영 CLI의 CI 차단은 유지한다.

운영 환경의 비밀 설정을 주입한 뒤 아래 명령으로 메시지 없이 연결을 점검한다.
토큰이나 수신 ID를 명령 인자로 전달하지 않는다.

```bash
python -m astral_stock_agent.cli check-telegram
```

공식 getMe/getChat만 각각 한 번 호출한다. 대화 내용/getUpdates·메시지 전송·원장 접근·
자동 재시도가 없으며 Public CI에서는 비밀 설정을 읽기 전에 차단한다.
봇 인증과 설정된 개인 대화의 정확한 ID/유형만 확인하고 이름/ID/URL/원문 오류는 출력하지 않는다.
공유 전송기의 수신 ID 검사도 개인용 양의 숫자·공식52비트 범위로 제한한다.
잘못된 명령 인자는 값을 되비추지 않고 고정 오류로 거부한다.
성공은 종료 코드0과 두 확인값을 반환하지만 send_permission_verified/delivery_verified는
항상false다. 사람의 신원·발송 허용 여부·수신·영속 원장·운영 준비를 증명하지 않는다.
설정 누락/조회 실패는 고정 문구와 코드2이며 실제 전송/원장 상태에 영향을 주지 않는다.

현재 Work 실행 환경에는 위 비밀 설정이 없고 비공개 영속 운영 환경도 확인되지 않았다.
그 상태를 임시 파일/공개CI로 대체하지 않는다. 봇 생성 여부가 미확인이면 새 봇을 중복 생성하지 않는다.

운영자가 영속 볼륨과 백업을 확인한 뒤, 소유자 전용 디렉터리(0700)에 원장을 명시적으로 준비한다.
다음 명령은 Telegram 인증정보를 읽거나 메시지를 보내지 않는다. Public CI에서는 실행을 차단한다.

```bash
python -m astral_stock_agent.cli init-outbox --database /private-volume/reports.sqlite
python -m astral_stock_agent.cli private/daily_reconciliation.json --reconcile --us-symbol SPY --send --database /private-volume/reports.sqlite
```

새 원장은 0600으로 생성하며, 기존 파일·디렉터리 권한을 자동 변경하지 않는다.
심볼릭 링크·하드 링크·공유 읽기 파일·안전하지 않은 상위 경로는 거부한다.
기존 원장의 상태를 지우지 않으며, 전송 경로는 누락/빈 원장을 자동 생성하지 않는다.
실행 중 파일이 사라지거나 교체돼도 전송을 차단한다. 분실 원장을 새로 만들어 재전송하지 말고,
먼저 기존 전송 기록과 백업을 대조해야 한다. 이 검사는 POSIX 파일 안전성 검사이며
영속 볼륨·암호화·백업이 준비됐다는 증명은 아니다.

SQLite 파일은 실행 간 유지되는 비공개 볼륨에 있어야 한다. 삭제·초기화하거나
작업마다 새 파일을 만들면 중복 방지가 사라진다. 운영 볼륨이 없으면 실제 전송을
활성화하지 않는다. 현재 실행 호스트의 임시 파일을 영구 저장소라고 간주하지 않는다.

운영 전송은 [Telegram sendMessage 문서](https://core.telegram.org/bots/api#sendmessage)의
본문 제한을 고려해 로컬에서 먼저 검사한다. 현재 구현은 plain text만 전송하고,
보수적으로 UTF-16 코드 단위 4096개까지 허용한다(일부 이모지는 2개로 계산).
빈 본문·잘못된 Unicode·초과 본문은 전송이나 원장 선점 전에 차단한다.
미리보기는 전체 보고서를 유지하며, 전송을 위해 내용을 자르거나 자동 분할하지 않는다.
기존 원장의 잘못된 pending 본문도 상태를 바꾸지 않고 차단한다.

장애 점검은 다음 읽기 전용 명령으로 상태별 건수만 확인할 수 있다.

```bash
python -m astral_stock_agent.cli inspect-outbox --database /private-volume/reports.sqlite
```

기존 비공개 원장을 SQLite `mode=ro`로 열며 생성·선점·전송·복구하지 않는다.
본문·보고 키·메시지 ID·경로는 출력하지 않는다. `sending/unknown`이 있으면
`operator_reconciliation_required=true`와 종료 코드 2를 반환한다. 정상 조회는 0,
저장소 오류도 2이며 고정 오류 문구로 구분한다. 결과는 조회 시점의 집계다.
0이나 빈 원장은 수신/운영 준비의 증명이 아니다. Public CI의 운영 조회는 차단한다.

Telegram API 승인 응답은 사람의 수신·열람 확인이 아니다. `sending` 또는
`unknown` 상태는 중복 발송 방지를 위해 자동 복구하지 않는다. 수신 메시지와
Telegram 기록을 사람이 대조하고 복구 정책을 구현한 뒤 처리한다. 현재 도구에는
재전송/기존 원장 삭제·전송 상태 초기화 기능이 없으며, 자동 정확히 1회 전송을 보장하지 않는다.

## 연구 후보 문법 미리보기

`authoring.sma_preview_request`는 별도로 검증한 미국 주식 심볼과 명시적 연구 수량을 받아
자체 SMA 후보의 `validate_only=True` 요청만 만든다. `validate_sma_preview`는 주입받은
인증된 호출 함수로 `astral_strategy_build`를 한 번 호출한다. 저장·백테스트 시작·배포·
주문 API와 자동 재시도는 없으며, Python/CI용 인증정보를 획득하지 않는다.

실제 미리보기에서 확인한 입력은 signal의 `expr`, rule의 `actions`,
order의 `sizing_type=fixed_shares`와 `sizing_value`다. 수량을 생략하면 지분 비율 기본값이
들어갈 수 있으므로 항상 명시한다. 이번 SPY/SMA20/1주 검증은 문법 확인용이며 운영 수량
승인이 아니다. 응답의 정규화된 조건·주문 수량·누적 여부·엔진·암묵 가정을 엄격히 대조하고
저장 식별자가 있거나 계약이 바뀌면 차단한다. 반환물에는 원본 코드나 계좌 정보가 없다.

실제 응답의 `fill_price_mode=on_open`은 그대로 기록한다. 이 이름만으로 다음 거래일
시가 체결·로컬 전략과의 전체 일치·수익성을 검증했다고 하지 않는다.
`full_strategy_parity_verified`, `deployment_approved`, `new_orders_allowed`는
항상 false다. 기존 체결 모드 검증기를 완화하지 않으며, 이전 손실 후보의 보류도 유지한다.

## 연구용 일별 평가액과 낙폭

`simulate_next_open`은 모든 검증된 거래일 종가에 현금·보유 수량·평가액을 기록한다.
`daily_marks`의 `bar_index`는 입력 일봉의 인덱스이며 실제 체결 시각이 아니다.
평가액은 현금 + 수량 × 종가다. 가정한 수수료와 불리한 슬리피지는 현금에 한 번 반영한다.
거래 없는 날과 매도 후 현금만 남은 날도 기록하고, 마지막 보유분은 강제 청산하지 않는다.

`max_close_drawdown_fraction`은 초기 가상 현금 및 이후 종가 평가액의 누적 최고점 대비
하락률의 최댓값이다. 0.5는 50%다. 같은 날의 고가/저가·시가 갭·거래 중 최대 손실을
측정하지 않으며, 가상 시작 현금과 주문 수량에 따라 값이 달라진다.
배당·분할 등은 여전히 미검증이다. 이 가격 기반 연구 수치는 실제 Paper 손익이나
전체 전략 동등성·위험 한도 승인·배포 적합성의 증거가 아니다. 자동 합격 기준은 없다.

## 완료된 Astral 백테스트 곡선 대조

`research_audit.compare_backtest_curve`는 정확한 완료 실행 ID/전략 ID와 전체 일별
곡선을 로컬 `daily_marks`에 대조한다. 비교 시작 인덱스는 명시하며 외부 실행의 첫
판단 가능 라벨과 일치해야 한다. 워밍업·신호·날짜를 자동 보정하지 않는다.
누락/추가/중복 라벨, 다운샘플링·잘린 응답·불완전 기간·비정상 실행은 거부한다.
각 날짜의 USD 평가액을 비교하므로 최종 수익이 같아도 중간 회계 차이를 드러낸다.
직렬화 오차 허용은 고정 0.0000001 USD다. 결과에는 비교 건수·차이 건수·최대 차이만
남고 원본 행/식별자는 없다. 곡선 일치도 전체 전략 동등성이나 배포 승인이 아니다.

2026-11-09 실제 자체 SPY/SMA20/1주 연구의 완료 곡선에서 체결 라벨/마지막 봉 처리
불일치를 관측해 Astral에 보고했다. `on_open`을 다음 거래일 종가 회계의 일치 증거로
매핑하지 않으며 후보 배포를 보류한다. 집계와 한계는 프로젝트 상태에 기록한다.

## 다음 작업

[프로젝트 상태](docs/STATUS.md), [아키텍처](docs/ARCHITECTURE.md),
[개발 지침](AGENTS.md)을 먼저 읽는다. 실거래는 별도 승인 단계다.
