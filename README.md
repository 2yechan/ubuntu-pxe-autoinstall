# Ubuntu 26.04 LTS 네트워크 무인 설치 컨테이너

단일 Docker 컨테이너로 **DHCP + PXE(UEFI) + HTTP + autoinstall** 을 제공한다.
같은 L2 네트워크의 빈 서버들을 전원만 넣으면 Ubuntu 26.04 LTS 가 설치되고,
설치가 끝나면 지정한 계정으로 SSH 접속과 `sudo` 가 가능한 상태가 된다.

서버를 켠 순서대로 `DHCP_RANGE` 시작 주소부터 IP 가 순차 할당되고,
그 IP 로부터 호스트명이 결정된다 (`192.168.101.1` → `node01`, `.45` → `node45`).

```
       빈 서버                              이 컨테이너 (--network host)
  ┌──────────────┐                   ┌──────────────────────────────────┐
  │ UEFI PXE ROM │──DHCP DISCOVER──▶ │ kea-dhcp4   IP 순차 할당         │
  │              │◀─offer+bootfile── │             next-server/bootfile │
  │              │──TFTP shimx64────▶│ tftpd-hpa   서명된 부트 체인     │
  │  shim        │──TFTP grubx64────▶│                                  │
  │  grubnetx64  │──TFTP grub.cfg───▶│                                  │
  │              │──HTTP vmlinuz/────▶│ nginx      ISO/커널/initrd      │
  │  installer   │──HTTP user-data──▶│ python     MAC/IP별 렌더링       │
  │              │──HTTP /done/mac──▶│            이후 로컬 디스크 부팅 │
  └──────────────┘                   └──────────────────────────────────┘
```

---

## 1. 요구 사항

* 이 컨테이너를 돌릴 호스트가 대상 서버들과 **같은 L2 세그먼트**에 있어야 한다.
* `--network host` 와 `--cap-add NET_ADMIN` 이 필요하다.
  (DHCP 는 L2 브로드캐스트를 봐야 하고, Kea 는 raw 소켓을 쓴다.)
* 호스트의 **67/udp, 69/udp, 80/tcp** 가 비어 있어야 한다.
  nginx 는 `SERVER_IP` 주소에만 바인딩하므로, 호스트의 다른 주소에서
  웹 서버를 돌리고 있다면 충돌하지 않는다.
* `ubuntu-26.04-live-server-amd64.iso` 를 읽기 전용으로 마운트한다.
  ISO 는 이미지에 포함하지 않는다.
* 이미지 **빌드 시점에는 인터넷이 필요**하다 (Ubuntu 아카이브).
  **설치 시점에는 인터넷이 필요 없다.**

---

## 2. 환경변수

### 필수 — 하나라도 없거나 형식이 틀리면 명확한 에러를 찍고 즉시 종료한다

| 변수 | 형식 | 예 | 설명 |
|---|---|---|---|
| `SUBNET` | CIDR | `192.168.101.0/24` | DHCP 서브넷 |
| `SERVER_IP` | IPv4 | `192.168.101.253` | 이 호스트의 IP. TFTP `next-server` 와 모든 HTTP URL 의 기준 |
| `DHCP_RANGE` | `START-END` | `192.168.101.1-192.168.101.45` | 순차 할당 범위 |
| `USER_NAME` | 리눅스 사용자명 | `ubuntu` | 설치될 uid 1000 사용자 |
| `USER_PASSWORD` | 문자열 | `hunter2` | 위 사용자의 비밀번호 |

### 선택 — 값이 없으면 해당 설정 자체를 생략한다

| 변수 | 기본값 | 설명 |
|---|---|---|
| `GATEWAY` | (없음) | 있을 때만 DHCP `routers` 옵션을 내보낸다 |
| `DNS` | (없음) | DHCP `domain-name-servers` 옵션. **사실상 필수** — 없으면 설치된 서버가 매 부팅마다 2분씩 멈춘다(§6.7). 쉼표로 복수 지정 |
| `DHCP_INTERFACE` | 자동 탐지 | `SUBNET` 안의 IP 를 가진 호스트 인터페이스를 자동으로 찾는다 |
| `SSH_PUBKEY` | (없음) | 있으면 `authorized-keys` 에 추가 |
| `DISK_SIZING_POLICY` | `all` | `all` 또는 `scaled` |
| `ISO_PATH` | `/iso/ubuntu-26.04-live-server-amd64.iso` | 컨테이너 안의 ISO 경로 |
| `BOOT_TRANSPORT` | `tftp` | GRUB 이 커널/initrd 를 받는 방식. `tftp` 또는 `http`. **`http` 는 쓰지 마라** — §7 참고 |
| `INSTALL_LOCALE` | `en_US.UTF-8` | 설치될 OS 의 locale |
| `INSTALL_KEYBOARD_LAYOUT` | `us` | 설치될 OS 의 키보드 레이아웃 |

### 기동 시 검증 항목

* `DHCP_RANGE` 의 시작/끝, `SERVER_IP`, `GATEWAY`(있을 경우)가 `SUBNET` 안에 있는가
* `DHCP_RANGE` 시작 ≤ 끝인가
* `SERVER_IP` 가 실제로 이 호스트의 인터페이스에 붙어 있는가
* `ISO_PATH` 파일이 존재하는가
* `DHCP_INTERFACE` 자동 탐지가 **유일한** 후보를 찾았는가 (여러 개면 에러)
* 생성된 Kea 설정이 `kea-dhcp4 -t` 를 통과하는가

에러는 **모두 모아서 한 번에** 출력한다. 하나 고치고 다시 돌리는 일을 반복하지 않아도 된다.

> `SERVER_IP` 와 `DHCP_RANGE` 가 겹치는지는 **검사하지 않는다** (사용자 책임).
> 겹치면 Kea 가 자기 주소를 클라이언트에 나눠 줄 수 있다.

---

## 3. 실행

아래는 "일단 띄워 보는" 방법이다. 실제 랙에 배포하는 순서는 §4 를 따라라.

### docker run

```bash
docker build -t ubuntu-pxe-autoinstall:26.04 .

docker run -d --name ubuntu-pxe-autoinstall \
  --network host \
  --cap-add NET_ADMIN \
  -v /srv/iso/ubuntu-26.04-live-server-amd64.iso:/iso/ubuntu-26.04-live-server-amd64.iso:ro \
  -e SUBNET=192.168.101.0/24 \
  -e SERVER_IP=192.168.101.253 \
  -e DHCP_RANGE=192.168.101.1-192.168.101.45 \
  -e GATEWAY=192.168.101.254 \
  -e DNS=192.168.101.254 \
  -e USER_NAME=ubuntu \
  -e USER_PASSWORD='change-me' \
  -e SSH_PUBKEY="$(cat ~/.ssh/id_ed25519.pub)" \
  -e DISK_SIZING_POLICY=all \
  ubuntu-pxe-autoinstall:26.04

docker logs -f ubuntu-pxe-autoinstall
```

### docker compose

`docker-compose.yml` 을 그대로 쓰면 된다. ISO 를 `docker-compose.yml` 옆에 두고:

```bash
docker compose up --build
```

### 정상 기동 로그

```
[entrypoint] Ubuntu 26.04 LTS PXE autoinstall appliance starting
[config] auto-detected DHCP_INTERFACE=eno1
[config] generated SHA-512 crypt hash for USER_NAME ubuntu (plaintext is not written anywhere)
[config] summary: subnet=192.168.101.0/24 pool=192.168.101.1-192.168.101.45 (45 hosts,
         node01-node45) iface=eno1 server=192.168.101.253 transport=http iso=...
[entrypoint] published signed boot chain: shimx64.efi -> grubx64.efi (grubnetx64 2.14-2ubuntu1)
[entrypoint] extracted vmlinuz (15M) and initrd (98M)
[entrypoint] Kea configuration OK
[entrypoint] starting supervisord (kea-dhcp4, tftpd-hpa, nginx, autoinstall-http)
```

설치가 진행되면 이런 로그가 순서대로 찍힌다:

```
... [autoinstall] INFO  192.168.101.1 GET user-data -> node #1 hostname=node01 mac=aa:bb:cc:dd:ee:01
... [autoinstall] INFO  192.168.101.1 install complete: mac=aa:bb:cc:dd:ee:01 hostname=node01
                        -> wrote /srv/tftp/grub/grub.cfg-aa:bb:cc:dd:ee:01;
                           this host will now boot from local disk
```

### 상태 확인

```bash
curl http://192.168.101.253/health                      # 서비스 + 설정 요약
docker exec ubuntu-pxe-autoinstall ls /srv/tftp/grub/   # 설치 완료된 MAC 목록
docker exec ubuntu-pxe-autoinstall cat /var/lib/kea/kea-leases4.csv   # 리스 현황
```

---

## 4. 실제 인프라 배포 절차

§3 은 이 컨테이너를 그냥 띄우는 방법이고, 여기서는 실제 랙에 굴릴 때의 순서를
다룬다. 각 단계에 실제로 밟았던 지뢰를 붙여 두었다.

### 4.1 반입물 준비 (인터넷 되는 곳에서)

```bash
docker build -t ubuntu-pxe-autoinstall:26.04 .
docker save ubuntu-pxe-autoinstall:26.04 | gzip > pxe-image.tar.gz      # 약 190MB
sha256sum pxe-image.tar.gz ubuntu-26.04-live-server-amd64.iso > MANIFEST.sha256
```

반입할 것은 세 개다. **ISO 는 이미지에 들어 있지 않으니 따로 챙겨야 한다.**

| 파일 | 크기 |
|---|---|
| `pxe-image.tar.gz` | 약 190MB |
| `ubuntu-26.04-live-server-amd64.iso` | 2.9GB |
| `MANIFEST.sha256` | - |

현장에서 설정을 고칠 수 있게 이 소스 디렉터리도 같이 가져가는 편이 좋다.

### 4.2 호스트 준비 (오프라인 망)

```bash
sha256sum -c MANIFEST.sha256
docker load < pxe-image.tar.gz

mkdir -p /srv/iso && cp ubuntu-26.04-live-server-amd64.iso /srv/iso/
chmod a+r /srv/iso/ubuntu-26.04-live-server-amd64.iso
```

`chmod a+r` 을 빠뜨리면 nginx 워커(www-data)가 ISO 를 못 읽어 `/iso/` 에 403 을
돌려준다. 기동 로그에 경고가 뜨긴 하지만 미리 해 두는 편이 낫다.

띄우기 전에 반드시 확인한다.

```bash
ip -o -4 addr show                     # SERVER_IP 가 이 호스트에 실제로 붙어 있어야 한다
ss -lntup | grep -E ':(67|69|80)\s'    # 비어 있어야 한다
```

그리고 **그 세그먼트에 다른 DHCP 서버가 없어야 한다.** 확신이 없으면 노트북을 그
VLAN 에 물려 `dhclient -v` 로 누가 응답하는지 먼저 확인하라. 다른 DHCP 가 먼저
응답하면 대상 서버가 범위 밖 IP 를 받고, 그러면 순번을 계산할 수 없어 시드 요청이
400 으로 떨어진다.

### 4.3 대상 서버 준비

여기서 대부분의 사고가 난다.

| 항목 | 값 | 안 지키면 |
|---|---|---|
| 펌웨어 | **UEFI** (Legacy/CSM 끄기) | option 93 이 `0x0000` 이라 부트파일을 주지 않는다. DISCOVER 만 반복하다 포기한다 |
| 부팅 순서 | **PXE(IPv4) 1순위** | 설치가 시작되지 않는다 |
| PXE 활성 NIC | **1개만** | 서버당 IP 를 두 개 먹어서 그 뒤 순번이 전부 밀린다 |
| RAM | **ISO 크기 × 2 이상 = 8GB** | `wget: short write: No space left on device` (§5 4번 항목) |
| 디스크 | 설치 대상이 명확할 것 | `match: size: largest` 가 엉뚱한 디스크를 고를 수 있다 |

NIC 이 여러 개인 서버라면, 쓰지 않는 포트의 케이블을 빼 두더라도 설치 후 매 부팅
`systemd-networkd-wait-online` 이 그것을 기다릴 수 있다 — subiquity 가 쓰는
netplan 에는 `optional: true` 가 없다. 파일럿에서 확인하라.

### 4.4 컨테이너 기동

```bash
docker run -d --name ubuntu-pxe-autoinstall \
  --network host --cap-add NET_ADMIN \
  -v /srv/iso/ubuntu-26.04-live-server-amd64.iso:/iso/ubuntu-26.04-live-server-amd64.iso:ro \
  -e SUBNET=10.20.30.0/24 \
  -e SERVER_IP=10.20.30.250 \
  -e DHCP_RANGE=10.20.30.1-10.20.30.45 \
  -e DNS=10.20.30.250 \
  -e USER_NAME=ops -e USER_PASSWORD='...' \
  -e SSH_PUBKEY="$(cat ~/.ssh/id_ed25519.pub)" \
  --log-opt max-size=50m --log-opt max-file=3 \
  ubuntu-pxe-autoinstall:26.04

docker logs -f ubuntu-pxe-autoinstall
```

* `DNS` 는 표기상 선택이지만 **넣어라.** 빼면 설치된 서버가 매 부팅 2분씩 멈춘다
  (§6.7). 진짜 resolver 가 없는 망이면 `SERVER_IP` 를 그대로 넣어도 된다 —
  응답할 필요 없이 "설정되어 있음" 만이 조건이다. 안 넣으면 기동 로그에 경고가 뜬다.
* `GATEWAY` 는 완전 오프라인이면 생략하는 편이 낫다. subiquity 가 아예
  "네트워크 없음" 으로 판단해 미러 검사를 건너뛴다. 게이트웨이는 있는데 인터넷이
  없는 망이라면 넣어라 — 그때 §6.6 의 즉시-404 트릭이 일한다.
* `SSH_PUBKEY` 를 넣으면 비밀번호 없이 들어갈 수 있어 이후 운영이 편하다.

### 4.5 파일럿 1대 (전체 투입 전 필수)

한 대만 켜고 `docker logs -f` 에 이 순서가 전부 나오는지 확인한다.

```
DHCP4_PACKET_RECEIVED ... DHCPDISCOVER      <- 안 오면 4.3 단계 (UEFI/부팅순서/VLAN)
DHCP4_LEASE_ALLOC ... 10.20.30.1            <- IP 배정
(tftp 전송)                                  <- shim/grubx64/grub.cfg/vmlinuz/initrd
GET /iso/...iso  200  2918598656  "Wget"    <- 전량이어야 한다. 잘리면 RAM 부족
GET user-data -> node #1 hostname=node01    <- 시드 렌더링
install complete: ... wrote grub.cfg-<mac>  <- 완료 콜백
```

설치가 끝나면 재부팅시켜서 **디스크로 부팅되는지**(재설치 루프가 아닌지) 확인하고,
SSH 로 들어가 아래를 본다.

```bash
systemd-analyze          # 30초 이내여야 한다. 2분 넘으면 §6.7
id                       # uid=1000, sudo 그룹
lsblk                    # 의도한 스토리지 레이아웃
cat /etc/apt/sources.list.d/ubuntu.sources   # archive.ubuntu.com 으로 복구되어 있어야 한다
```

### 4.6 전량 투입

파일럿이 깨끗하면 **10초 간격**으로 전원을 넣는다. 동시에 켜면 DHCP 응답 순서가
전원 순서와 달라져 번호가 섞인다(할당 자체는 정상이다).

```bash
docker exec ubuntu-pxe-autoinstall ls /srv/tftp/grub/ | grep -c grub.cfg-   # 완료 대수
docker exec ubuntu-pxe-autoinstall cat /var/lib/kea/kea-leases4.csv         # IP/호스트명 매핑
```

**작업 중에는 컨테이너를 재시작하지 마라.** 리스 DB 와 per-MAC 설정이 전부 컨테이너
안에만 있어서, 재시작하면 이미 설치된 서버가 다음 부팅에 재설치된다. 불가피하면
먼저 빼내고 나중에 되돌려라.

```bash
docker cp 'ubuntu-pxe-autoinstall:/srv/tftp/grub/.' /root/pxe-state/
# ... 재시작 후 ...
docker cp /root/pxe-state/. 'ubuntu-pxe-autoinstall:/srv/tftp/grub/'
```

### 4.7 마무리

전량 설치가 끝나면 **대상 서버들의 부팅 순서를 디스크 1순위로 바꾸고** 컨테이너를
내린다. PXE 가 1순위로 남아 있으면 이 컨테이너의 per-MAC 설정이 재설치를 막는
유일한 안전장치가 된다.

```bash
docker stop ubuntu-pxe-autoinstall && docker rm ubuntu-pxe-autoinstall
```

## 5. 운영 전제

반드시 지켜야 하는 것들이다. 어기면 조용히 잘못된 결과가 나온다.

1. **해당 네트워크에 다른 DHCP 서버가 없어야 한다.**
   다른 DHCP 가 먼저 응답하면 서버가 범위 밖 IP 를 받고, 그러면 순번을 계산할 수
   없어 autoinstall 서비스가 400 을 돌려주며 로그에 경고를 남긴다.
2. **서버당 하나의 NIC 만 PXE/DHCP 를 요청해야 한다.**
   두 NIC 가 요청하면 IP 를 두 개 소비해서 이후 모든 서버의 순번이 밀린다.
   BIOS 에서 쓰지 않는 NIC 의 PXE 를 꺼라.
3. **대상 서버는 UEFI 부팅이어야 한다.** Legacy BIOS(option 93 값 0)에는
   부트 파일을 주지 않는다 — 의도적이다.
4. **서버당 RAM 은 ISO 크기의 2배 이상** — 26.04 ISO(2.72 GiB)면 실질 **8GB**.
   casper 가 `url=` 로 ISO 전체를 initramfs 의 tmpfs 에 받는데(`wget "${URL}" -O
   "${target}"`, 상대경로 = rootfs), **tmpfs 기본 상한이 RAM 의 50%** 다.
   따라서 4GB 장비는 tmpfs 가 2.0 GiB 라 2.72 GiB ISO 를 못 담고
   `wget: short write: No space left on device` 뒤
   `Unable to find a live file system on the network` 로 멈춘다.
   6GB 면 tmpfs 3.0 GiB 로 들어가긴 하나 여유가 거의 없다.
5. **전원 투입 간격을 두어라** (예시의 10초 정도). 동시에 켜면 DHCP 응답 순서가
   전원 순서와 달라질 수 있고, 그러면 호스트명 순서도 달라진다.
   할당 자체는 정상이지만 "켠 순서 = 번호" 가 깨진다.
6. **컨테이너를 함부로 재시작하지 마라.** 아래 참고.

### 상태는 전부 컨테이너 안에만 있다

영구 볼륨을 쓰지 않는다 (요구사항). 컨테이너를 재시작하면:

* Kea 리스 DB 가 초기화된다 → IP 할당이 다시 범위 처음부터 시작된다.
* `/srv/tftp/grub/grub.cfg-<mac>` 파일들이 사라진다 →
  **이미 설치가 끝난 서버가 다시 PXE 부팅하면 재설치된다.**

그래서 `docker-compose.yml` 의 `restart` 는 일부러 `"no"` 다.
설치 작업이 다 끝났으면 컨테이너를 내리고, 대상 서버들의 부팅 순서를
디스크 우선으로 바꾸는 것이 안전하다.

---

## 6. 동작 방식

### 6.1 순차 IP 할당

Kea 에 `"allocator": "iterative"` 를 명시해서 풀의 **첫 주소부터** 순서대로 준다.
그리고 `"match-client-id": false` 로 **MAC 만 보고** 클라이언트를 식별한다.

이게 중요한 이유: 한 대의 서버가 설치되는 동안 DHCP 를 최소 네 번 요청한다
(PXE 펌웨어 → shim/GRUB → 설치 커널 → 설치 환경). 이들이 보내는 client-id 는
서로 **다르다**. client-id 로 식별하면 한 서버가 IP 를 네 개 먹고, "소스 IP →
순번" 매핑이 통째로 무너진다. MAC 기준이면 네 번 모두 같은 IP 를 받는다.

`valid-lifetime` 은 86400초로, 설치 시간보다 충분히 길게 잡았다.

### 6.2 호스트명

autoinstall HTTP 서비스는 **요청 소스 IP** 로 순번을 계산한다:

```
index    = (소스 IP − DHCP_RANGE 시작) + 1
hostname = "node" + index            (범위 크기 자릿수만큼 0 패딩)
```

* 45대 (`.1`–`.45`) → `node01` … `node45`
* 191대 → `node001` … `node191`

소스 IP 가 범위 밖이면 **400 을 반환하고 로그를 남긴다.** 조용히 이상한
호스트명을 만드느니 설치를 실패시키는 쪽이 낫다 — 범위 밖이라는 것은
보통 다른 DHCP 서버가 끼어들었다는 뜻이다.

### 6.3 부팅 체인 — Secure Boot On/Off 모두 동작

Canonical 서명 체인만 쓴다. iPXE 도, 직접 빌드한 GRUB 도 쓰지 않는다.

```
UEFI 펌웨어 ──▶ shimx64.efi ──▶ grubx64.efi ──▶ 서명된 커널
              (shim-signed)   (= grubnetx64,      (ISO 안의 vmlinuz)
                               grub-efi-amd64-signed)
```

shim 은 자기가 로드된 디렉터리에서 **`grubx64.efi` 라는 이름**을 찾으므로,
`grubnetx64.efi.signed` 를 그 이름으로 복사해 둔다.

TFTP 루트에는 이 세 개만 둔다:

```
/srv/tftp/shimx64.efi        ← DHCP boot-file-name
/srv/tftp/grubx64.efi        ← shim 의 2단계
/srv/tftp/grub/grub.cfg      ← grubnetx64 의 prefix
/srv/tftp/grub/grub.cfg-<mac>  ← 설치 완료 후 생성됨
```

`grub/grub.cfg` 라는 경로는 추측이 아니라 26.04 패키지에서 확인한 값이다 (§9).

### 6.4 grub.cfg 분기

1. `grub/grub.cfg-$net_default_mac` 이 있으면 `configfile` 로 넘긴다
   → 로컬 디스크 부팅. 재설치하지 않는다.
2. 없으면 timeout 3초 후 설치 엔트리로 부팅한다.
3. 설치 엔트리의 커널 인자:

```
ip=dhcp url=http://SERVER_IP/iso/NAME.iso cloud-config-url=/dev/null autoinstall
"ds=nocloud-net;s=http://SERVER_IP/autoinstall/$net_default_mac/" ---
```

`ds=` 인자는 **반드시 따옴표로 감싸야 한다.** GRUB 파서에서 `;` 는 명령
구분자라서, 따옴표가 없으면 커널 명령줄이 세미콜론에서 잘리고 설치기가
시드 없이 대화형으로 올라온다.

`$net_default_mac` 은 GRUB 이 `"%02x:"` 포맷으로 만든다 → **소문자 콜론 구분**.
`/done/<mac>` 콜백이 만드는 파일명도 같은 표기로 정규화한다.

### 6.5 로컬 디스크 부팅

`grub.cfg-<mac>` 은 ESP 에서 `\EFI\ubuntu\shimx64.efi` 를 `search` 로 찾아
`chainloader` 한다 (Secure Boot 호환 — grubx64.efi 를 직접 체인로드하지 않는다).
못 찾으면 `exit` 해서 펌웨어의 다음 부팅 항목으로 넘긴다. 프롬프트에 멈추지 않는다.

특정 서버를 다시 설치하고 싶으면 그 파일만 지우면 된다:

```bash
docker exec ubuntu-pxe-autoinstall rm /srv/tftp/grub/grub.cfg-aa:bb:cc:dd:ee:01
```

### 6.6 오프라인 설치를 강제하는 방법

여기가 제일 미묘한 부분이다.

대상 서버는 **링크가 살아 있다** (방금 우리가 리스를 줬으니까). 그래서
subiquity 는 미러에 접근을 시도하고, 인터넷이 없으면 TCP 타임아웃을 몇 분씩
기다린다. 게이트웨이가 있는데 인터넷이 없는 경우가 제일 나쁘다 — 패킷이
블랙홀로 빠져서 타임아웃이 전부 최대치로 걸린다.

그래서 user-data 에서 미러 후보를 **이 컨테이너 자신**으로 지정한다:

```yaml
apt:
  geoip: false
  fallback: offline-install
  mirror-selection:
    primary:
      - uri: http://SERVER_IP/mirror
```

nginx 의 `/mirror` 는 즉시 404 를 돌려준다. 미러 검사가 **밀리초 단위로**
실패하고, `fallback: offline-install` 이 발동해서 ISO 안의 pool 로 설치한다.
타임아웃을 기다리는 구간이 없다.

부작용으로 설치된 시스템의 `sources.list` 가 깨지므로, `late-commands` 에서
진짜 Ubuntu 아카이브를 가리키도록 다시 써 준다. 코드네임은 하드코딩하지 않고
설치된 시스템의 `/etc/os-release` 에서 읽는다.

실기 확인 결과, `GATEWAY` 를 주지 않으면 subiquity 가 아예
`Skipping mirror check since network is not available` 로 미러 검사를 건너뛴다.
즉 위 `/mirror` 트릭은 그때는 발동하지도 않는다. 트릭이 필요한 경우는
**게이트웨이는 있는데 인터넷이 없는** 망이다 — subiquity 가 네트워크가 있다고
판단해 미러에 접근하려 들고, 그때 타임아웃 대신 즉시 404 를 받게 된다.

그 외 네트워크를 건드리는 단계도 전부 껐다:
`refresh-installer.update: false`, `source.search_drivers: false`,
`drivers/codecs/oem.install: false`, `kernel-crash-dumps.enabled: false`.

### 6.7 DNS 를 반드시 넘겨야 하는 이유 (실기에서 확인)

`DNS` 는 "선택" 으로 분류되어 있지만, 비워 두면 **설치된 서버가 부팅할 때마다
2분을 그냥 버린다.** 실측:

```
DNS 없음:  Startup finished in ... = 2min 18.343s   (wait-online 2min 1.176s)
DNS 설정:  Startup finished in ... = 24.669s        (wait-online 1s, exit 0)
```

원인은 netplan 이 생성하는 드롭인이다. `systemctl cat
systemd-networkd-wait-online.service` 로만 보이고 `/run/systemd/system/` 밑에는
없다:

```
ExecStart=/lib/systemd/systemd-networkd-wait-online --any --dns -o routable -i ens33
                                                          ^^^^^
```

`--dns` 는 "DNS 서버가 최소 하나" 를 요구한다. DHCP 로 `domain-name-servers` 를
주지 않으면 resolver 가 0개라 이 조건이 영원히 만족되지 않고, 유닛은 2분 타임아웃까지
간다. IPv4 주소는 1초 만에 붙는데도 그렇다.

**그 DNS 서버가 실제로 응답할 필요는 없다.** 조건은 "설정되어 있을 것" 이다
(응답하지 않는 주소로 실험해도 `exit=0`). 그러니 오프라인 망이라 진짜 resolver 가
없다면 `DNS=<SERVER_IP>` 로 이 컨테이너 자신을 가리켜도 부팅 지연은 사라진다.

### 6.8 cloud-init 시드에서 4개를 모두 200 으로 주는 이유

cloud-init 26.1 의 `util.read_seeded()` 는 `meta-data` 와 `user-data` 는
예외 처리 없이 가져온다 (실패하면 데이터소스 자체가 실패). 반면
`vendor-data` 와 `network-config` 는 선택이지만, **`retries=10`,
`sec_between=1`** 로 가져온다. 즉 404 하나당 약 10초가 그냥 날아간다.

그래서 넷 다 200 으로 응답한다. `vendor-data` 와 `network-config` 는
내용이 비어 있고, 빈 `network-config` 는 "덮어쓰지 않음" 을 뜻하므로
설치된 시스템은 subiquity 기본값인 DHCP 를 그대로 유지한다.

### 6.9 비밀번호

`USER_PASSWORD` 는 기동 시 한 번 `openssl passwd -6` 로 SHA-512 crypt 해시가
되고, **해시만** `/run/pxe/config.json` (0600) 에 저장된다. 평문은 파일에
쓰지 않는다. 비밀번호는 argv 가 아니라 stdin 으로 넘기므로 `ps` 에도 안 보인다.
entrypoint 는 supervisord 를 exec 하기 전에 `USER_PASSWORD` 를 `unset` 해서
감시 대상 프로세스들이 평문을 상속받지 않게 한다.

단, `docker inspect` 에는 환경변수가 그대로 남는다. 이건 Docker 의 성질이다.

---

## 7. HTTP vs TFTP (`BOOT_TRANSPORT`)

기본값은 `tftp` 다. 처음에는 `http` 로 두었는데, 실제 UEFI 펌웨어에서 부팅해 보니
**GRUB 2.14 의 HTTP 스택이 큰 initrd 를 전송하지 못했다.** nginx 액세스 로그:

```
GET /boot/vmlinuz  200  17,275,272 B    8.3s    "GRUB 2.14-2ubuntu1"   <- 정상
GET /boot/initrd   200  14,536,601 B  312.5s    "GRUB 2.14-2ubuntu1"   <- 99,722,544 중 일부만
```

평균 **0.046 MiB/s** 로 기어가다 5분 만에 끊겼다. 커널은 잘린 initramfs 를 받고
`VFS: unable to mount root fs on unknown-block(0,0)` 로 패닉한다.
같은 파일을 같은 링크에서 TFTP 로 받으면:

```
boot/vmlinuz   17,275,272 B  in 1.5s  (11.05 MiB/s, blksize=1468)
boot/initrd    99,722,544 B  in 5.2s  (18.41 MiB/s, blksize=1468)   <- 전량
```

그래서 GRUB 이 받는 커널/initrd 는 TFTP 로 간다.
`grubnetx64` 바이너리에 `http` 모듈이 내장되어 있는 것은 사실이지만(§9),
**들어 있다고 쓸 만한 것은 아니었다.**

**ISO 는 계속 HTTP 로 받는다.** 이건 GRUB 이 아니라 부팅된 리눅스 안의 casper 가
`wget` 으로 받기 때문이다 (initrd 의 `scripts/casper` 에서 확인:
`wget "${URL}" -O "${target}"`). 일반 유저스페이스 wget 이므로 속도 문제가 없고,
2.9GB 를 TFTP 로 옮기는 일도 없다. 즉 역할 분담이 이렇게 된다:

| 받는 주체 | 대상 | 전송 방식 |
|---|---|---|
| GRUB (펌웨어 네트워크 스택) | shim, grubx64, grub.cfg, vmlinuz, initrd | TFTP |
| casper (리눅스 wget) | ISO 2.9GB | HTTP |

`BOOT_TRANSPORT=http` 는 남겨 두었지만, 위 실측 때문에 권장하지 않는다.

## 8. 트러블슈팅

| 증상 | 원인과 조치 |
|---|---|
| 컨테이너가 바로 죽는다 | `docker logs` 를 봐라. `Refusing to start: invalid configuration` 아래에 문제가 전부 나열된다. |
| PXE 가 "No boot filename received" | Kea 가 그 인터페이스에서 안 듣고 있거나 클라이언트가 UEFI 가 아니다. `docker logs` 에서 `DHCPSRV_CFGMGR_ADD_IFACE` 확인. Legacy BIOS 모드면 UEFI 로 바꿔라. |
| DHCP 응답이 아예 없다 | 다른 DHCP 서버가 먼저 응답하거나, `--network host` 를 안 줬거나, 인터페이스 자동 탐지가 틀렸다. `DHCP_INTERFACE` 를 명시하라. |
| shim 은 받는데 GRUB 메뉴가 안 뜬다 | `grub/grub.cfg` 경로 문제다. `docker exec ... ls -R /srv/tftp` 로 구조 확인. tftpd 로그(`--verbose`)에 어떤 파일을 요청했는지 다 찍힌다. |
| Secure Boot 에서 shim 이 거부됨 | `--build-arg SHIM_VARIANT=signed.latest` 로 빌드해 보라. 기본값 `dualsigned` 는 Microsoft + Canonical 양쪽 서명이 들어 있어 더 넓게 동작한다. |
| 설치기가 대화형으로 뜬다 | 커널 명령줄의 `ds=` 가 잘렸을 가능성이 높다. 설치기 콘솔에서 `cat /proc/cmdline` 확인. 세미콜론 뒤가 없으면 따옴표 문제다. |
| 설치 중 미러에서 멈춘다 | §6.6 참고. `curl http://SERVER_IP/mirror` 가 즉시 404 를 돌려주는지 확인하라. |
| 설치된 서버가 매 부팅마다 `systemd-networkd-wait-online` 에서 2분 멈춘다 | `DNS` 환경변수를 설정하지 않았다. §6.7 참고. 응답하는 DNS 서버일 필요도 없다 — `DNS=<SERVER_IP>` 만 넣어도 해결된다. |
| `wget: short write: No space left on device` 후 `Unable to find a live file system` | 대상 서버 RAM 부족. initramfs tmpfs 가 RAM 의 50% 라 ISO(2.72GiB)를 못 담는다. RAM 을 ISO 크기의 2배 이상(실질 8GB)으로 올려라. |
| 커널이 `VFS: unable to mount root fs on unknown-block(0,0)` 로 패닉 | initrd 가 잘렸다. `BOOT_TRANSPORT=http` 를 쓰고 있지 않은지 확인하라 — GRUB 의 HTTP 스택은 큰 initrd 를 전송하지 못한다(§7). 기본값 `tftp` 를 써라. |
| 호스트명이 예상과 다르다 | 서버가 받은 IP 를 확인하라. 순번은 IP 에서 나온다. 한 서버의 NIC 두 개가 DHCP 를 요청하면 그 뒤로 전부 밀린다. |
| 설치 후에도 계속 재설치된다 | `/done` 콜백이 실패했다. `docker exec ... ls /srv/tftp/grub/` 로 해당 MAC 파일이 있는지 보라. 없으면 설치기에서 컨테이너로 HTTP 가 안 닿은 것이다. |
| nginx 가 `/iso/` 에 403 | 호스트의 ISO 파일이 others 에게 읽기 권한이 없다. 기동 로그에 경고가 찍힌다. `chmod a+r` 하고 재시작. |
| 포트 80 충돌 | nginx 는 `SERVER_IP` 에만 바인딩한다. 그 주소에서 다른 웹 서버가 돌고 있으면 그것을 멈춰라. |

디버깅용 수동 확인:

```bash
# 시드 렌더링을 임의의 IP/MAC 으로 확인 (X-Real-IP 로 소스 IP를 흉내낸다)
curl -H 'X-Real-IP: 192.168.101.7' \
     http://192.168.101.253/autoinstall/aa:bb:cc:dd:ee:ff/user-data

# 완료 콜백 수동 실행
curl -X POST http://192.168.101.253/done/aa:bb:cc:dd:ee:ff
```

---

## 9. 26.04 에서 직접 확인한 값들

아래는 문서나 기억이 아니라 **26.04 (resolute) 패키지 바이너리를 직접 열어서**
확인한 내용이다. 이전 릴리스와 다른 부분이 있다.

| 항목 | 확인 결과 |
|---|---|
| 26.04 코드네임 | `resolute` (`dists/resolute/Release`: `Version: 26.04`) |
| `shim-signed` 1.59+15.8-0ubuntu2 | **평범한 `shimx64.efi.signed` 파일이 .deb 안에 없다.** `shimx64.efi.signed.latest`, `.signed.previous`, `.dualsigned`, `shimx64.nx.efi.signed.latest` 가 들어 있고, `/usr/lib/shim/shimx64.efi.signed` 는 postinst 가 update-alternatives 로 만드는 심볼릭 링크다. 그래서 이 이미지는 .deb 을 풀어서 파일을 직접 고른다. |
| shim 서명 개수 (PE 인증서 테이블 파싱) | `.signed.latest` = 1개(Microsoft), `.dualsigned` = **2개**(1924B Canonical + 9720B Microsoft). `.dualsigned` 가 `.signed.latest` 의 상위집합이라 기본값으로 선택했다. |
| `grub-efi-amd64-signed` 1.215+2.14-2ubuntu1 | `/usr/lib/grub/x86_64-efi-signed/grubnetx64.efi.signed`, GRUB 2.14 |
| grubnetx64 내장 **prefix** | `/grub` (PE `mods` 섹션의 PREFIX 모듈에서 추출) |
| grubnetx64 내장 **early config** | `normal (memdisk)/grub.cfg` |
| memdisk 내용 | **squashfs**(xz). 안의 `grub.cfg` 는:<br>`if [ -e $prefix/x86_64-efi/grub.cfg ]; then source …`<br>`elif [ -e $prefix/grub.cfg-amd64 ]; then source $prefix/grub.cfg-default-amd64`<br>`else source $prefix/grub.cfg; fi`<br>→ 우리는 앞의 두 개를 만들지 않으므로 **`$prefix/grub.cfg`**, 즉 TFTP 의 `grub/grub.cfg` 가 로드된다. |
| grubnetx64 내장 모듈 (97개) | `chain fat part_gpt search search_fs_file configfile test linux http tftp net efinet normal echo sleep peimage …` → 로컬 디스크 체인로드와 HTTP 커널 로딩 모두 **`.mod` 를 추가로 받지 않고** 동작한다. |
| `net_default_mac` 표기 | 바이너리에 단독 `"%02x:"` 포맷 문자열 존재 → **소문자, 콜론 구분** |
| cloud-init 26.1 | `ds=nocloud-net` **아직 동작한다** (`DataSourceNoCloudNet` 이 하위 호환으로 남아 있고 deprecation 경고만 낸다). `s=` → `seedfrom`. |
| cloud-init `read_seeded()` | `meta-data`/`user-data` 는 필수, `vendor-data`/`network-config` 는 선택이지만 `retries=10`·`sec_between=1` → 404 당 약 10초 손해. §6.8 의 근거. |
| subiquity autoinstall 스키마 | `apt.fallback` enum 에 `offline-install` 있음, `apt.geoip`, `refresh-installer.update`, `kernel-crash-dumps.enabled`, `drivers/codecs/oem.install`, `source.search_drivers`, `shutdown` enum 확인. `updates` 는 `security`/`all` 뿐이라 **끌 수 없어서 생략**했다. |
| 실제 ISO 의 `/casper` (HTTP range 로 ISO9660+Rock Ridge 직접 파싱) | Rock Ridge 이름이 **`vmlinuz` (17,275,272 B)** 와 **`initrd` (99,722,544 B)** — entrypoint 가 첫 번째로 찾는 이름과 일치. (ISO9660 8.3 뷰로는 `VMLINUZ.;1` / `INITRD.;1`) |
| 패키지 버전 (resolute/main) | kea 3.0.3, nginx 1.28.3, tftpd-hpa 5.3+20251209, xorriso 1.5.6, python3 **3.14.3** |
| Python 3.14 | `crypt` 모듈이 3.13 에서 제거되었다. 그래서 해시는 `openssl passwd -6` 로 만든다. |

---

## 10. 검증한 것과 못 한 것

### 실제로 빌드하고 띄워서 확인한 것

**이미지 빌드** — `docker build` 성공, 190MB.
베이스가 진짜 **Ubuntu 26.04.1 LTS (resolute)** 이고, 안에 들어간 것이
kea **3.0.3**, nginx 1.28.3, tftpd-hpa 5.3, supervisor 4.3.0, xorriso 1.5.6,
**Python 3.14.4** 임을 확인했다. 컨테이너 안에서 `import crypt` 가
`ModuleNotFoundError` 로 실패하는 것도 확인했다 — `openssl passwd -6` 로 간 것이 맞았다.
shim 선택 로직이 의도대로 `shimx64.efi.dualsigned` 를 골랐다.

**컨테이너 기동** — 4개 데몬 전부 `RUNNING`, 소켓 바인딩이 설계대로:

```
udp  192.168.101.253:67   kea-dhcp4     (지정한 인터페이스에만)
udp  0.0.0.0:69           in.tftpd
tcp  192.168.101.253:80   nginx         (SERVER_IP 에만, 호스트의 다른 주소는 건드리지 않음)
tcp  127.0.0.1:8080       python3       (외부 노출 없음)
```

생성된 Kea 설정이 **진짜 Kea 3.0.3 의 `kea-dhcp4 -t` 를 통과**했다 (경고 없음).

**DHCP — 격리된 브리지와 network namespace 를 만들고, option 93 을 실어 보내는
DHCP 클라이언트를 직접 작성해서 DORA 를 완주시켰다:**

* 6대를 순서대로 부팅 → `10.99.0.1, .2, .3, .4, .5, .6` **순차 할당**.
  응답마다 `next-server`, `boot-file-name=shimx64.efi`, `routers`, `dns`,
  `lease=86400` 이 모두 정상.
* **`match-client-id: false` 증명** — 같은 MAC 으로 client-id 를 4번 바꿔 가며
  (없음 / PXE ROM / GRUB / 설치기 흉내) 요청했는데 **전부 같은 `10.99.0.4`** 를 받았다.
  이게 "소스 IP → 순번" 매핑이 설치 내내 유지되는 근거다.
* **client-class 증명** — option 93 이 `0x0007`/`0x0009` 인 클라이언트만
  next-server 와 boot-file 을 받았고, Legacy BIOS(`0x0000`)와 option 93 자체가
  없는 클라이언트는 `next-server=0.0.0.0`, boot-file 없음.

**TFTP** — GRUB 과 동일한 방식으로 원시 RRQ 를 보내서
`shimx64.efi`(968,696B), `grubx64.efi`(2,418,568B), `grub/grub.cfg` 를 받아
컨테이너 안의 파일과 **md5 일치** 확인. blksize 1468 협상도 동작.
`grub.cfg-de:ad:be:ef:00:03` 처럼 **콜론이 든 파일명도 정상 전송**된다.
(참고: BSD `tftp` CLI 는 `host:file` 문법 때문에 이 이름을 파싱하지 못한다.
GRUB 은 그런 파싱을 하지 않으므로 문제없다. 디버깅할 때만 주의하면 된다.)

**HTTP** — `/health` 200, `/boot/vmlinuz`·`/boot/initrd`·`/iso/*.iso` 전량 전송,
`/` 404, 그리고 `/mirror/dists/resolute/InRelease` 가 **0.00015초만에 404** —
§6.6 의 오프라인 설치 강제가 의도대로 즉시 실패한다.

**시드 렌더링** — 실행 중인 컨테이너에 풀 안의 소스 IP 로 요청해서
`user-data`/`meta-data`/`vendor-data`/`network-config` 넷 다 200,
user-data 가 YAML 로 파싱되고 **subiquity 실제 스키마 검증 통과**,
호스트명이 소스 IP 와 일치(`10.99.0.4` → `node04`), 비밀번호 해시를 평문으로
역검증 일치, `late-commands` 스크립트가 `sh -n` 통과.

**완료 콜백** — user-data 안의 late-command 를 그대로 꺼내 실행 →
`grub.cfg-<mac>` 생성 → GRUB 방식 TFTP 로 받아서 내용 확인.

**실제 26.04 ISO** — 2.9GB 를 받지 않고 HTTP range 요청으로 ISO9660 +
Rock Ridge 구조만 직접 파싱해서, `/casper/vmlinuz`(17,275,272B) 와
`/casper/initrd`(99,722,544B) 가 entrypoint 가 첫 번째로 찾는 이름과
일치함을 확인했다.

**프로세스 감시** — 4개 데몬을 각각 `kill -9` 한 뒤 supervisord 가
`WARN exited: ... (terminated by SIGKILL; not expected)` 를 남기고 전부
되살리는 것을 확인했다.

**기동 검증** — 필수 변수 누락, 범위 이탈, `start > end`, 잘못된
CIDR/DNS/사용자명/sizing policy, ISO 부재 → 전부 exit 1 + 구체적 메시지,
에러가 여러 개면 한 번에 출력. 선택 변수가 없으면 `option-data` 가 `[]` 가 되고
그 설정도 `kea-dhcp4 -t` 를 통과한다.

### 컨테이너를 돌려서 찾아 고친 버그

1. **supervisord 로그 중복** — `nodaemon=true` 면 supervisord 가 stdout 핸들러를
   자동으로 붙이는데 `logfile=/dev/stdout` 까지 줘서 모든 줄이 두 번씩 찍혔다.
   `logfile=/dev/null` 로 수정.
2. **nginx 마스터가 SIGKILL 되면 영구 크래시 루프** — 워커는 마스터가 죽어도
   같이 죽지 않고 PID 1 로 재부모화되어 :80 을 계속 쥐고 있다. supervisord 가
   새 마스터를 띄워도 `bind() ... Address already in use` 로 영원히 실패하는데,
   그동안 고아 워커들이 조용히 서비스를 계속하므로 알아차리기 어렵다.
   `bin/run_nginx.sh` 래퍼를 추가해 기동 전에 고아 프로세스를 정리하도록 했다.
   수정 후 같은 시나리오에서 **첫 재시도만에 정상 복구**되는 것을 확인했다.
3. **GRUB HTTP 로는 initrd 를 못 받는다** — 실기 부팅에서 발견. §7 참고.
   기본값을 `BOOT_TRANSPORT=tftp` 로 바꿨다.

### 실기(VMware UEFI VM)에서 끝까지 돌려 확인한 것

한 대를 실제로 PXE 부팅시켜 설치 완료 → 재부팅까지 완주했다.

* **UEFI PXE 체인 전체** — 펌웨어가 option 93=0x0007 로 DISCOVER
  (`vendor='PXEClient:Arch:00007:UNDI:003000'`), 우리 OFFER 에
  `next-server`/`boot-file-name=shimx64.efi` 가 실려 나가고, shim → grubx64 →
  grub.cfg → 커널/initrd 까지 동작.
* **순차 할당** — `node01` = `192.168.101.1`, 시드도 그 IP 로 렌더링됨.
* **오프라인 설치** — subiquity 로그에
  `Skipping mirror check since network is not available`.
  인터넷 없이 ISO 의 pool 로 설치 완료.
* **`late-commands` 의 sources.list 복구** — 설치된 시스템의
  `/etc/apt/sources.list.d/ubuntu.sources` 가 `archive.ubuntu.com` +
  코드네임 `resolute` 로 정상 복원됨(하드코딩이 아니라 타깃의
  `/etc/os-release` 에서 읽은 값).
* **uid 1000 + sudo** — `uid=1000(ubuntu) ... 27(sudo) ...`,
  `sudo id -un` → `root`.
* **비밀번호 SSH 로그인** — `ssh ubuntu@192.168.101.1` 접속 성공.
* **스토리지** — `layout: lvm` + `sizing-policy: all` 이 의도대로:
  `ubuntu-vg/ubuntu-lv` 가 17.32G 전량, `VFree 0`.
* **`/done` 콜백과 로컬 디스크 체인로드** — 설치 후
  `grub.cfg-00:0c:29:b8:1f:23` 이 생성되고, 재부팅 시 재설치되지 않고
  디스크로 부팅됨(시드 요청이 더 이상 오지 않음).

### 실기에서 발견해 고친 것

3. **GRUB HTTP 로는 initrd 를 받지 못한다** — §7. 기본값을 `tftp` 로 바꿨다.
4. **`DNS` 를 비우면 부팅마다 2분을 잃는다** — §6.7. 기동 시 경고를 띄우도록 했다.
5. **대상 서버 RAM 은 ISO 의 2배** — §5. tmpfs 가 RAM 의 50% 라는 것이 진짜 벽이다.

### 여전히 확인하지 못한 것

1. **45대 동시 설치.** 검증은 1대로 했다. 동시에 몰릴 때의 TFTP/HTTP 처리량과
   전원 순서 ↔ 번호 매핑은 실제 랙에서 봐야 한다.
2. **Secure Boot 를 켠 상태.** 서명 체인은 Canonical 패키지에서 바이트 단위로
   가져왔고 서명 개수까지 확인했지만, 테스트한 VM 은 Secure Boot 가 꺼져 있었다.
3. **베어메탈 NIC.** 테스트는 VMware pcnet32 에뮬레이션이었다.
4. **다중 NIC 서버.** subiquity 가 쓰는 netplan 에는 `optional: true` 가 없으므로,
   케이블이 빠진 NIC 이 있으면 `systemd-networkd-wait-online` 이 또 기다릴 수 있다.
5. `pollinate.service` 가 11초를 쓴다(entropy.ubuntu.com 접근 시도).
   오프라인 망에서는 꺼도 되지만 지금은 건드리지 않았다.
6. subiquity 스키마는 GitHub `main` 브랜치 것으로 검증했다. 26.04 에 실린
   버전과 미세하게 다를 수 있다.

## 11. 파일 구성

```
Dockerfile              ubuntu:26.04 기반, 서명된 부트 체인을 .deb 에서 추출
entrypoint.sh           환경변수 검증 → 부트 체인 게시 → ISO 추출 → kea -t → supervisord
bin/render_config.py    검증 + 모든 설정 파일 렌더링 + 비밀번호 해시
bin/autoinstall_server.py  시드 렌더러 + /done 콜백 (표준 라이브러리만)
bin/run_tftpd.sh        tftpd 옵션 파일을 읽어 in.tftpd 를 exec
bin/run_nginx.sh        죽은 마스터가 남긴 고아 워커를 정리하고 nginx 를 exec
templates/
  kea-dhcp4.conf.tmpl   iterative allocator, match-client-id=false, option 93 클래스
  nginx.conf.tmpl       ISO/커널/initrd + 프록시 + 의도적으로 죽은 /mirror
  supervisord.conf.tmpl 4개 데몬, 전부 stdout 로깅 + 무한 재시작
  tftpd-hpa.options.tmpl
  grub.cfg.tmpl         MAC별 분기 + 설치 엔트리
  grub.cfg-host.tmpl    설치 완료 후 로컬 디스크 체인로드
  user-data.tmpl        autoinstall version 1
  meta-data.tmpl / vendor-data.tmpl / network-config.tmpl
docker-compose.yml
```
