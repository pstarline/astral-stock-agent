# 네이버 클라우드: 모바일 설치와 운영 점검

사용자 2026-11-16 선택. 자체 엔진을 사용하며 Astral 회신/구독은 필요하지 않다.
이 문서는 설치 준비이며 서버 생성·결제·실제 Paper 운영/Telegram 수신 증명이 아니다.
Render 설정은 참고용으로 남기고 양쪽 Worker를 동시에 활성화하지 않는다.

## 1. 모바일에서 먼저 할 일

1. https://www.ncloud.com 에서 한국 개인 계정을 만들고 국내 결제수단을 등록한다.
2. 콘솔의 한국 리전/VPC에서 Linux 서버를 검토한다. 초기 후보는 Micro-g3,
   1vCPU/1GB/기본 CB1/2 10GB, Ubuntu 22.04 또는 24.04다. 실제 제공 이미지와
   무료 대상/남은 기간/최종 견적을 확인한 뒤 생성한다. Micro의 성능·가용성은
   보장되지 않으며 이 프로그램의 실제 메모리 충분성은 아직 검증하지 않았다.
3. Public Subnet과 공인 IP 1개로 직접 인터넷을 이용한다. 별도 NAT Gateway,
   로드밸런서, 유료 DB, 도메인은 이 구성에 필요하지 않다. 생성 화면의 추가
   유료 옵션을 확인한다. 서버 무료 기간에도 IP/초과 통신/추가 디스크는 별도다.
4. SSH용 인증키는 개인의 안전한 위치에 보관한다. 채팅/GitHub에 올리지 않는다.
   ACG/Network ACL에서 관리용 SSH는 본인 접속 IP만 허용한다. 봇 웹 포트는
   열지 않는다. 외부 HTTPS와 DNS 등 필수 통신은 허용해야 한다.
5. 개인 휴대전화의 신뢰하는 SSH 클라이언트에서 접속한다. 해당 앱의 키 보관,
   비용, 호스트 키 지문을 확인한다. 군용 PC의 보안 제한을 우회하지 않는다.
   통신망/IP 변경 시 ACG 허용 IP를 바꾼다. 일반 서버 접속은 SSH이며 콘솔의
   장애복구용 접속 화면을 상시 브라우저 터미널로 가정하지 않는다.

한 단계씩 진행해도 된다. 화면 공유 시 인증키/비밀번호/카드/토큰/계좌 값은 가린다.
현재 연결 도구로 네이버 계정/서버에 직접 접근하는 기능은 확인되지 않았다.
사용자에게 API 키나 SSH 개인키를 채팅으로 보내도록 요청하지 않는다.

## 2. 서버 접속 후 설치 준비

아래는 새 전용 Ubuntu 호스트용이다. 기존 서버/복구 디스크에서는 실행하지 않는다.
먼저 공식 Docker Ubuntu 설치 안내에 따라 Docker Engine을 설치한다:
https://docs.docker.com/engine/install/ubuntu/ . 비공식 설치 스크립트를 실행하지 않는다.
`git`과 `flock`(util-linux)도 필요하다. 일반 사용자를 docker 그룹에 추가할 필요는 없다.

```bash
sudo apt-get update
sudo apt-get install -y git util-linux
git clone https://github.com/pstarline/astral-stock-agent.git
cd astral-stock-agent
git log -1 --format=%H
```

출력 커밋이 검토·CI 통과한 배포 대상인지 확인한다. 실행 전
`deploy/ncloud/prepare.sh`, Dockerfile, service를 검토한다. 최신 코드를 자동 pull하는
예약은 만들지 않는다. 스크립트는 초기 설치 전용이며 기존 경로/이미지를 덮어쓰지 않는다.

```bash
sudo bash deploy/ncloud/prepare.sh
```

Python3.11 이미지와 기존 앱을 로컬 빌드하고 UID/GID10001의 0700 저장소를 명시
초기화한다. 기존 UID/GID와 충돌하면 중단한다. Secret 없는 기본 설정은
`/etc/astral-paper/paper.env`(root 0600), 원장은 `/var/lib/astral`에 둔다.
서비스는 시작/활성화하지 않는다. 설치 중단 시 파일을 지우거나 스크립트를 반복하지 말고
부분 설치 상태를 대조한다. 이미지 빌드 공간 부족/OOM은 실제 호스트에서 확인해야 한다.

## 3. 영속 저장소 점검과 비공개 설정

```bash
sudo docker run --rm --pull=never --network=none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges:true --user=10001:10001 \
  --mount=type=bind,src=/var/lib/astral,dst=/var/lib/astral \
  astral-paper:reviewed python -m astral_stock_agent.host_storage inspect --directory /var/lib/astral
```

점검은 네트워크 없이 디렉터리 권한/초기화 완료/Worker 미완료 시도/Outbox 상태만
읽는다. 전체 회계/시세 진위/영속성/실제 수신 검증을 대신하지 않는다. 재부팅 후 같은
점검을 반복해 원장이 유지되는지 대조한다. 백업은 가격/원장/control/Outbox 전체를
Worker를 중지한 상태로 함께 보관한다. 백업 저장소 비용/접근은 별도 검토 대상이다.

`sudo nano /etc/astral-paper/paper.env`에서 기존 무료 시세 키/Telegram 값을 서버에
직접 입력한다. Docker env-file은 값 전체를 그대로 읽으므로 shell의 export/eval/source를
사용하지 않고 바깥 따옴표도 붙이지 않는다. PAPER_CONFIG_JSON은 한 줄 JSON이다.
키/채팅 식별자/가격/원장을 채팅·공개 로그·GitHub에 복사하지 않는다.

명시 후보 규칙·비용·가상 현금/수량·미국 종목·bootstrap 거래일은 별도 검증 후 설정한다.
이 가이드는 운영 전략이나 금액을 선정하지 않는다. 검증/승인 플래그는 근거 자체가 아니다.
기본 WORKER/CANDIDATE_APPROVED/REPORTING 세 값은 false로 유지한다.

## 4. 검증 후 별도로 활성화

저장소·실제 시세·후보 설정·전송 권한을 확인한 다음만 세 플래그를 true로 바꾼다.
초기 bootstrap 날짜는 실제 시작 거래일이어야 한다. 과거 날짜로 forward 실적을 만들지 않는다.

```bash
sudo systemctl start astral-paper
sudo systemctl status astral-paper --no-pager
sudo journalctl -u astral-paper -n 20 --no-pager
```

NYSE 폐장90분 뒤 오늘 거래일에 한 번 수집/기록/신규 보고를 시도한다. 지연 데이터나
실패는 중단한다. `active`만으로 운영 성공을 판단하지 않고 실제 저장된 거래일·큐 상태·
사용자의 Telegram 일일 수신을 대조한다. 최초일은 현금 기준선이며 가상 체결0일 수 있다.
실제 첫 실행과 복구를 검증한 뒤에만 부팅 시 시작을 설정한다:
`sudo systemctl enable astral-paper`. Docker/systemd 자동 재시도는 설정하지 않았다.
기존 Worker의 실패 latch와 영속 attempt 차단을 유지한다. 서비스가 살아 있어도
`halted`일 수 있으므로 로그와 상태 점검이 필요하다.

중지: `sudo systemctl stop astral-paper`. 코드 갱신/컨테이너 교체/서버 복원 때는 먼저
중지하고 원장/Outbox를 대조한다. 기존 컨테이너 이름 충돌을 자동 삭제하지 않는다.
pending/sending/unknown은 수신 여부를 확인하기 전 재전송하지 않는다. 스냅샷 복원은
이미 전송된 보고 상태를 되돌릴 수 있으므로 자동 재가동하지 않는다.

## 공식 근거와 남은 검증

- https://guide.ncloud-docs.com/docs/server-spec-vpc — Micro 제한/기본 저장소
- https://guide.ncloud-docs.com/docs/server-access-vpc — SSH/공인 IP/ACG
- https://www.ncloud.com/charge/price/ko — 최종 요금 확인
- https://docs.docker.com/reference/cli/docker/container/run/ — env-file/bind/권한

실제 네이버 서버 생성, Docker 빌드/기동/UID 파일 접근, 재부팅 영속성, 메모리/디스크
사용량과 실제 일일 Telegram 수신은 미검증이다. Public CI에서는 운영 비밀/실제 전송을
사용하지 않는다. 개발은 매시간이며 공개 전략 주간 탐색은 MVP 이후 미활성이다.
